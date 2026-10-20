"""Replace the installed bundle with a staged, already verified payload, and roll back on failure.

This code runs in the *new* version's executable, started from the staging folder, so it can
replace the folder the old version ran from and an updated updater needs no special case. The
payload cannot be renamed into place (its own executable is running), so it is copied.

Sequence: validate the target -> wait for the old application to exit -> rename the installation
to a backup -> copy the payload in and compare it with the payload -> start the new version and
wait for its startup marker -> only then delete the backup. Any failure after the first rename
restores the backup. A small journal next to the installation lets a later run recover from a
crash in the middle. Messages and codes name no user paths, only the two folder names.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROTOCOL = 1
SUPPORTED_PROTOCOLS = frozenset({1})
EXE = "OfflineDocTools.exe"
# What a bundle may hold at its top level. A folder with anything else in it is not ours to replace
# (and to delete when the backup goes), whatever it is called.
_KNOWN_TOP_LEVEL = frozenset(
    {"offlinedoctools.exe", "_internal", "licenses", "third-party-notices.txt", "readme.md"}
    | {"start-here.md"}
)
SPACE_MARGIN = 64 * 1024**2
WAIT_FOR_EXIT_SECONDS = 60
_ATTEMPTS = 20
_RETRY_DELAY = 0.5

_MESSAGES = {
    "installed": "The update was installed.",
    "startup_failed": "The new version did not start, so the previous version was restored.",
    "copy_failed": (
        "The update could not be copied into place, so the previous version was restored."
    ),
    "locked": "The application folder is in use or protected, so nothing was changed.",
    "busy": "The application did not close in time, so nothing was changed.",
    "refused": "The update was refused because the application folder could not be confirmed.",
    "space": "There is not enough free disk space to install the update. Nothing was changed.",
    "not_writable": (
        "The folder that contains the application is not writable. Nothing was changed."
    ),
    "recovery_failed": (
        "An earlier interrupted update could not be undone automatically. Nothing was changed."
    ),
    "unsupported": (
        "This update uses an installer protocol this version cannot run. Nothing was changed."
    ),
}


class InstallError(Exception):
    """The update was refused before anything was changed."""

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.message = _MESSAGES[code]


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    rolled_back: bool
    code: str
    message: str


# --- helpers ---------------------------------------------------------------------------------


def _retry(action: Callable[[], object], sleep: Callable[[float], None]) -> None:
    last: OSError | None = None
    for _ in range(_ATTEMPTS):
        try:
            action()
            return
        except OSError as exc:
            last = exc
            sleep(_RETRY_DELAY)
    raise last if last else OSError("no attempt was made")


def _make_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                (Path(root) / name).chmod(stat.S_IWRITE | stat.S_IREAD)


def _remove_tree(path: Path, sleep: Callable[[float], None]) -> None:
    def attempt() -> None:
        if path.exists():
            try:
                shutil.rmtree(path)
            except OSError:
                _make_writable(path)
                raise

    _retry(attempt, sleep)


def _size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _digest(root: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[path.relative_to(root).as_posix()] = (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return out


def _is_bundle(root: Path) -> bool:
    return (root / EXE).is_file() and (root / "_internal").is_dir()


def _can_write(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".odt-write-test-"):
            return True
    except OSError:
        return False


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir()
    for root, dirs, files in os.walk(source):
        relative = Path(root).relative_to(source)
        for name in dirs:
            (target / relative / name).mkdir()
        for name in files:
            shutil.copy2(Path(root) / name, target / relative / name)


def wait_for_process_exit(pid: int, timeout: float) -> bool:
    """True once the process has ended (or never existed); False if it outlives ``timeout``."""
    if sys.platform != "win32":  # pragma: no cover - the application is Windows-only
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.2)
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # no such process any more
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


# --- journal and recovery --------------------------------------------------------------------


def _journal_path(parent: Path, install_name: str) -> Path:
    return parent / f".{install_name}.update-journal.json"


def _write_journal(parent: Path, install_name: str, backup_name: str, state: str) -> None:
    data = {"install": install_name, "backup": backup_name, "state": state}
    _journal_path(parent, install_name).write_text(json.dumps(data), encoding="utf-8")


def recover_interrupted(
    install_dir: Path, sleep: Callable[[float], None] = time.sleep, *, strict: bool = False
) -> str | None:
    """Undo (or finish) an update that was interrupted. Returns a note, or None if there was
    nothing to do. Only folders named after the installation are ever touched."""
    parent = install_dir.parent
    journal = _journal_path(parent, install_dir.name)
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backup_name = data.get("backup") if isinstance(data, dict) else None
    pattern = re.compile(rf"{re.escape(install_dir.name)}\.backup-[0-9A-Za-z_-]+")
    if (
        not isinstance(backup_name, str)
        or not pattern.fullmatch(backup_name)
        or data.get("install") != install_dir.name
    ):
        journal.unlink(missing_ok=True)  # not written by this updater: ignore it
        return None
    backup = parent / backup_name
    state = data.get("state")
    try:
        if state == "verified":
            if backup.exists():
                _remove_tree(backup, sleep)
            note = "An interrupted update had already been verified; the old copy was removed."
        elif backup.exists():
            if install_dir.exists():
                _remove_tree(install_dir, sleep)
            backup.replace(install_dir)
            note = "An interrupted update was undone and the previous version restored."
        else:
            note = "An interrupted update left nothing to restore."
        journal.unlink(missing_ok=True)
        return note
    except OSError:
        if strict:
            raise InstallError("recovery_failed") from None
        return "An interrupted update could not be undone automatically."


# --- the update ------------------------------------------------------------------------------


def _validate(install_dir: Path, payload: Path, free_bytes, parent: Path) -> None:
    anchors = {Path(install_dir.anchor), Path.home().resolve(), Path.home().resolve().parent}
    if install_dir in anchors or install_dir.parent == install_dir:
        raise InstallError("refused")
    if not install_dir.is_dir() or not _is_bundle(install_dir):
        raise InstallError("refused")
    if _inside(payload, install_dir) or _inside(install_dir, payload):
        raise InstallError("refused")
    if (
        not payload.is_dir()
        or not _is_bundle(payload)
        or not (payload / "THIRD-PARTY-NOTICES.txt").is_file()
    ):
        raise InstallError("refused")
    allowed = _KNOWN_TOP_LEVEL | {p.name.lower() for p in payload.iterdir()}
    if any(p.name.lower() not in allowed for p in install_dir.iterdir()):
        raise InstallError("refused")  # other people's files live here too
    if not _can_write(parent):
        raise InstallError("not_writable")
    if free_bytes(parent) < _size(payload) + SPACE_MARGIN:
        raise InstallError("space")


def apply_update(
    install_dir: Path,
    payload_dir: Path,
    *,
    parent_pid: int | None,
    start_and_verify: Callable[[Path], bool],
    protocol: int = PROTOCOL,
    wait_for_exit: Callable[[int, float], bool] = wait_for_process_exit,
    sleep: Callable[[float], None] = time.sleep,
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
) -> InstallResult:
    """Install ``payload_dir`` over ``install_dir``. Raises InstallError if refused before any
    change; otherwise returns the outcome (the old version is restored on failure)."""
    install = install_dir.resolve()
    payload = payload_dir.resolve()
    parent = install.parent
    if protocol not in SUPPORTED_PROTOCOLS:
        raise InstallError("unsupported")
    if parent_pid is not None and not wait_for_exit(parent_pid, WAIT_FOR_EXIT_SECONDS):
        return InstallResult(False, False, "busy", _MESSAGES["busy"])
    recover_interrupted(install, sleep, strict=True)  # only once the application is gone
    _validate(install, payload, free_bytes, parent)

    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = parent / f"{install.name}.backup-{stamp}"
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = parent / f"{install.name}.backup-{stamp}_{suffix}"
    _write_journal(parent, install.name, backup.name, "swapping")
    try:
        _retry(lambda: install.replace(backup), sleep)
    except OSError:
        _journal_path(parent, install.name).unlink(missing_ok=True)
        return InstallResult(False, False, "locked", _MESSAGES["locked"])

    failure = "copy_failed"
    try:
        _copy_tree(payload, install)
        if _digest(install) != _digest(payload):
            raise OSError("copy differs from the payload")
        _write_journal(parent, install.name, backup.name, "verifying")
        failure = "startup_failed"
        if start_and_verify(install / EXE):
            with contextlib.suppress(OSError):  # the new version is running: never undo it
                _write_journal(parent, install.name, backup.name, "verified")
            _finish(parent, install, backup, sleep)
            return InstallResult(True, False, "installed", _MESSAGES["installed"])
    except Exception as exc:  # any failure, however odd, must end in a rollback
        log.warning("Update step %s failed: %s", failure, type(exc).__name__)
    return _roll_back(parent, install, backup, failure, sleep)


def _finish(parent: Path, install: Path, backup: Path, sleep) -> None:
    # A leftover backup is harmless: the journal makes the next run remove it.
    with contextlib.suppress(OSError):
        _remove_tree(backup, sleep)
        _journal_path(parent, install.name).unlink(missing_ok=True)


def _roll_back(parent: Path, install: Path, backup: Path, failure: str, sleep) -> InstallResult:
    try:
        if install.exists():
            _remove_tree(install, sleep)
        _retry(lambda: backup.replace(install), sleep)
        if not _is_bundle(install):
            raise OSError("restored folder is not a bundle")
    except OSError:
        message = (
            "The update failed and the previous version could not be restored automatically. "
            f"It is in the folder '{backup.name}' next to the application: rename that folder to "
            f"'{install.name}' to restore it."
        )
        return InstallResult(False, False, "rollback_failed", message)
    _journal_path(parent, install.name).unlink(missing_ok=True)
    return InstallResult(False, True, failure, _MESSAGES[failure])


# --- the result shown by the next start ------------------------------------------------------


def write_result(path: Path, result: InstallResult, from_version: str, to_version: str) -> None:
    data = {
        "ok": result.ok,
        "rolled_back": result.rolled_back,
        "code": result.code,
        "message": result.message,
        "from": from_version,
        "to": to_version,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def read_result(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("message"), str) else None
