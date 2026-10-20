r"""Stand-in for ``OfflineDocTools.exe`` for tests/test_packaging_smoke.py (not the product).

Usage: python fake_app.py SCENARIO DUMP_FILE

Like the real app it logs to ``%LOCALAPPDATA%\OfflineDocTools\logs\app.log`` and logs
``Starting version`` first. It also appends one JSON line about its environment to DUMP_FILE, so
the tests can check what the smoke test handed to the process. It gives up after two minutes, so
a test run that dies cannot leave it behind for long.

Scenarios:
  ok                log the start, then run until killed
  slow-start        wait a second before logging the start
  silent            never log anything
  exit-after-start  log the start, then exit with code 3
  critical          log the start, then an "Unhandled exception" record with a traceback
  soffice-fatal     like ok, but crash when OFFLINE_DOC_TOOLS_SOFFICE names a missing file
  tree              like ok, plus a child process that also runs until killed
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

LIFETIME_SECONDS = 120
SCENARIOS = ("ok", "slow-start", "silent", "exit-after-start", "critical", "soffice-fatal", "tree")


def log(path: Path, level: str, message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {level} offline_doc_tools.app: {message}\n")


def run_until_killed() -> None:
    deadline = time.monotonic() + LIFETIME_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.1)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in SCENARIOS:
        print(f"usage: fake_app.py {{{'|'.join(SCENARIOS)}}} DUMP_FILE", file=sys.stderr)
        return 2
    scenario, dump = argv[1], Path(argv[2])
    log_path = Path(os.environ["LOCALAPPDATA"]) / "OfflineDocTools" / "logs" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, object] = {
        "scenario": scenario,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "env": dict(os.environ),
    }
    if scenario == "tree":
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({LIFETIME_SECONDS})"]
        )
        record["child_pid"] = child.pid
    with dump.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    if scenario == "silent":
        run_until_killed()
        return 0
    if scenario == "slow-start":
        time.sleep(1.0)
    log(log_path, "INFO", "Starting version 0.0.0-fake")

    if scenario == "exit-after-start":
        time.sleep(0.3)
        return 3
    if scenario == "critical":
        log(log_path, "CRITICAL", "Unhandled exception")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("Traceback (most recent call last):\n")
            handle.write('  File "fake_app.py", line 1, in <module>\n')
            handle.write("RuntimeError: fake failure\n")
    if scenario == "soffice-fatal":
        office = os.environ.get("OFFLINE_DOC_TOOLS_SOFFICE")
        if office and not Path(office).is_file():
            log(log_path, "CRITICAL", "Unhandled exception")
            return 4
    run_until_killed()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
