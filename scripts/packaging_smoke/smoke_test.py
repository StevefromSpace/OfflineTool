r"""Smoke test for the packaged Windows app (``dist\OfflineDocTools``).

Run it after ``python scripts\packaging\build_windows.py``::

    python scripts\packaging_smoke\smoke_test.py [--app-dir DIR] [--timeout SECONDS]

Exit status 0: every check passed. Exit status 1: a check failed (or the arguments were unusable).
Standard library only (Python >= 3.11). No network access: the only OS queries are local
(``taskkill`` and Windows PowerShell's ``Get-NetTCPConnection`` / ``Get-NetUDPEndpoint``).

It proves that the *built* app does not lean on the development environment:

 1. layout: ``OfflineDocTools.exe`` exists, ``licenses\`` holds licence texts, and the Qt platform
    plugins ``qwindows.dll`` (what users run) and ``qoffscreen.dll`` (what this test runs) exist;
 2. forbidden content is absent: Qt Network, WebEngine, Qml, Quick, documents, test files;
 3. the bundle is copied to a fresh, short folder in the system temp directory (outside the repo);
 4. the copy is started with a scrubbed environment: only OS essentials survive (no PYTHON*,
    VIRTUAL_ENV, CONDA*, QT_* ...), Python/venv folders are removed from PATH, LOCALAPPDATA
    points to a temporary folder, QT_QPA_PLATFORM=offscreen, and the working directory is the copy;
 5. "Starting version" shows up in the redirected app.log within --timeout seconds;
 6. the process is still running 5 s later and app.log has no CRITICAL / "Unhandled exception" line;
 7. the process tree owns no TCP or UDP endpoint (sampled repeatedly during those 5 s);
 8. the process tree is killed (``taskkill /F /T`` on the PID this script started, never by image
    name) and the temporary files are removed, even when a check failed;
 9. with OFFLINE_DOC_TOOLS_SOFFICE pointing to a missing file (the "LibreOffice not found" banner
    case) the app still starts.

What it cannot show: ``Starting version`` is logged before Qt starts, so "Qt came up" rests on the
process still running (crash dialogs are switched off so a crash ends the process) and on the
platform plugins being present. It never looks at the window itself.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

EXE_NAME = "OfflineDocTools.exe"
LICENSES_DIR = "licenses"
# Qt is used under the LGPL-3.0, which needs both texts to accompany the program.
REQUIRED_LICENCE_TEXTS = ("LGPL-3.0.txt", "GPL-3.0.txt", "OpenSSL-Apache-2.0.txt")
# The update check needs Python's ssl: without these every check would fail like "offline".
REQUIRED_TLS_FILES = ("_ssl.pyd", "libssl-3*.dll", "libcrypto-3*.dll")
ROOT_NOTICE = "THIRD-PARTY-NOTICES.txt"  # beside the EXE: the prominent LGPL notice
START_MARKER = "Starting version"
SOFFICE_ENV_VAR = "OFFLINE_DOC_TOOLS_SOFFICE"

DEFAULT_TIMEOUT = 60.0  # seconds to wait for the start-up log line (Defender scans a fresh copy)
SETTLE_SECONDS = 5.0  # the app must keep running this long after it logged its start
POLL_INTERVAL = 0.1
KILL_WAIT_SECONDS = 15.0
CLEANUP_SECONDS = 30.0
PATH_BUDGET = 250  # stay clear of MAX_PATH (260) when copying the bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP_DIR = REPO_ROOT / "dist" / "OfflineDocTools"

# SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX. Child processes inherit
# the error mode, so a crashing app ends instead of waiting behind a Windows Error Reporting
# dialog (which would make a dead app look alive).
_ERROR_MODE_FLAGS = 0x0001 | 0x0002 | 0x8000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SmokeError(Exception):
    """A setup step failed in a way that makes the remaining checks meaningless."""


# --- reporting ----------------------------------------------------------------------------------


def _ascii(text: str) -> str:
    """Console-safe text: Windows consoles and redirected pipes may not encode everything."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _summarise(items: Sequence[str], limit: int = 8) -> str:
    shown = "; ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown}; (+{len(items) - limit} more)"


def _print_line(text: str) -> None:
    print(text, flush=True)


class Reporter:
    """Prints concise PASS / FAIL / SKIP lines (ASCII only) and counts the failures."""

    def __init__(self, emit: Callable[[str], None] = _print_line) -> None:
        self._emit = emit
        self.failures = 0

    def ok(self, check: str, detail: str = "") -> None:
        self._line("PASS", check, detail)

    def fail(self, check: str, detail: str = "") -> None:
        self.failures += 1
        self._line("FAIL", check, detail)

    def skip(self, check: str, detail: str = "") -> None:
        self._line("SKIP", check, detail)

    def note(self, text: str) -> None:
        """A diagnostic line shown under the previous result."""
        self._emit(_ascii(f"     | {_clip(text, 220)}"))

    def summary(self) -> bool:
        """Print the closing line; True when nothing failed."""
        passed = self.failures == 0
        self._emit("SMOKE TEST PASSED" if passed else f"SMOKE TEST FAILED ({self.failures} failed)")
        return passed

    def _line(self, status: str, check: str, detail: str) -> None:
        text = f"{status} {check}: {_clip(detail, 700)}" if detail else f"{status} {check}"
        self._emit(_ascii(text))


# --- arguments ----------------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """The contract knows two exit codes (0 pass, 1 fail), so unusable arguments exit with 1."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {text!r}")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _ArgumentParser(
        prog="smoke_test.py",
        description=(
            "Smoke-test the built OfflineDocTools bundle outside the development environment. "
            "Exit status 0 = pass, 1 = fail."
        ),
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=DEFAULT_APP_DIR,
        metavar="DIR",
        help="folder holding OfflineDocTools.exe (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="how long to wait for the app to log 'Starting version' (default: %(default)s)",
    )
    return parser.parse_args(argv)


# --- the child environment ----------------------------------------------------------------------

# Only these variables (matched case-insensitively) are passed on. Everything else in the caller's
# environment is dropped, so nothing from the development environment (PYTHON*, VIRTUAL_ENV,
# CONDA*, QT_PLUGIN_PATH, __PYVENV_LAUNCHER__, OFFLINE_DOC_TOOLS_SOFFICE ...) can reach the app
# and make a broken bundle look fine. PATH is rebuilt below; LOCALAPPDATA is replaced.
ESSENTIAL_ENV_VARS = (
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "APPDATA",
    "PROGRAMDATA",
    "ALLUSERSPROFILE",
    "PUBLIC",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
)

# A PATH folder is a Python/venv folder when a component of its path looks like one ...
_PYTHON_DIR_NAME = re.compile(
    r"^(python[\d.\-]*|\.?venvs?|\.?virtualenvs?|site-packages|[a-z]*conda\d*|condabin"
    r"|miniforge\d*|mambaforge\d*|micromamba|pyenv(-win)?|pipx|pypoetry)$",
    re.IGNORECASE,
)
# ... or when it, or its parent (a venv's Scripts folder), holds an interpreter or venv marker.
# (Not py.exe: the Python launcher lives in C:\Windows.)
_PYTHON_MARKERS = frozenset({"python.exe", "pythonw.exe", "python3.exe", "pyvenv.cfg"})


def _norm(text: str) -> str:
    return os.path.normcase(os.path.normpath(text))


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _dir_names(path: Path) -> frozenset[str]:
    try:
        return frozenset(entry.name.lower() for entry in path.iterdir())
    except OSError:
        return frozenset()


def python_environment_roots(env: Mapping[str, str]) -> list[str]:
    """Normalised folders that belong to a Python installation or virtual environment."""
    upper = {key.upper(): value for key, value in env.items()}
    candidates = [
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
        str(Path(sys.executable).parent),
        upper.get("VIRTUAL_ENV", ""),
        upper.get("CONDA_PREFIX", ""),
    ]
    roots: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        key = _norm(candidate)
        if key not in roots and Path(candidate).parent != Path(candidate):  # never a drive root
            roots.append(key)
    return roots


def is_python_path_entry(entry: str, roots: Sequence[str] = ()) -> bool:
    """True if a PATH folder belongs to a Python installation or a virtual environment."""
    if any(_is_under(_norm(entry), root) for root in roots):
        return True
    path = Path(entry)
    if any(_PYTHON_DIR_NAME.match(part) for part in path.parts):
        return True
    return any(_PYTHON_MARKERS & _dir_names(folder) for folder in (path, path.parent))


def sanitize_path(
    path_value: str, *, roots: Sequence[str] = (), keep_under: str | None = None
) -> str:
    """Drop empty entries, quotes and Python/venv folders; always keep ``keep_under`` (Windows)."""
    protected = _norm(keep_under) if keep_under else None
    kept: list[str] = []
    for raw in path_value.split(os.pathsep):
        entry = raw.strip().strip('"').strip()
        if not entry:
            continue
        system_folder = protected is not None and _is_under(_norm(entry), protected)
        if system_folder or not is_python_path_entry(entry, roots):
            kept.append(entry)
    return os.pathsep.join(kept)


def build_child_env(
    base_env: Mapping[str, str],
    local_appdata: Path,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The scrubbed environment the app is started with."""
    upper = {key.upper(): value for key, value in base_env.items()}
    env = {name: upper[name] for name in ESSENTIAL_ENV_VARS if name in upper}
    path = sanitize_path(
        upper.get("PATH", ""),
        roots=python_environment_roots(base_env),
        keep_under=upper.get("SYSTEMROOT") or upper.get("WINDIR"),
    )
    if path:
        env["PATH"] = path
    env["LOCALAPPDATA"] = str(local_appdata)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_LOGGING_TO_CONSOLE"] = "1"  # Qt's own error messages land in the captured output
    env.update(extra or {})
    return env


# --- the bundle on disk -------------------------------------------------------------------------

_NETWORK_NAME = re.compile(r"^(lib)?qt\d*network", re.IGNORECASE)
_NETWORK_PLUGIN_DIRS = frozenset({"networkinformation", "tls"})  # Qt Network's plugin folders
_SUBSTRING_CATEGORIES = (("Qt WebEngine", "webengine"), ("Qt Qml", "qml"), ("Qt Quick", "quick"))
_DOCUMENT_SUFFIXES = frozenset({".docx", ".pdf"})
_TEST_FILE = re.compile(r"^(test_.*|.*_test|conftest)\.py[co]?$", re.IGNORECASE)
_TEST_DIRS = frozenset({"test", "tests", ".pytest_cache"})
_PLATFORM_PLUGINS = ("qwindows.dll", "qoffscreen.dll")


@dataclass(frozen=True)
class Finding:
    category: str
    path: str  # relative to the bundle, with forward slashes

    def __str__(self) -> str:
        return f"{self.category}: {self.path}"


@dataclass
class BundleScan:
    files: int = 0
    longest_path: int = 0  # longest path relative to the bundle root
    findings: list[Finding] = field(default_factory=list)
    platform_plugins: set[str] = field(default_factory=set)  # lower-case names inside platforms/
    errors: list[str] = field(default_factory=list)


def classify_name(name: str, *, is_dir: bool) -> str | None:
    """Why a file or folder name must not be in the bundle (``None`` if it may be)."""
    lower = name.lower()
    if _NETWORK_NAME.match(lower) or (is_dir and lower in _NETWORK_PLUGIN_DIRS):
        return "Qt Network"
    for category, fragment in _SUBSTRING_CATEGORIES:
        if fragment in lower:
            return category
    if is_dir:
        return "test files" if lower in _TEST_DIRS else None
    if Path(lower).suffix in _DOCUMENT_SUFFIXES:
        return "document"
    return "test files" if _TEST_FILE.match(lower) else None


def scan_bundle(app_dir: Path) -> BundleScan:
    """One pass over the bundle: forbidden content, platform plugins and path lengths."""
    scan = BundleScan()
    flagged: set[Path] = set()  # folders that are (or sit inside) a reported folder
    root = Path(app_dir)
    for current, dirnames, filenames in os.walk(root, onerror=lambda e: scan.errors.append(str(e))):
        folder = Path(current)
        inside_flagged = folder in flagged
        relative = folder.relative_to(root)
        for name in dirnames:
            if inside_flagged:
                flagged.add(folder / name)
            elif (category := classify_name(name, is_dir=True)) is not None:
                flagged.add(folder / name)
                scan.findings.append(Finding(category, (relative / name).as_posix()))
        for name in filenames:
            scan.files += 1
            scan.longest_path = max(scan.longest_path, len(str(relative / name)))
            if folder.name.lower() == "platforms" and name.lower() in _PLATFORM_PLUGINS:
                scan.platform_plugins.add(name.lower())
            if not inside_flagged and (category := classify_name(name, is_dir=False)):
                scan.findings.append(Finding(category, (relative / name).as_posix()))
    return scan


@dataclass
class Layout:
    problems: list[str]
    licence_files: int
    has_exe: bool


def check_layout(app_dir: Path, scan: BundleScan) -> Layout:
    problems: list[str] = []
    exe = app_dir / EXE_NAME
    has_exe = exe.is_file()
    if not has_exe:
        problems.append(f"{EXE_NAME} is missing")
    else:
        with exe.open("rb") as handle:
            header = handle.read(2)
        if exe.stat().st_size == 0 or header != b"MZ":
            problems.append(f"{EXE_NAME} is not a Windows executable")

    licence_files, empty = 0, []
    licences = app_dir / LICENSES_DIR
    if licences.is_dir():
        for path in licences.rglob("*"):
            if path.is_file():
                licence_files += 1
                if path.stat().st_size == 0:
                    empty.append(path.name)
    if licence_files == 0:
        problems.append(f"{LICENSES_DIR}\\ is missing or has no files")
    if empty:
        problems.append(f"empty licence file(s): {', '.join(empty)}")
    absent = [n for n in REQUIRED_LICENCE_TEXTS if not (licences / n).is_file()]
    if absent:
        problems.append(f"{LICENSES_DIR}\\ lacks{', '.join(absent)}")

    if not (app_dir / ROOT_NOTICE).is_file():
        problems.append(f"{ROOT_NOTICE} is missing beside the EXE")
    contents = app_dir / "_internal"
    absent_tls = [n for n in REQUIRED_TLS_FILES if not any(contents.glob(n))]
    if absent_tls:
        problems.append(f"TLS support missing from _internal: {', '.join(absent_tls)}")

    for plugin in _PLATFORM_PLUGINS:
        if plugin not in scan.platform_plugins:
            reason = (
                "the plugin the app runs with"
                if plugin == "qwindows.dll"
                else "needed by this test, QT_QPA_PLATFORM=offscreen"
            )
            problems.append(f"platforms\\{plugin} not found ({reason})")
    return Layout(problems, licence_files, has_exe)


# --- temporary files ----------------------------------------------------------------------------


def ensure_outside(path: Path, forbidden_root: Path) -> None:
    if path.resolve().is_relative_to(forbidden_root.resolve()):
        raise SmokeError(
            f"the temporary folder {path} is inside the repository; point TEMP somewhere else"
        )


def make_workspace(repo_root: Path = REPO_ROOT) -> Path:
    """A fresh, short temporary folder outside the repository."""
    root = Path(tempfile.mkdtemp(prefix="odts-"))
    try:
        ensure_outside(root, repo_root)
    except SmokeError:
        remove_tree(root, timeout=5)
        raise
    return root


def copy_bundle(app_dir: Path, destination: Path, scan: BundleScan) -> None:
    needed = len(str(destination)) + 1 + scan.longest_path
    if needed > PATH_BUDGET:
        raise SmokeError(
            f"the copy would need paths of {needed} characters (limit about {PATH_BUDGET}); "
            "point TEMP to a shorter folder"
        )
    try:
        shutil.copytree(app_dir, destination)
    except OSError as exc:
        raise SmokeError(f"could not copy the bundle: {_clip(str(exc))}") from exc


def _clear_read_only(path: Path) -> None:
    for current, dirnames, filenames in os.walk(path):
        for name in (*dirnames, *filenames):
            with contextlib.suppress(OSError):
                (Path(current) / name).chmod(stat.S_IWRITE | stat.S_IREAD)


def remove_tree(path: Path, timeout: float = CLEANUP_SECONDS) -> bool:
    """Delete a folder, retrying: antivirus and a just-killed process can hold files briefly."""
    deadline = time.monotonic() + timeout
    while path.exists():
        try:
            shutil.rmtree(path)
        except OSError:
            _clear_read_only(path)
        if not path.exists():
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


# --- the app process ----------------------------------------------------------------------------


def windows_system32() -> Path:
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    return Path(root) / "System32"


@contextlib.contextmanager
def _no_crash_dialogs() -> Iterator[None]:
    if sys.platform != "win32":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    previous = kernel32.SetErrorMode(0)
    kernel32.SetErrorMode(previous | _ERROR_MODE_FLAGS)
    try:
        yield
    finally:
        kernel32.SetErrorMode(previous)


def default_command(exe: Path) -> list[str]:
    return [str(exe)]


def launch_app(
    command: Sequence[str], cwd: Path, env: Mapping[str, str], output_file: Path
) -> subprocess.Popen:
    """Start the app; its console output (normally none) is captured in ``output_file``."""
    with output_file.open("wb") as sink, _no_crash_dialogs():
        return subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )


_LAUNCH_HINTS = {
    225: "antivirus blocked the file",
    1260: "blocked by group policy",
    4551: "an application-control policy such as Smart App Control blocked the unsigned file",
}


def describe_os_error(exc: OSError) -> str:
    hint = _LAUNCH_HINTS.get(getattr(exc, "winerror", None) or 0)
    return f"{_clip(str(exc))} ({hint})" if hint else _clip(str(exc))


def describe_exit(code: int) -> str:
    if 0 <= code <= 255:
        return f"exit code {code}"
    return f"exit code {code} (0x{code & 0xFFFFFFFF:08X})"


def kill_process_tree(proc: subprocess.Popen, *, runner: Callable = subprocess.run) -> bool:
    """Kill the process this script started and its descendants; True once it has exited.

    By PID, and while the Popen object still holds its handle (so the PID cannot have been
    reused). Never by image name: other copies of the app may be running.
    """
    taskkill = windows_system32() / "taskkill.exe"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        runner(
            [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=_NO_WINDOW,
        )
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_WAIT_SECONDS)
    return proc.poll() is not None


# --- the application log ------------------------------------------------------------------------

_START_LINE = re.compile(re.escape(START_MARKER) + r" (\S+)")
_PROBLEM_LINE = re.compile(r"\bCRITICAL\b|Unhandled exception|Traceback \(most recent call last\)")


def read_text(path: Path, limit: int = 1_000_000) -> str:
    try:
        return path.read_bytes()[-limit:].decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_start_version(text: str) -> str | None:
    match = _START_LINE.search(text)
    return match.group(1) if match else None


def find_problem_lines(text: str, limit: int = 5) -> list[str]:
    """Lines that mean the app is unhealthy: CRITICAL, "Unhandled exception", a traceback."""
    found = [line.strip()[:200] for line in text.splitlines() if _PROBLEM_LINE.search(line)]
    return found[:limit]


def tail_lines(path: Path, count: int = 10) -> list[str]:
    lines = [line.rstrip() for line in read_text(path).splitlines() if line.strip()]
    return lines[-count:]


@dataclass
class StartResult:
    started: bool
    version: str | None = None
    seconds: float = 0.0
    exit_code: int | None = None  # set when the process ended before it logged its start
    timed_out: bool = False


def wait_for_start(
    proc: subprocess.Popen,
    log_path: Path,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StartResult:
    """Wait for ``Starting version`` in the log; stop early if the process ends."""
    begun = clock()
    deadline = begun + timeout
    while True:
        version = parse_start_version(read_text(log_path))
        if version is not None:
            return StartResult(True, version, clock() - begun)
        exit_code = proc.poll()
        if exit_code is not None:  # the log is final now: look once more
            version = parse_start_version(read_text(log_path))
            if version is not None:
                return StartResult(True, version, clock() - begun)
            return StartResult(False, seconds=clock() - begun, exit_code=exit_code)
        if clock() >= deadline:
            return StartResult(False, seconds=clock() - begun, timed_out=True)
        sleep(POLL_INTERVAL)


def wait_while_alive(
    proc: subprocess.Popen,
    seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Poll for ``seconds``; the exit code if the process ended meanwhile, else ``None``."""
    deadline = clock() + seconds
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            return exit_code
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(POLL_INTERVAL, remaining))


# --- network endpoints --------------------------------------------------------------------------

# One PowerShell process samples for the whole window (a fresh PowerShell takes 1-3 s to start).
# Every sample lists the process tree below the app's PID (children are matched by parent PID and
# creation time, so a recycled PID cannot pull in strangers) and asks Windows for the TCP/UDP
# endpoints those processes own. The markers prove it really sampled: a sampler that failed
# quietly must not read as "no endpoints".
_SAMPLER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = [uint32]__PID__
$seconds = __SECONDS__
foreach ($name in 'Get-NetTCPConnection', 'Get-NetUDPEndpoint', 'Get-CimInstance') {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        'ODT-ERROR|missing cmdlet ' + $name
        exit 3
    }
}
function Owned($cmdlet, $owners) {
    try { & $cmdlet -OwningProcess $owners }
    catch { if ($_.CategoryInfo.Category -ne 'ObjectNotFound') { throw } }
}
$props = 'ProcessId', 'ParentProcessId', 'CreationDate'
$clock = [Diagnostics.Stopwatch]::StartNew()
$samples = 0
while ($true) {
    $all = @(Get-CimInstance -ClassName Win32_Process -Property $props)
    $byId = @{}
    foreach ($p in $all) { $byId[[uint32]$p.ProcessId] = $p }
    if (-not $byId.ContainsKey($root)) { 'ODT-ROOT-GONE|' + $samples; break }
    $ids = New-Object 'System.Collections.Generic.List[uint32]'
    $ids.Add($root)
    for ($i = 0; $i -lt $ids.Count; $i++) {
        $parent = $byId[$ids[$i]]
        foreach ($p in $all) {
            if ($ids.Contains([uint32]$p.ProcessId)) { continue }
            if ($p.ParentProcessId -ne $ids[$i]) { continue }
            if ($p.CreationDate -lt $parent.CreationDate) { continue }
            $ids.Add([uint32]$p.ProcessId)
        }
    }
    $arr = $ids.ToArray()
    foreach ($c in @(Owned 'Get-NetTCPConnection' $arr)) {
        'TCP|{0}|{1}|{2}|{3}|{4}|{5}' -f $c.OwningProcess, $c.LocalAddress, $c.LocalPort,
            $c.RemoteAddress, $c.RemotePort, $c.State
    }
    foreach ($u in @(Owned 'Get-NetUDPEndpoint' $arr)) {
        'UDP|{0}|{1}|{2}' -f $u.OwningProcess, $u.LocalAddress, $u.LocalPort
    }
    foreach ($w in @(Get-Process -Id $arr -ErrorAction SilentlyContinue)) {
        if ($w.MainWindowHandle.ToInt64() -ne 0) {
            'ODT-WINDOW|{0}|{1}' -f $w.Id, $w.MainWindowTitle
        }
    }
    $samples++
    'ODT-SAMPLE|{0}|{1}' -f $samples, $ids.Count
    if ($clock.Elapsed.TotalSeconds -ge $seconds) { break }
    Start-Sleep -Milliseconds 300
}
'ODT-DONE|' + $samples
"""


@dataclass
class EndpointReport:
    samples: int = 0
    endpoints: list[str] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    root_gone: bool = False
    done: bool = False
    error: str | None = None

    def problem(self) -> str | None:
        """Why the sampling cannot be trusted (``None`` when it can)."""
        if self.error:
            return self.error
        if self.root_gone:
            return "the app process vanished while it was being sampled"
        if not self.done or self.samples < 1:
            return "the sampler did not complete a single sample"
        return None


def build_sampler_script(pid: int, seconds: float) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"not a process id: {pid!r}")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"not a duration: {seconds!r}")
    return _SAMPLER_SCRIPT.replace("__PID__", str(pid)).replace("__SECONDS__", f"{seconds:.2f}")


def _address(host: str, port: str) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_sampler_output(stdout: str, returncode: int = 0, stderr: str = "") -> EndpointReport:
    report = EndpointReport()
    try:
        for raw in stdout.splitlines():
            parts = raw.strip().split("|")
            kind = parts[0]
            if kind == "TCP" and len(parts) >= 7:
                pid, local, lport, remote, rport, state = parts[1:7]
                report.endpoints.append(
                    f"TCP {_address(local, lport)} -> {_address(remote, rport)} {state} (pid {pid})"
                )
            elif kind == "UDP" and len(parts) >= 4:
                pid, local, lport = parts[1:4]
                report.endpoints.append(f"UDP {_address(local, lport)} (pid {pid})")
            elif kind == "ODT-WINDOW" and len(parts) >= 3:
                report.windows.append(f"pid {parts[1]} window {parts[2]!r}")
            elif kind == "ODT-SAMPLE" and len(parts) >= 2:
                report.samples = max(report.samples, int(parts[1]))
            elif kind in ("TCP", "UDP"):  # too few fields to read: still an endpoint, never skipped
                report.endpoints.append(f"unreadable {kind} line: {_clip(raw.strip(), 80)}")
            elif kind == "ODT-ROOT-GONE":
                report.root_gone = True
            elif kind == "ODT-ERROR":
                report.error = "|".join(parts[1:])
            elif kind == "ODT-DONE":
                report.done = True
    except ValueError:
        report.error = "unreadable sampler output"
    report.endpoints = list(dict.fromkeys(report.endpoints))  # every sample repeats them
    report.windows = list(dict.fromkeys(report.windows))
    if returncode != 0 and report.error is None:
        detail = f": {_clip(stderr, 200)}" if stderr.strip() else ""
        report.error = f"powershell exited with code {returncode}{detail}"
    return report


def sample_endpoints(
    pid: int, seconds: float, *, runner: Callable = subprocess.run
) -> EndpointReport:
    """Sample the TCP/UDP endpoints owned by ``pid`` and its descendants for ``seconds``."""
    powershell = windows_system32() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return EndpointReport(error=f"{powershell} not found")
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        build_sampler_script(pid, seconds),
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=seconds + 90,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return EndpointReport(error="powershell.exe did not finish sampling in time")
    except OSError as exc:
        return EndpointReport(error=f"could not run powershell.exe: {describe_os_error(exc)}")
    return parse_sampler_output(completed.stdout, completed.returncode, completed.stderr)


# --- observing one run of the app ---------------------------------------------------------------


@dataclass
class Context:
    report: Reporter
    workspace: Path
    app_copy: Path
    timeout: float
    settle: float
    command: Callable[[Path], Sequence[str]]
    sampler: Callable[[int, float], EndpointReport]
    base_env: Mapping[str, str]


@dataclass
class Observation:
    launch_error: str | None = None
    start: StartResult | None = None
    exit_code: int | None = None  # the app ended before the observation window was over
    alive_seconds: float = 0.0
    log_problems: list[str] = field(default_factory=list)
    endpoints: EndpointReport | None = None
    log_tail: list[str] = field(default_factory=list)
    output_tail: list[str] = field(default_factory=list)


def observe_app(
    ctx: Context, index: int, extra_env: Mapping[str, str] | None, *, sample: bool
) -> Observation:
    """Start the copied app in a scrubbed environment, watch it, then always kill it."""
    obs = Observation()
    local_appdata = ctx.workspace / f"lad{index}"
    local_appdata.mkdir()
    log_path = local_appdata / "OfflineDocTools" / "logs" / "app.log"
    output_path = ctx.workspace / f"out{index}.txt"
    env = build_child_env(ctx.base_env, local_appdata, extra=extra_env)
    try:
        proc = launch_app(ctx.command(ctx.app_copy / EXE_NAME), ctx.app_copy, env, output_path)
    except OSError as exc:
        obs.launch_error = describe_os_error(exc)
        return obs
    try:
        obs.start = wait_for_start(proc, log_path, ctx.timeout)
        if obs.start.started:
            begun = time.monotonic()
            if sample:
                obs.endpoints = ctx.sampler(proc.pid, ctx.settle)
            remaining = max(0.0, ctx.settle - (time.monotonic() - begun))
            obs.exit_code = wait_while_alive(proc, remaining)
            obs.alive_seconds = time.monotonic() - begun
            obs.log_problems = find_problem_lines(read_text(log_path))
        else:
            obs.exit_code = obs.start.exit_code
    finally:
        obs.log_tail = tail_lines(log_path)
        gone = kill_process_tree(proc)
        obs.output_tail = tail_lines(output_path)
        if not gone:
            ctx.report.fail("cleanup", f"process {proc.pid} is still running after taskkill")
    return obs


def _diagnostics(report: Reporter, obs: Observation) -> None:
    for line in obs.log_tail:
        report.note(f"app.log: {line}")
    for line in obs.output_tail:
        report.note(f"output: {line}")


def _report_start(
    report: Reporter, check: str, obs: Observation, timeout: float
) -> StartResult | None:
    """Report a start-up failure; the ``StartResult`` when the app did log its start."""
    if obs.launch_error is not None:
        report.fail(check, f"could not start the app: {obs.launch_error}")
        return None
    start = obs.start
    if start is None:
        report.fail(check, "the app was not observed")
        return None
    if start.started:
        return start
    if start.exit_code is not None:
        report.fail(
            check,
            f"the app ended with {describe_exit(start.exit_code)} before logging "
            f"'{START_MARKER}' ({start.seconds:.1f} s)",
        )
    else:
        report.fail(
            check,
            f"no '{START_MARKER}' line in app.log within {timeout:g} s; the process was "
            "still running (a startup error dialog, or a hang?)",
        )
    _diagnostics(report, obs)
    return None


def _report_stability(report: Reporter, obs: Observation, settle: float) -> bool:
    problems: list[str] = []
    if obs.exit_code is not None:
        problems.append(
            f"the app ended with {describe_exit(obs.exit_code)} {obs.alive_seconds:.1f} s "
            "after it logged its start"
        )
    if obs.log_problems:
        problems.append("app.log contains: " + " | ".join(obs.log_problems))
    if obs.endpoints is not None and obs.endpoints.windows:
        problems.append(
            "a native window exists although QT_QPA_PLATFORM=offscreen (probably an error "
            "dialog): " + "; ".join(obs.endpoints.windows)
        )
    if problems:
        report.fail("stability", "; ".join(problems))
        _diagnostics(report, obs)
        return False
    report.ok(
        "stability",
        f"still running {obs.alive_seconds:.1f} s after start (required {settle:g} s); "
        "app.log has no CRITICAL / Unhandled exception line",
    )
    return True


def _report_network(report: Reporter, obs: Observation) -> bool:
    sampled = obs.endpoints
    if sampled is not None and sampled.endpoints:
        report.fail("network", "endpoints owned by the app: " + _summarise(sampled.endpoints))
        return False
    if obs.exit_code is not None:
        report.skip("network", "the app was not running for the whole window")
        return False
    if sampled is None or sampled.problem() is not None:
        why = sampled.problem() if sampled is not None else "nothing was sampled"
        report.fail("network", f"could not verify that there are no endpoints: {why}")
        return False
    report.ok(
        "network",
        f"no TCP/UDP endpoint owned by the app process tree ({sampled.samples} samples)",
    )
    return True


# --- the whole run ------------------------------------------------------------------------------


def _check_bundle(report: Reporter, app_dir: Path) -> tuple[BundleScan, bool]:
    scan = scan_bundle(app_dir)
    layout = check_layout(app_dir, scan)
    if layout.problems:
        report.fail("layout", "; ".join(layout.problems))
    else:
        report.ok(
            "layout",
            f"{EXE_NAME} present; {LICENSES_DIR}\\ has {layout.licence_files} file(s); "
            "qwindows.dll and qoffscreen.dll present",
        )
    if scan.errors:
        report.fail("forbidden content", "could not scan everything: " + _summarise(scan.errors, 3))
    elif scan.findings:
        report.fail(
            "forbidden content",
            f"{len(scan.findings)} item(s): " + _summarise([str(f) for f in scan.findings]),
        )
    else:
        report.ok("forbidden content", f"none found in {scan.files} file(s)")
    return scan, layout.has_exe


def run_smoke_test(
    app_dir: Path,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    report: Reporter | None = None,
    settle_seconds: float = SETTLE_SECONDS,
    command: Callable[[Path], Sequence[str]] = default_command,
    sampler: Callable[[int, float], EndpointReport] = sample_endpoints,
    base_env: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
) -> bool:
    """Run every check; True when all of them passed.

    ``command``, ``sampler`` and ``base_env`` exist so the tests can drive the checks with a
    stand-in for the exe; the command line interface never changes them.
    """
    report = report if report is not None else Reporter()
    app_dir = Path(app_dir)
    base_env = os.environ if base_env is None else base_env
    if sys.platform != "win32":
        report.fail("platform", "the packaged app is a Windows program; run this test on Windows")
        return False
    if not app_dir.is_dir():
        report.fail(
            "layout",
            f"{app_dir} does not exist; build it first with "
            "python scripts\\packaging\\build_windows.py",
        )
        return False

    scan, has_exe = _check_bundle(report, app_dir)
    if not has_exe:
        return False

    workspace: Path | None = None
    try:
        workspace = make_workspace(repo_root)
        app_copy = workspace / "app"
        copy_bundle(app_dir, app_copy, scan)
        report.ok("copy", f"{scan.files} file(s) copied to {app_copy}")
        ctx = Context(
            report, workspace, app_copy, timeout, settle_seconds, command, sampler, base_env
        )
        if _run_main_checks(ctx):
            _run_missing_soffice_check(ctx)
        else:
            report.skip("missing LibreOffice", "the main run failed")
    except SmokeError as exc:
        report.fail("setup", str(exc))
    finally:
        if workspace is not None:
            if remove_tree(workspace):
                report.ok("cleanup", "process tree killed; temporary copy and profile removed")
            else:
                report.fail("cleanup", f"could not remove the temporary folder {workspace}")
    return report.failures == 0


def _run_main_checks(ctx: Context) -> bool:
    report = ctx.report
    obs = observe_app(ctx, 1, None, sample=True)
    start = _report_start(report, "startup", obs, ctx.timeout)
    if start is None:
        report.skip("stability", "the app did not start")
        report.skip("network", "the app did not start")
        return False
    report.ok("startup", f"'{START_MARKER} {start.version}' logged after {start.seconds:.1f} s")
    stable = _report_stability(report, obs, ctx.settle)
    quiet = _report_network(report, obs)
    return stable and quiet


def _run_missing_soffice_check(ctx: Context) -> bool:
    missing = ctx.workspace / "no-office" / "soffice.exe"
    obs = observe_app(ctx, 2, {SOFFICE_ENV_VAR: str(missing)}, sample=False)
    check = "missing LibreOffice"
    if _report_start(ctx.report, check, obs, ctx.timeout) is None:
        return False
    problems: list[str] = []
    if obs.exit_code is not None:
        problems.append(f"the app ended with {describe_exit(obs.exit_code)}")
    problems.extend(obs.log_problems)
    if problems:
        ctx.report.fail(check, "; ".join(problems))
        _diagnostics(ctx.report, obs)
        return False
    ctx.report.ok(
        check, f"the app starts and keeps running with {SOFFICE_ENV_VAR} set to a missing file"
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = Reporter()
    try:
        passed = run_smoke_test(args.app_dir, args.timeout, report=report)
    except KeyboardInterrupt:
        report.fail("interrupted", "stopped by the user; the app was killed and the copy removed")
        passed = False
    except Exception as exc:  # a bug in this script must still end in a FAIL and exit status 1
        traceback.print_exc()
        report.fail("internal error", f"{type(exc).__name__}: {exc}")
        passed = False
    if not passed:
        report.failures = max(report.failures, 1)  # an interruption or bug counts as a failure
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
