"""The replacement helper: argument contract, startup confirmation, result reporting."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from offline_doc_tools.updater import helper as h
from offline_doc_tools.updater import installer as ins


def argv(tmp_path: Path, **overrides) -> list[str]:
    values = {
        "--protocol": "1",
        "--install-dir": str(tmp_path / "apps" / "OfflineDocTools"),
        "--payload": str(tmp_path / "stage" / "OfflineDocTools"),
        "--parent-pid": "4321",
        "--result-file": str(tmp_path / "result.json"),
        "--from-version": "0.1.0",
        "--to-version": "0.2.0",
    }
    values.update(overrides)
    out = ["--apply-update"]
    for key, value in values.items():
        if value is not None:
            out += [key, value]
    return out


def test_the_arguments_are_parsed_into_typed_values(tmp_path):
    parsed = h.parse_args(argv(tmp_path)[1:])

    assert parsed.protocol == 1 and parsed.parent_pid == 4321
    assert parsed.install_dir == tmp_path / "apps" / "OfflineDocTools"
    assert (parsed.from_version, parsed.to_version) == ("0.1.0", "0.2.0")


@pytest.mark.parametrize(
    "bad",
    [
        {"--protocol": "x"},
        {"--parent-pid": "-1"},
        {"--parent-pid": "abc"},
        {"--install-dir": "relative/path"},
        {"--payload": "relative"},
        {"--result-file": "relative.json"},
        {"--to-version": "latest"},
        {"--from-version": None},
        {"--install-dir": None},
    ],
)
def test_malformed_or_missing_arguments_are_refused(tmp_path, bad):
    with pytest.raises(h.UsageError):
        h.parse_args(argv(tmp_path, **bad)[1:])


def test_an_unknown_argument_is_refused(tmp_path):
    with pytest.raises(h.UsageError):
        h.parse_args([*argv(tmp_path)[1:], "--run-this", "calc.exe"])


class FakeProcess:
    def __init__(self, exits_after=None):
        self.exits_after = exits_after
        self.pid = 999
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls > self.exits_after:
            return 1
        return None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def confirm(tmp_path, *, write_marker_after=None, content="ok 0.2.0", process=None, timeout=30):
    clock = Clock()
    marker = tmp_path / "marker"
    killed = []
    process = process or FakeProcess()
    launches = []

    def popen(command, **kwargs):
        launches.append((command, kwargs))
        return process

    original_sleep = clock.sleep

    def sleep(seconds):
        original_sleep(seconds)
        if write_marker_after is not None and clock.now >= write_marker_after:
            marker.write_text(content)

    ok = h.launch_and_confirm(
        tmp_path / "OfflineDocTools.exe",
        marker,
        "0.2.0",
        timeout=timeout,
        popen=popen,
        clock=clock,
        sleep=sleep,
        kill=killed.append,
    )
    return ok, killed, launches, marker


def test_the_new_version_is_confirmed_when_it_writes_its_marker(tmp_path):
    ok, killed, launches, marker = confirm(tmp_path, write_marker_after=3)

    assert ok and killed == []
    command, kwargs = launches[0]
    assert command == [str(tmp_path / "OfflineDocTools.exe")]
    assert kwargs["env"]["ODT_STARTUP_OK_FILE"] == str(marker)
    assert kwargs["cwd"] == str(tmp_path)


def test_a_new_version_that_never_reports_is_killed_and_not_confirmed(tmp_path):
    ok, killed, _, _ = confirm(tmp_path, timeout=10)

    assert not ok and killed == [999]


def test_a_new_version_that_exits_early_is_not_confirmed(tmp_path):
    ok, killed, _, _ = confirm(tmp_path, process=FakeProcess(exits_after=2))

    assert not ok and killed == []  # nothing to kill; the id could even have been reused


def test_a_marker_from_another_version_is_not_accepted(tmp_path):
    ok, killed, _, _ = confirm(tmp_path, write_marker_after=2, content="ok 0.1.0", timeout=10)

    assert not ok and killed == [999]


def test_a_stale_marker_from_an_earlier_run_is_ignored(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("ok 0.2.0")
    clock = Clock()

    ok = h.launch_and_confirm(
        tmp_path / "OfflineDocTools.exe",
        marker,
        "0.2.0",
        timeout=5,
        popen=lambda *_a, **_k: FakeProcess(),
        clock=clock,
        sleep=clock.sleep,
        kill=lambda _pid: None,
    )

    assert not ok


def test_a_launch_failure_is_not_confirmed(tmp_path):
    def popen(*_a, **_k):
        raise OSError("blocked")

    ok = h.launch_and_confirm(
        tmp_path / "x.exe",
        tmp_path / "m",
        "0.2.0",
        timeout=5,
        popen=popen,
        clock=Clock(),
        sleep=lambda _s: None,
        kill=lambda _pid: None,
    )

    assert not ok


def read(tmp_path):
    return json.loads((tmp_path / "result.json").read_text())


def test_the_helper_reports_success_through_the_result_file(tmp_path):
    seen = {}

    def apply(install, payload, **kwargs):
        seen.update(install=install, payload=payload, **kwargs)
        return ins.InstallResult(True, False, "installed", "The update was installed.")

    code = h.run(
        argv(tmp_path)[1:], apply=apply, confirm=lambda *a, **k: True, check_location=False
    )

    assert code == 0 and read(tmp_path)["ok"] is True and read(tmp_path)["to"] == "0.2.0"
    assert seen["parent_pid"] == 4321 and seen["protocol"] == 1


def test_a_rolled_back_update_is_reported_and_is_a_failure_exit(tmp_path):
    def apply(install, payload, **kwargs):
        return ins.InstallResult(False, True, "startup_failed", "restored")

    code = h.run(
        argv(tmp_path)[1:], apply=apply, confirm=lambda *a, **k: False, check_location=False
    )

    assert code == 1 and read(tmp_path)["rolled_back"] is True


def test_a_refusal_before_any_change_is_reported(tmp_path):
    def apply(*_a, **_k):
        raise ins.InstallError("refused")

    code = h.run(
        argv(tmp_path)[1:], apply=apply, confirm=lambda *a, **k: True, check_location=False
    )

    assert code == 1 and read(tmp_path)["code"] == "refused" and read(tmp_path)["ok"] is False


def test_an_unexpected_error_is_reported_without_its_text(tmp_path):
    def apply(*_a, **_k):
        raise RuntimeError(r"C:\Users\someone\secret")

    code = h.run(
        argv(tmp_path)[1:], apply=apply, confirm=lambda *a, **k: True, check_location=False
    )

    assert code == 1 and "secret" not in json.dumps(read(tmp_path))


def test_bad_arguments_exit_with_an_error_and_write_nothing(tmp_path):
    assert h.run(["--protocol", "x"], check_location=False) == 2
    assert not (tmp_path / "result.json").exists()


def test_the_helper_refuses_to_run_from_anywhere_but_the_payload(tmp_path):
    # In the frozen application the helper must be the executable inside the payload folder.
    code = h.run(
        argv(tmp_path)[1:],
        apply=lambda *a, **k: None,
        confirm=lambda *a, **k: True,
        check_location=True,
        running_from=tmp_path / "somewhere" / "OfflineDocTools.exe",
    )

    assert code == 1 and read(tmp_path)["code"] == "refused"


# --- the real process helpers ----------------------------------------------------------------


def test_waiting_for_a_running_process_times_out_and_then_sees_it_exit():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert ins.wait_for_process_exit(child.pid, 0.3) is False
    finally:
        child.kill()
        child.wait()

    assert ins.wait_for_process_exit(child.pid, 5) is True


def test_a_process_that_does_not_exist_counts_as_exited():
    assert ins.wait_for_process_exit(2**22 + 12345, 1) is True


def test_the_marker_is_written_only_when_asked_for(tmp_path):
    marker = tmp_path / "marker"

    h.write_startup_marker({}, "0.2.0")
    h.write_startup_marker({"ODT_STARTUP_OK_FILE": str(marker)}, "0.2.0")

    assert marker.read_text() == "ok 0.2.0"


def test_an_unwritable_marker_path_does_not_stop_the_application(tmp_path):
    h.write_startup_marker(
        {"ODT_STARTUP_OK_FILE": str(tmp_path / "no" / "such" / "dir" / "m")}, "0.2.0"
    )


def test_the_real_taskkill_helper_ends_a_process_tree():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    h.kill_process_tree(child.pid)
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)

    assert child.poll() is not None
