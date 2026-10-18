"""Locating and running a locally installed LibreOffice in headless mode."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .engine import ConversionFailedError, ConversionTimeoutError

log = logging.getLogger(__name__)

ENV_VAR = "OFFLINE_DOC_TOOLS_SOFFICE"
DEFAULT_TIMEOUT_SECONDS = 120.0
DOWNLOAD_URL = "https://www.libreoffice.org/download/"
_OUTPUT_LIMIT_BYTES = 64 * 1024
_LOG_OUTPUT_CHARS = 2000
# STATUS_STACK_BUFFER_OVERRUN / fail-fast: how LibreOffice 26.2.1 exits when its profile path
# is too deep (observed when the temporary folder path exceeded about 120 characters).
_WINDOWS_FAST_FAIL_EXIT = 0xC0000409

_MACOS_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_WINDOWS_PROGRAM_DIRS = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")


def find_soffice(
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
    is_file: Callable[[Path], bool] = Path.is_file,
    cwd: Path | None = None,
) -> Path | None:
    """Return the LibreOffice ``soffice`` executable, or ``None`` if it cannot be found.

    Order: the ``OFFLINE_DOC_TOOLS_SOFFICE`` environment variable (authoritative when set),
    then the standard install locations and ``PATH``. On Windows the standard locations come
    first and a ``PATH`` hit must be an ``.exe``/``.com`` outside the current directory, because
    Windows searches the current directory and runs ``.bat``/``.cmd`` files via ``cmd.exe``.
    """
    env = os.environ if env is None else env

    override = env.get(ENV_VAR)
    if override:
        path = Path(override)
        return path if is_file(path) else None

    standard = next((p for p in _standard_locations(env, platform) if is_file(p)), None)
    if platform == "win32" and standard is not None:
        return standard

    for name in ("soffice", "libreoffice"):
        found = which(name)
        if found and is_file(Path(found)) and _acceptable_path_hit(Path(found), platform, cwd):
            return Path(found)
    return standard


def _standard_locations(env: Mapping[str, str], platform: str) -> list[Path]:
    if platform == "win32":
        return [
            Path(env[var]) / "LibreOffice" / "program" / "soffice.exe"
            for var in _WINDOWS_PROGRAM_DIRS
            if env.get(var)
        ]
    if platform == "darwin":
        return [Path(_MACOS_SOFFICE)]
    return []


def _acceptable_path_hit(path: Path, platform: str, cwd: Path | None) -> bool:
    if platform != "win32":
        return True
    cwd = Path.cwd() if cwd is None else cwd
    return (
        path.is_absolute()
        and path.suffix.lower() in {".exe", ".com"}
        and os.path.normcase(str(path.parent)) != os.path.normcase(str(cwd))
    )


def missing_libreoffice_message(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    override = env.get(ENV_VAR)
    if override:
        return (
            f"LibreOffice was not found at the location set in {ENV_VAR}:\n{override}\n\n"
            "Correct or remove that environment variable, then restart this application."
        )
    return (
        "LibreOffice was not found. It is needed to convert documents and is installed "
        "separately; this application never downloads anything itself.\n\n"
        f"Install LibreOffice from {DOWNLOAD_URL} and restart this application. If it is "
        f"installed in a non-standard folder, set the environment variable {ENV_VAR} to the "
        "full path of soffice.exe."
    )


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    def __call__(
        self, args: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> ProcessResult: ...


def run_process(
    args: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
) -> ProcessResult:
    """Run ``args`` without a shell. On timeout, kill the whole process tree and re-raise.

    Output goes to anonymous temporary files rather than pipes, so a grandchild process that
    keeps the handles open can never block us, and only the last 64 KiB is kept.
    """
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            **popen_kwargs,
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise
        return ProcessResult(returncode, _tail(stdout), _tail(stderr))


def _tail(handle, limit: int = _OUTPUT_LIMIT_BYTES) -> bytes:
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - limit))
    # Bounded even if a lingering child process is still appending.
    return handle.read(limit)


def _kill_tree(proc: subprocess.Popen) -> None:
    # soffice.exe starts soffice.bin; killing only the launcher would leave the worker running.
    try:
        if sys.platform == "win32":
            system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            subprocess.run(
                [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=30,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("Could not terminate the LibreOffice process tree", exc_info=True)
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("LibreOffice did not exit after being terminated")


# Ensured in the profile before every LibreOffice run. Without it, LibreOffice fetches images
# that a document links to by URL (a network request, and a tracking beacon for whoever sent
# the document) and embeds linked local files into the PDF. Verified against LibreOffice 26.2
# with tests/integration/test_libreoffice_integration.py.
_PROFILE_SETTINGS = """\
<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">\
<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
"""
_HARDENING_PATTERN = re.compile(
    r'oor:name="BlockUntrustedRefererLinks"[^>]*>\s*<value>true</value>', re.IGNORECASE
)


class LibreOfficeConverter:
    """Converts documents with ``soffice --headless --convert-to pdf``.

    LibreOffice profiles are created inside ``profile_root``. One profile is reused between
    conversions; after a timeout (when the process was killed and may have left the profile
    half-written) a fresh one is started.
    """

    def __init__(
        self,
        executable: Path,
        profile_root: Path,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        runner: Runner = run_process,
    ) -> None:
        self.executable = executable
        self.timeout = timeout
        self._profile_root = profile_root
        self._profile_generation = 0
        self._runner = runner

    @property
    def profile_dir(self) -> Path:
        # Deliberately short: LibreOffice 26.2.1 crashes (0xC0000409) when the profile path is
        # too deep, so every character here is headroom for long %TEMP% paths.
        return self._profile_root / f"p{self._profile_generation}"

    def _prepare_profile(self) -> dict[str, str]:
        """Ensure the link-blocking setting is in place; return the child's environment."""
        settings = self.profile_dir / "user" / "registrymodifications.xcu"
        try:
            current = settings.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            current = ""
        if not _HARDENING_PATTERN.search(current):
            if current:
                log.warning("LibreOffice profile lost the link-blocking setting; restoring it")
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(_PROFILE_SETTINGS, encoding="utf-8")

        # LibreOffice's own temporary files go inside the profile, so they are removed with it.
        temp_dir = self.profile_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return {**os.environ, "TMP": str(temp_dir), "TEMP": str(temp_dir), "TMPDIR": str(temp_dir)}

    def build_args(self, source: Path, out_dir: Path) -> list[str]:
        return [
            str(self.executable),
            # Isolated profile: no clash with a running LibreOffice, nothing persists.
            f"-env:UserInstallation={self.profile_dir.as_uri()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--nodefault",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(out_dir),
            str(source),
        ]

    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        env = self._prepare_profile()
        args = self.build_args(source, out_dir)
        try:
            result = self._runner(args, self.timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            log.warning("LibreOffice timed out after %s s; starting a fresh profile", self.timeout)
            self._profile_generation += 1
            raise ConversionTimeoutError(
                f"Conversion did not finish within {self.timeout:g} seconds and was stopped. "
                "The document may be very large or damaged."
            ) from exc
        except OSError as exc:
            log.error("Could not start LibreOffice: %s", exc.strerror)
            raise ConversionFailedError(f"Could not start LibreOffice: {exc.strerror}") from exc

        # LibreOffice echoes the temporary paths it works on, which contain the Windows
        # user name; keep them out of logs and user-facing messages.
        # Redact before truncating, so no path is cut into an unrecognisable fragment.
        output_text = _redact(
            _decode(result.stdout, result.stderr),
            [(source.parent, "<workspace>"), (out_dir, "<workspace>")],
            [(self._profile_root, "<profile>")],
            [(Path(tempfile.gettempdir()), "<temp>"), (Path.home(), "~")],
        )[-_LOG_OUTPUT_CHARS:]
        log.info("LibreOffice exited with code %s", result.returncode)
        log.debug("LibreOffice output: %s", output_text)
        detail = _last_line(output_text)

        if result.returncode != 0:
            log.info("LibreOffice output: %s", output_text)
            if result.returncode == _WINDOWS_FAST_FAIL_EXIT:
                message = (
                    "LibreOffice crashed while converting the document. If this happens for "
                    "every document, the temporary folder path may be too long for "
                    "LibreOffice (see the README's known limitations)."
                )
            else:
                message = (
                    f"LibreOffice could not convert the document (exit code {result.returncode})."
                )
            raise ConversionFailedError(f"{message} {detail}".strip())

        pdf = out_dir / f"{source.stem}.pdf"
        if not pdf.is_file():
            message = (
                "LibreOffice did not produce a PDF. The document may be damaged or in an "
                "unsupported format."
            )
            raise ConversionFailedError(f"{message} {detail}".strip())
        return pdf


@contextmanager
def libreoffice_converter(
    executable: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[LibreOfficeConverter]:
    """A converter with throw-away LibreOffice profiles that are deleted afterwards."""
    profile_root = Path(tempfile.mkdtemp(prefix="odt-profile-"))
    try:
        yield LibreOfficeConverter(executable, profile_root, timeout)
    finally:
        shutil.rmtree(profile_root, ignore_errors=True)
        if profile_root.exists():
            log.warning("Could not fully remove the temporary LibreOffice profile folder")


def _redact(text: str, *groups: list[tuple[Path, str]]) -> str:
    """Replace every spelling of the given paths: as given and resolved (long and 8.3 short
    names), with either slash style, and as file URIs. Earlier groups take precedence."""
    replacements: dict[str, str] = {}
    for group in groups:
        for path, label in group:
            for variant in {path, path.resolve()}:
                spellings = [str(variant), str(variant).replace("\\", "/")]
                if variant.is_absolute():
                    spellings.append(variant.as_uri())
                for spelling in spellings:
                    replacements.setdefault(spelling, label)
    for variant in sorted(replacements, key=len, reverse=True):
        text = re.sub(re.escape(variant), replacements[variant], text, flags=re.IGNORECASE)
    return text


def _decode(*streams: bytes) -> str:
    return "\n".join(s.decode("utf-8", errors="replace") for s in streams if s).strip()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""
