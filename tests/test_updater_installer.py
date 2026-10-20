"""Replacing the installed bundle: backup, copy, startup verification, rollback, recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offline_doc_tools.updater import installer as ins
from updater_fixtures import bundle_files, write_bundle


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class Env:
    """An installed 0.1.0 bundle with an obsolete file, and a staged 0.2.0 payload."""

    def __init__(self, tmp_path: Path) -> None:
        self.parent = tmp_path / "apps"
        self.install = write_bundle(
            self.parent / "OfflineDocTools", "0.1.0", extra={"_internal/obsolete.dll": b"old"}
        )
        self.payload = write_bundle(
            tmp_path / "staging" / "OfflineDocTools",
            "0.2.0",
            extra={"_internal/brand_new.dll": b"new"},
        )
        self.old = snapshot(self.install)
        self.new = snapshot(self.payload)
        self.started: list[Path] = []

    def run(self, verify=None, **kwargs):
        def default_verify(exe: Path) -> bool:
            self.started.append(exe)
            return True

        defaults = dict(
            parent_pid=None,
            start_and_verify=verify or default_verify,
            sleep=lambda _s: None,
            free_bytes=lambda _p: 10**12,
        )
        defaults.update(kwargs)
        return ins.apply_update(self.install, self.payload, **defaults)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.parent.iterdir() if p.name != "OfflineDocTools")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_a_normal_update_replaces_every_file_and_removes_obsolete_ones(env):
    result = env.run()

    assert result.ok and not result.rolled_back and result.code == "installed"
    assert snapshot(env.install) == env.new
    assert "_internal/obsolete.dll" not in snapshot(env.install)
    assert env.started == [env.install / "OfflineDocTools.exe"]
    assert env.leftovers() == []  # no backup and no journal remain


def test_the_updater_itself_is_replaced_like_any_other_file(env):
    env.run()

    assert (env.install / "OfflineDocTools.exe").read_bytes() == b"EXE 0.2.0"


def test_a_new_version_that_does_not_start_is_rolled_back_to_the_old_one(env):
    result = env.run(verify=lambda _exe: False)

    assert not result.ok and result.rolled_back and result.code == "startup_failed"
    assert snapshot(env.install) == env.old
    assert env.leftovers() == []


def test_a_crash_while_starting_the_new_version_is_rolled_back(env):
    def boom(_exe):
        raise RuntimeError("process vanished")

    result = env.run(verify=boom)

    assert result.rolled_back and snapshot(env.install) == env.old


def test_a_failure_while_copying_is_rolled_back_and_leaves_no_half_copy(env, monkeypatch):
    real = ins.shutil.copy2
    calls = []

    def flaky(src, dst, *a, **k):
        calls.append(src)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(ins.shutil, "copy2", flaky)

    result = env.run()

    assert not result.ok and result.rolled_back and result.code == "copy_failed"
    assert snapshot(env.install) == env.old and env.leftovers() == []


def test_a_copy_that_differs_from_the_payload_is_rolled_back(env, monkeypatch):
    real = ins.shutil.copy2

    def corrupting(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        if str(src).endswith("Qt6Core.dll"):
            Path(dst).write_bytes(b"truncated")
        return out

    monkeypatch.setattr(ins.shutil, "copy2", corrupting)

    result = env.run()

    assert result.rolled_back and result.code == "copy_failed" and snapshot(env.install) == env.old


def test_a_locked_installation_is_left_exactly_as_it_was(env, monkeypatch):
    def locked(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(ins.os, "replace", locked)

    result = env.run()

    assert not result.ok and not result.rolled_back and result.code == "locked"
    assert snapshot(env.install) == env.old and env.leftovers() == []


def test_a_still_running_application_blocks_the_update_without_changes(env):
    result = env.run(wait_for_exit=lambda _pid, _timeout: False, parent_pid=4242)

    assert result.code == "busy" and snapshot(env.install) == env.old and env.leftovers() == []


def test_the_helper_waits_for_the_application_before_touching_files(env):
    order = []

    def waiting(pid, _timeout):
        order.append(("wait", pid, snapshot(env.install) == env.old))
        return True

    env.run(wait_for_exit=waiting, parent_pid=4242)

    assert order == [("wait", 4242, True)]


def test_if_the_old_version_cannot_be_restored_both_folders_are_kept_and_named(env, monkeypatch):
    real = ins.os.replace
    calls = []

    def replace(src, dst):
        calls.append((src, dst))
        if len(calls) >= 2:  # every attempt to rename the backup back
            raise PermissionError("cannot restore")
        return real(src, dst)

    monkeypatch.setattr(ins.os, "replace", replace)

    result = env.run(verify=lambda _exe: False)

    assert not result.ok and result.code == "rollback_failed"
    names = env.leftovers()
    assert any(n.startswith("OfflineDocTools.backup-") for n in names)
    assert "OfflineDocTools.backup-" in result.message and str(env.parent) not in result.message


@pytest.mark.parametrize(
    "make_bad",
    [
        lambda e: e.install / "OfflineDocTools.exe",  # not a folder
        lambda e: e.parent,  # a folder that is not an installation
        lambda e: Path(e.parent.anchor),  # a drive root
        lambda e: Path.home(),
        lambda e: e.payload,  # the staging folder itself
    ],
)
def test_a_directory_that_is_not_an_installation_is_never_touched(env, make_bad):
    bad = make_bad(env)
    before = sorted(p.name for p in bad.iterdir()) if bad.is_dir() else None

    with pytest.raises(ins.InstallError) as caught:
        ins.apply_update(
            bad,
            env.payload,
            parent_pid=None,
            start_and_verify=lambda _e: True,
            sleep=lambda _s: None,
            free_bytes=lambda _p: 10**12,
        )

    assert caught.value.code == "refused"
    assert (sorted(p.name for p in bad.iterdir()) if bad.is_dir() else None) == before


def test_a_payload_inside_the_installation_is_refused(env):
    inner = write_bundle(env.install / "nested" / "OfflineDocTools", "0.2.0")

    with pytest.raises(ins.InstallError):
        ins.apply_update(env.install, inner, parent_pid=None, start_and_verify=lambda _e: True)

    assert snapshot(env.install).keys() >= env.old.keys()


def test_a_payload_without_the_required_layout_is_refused(env):
    (env.payload / "OfflineDocTools.exe").unlink()

    with pytest.raises(ins.InstallError):
        env.run()

    assert snapshot(env.install) == env.old


def test_too_little_disk_space_is_refused_before_anything_changes(env):
    with pytest.raises(ins.InstallError) as caught:
        env.run(free_bytes=lambda _p: 10)

    assert caught.value.code == "space" and snapshot(env.install) == env.old


def test_an_unwritable_parent_folder_is_refused_before_anything_changes(env, monkeypatch):
    monkeypatch.setattr(ins, "_can_write", lambda _p: False)

    with pytest.raises(ins.InstallError) as caught:
        env.run()

    assert caught.value.code == "not_writable" and snapshot(env.install) == env.old


def test_an_unknown_protocol_is_refused(env):
    with pytest.raises(ins.InstallError) as caught:
        env.run(protocol=99)

    assert caught.value.code == "unsupported"


# --- interrupted installations ---------------------------------------------------------------


def test_an_installation_interrupted_before_the_copy_is_restored(env):
    backup = env.parent / "OfflineDocTools.backup-x"
    env.install.replace(backup)  # as if the helper died right after the first rename
    ins._write_journal(env.parent, "OfflineDocTools", backup.name, "swapping")

    note = ins.recover_interrupted(env.install)

    assert note and snapshot(env.install) == env.old and env.leftovers() == []


def test_an_unverified_new_copy_is_discarded_when_recovering(env):
    backup = env.parent / "OfflineDocTools.backup-x"
    env.install.replace(backup)
    write_bundle(env.install, "0.2.0")  # the new, never verified, copy
    ins._write_journal(env.parent, "OfflineDocTools", backup.name, "verifying")

    ins.recover_interrupted(env.install)

    assert snapshot(env.install) == env.old and env.leftovers() == []


def test_a_verified_new_copy_is_kept_and_only_the_backup_is_deleted(env):
    backup = env.parent / "OfflineDocTools.backup-x"
    env.install.replace(backup)
    write_bundle(env.install, "0.2.0")
    ins._write_journal(env.parent, "OfflineDocTools", backup.name, "verified")

    ins.recover_interrupted(env.install)

    assert snapshot(env.install) == bundle_files("0.2.0") or "OfflineDocTools.exe" in snapshot(
        env.install
    )
    assert env.leftovers() == []


def test_recovery_does_nothing_when_there_is_no_journal(env):
    assert ins.recover_interrupted(env.install) is None
    assert snapshot(env.install) == env.old


def test_a_forged_journal_cannot_make_recovery_touch_other_folders(env, tmp_path):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("keep")
    (env.parent / ".OfflineDocTools.update-journal.json").write_text(
        json.dumps({"install": "OfflineDocTools", "backup": "../precious", "state": "swapping"})
    )

    ins.recover_interrupted(env.install)

    assert (victim / "data.txt").read_text() == "keep" and snapshot(env.install) == env.old


def test_the_result_is_written_for_the_next_start_to_show(env, tmp_path):
    result = env.run()
    target = tmp_path / "result.json"

    ins.write_result(target, result, "0.1.0", "0.2.0")

    data = json.loads(target.read_text())
    assert data["ok"] is True and data["to"] == "0.2.0" and "message" in data
    assert ins.read_result(target)["ok"] is True
    assert ins.read_result(tmp_path / "missing.json") is None


@pytest.mark.parametrize(
    "foreign",
    [
        ("notes.txt", b"my notes"),
        ("OtherProgram/tool.exe", b"other"),
        ("Documents/report.docx", b"data"),
    ],
)
def test_a_folder_that_holds_other_peoples_files_is_never_replaced(env, foreign):
    name, data = foreign
    target = env.install / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    before = snapshot(env.install)

    with pytest.raises(ins.InstallError) as caught:
        env.run()

    assert caught.value.code == "refused"
    assert snapshot(env.install) == before and env.leftovers() == []


def test_a_folder_name_does_not_matter_only_what_is_in_it(env, tmp_path):
    renamed = tmp_path / "apps" / "My Converter"
    env.install.replace(renamed)
    env.install = renamed

    assert env.run().ok


def test_top_level_items_that_the_new_version_also_has_are_accepted(env):
    (env.install / "plugins").mkdir()
    (env.install / "plugins" / "a.dll").write_bytes(b"a")
    (env.payload / "plugins").mkdir()
    (env.payload / "plugins" / "a.dll").write_bytes(b"b")

    assert env.run().ok


def test_recovery_waits_until_the_application_has_exited(env):
    backup = env.parent / "OfflineDocTools.backup-x"
    env.install.replace(backup)
    ins._write_journal(env.parent, "OfflineDocTools", backup.name, "swapping")

    result = env.run(wait_for_exit=lambda _p, _t: False, parent_pid=4242)

    assert result.code == "busy" and backup.exists() and not env.install.exists()


def test_a_recovery_that_fails_stops_the_update(env, monkeypatch):
    backup = env.parent / "OfflineDocTools.backup-x"
    env.install.replace(backup)
    ins._write_journal(env.parent, "OfflineDocTools", backup.name, "swapping")
    real = Path.replace

    def failing(self, target):
        if self == backup:
            raise PermissionError("locked")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", failing)

    with pytest.raises(ins.InstallError) as caught:
        env.run()

    assert caught.value.code == "recovery_failed" and backup.exists()


def test_a_journal_that_cannot_be_updated_does_not_undo_a_confirmed_start(env, monkeypatch):
    real = ins._write_journal

    def flaky(parent, install_name, backup_name, state):
        if state == "verified":
            raise OSError("disk full")
        return real(parent, install_name, backup_name, state)

    monkeypatch.setattr(ins, "_write_journal", flaky)

    result = env.run()

    assert result.ok and snapshot(env.install) == env.new
