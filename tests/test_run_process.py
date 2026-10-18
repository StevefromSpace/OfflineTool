"""Tests of the real subprocess runner, using the Python interpreter as a stand-in process."""

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from offline_doc_tools.libreoffice import run_process

# A parent that starts a long-sleeping grandchild (which inherits the output handles, as
# soffice.bin does from soffice.exe), records its PID, then sleeps itself.
_SPAWN_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""

# Same, but the parent exits at once while the grandchild keeps the output handles open.
_ORPHAN_GRANDCHILD = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
open(sys.argv[1], "w").write(str(child.pid))
print("parent done", flush=True)
"""


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/F", "/PID", str(pid)], capture_output=True, check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


def _is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        tasklist = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "tasklist.exe"
        out = subprocess.run(
            [str(tasklist), "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_captures_exit_code_and_output():
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        timeout=30,
    )
    assert result.returncode == 3
    assert b"out" in result.stdout
    assert b"err" in result.stderr


def test_arguments_are_not_interpreted_by_a_shell():
    tricky = 'a b & echo "pwned" | ünï'
    echo = "import sys; sys.stdout.buffer.write(sys.argv[1].encode('utf-8'))"
    result = run_process([sys.executable, "-c", echo, tricky], 30)
    assert result.stdout.decode("utf-8").strip() == tricky


def test_output_capture_is_bounded():
    flood = "import sys; sys.stdout.write('x' * 2_000_000 + 'END')"
    result = run_process([sys.executable, "-c", flood], 30)
    assert len(result.stdout) <= 64 * 1024
    assert result.stdout.endswith(b"END")


def test_returns_when_grandchild_keeps_output_handles_open(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    try:
        result = run_process([sys.executable, "-c", _ORPHAN_GRANDCHILD, str(pid_file)], 60)
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists():
            _kill(int(pid_file.read_text()))
    assert result.returncode == 0
    assert b"parent done" in result.stdout
    assert elapsed < 20


def test_timeout_kills_whole_process_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    # Generous timeout: a cold interpreter start on a CI runner can take several seconds.
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, "-c", _SPAWN_GRANDCHILD, str(pid_file)], timeout=8)
    assert time.monotonic() - started < 30

    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _is_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _is_alive(grandchild)


def test_tail_reads_at_most_the_limit():
    from offline_doc_tools.libreoffice import _tail

    class GrowingFile:
        """Reports 100 bytes, but a still-running process keeps appending after the seek."""

        def seek(self, offset, whence=0):
            return 100 if whence == os.SEEK_END else offset

        def read(self, size=-1):
            data = b"x" * 1_000_000
            return data if size < 0 else data[:size]

    assert len(_tail(GrowingFile(), limit=10)) == 10


def test_environment_is_passed_to_the_child():
    probe = "import os, sys; sys.stdout.write(os.environ['ODT_PROBE'])"
    env = dict(os.environ, ODT_PROBE="from-parent")
    result = run_process([sys.executable, "-c", probe], 30, env=env)
    assert result.stdout == b"from-parent"
