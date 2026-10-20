"""Updates in the main window: every step needs a click, installing needs consent."""

import json
import threading
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from offline_doc_tools.main_window import MainWindow
from offline_doc_tools.updater import manifest as mf
from offline_doc_tools.updater import service as svc
from offline_doc_tools.updater.runtime import UpdateRuntime

TOKEN = "github_pat_" + "Z1x2C3v4B5" * 6


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


class FakeService:
    def __init__(self, check_result=None, *, configured=True, token=True, gate=None):
        self._configured, self.token, self.gate = configured, token, gate
        self.check_result = check_result
        self.calls: list[str] = []
        self.saved: list[str] = []
        self.stage_error: Exception | None = None
        self.launch_error: Exception | None = None
        self.launched: list = []

    def configured(self):
        return self._configured

    def has_token(self):
        return self.token

    def save_token(self, text):
        self.calls.append("save_token")
        if not text.startswith("github_pat_"):
            raise ValueError("no")
        self.saved.append(text)
        self.token = True

    def check(self):
        self.calls.append("check")
        if self.gate is not None:
            self.gate.wait(10)
        return self.check_result

    def download_and_stage(self, found, install_dir, *, progress=None, cancel=None):
        self.calls.append("download")
        if progress:
            progress(50, 100)
        if self.stage_error:
            raise self.stage_error
        return svc.StagedUpdate(Path("staged"), found.manifest)

    def launch_helper(self, staged, install_dir, result_file):
        self.calls.append("launch")
        if self.launch_error:
            raise self.launch_error
        self.launched.append((staged, install_dir, result_file))


def newer(version="0.2.0"):
    manifest = mf.Manifest(version, f"OfflineDocTools-{version}-win64.zip", "a" * 64, 5, 1, None)
    return svc.CheckResult(
        svc.CheckStatus.NEWER, f"Version {version} is available.", version, manifest, 3
    )


def result(status, message, **kwargs):
    return svc.CheckResult(status, message, **kwargs)


def make(tmp_path, service, *, answers=(), confirm=True, install_dir="auto"):
    quits, asked, confirms = [], [], []
    install = tmp_path / "apps" / "OfflineDocTools" if install_dir == "auto" else install_dir
    runtime = UpdateRuntime(
        service,
        install,
        tmp_path / "data" / "update-result.json",
        tmp_path / "data" / svc.STAGING_NAME,
    )
    pending = list(answers)

    def ask(_parent):
        asked.append(1)
        return pending.pop(0) if pending else None

    def confirmer(_parent, title, text):
        confirms.append((title, text))
        return confirm

    window = MainWindow(
        find_executable=lambda: Path("soffice"),
        updates=runtime,
        ask_token=ask,
        confirm=confirmer,
        quit_app=lambda: quits.append(1),
    )
    return window, quits, asked, confirms


def test_nothing_is_requested_until_the_user_clicks(qapp, tmp_path):
    service = FakeService(newer())

    window, *_ = make(tmp_path, service)
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.005)

    assert service.calls == [] and window.update_label.text() == ""
    assert not window.install_button.isVisibleTo(window)


def test_a_window_without_an_updater_says_so_and_requests_nothing(qapp):
    window = MainWindow(find_executable=lambda: Path("soffice"))

    window.update_button.click()

    assert "not available" in window.update_label.text()


def test_an_unconfigured_build_reports_it(qapp, tmp_path):
    service = FakeService(
        result(svc.CheckStatus.NOT_CONFIGURED, "Updates are not configured in this build."),
        configured=False,
        token=False,
    )
    window, _, asked, _ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: "not configured" in window.update_label.text())

    assert asked == []  # no point asking for a token


def test_an_unconfigured_build_hides_the_update_controls(qapp, tmp_path):
    window, _, _, _ = make(tmp_path, FakeService(configured=False, token=False))

    assert window.updates_box.isHidden()


def test_a_window_without_an_updater_hides_the_update_controls(qapp):
    window = MainWindow(find_executable=lambda: Path("soffice"))

    assert window.updates_box.isHidden()


def test_a_configured_build_shows_the_update_controls(qapp, tmp_path):
    window, _, _, _ = make(tmp_path, FakeService(configured=True))

    assert not window.updates_box.isHidden()


def test_a_newer_version_is_announced_and_offers_installation(qapp, tmp_path):
    service = FakeService(newer())
    window, quits, _, _ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: window.install_button.isVisibleTo(window))

    assert window.update_label.text() == "Version 0.2.0 is available."
    assert service.calls == ["check"] and quits == []


def test_up_to_date_offers_no_installation(qapp, tmp_path):
    service = FakeService(
        result(svc.CheckStatus.UP_TO_DATE, "You have the latest version (0.1.0).")
    )
    window, *_ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: "latest" in window.update_label.text())

    assert not window.install_button.isVisibleTo(window)


def test_without_a_token_the_user_is_asked_and_the_check_then_runs(qapp, tmp_path):
    service = FakeService(newer(), token=False)
    window, _, asked, _ = make(tmp_path, service, answers=[TOKEN])

    window.update_button.click()
    _wait_until(lambda: window.install_button.isVisibleTo(window))

    assert asked == [1] and service.saved == [TOKEN] and service.calls == ["save_token", "check"]


def test_declining_to_enter_a_token_requests_nothing(qapp, tmp_path):
    service = FakeService(newer(), token=False)
    window, *_ = make(tmp_path, service, answers=[None])

    window.update_button.click()

    assert service.calls == []


def test_something_that_is_not_a_token_is_not_saved_or_used(qapp, tmp_path):
    service = FakeService(newer(), token=False)
    window, *_ = make(tmp_path, service, answers=["hunter2"])

    window.update_button.click()

    assert "does not look like" in window.update_label.text() and "check" not in service.calls


def test_the_access_button_replaces_the_token_without_checking(qapp, tmp_path):
    service = FakeService(newer())
    window, *_ = make(tmp_path, service, answers=[TOKEN])

    window.token_button.click()

    assert service.saved == [TOKEN] and "check" not in service.calls
    assert window.update_label.text() == "Update access saved."


def test_a_refused_token_is_explained_and_conversion_stays_usable(
    qapp, tmp_path, make_docx, output_dir
):
    service = FakeService(
        result(
            svc.CheckStatus.FAILED,
            "The update access token was refused or has expired.",
            kind="auth",
        )
    )
    window, *_ = make(tmp_path, service)
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)

    window.update_button.click()
    _wait_until(lambda: "token" in window.update_label.text())

    assert window.convert_button.isEnabled() and window.token_button.isEnabled()
    assert not window.install_button.isVisibleTo(window)


def found_window(qapp, tmp_path, service=None, **kwargs):
    service = service or FakeService(newer())
    window, quits, asked, confirms = make(tmp_path, service, **kwargs)
    window.update_button.click()
    _wait_until(lambda: window.install_button.isVisibleTo(window))
    return window, service, quits, confirms


def test_installing_needs_consent_and_declining_changes_nothing(qapp, tmp_path):
    window, service, quits, confirms = found_window(qapp, tmp_path, confirm=False)

    window.install_button.click()

    assert len(confirms) == 1 and "0.2.0" in confirms[0][1]
    assert service.calls == ["check"] and quits == []


def test_a_consented_update_is_downloaded_handed_to_the_helper_and_the_app_quits(qapp, tmp_path):
    window, service, quits, _ = found_window(qapp, tmp_path)

    window.install_button.click()
    _wait_until(lambda: quits)

    assert service.calls == ["check", "download", "launch"]
    staged, install_dir, result_file = service.launched[0]
    assert install_dir == tmp_path / "apps" / "OfflineDocTools"
    assert result_file == tmp_path / "data" / "update-result.json"
    assert "restart" in window.update_label.text()


def test_a_failed_download_is_reported_and_the_app_keeps_running(qapp, tmp_path):
    window, service, quits, _ = found_window(qapp, tmp_path)
    service.stage_error = svc.UpdateFailure("The downloaded update does not match.", "hash")

    window.install_button.click()
    _wait_until(lambda: "does not match" in window.update_label.text())

    assert quits == [] and "launch" not in service.calls and window.update_button.isEnabled()


def test_an_unexpected_error_while_downloading_is_reported_without_its_text(qapp, tmp_path):
    window, service, quits, _ = found_window(qapp, tmp_path)
    service.stage_error = RuntimeError(r"C:\secret\path")

    window.install_button.click()
    _wait_until(lambda: window.update_label.text() == "The update failed.")

    assert quits == [] and "secret" not in window.update_label.text()


def test_a_helper_that_cannot_be_started_leaves_everything_as_it_was(qapp, tmp_path):
    window, service, quits, _ = found_window(qapp, tmp_path)
    service.launch_error = OSError("blocked by policy")

    window.install_button.click()
    _wait_until(lambda: "could not be started" in window.update_label.text())

    assert quits == [] and window.update_button.isEnabled()


def test_installing_is_refused_outside_the_packaged_application(qapp, tmp_path):
    window, service, quits, confirms = found_window(qapp, tmp_path, install_dir=None)

    window.install_button.click()

    assert "packaged application" in window.update_label.text()
    assert confirms == [] and service.calls == ["check"]


def test_installing_is_refused_while_a_conversion_runs(qapp, tmp_path, monkeypatch):
    window, service, quits, confirms = found_window(qapp, tmp_path)
    monkeypatch.setattr(window, "is_converting", lambda: True)

    window.install_button.click()

    assert confirms == [] and service.calls == ["check"]


def test_checking_is_refused_while_a_conversion_runs(qapp, tmp_path, monkeypatch):
    service = FakeService(newer())
    window, *_ = make(tmp_path, service)
    monkeypatch.setattr(window, "is_converting", lambda: True)

    window.update_button.click()

    assert service.calls == []


def test_conversion_is_blocked_only_while_an_update_is_being_fetched(
    qapp, tmp_path, make_docx, output_dir
):
    gate = threading.Event()
    service = FakeService(newer(), gate=gate)
    window, *_ = make(tmp_path, service)
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)
    assert window.convert_button.isEnabled()

    window.update_button.click()
    _wait_until(lambda: "check" in service.calls)
    assert not window.convert_button.isEnabled() and not window.update_button.isEnabled()
    gate.set()
    _wait_until(lambda: window.convert_button.isEnabled())


def test_a_second_click_while_checking_does_not_start_a_second_check(qapp, tmp_path):
    gate = threading.Event()
    service = FakeService(newer(), gate=gate)
    window, *_ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: service.calls == ["check"])
    window._check_for_updates()
    gate.set()
    _wait_until(lambda: window.install_button.isVisibleTo(window))

    assert service.calls == ["check"]


def test_closing_the_window_during_a_check_is_harmless(qapp, tmp_path):
    gate = threading.Event()
    service = FakeService(newer(), gate=gate)
    window, *_ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: service.calls == ["check"])
    window.close()
    gate.set()
    for _ in range(40):
        QApplication.processEvents()
        time.sleep(0.005)


def test_a_check_that_raises_unexpectedly_still_ends_with_a_message(qapp, tmp_path):
    service = FakeService(newer())
    service.check = lambda: 1 / 0
    window, *_ = make(tmp_path, service)

    window.update_button.click()
    _wait_until(lambda: "unexpectedly" in window.update_label.text())

    assert window.update_button.isEnabled()


def test_the_outcome_of_the_last_update_is_shown_once_at_start(qapp, tmp_path):
    result_file = tmp_path / "data" / "update-result.json"
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        json.dumps(
            {
                "ok": False,
                "message": "The new version did not start, so the previous version was restored.",
                "to": "0.2.0",
            }
        )
    )

    window, *_ = make(tmp_path, FakeService(newer()))

    assert window.update_label.text().startswith("Update failed: The new version did not start")
    assert not result_file.exists()
    again, *_ = make(tmp_path, FakeService(newer()))
    assert again.update_label.text() == ""


def test_a_successful_update_is_announced_after_the_restart(qapp, tmp_path):
    result_file = tmp_path / "data" / "update-result.json"
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        json.dumps({"ok": True, "message": "The update was installed.", "to": "0.2.0"})
    )

    window, *_ = make(tmp_path, FakeService(newer()))

    assert window.update_label.text() == "Update to 0.2.0: The update was installed."


def test_the_leftover_staging_folder_of_the_last_update_is_removed_at_start(qapp, tmp_path):
    staging = tmp_path / "data" / svc.STAGING_NAME / "payload"
    staging.mkdir(parents=True)
    result_file = tmp_path / "data" / "update-result.json"
    result_file.write_text(
        json.dumps({"ok": True, "message": "The update was installed.", "to": "0.2.0"})
    )

    make(tmp_path, FakeService(newer()))

    assert not (tmp_path / "data" / svc.STAGING_NAME).exists()


def test_staging_is_not_removed_while_no_result_exists_because_a_helper_may_still_run(
    qapp, tmp_path
):
    staging = tmp_path / "data" / svc.STAGING_NAME / "payload"
    staging.mkdir(parents=True)

    make(tmp_path, FakeService(newer()))

    assert staging.exists()


def test_staging_is_not_removed_by_the_copy_a_helper_has_just_started(qapp, tmp_path, monkeypatch):
    staging = tmp_path / "data" / svc.STAGING_NAME / "payload"
    staging.mkdir(parents=True)
    result_file = tmp_path / "data" / "update-result.json"
    result_file.write_text(json.dumps({"ok": True, "message": "ok", "to": "0.2.0"}))
    monkeypatch.setenv("ODT_STARTUP_OK_FILE", str(tmp_path / "marker"))

    make(tmp_path, FakeService(newer()))

    assert staging.exists()


def test_a_broken_update_setup_can_never_stop_the_window_opening(qapp, tmp_path):
    class Broken(UpdateRuntime):
        def startup_message(self):
            raise RuntimeError("boom")

    runtime = Broken(FakeService(newer()), None, tmp_path / "r.json", tmp_path / svc.STAGING_NAME)

    window = MainWindow(find_executable=lambda: Path("soffice"), updates=runtime)

    assert window.update_label.text() == "" and window.convert_button is not None
