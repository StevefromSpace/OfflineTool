"""The replacement helper: ``OfflineDocTools.exe --apply-update ...`` run from the staged payload.

The old application verifies the signed manifest and package, stages the payload next to the
installation and starts *the payload's own executable* with ``--apply-update``. That makes the
helper the new version's code, so an updated updater needs no special case, and it runs outside the
folder it replaces. This module is the command-line contract (protocol 1), the startup
confirmation and the result report. It imports no Qt.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import installer
from .manifest import parse_version

log = logging.getLogger(__name__)

MARKER_ENV = "ODT_STARTUP_OK_FILE"
STARTUP_TIMEOUT = 90.0  # a first start of a new executable can be slow while it is scanned
MARKER_NAME = "startup-ok.txt"


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class HelperArgs:
    protocol: int
    install_dir: Path
    payload: Path
    parent_pid: int
    result_file: Path
    from_version: str
    to_version: str


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # noqa: D102 - argparse hook
        raise UsageError(message)


def _absolute(text: str) -> Path:
    path = Path(text)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("not an absolute path")
    return path


def _version(text: str) -> str:
    if parse_version(text) is None:
        raise argparse.ArgumentTypeError("not a version")
    return text


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("not positive")
    return value


def parse_args(argv: Sequence[str]) -> HelperArgs:
    parser = _Parser(prog="--apply-update", add_help=False, allow_abbrev=False)
    parser.add_argument("--protocol", type=_positive, required=True)
    parser.add_argument("--install-dir", type=_absolute, required=True)
    parser.add_argument("--payload", type=_absolute, required=True)
    parser.add_argument("--parent-pid", type=_positive, required=True)
    parser.add_argument("--result-file", type=_absolute, required=True)
    parser.add_argument("--from-version", type=_version, required=True)
    parser.add_argument("--to-version", type=_version, required=True)
    ns = parser.parse_args(list(argv))
    return HelperArgs(
        ns.protocol,
        ns.install_dir,
        ns.payload,
        ns.parent_pid,
        ns.result_file,
        ns.from_version,
        ns.to_version,
    )


def _same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def kill_process_tree(pid: int) -> None:
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    taskkill = Path(root) / "System32" / "taskkill.exe"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [str(taskkill), "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _marker_matches(marker: Path, version: str) -> bool:
    try:
        return marker.read_text(encoding="utf-8").strip() == f"ok {version}"
    except OSError:
        return False


def launch_and_confirm(
    exe: Path,
    marker: Path,
    version: str,
    *,
    timeout: float = STARTUP_TIMEOUT,
    popen: Callable = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    kill: Callable[[int], None] = kill_process_tree,
) -> bool:
    """Start the new version and wait for it to write its startup marker for ``version``.

    On failure the process (tree) is ended before returning False, so the caller can restore the
    old version without a running copy of the new one."""
    with contextlib.suppress(OSError):
        marker.unlink(missing_ok=True)
    env = dict(os.environ)
    env[MARKER_ENV] = str(marker)
    try:
        process = popen(
            [str(exe)],
            cwd=str(exe.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError:
        return False
    deadline = clock() + timeout
    while clock() < deadline:
        if _marker_matches(marker, version):
            return True
        if process.poll() is not None:
            break
        sleep(0.25)
    if process.poll() is None:  # never signal an id that may have been reused
        kill(process.pid)
    return False


def write_startup_marker(environ: Mapping[str, str], version: str) -> None:
    """Called by the application once its window is up, if a helper asked for confirmation."""
    target = environ.get(MARKER_ENV)
    if target:
        with contextlib.suppress(OSError):
            Path(target).write_text(f"ok {version}", encoding="utf-8")


def run(
    argv: Sequence[str],
    *,
    apply: Callable = installer.apply_update,
    confirm: Callable = launch_and_confirm,
    check_location: bool = True,
    running_from: Path | None = None,
) -> int:
    try:
        args = parse_args(argv)
    except UsageError:
        return 2
    marker = args.result_file.parent / MARKER_NAME
    try:
        if check_location and (
            running_from is None or not _same_folder(running_from.parent, args.payload)
        ):
            raise installer.InstallError("refused")  # the helper must be the payload's own exe
        result = apply(
            args.install_dir,
            args.payload,
            parent_pid=args.parent_pid,
            protocol=args.protocol,
            start_and_verify=lambda exe: confirm(exe, marker, args.to_version),
        )
    except installer.InstallError as exc:
        result = installer.InstallResult(False, False, exc.code, exc.message)
    except Exception as exc:
        log.error("Update helper failed unexpectedly: %s", type(exc).__name__)
        result = installer.InstallResult(
            False, False, "failed", "The update failed unexpectedly. The previous version was kept."
        )
    log.info("Update %s -> %s: %s", args.from_version, args.to_version, result.code)
    installer.write_result(args.result_file, result, args.from_version, args.to_version)
    return 0 if result.ok else 1


def main(argv: Sequence[str]) -> int:
    """Entry for the packaged executable: log to update.log, then run."""
    base = os.environ.get("LOCALAPPDATA")
    folder = (Path(base) if base else Path.home()) / "OfflineDocTools" / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(folder / "update.log", encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    frozen = bool(getattr(sys, "frozen", False))
    return run(argv, check_location=frozen, running_from=Path(sys.executable) if frozen else None)
