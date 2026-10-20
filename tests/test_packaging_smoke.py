"""Tests for the pure logic of scripts/packaging_smoke/smoke_test.py.

That script gates a PASS for the built bundle, so its detectors need positive controls: a check
that can never fire is indistinguishable from a clean bundle. The end-to-end run stays manual.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "packaging_smoke" / "smoke_test.py"
_spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_test"] = smoke  # dataclasses look their module up here
_spec.loader.exec_module(smoke)


def touch(root: Path, *relative: str) -> None:
    for name in relative:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x")


def test_a_tcp_or_udp_line_that_cannot_be_read_still_counts_as_an_endpoint():
    report = smoke.parse_sampler_output("TCP|12|only|three\nUDP|12\nODT-SAMPLE|1|1\nODT-DONE|1\n")

    assert len(report.endpoints) == 2
    assert report.problem() is None


def test_a_clean_sample_has_no_endpoints_and_a_sampler_that_never_finished_is_no_proof():
    clean = smoke.parse_sampler_output("ODT-SAMPLE|3|1\nODT-DONE|3\n")
    unfinished = smoke.parse_sampler_output("ODT-SAMPLE|1|1\n")

    assert clean.endpoints == [] and clean.problem() is None
    assert unfinished.problem() is not None


def test_a_fully_described_endpoint_is_reported_once_however_often_it_was_sampled():
    line = "TCP|7|0.0.0.0|80|1.2.3.4|443|Established\n"

    report = smoke.parse_sampler_output(line * 3 + "ODT-SAMPLE|3|1\nODT-DONE|3\n")

    assert len(report.endpoints) == 1


@pytest.mark.parametrize(
    ("name", "is_dir"),
    [
        ("Qt6Network.dll", False),
        ("QtNetwork.pyd", False),
        ("tls", True),
        ("Qt6WebEngineCore.dll", False),
        ("Qt6Qml.dll", False),
        ("Qt6Quick.dll", False),
        ("report.docx", False),
        ("output.pdf", False),
        ("test_something.py", False),
        ("tests", True),
    ],
)
def test_forbidden_bundle_content_is_recognised(name, is_dir):
    assert smoke.classify_name(name, is_dir=is_dir) is not None


@pytest.mark.parametrize("name", ["Qt6Core.dll", "Qt6Widgets.dll", "qwindows.dll", "python3.dll"])
def test_ordinary_bundle_content_is_not_flagged(name):
    assert smoke.classify_name(name, is_dir=False) is None


def test_a_scan_finds_forbidden_content_nested_in_the_bundle(tmp_path):
    touch(tmp_path, "OfflineDocTools.exe", "_internal/PySide6/Qt6Network.dll")
    touch(tmp_path, "_internal/PySide6/plugins/platforms/qwindows.dll")

    scan = smoke.scan_bundle(tmp_path)

    assert [f.category for f in scan.findings] == ["Qt Network"]
    assert "qwindows.dll" in scan.platform_plugins


def test_the_layout_needs_both_licence_texts_not_just_any_licence_file(tmp_path):
    touch(tmp_path, "OfflineDocTools.exe", "licenses/Python-LICENSE.txt")
    (tmp_path / "OfflineDocTools.exe").write_bytes(b"MZ")
    scan = smoke.scan_bundle(tmp_path)

    layout = smoke.check_layout(tmp_path, scan)

    assert any("LGPL-3.0.txt" in problem for problem in layout.problems)


def test_python_folders_are_dropped_from_path_and_system_folders_are_kept():
    system = r"C:\Windows\System32"
    path = ";".join([system, r"C:\Users\x\proj\.venv\Scripts", r"C:\Python314", r"C:\Tools"])

    kept = smoke.sanitize_path(path, roots=[], keep_under=r"C:\Windows")

    assert kept.split(";") == [system, r"C:\Tools"]


def test_the_child_environment_carries_nothing_from_the_development_environment(tmp_path):
    base = {
        "SYSTEMROOT": r"C:\Windows",
        "PATH": r"C:\Windows\System32",
        "VIRTUAL_ENV": r"C:\proj\.venv",
        "PYTHONPATH": "x",
        "QT_PLUGIN_PATH": "x",
        "OFFLINE_DOC_TOOLS_SOFFICE": "x",
    }

    env = smoke.build_child_env(base, tmp_path)

    assert set(env) == {
        "SYSTEMROOT",
        "PATH",
        "LOCALAPPDATA",
        "QT_QPA_PLATFORM",
        "QT_LOGGING_TO_CONSOLE",
    }
    assert env["LOCALAPPDATA"] == str(tmp_path)


def test_the_layout_needs_the_tls_files_the_update_check_depends_on(tmp_path):
    touch(tmp_path, "OfflineDocTools.exe", "licenses/LGPL-3.0.txt")
    (tmp_path / "OfflineDocTools.exe").write_bytes(b"MZ")
    layout = smoke.check_layout(tmp_path, smoke.scan_bundle(tmp_path))
    assert any("TLS support missing" in problem for problem in layout.problems)

    touch(tmp_path, "_internal/_ssl.pyd", "_internal/libssl-3.dll", "_internal/libcrypto-3.dll")
    layout = smoke.check_layout(tmp_path, smoke.scan_bundle(tmp_path))
    assert not any("TLS support missing" in problem for problem in layout.problems)


def test_the_layout_needs_the_third_party_notices_beside_the_exe(tmp_path):
    touch(tmp_path, "OfflineDocTools.exe")
    (tmp_path / "OfflineDocTools.exe").write_bytes(b"MZ")
    layout = smoke.check_layout(tmp_path, smoke.scan_bundle(tmp_path))
    assert any("THIRD-PARTY-NOTICES.txt" in problem for problem in layout.problems)

    touch(tmp_path, "THIRD-PARTY-NOTICES.txt")
    layout = smoke.check_layout(tmp_path, smoke.scan_bundle(tmp_path))
    assert not any("THIRD-PARTY-NOTICES.txt" in problem for problem in layout.problems)
