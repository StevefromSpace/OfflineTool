"""The Windows bundle must carry the licence texts that Qt's LGPL licence requires.

``scripts/packaging/bundle_policy.py`` is a build script helper, not part of the installed
package, so it is loaded from its file.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "scripts" / "packaging"


def _load_policy():
    spec = importlib.util.spec_from_file_location("bundle_policy", PACKAGING / "bundle_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _load_policy()


def test_a_bundle_without_the_lgpl_and_gpl_texts_is_reported_as_incomplete(tmp_path):
    (tmp_path / "licenses").mkdir()
    (tmp_path / "licenses" / "Python-LICENSE.txt").write_text("PSF", encoding="utf-8")

    missing = policy.missing_files(tmp_path)

    assert "licenses/LGPL-3.0.txt" in missing
    assert "licenses/GPL-3.0.txt" in missing


def test_a_bundle_that_carries_both_texts_is_not_missing_them(tmp_path):
    (tmp_path / "licenses").mkdir()
    for name in policy.REQUIRED_LICENCE_TEXTS:
        (tmp_path / "licenses" / name).write_text("text", encoding="utf-8")

    assert not [m for m in policy.missing_files(tmp_path) if m.startswith("licenses/")]


def test_the_committed_licence_texts_are_the_ones_the_build_copies():
    markers = {
        "LGPL-3.0.txt": "GNU LESSER GENERAL PUBLIC LICENSE",
        "GPL-3.0.txt": "GNU GENERAL PUBLIC LICENSE",
        "OpenSSL-Apache-2.0.txt": "Apache License",
    }
    assert set(markers) == set(policy.REQUIRED_LICENCE_TEXTS)
    for name, marker in markers.items():
        text = (PACKAGING / "licenses" / name).read_text(encoding="utf-8")
        assert marker in text and "Version" in text, name
        assert len(text) > 5000, name


def test_a_bundle_without_the_tls_files_is_reported_as_incomplete(tmp_path):
    missing = policy.missing_files(tmp_path)

    assert "_internal/_ssl.pyd" in missing
    assert "_internal/libssl-3*.dll" in missing
    assert "_internal/libcrypto-3*.dll" in missing


def test_a_bundle_with_the_tls_files_is_not_missing_them(tmp_path):
    contents = tmp_path / "_internal"
    contents.mkdir()
    for name in ("_ssl.pyd", "libssl-3.dll", "libcrypto-3-x64.dll"):
        (contents / name).write_bytes(b"x")

    assert not [m for m in policy.missing_files(tmp_path) if "ssl" in m or "crypto" in m]


@pytest.mark.parametrize(
    "path",
    [
        "PySide6/Qt6Network.dll",
        "PySide6/QtNetwork.pyd",
        "PySide6/Qt6Qml.dll",
        "PySide6/plugins/tls/qschannelbackend.dll",
        "PySide6/plugins/imageformats/qjpeg.dll",
        "PySide6/opengl32sw.dll",
        "_hashlib.pyd",
        "libssl-1_1.dll",
        "libcrypto-1_1-x64.dll",
        "qtwebengineprocess.exe",
        "resources.pak",
        "soffice.exe",
    ],
)
def test_the_detector_rejects_what_the_app_must_not_ship(path):
    assert policy.unwanted_reason(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "PySide6/Qt6Core.dll",
        "PySide6/QtWidgets.pyd",
        "PySide6/plugins/platforms/qwindows.dll",
        "PySide6/plugins/styles/qmodernwindowsstyle.dll",
        "shiboken6/shiboken6.abi3.dll",
        "python3.dll",
        "_ssl.pyd",
        "libssl-3.dll",
        "libssl-3-x64.dll",
        "libcrypto-3.dll",
        "libcrypto-3-x64.dll",
    ],
)
def test_the_detector_accepts_what_the_app_needs(path):
    assert policy.unwanted_reason(path) is None


NOTICES = PACKAGING / "licenses" / "THIRD-PARTY-NOTICES.txt"


def test_a_bundle_without_the_third_party_notices_beside_the_exe_is_incomplete(tmp_path):
    assert policy.ROOT_NOTICE in policy.missing_files(tmp_path)

    (tmp_path / policy.ROOT_NOTICE).write_text("x", encoding="utf-8")

    assert policy.ROOT_NOTICE not in policy.missing_files(tmp_path)


def test_the_notices_start_with_the_prominent_lgpl_notice():
    text = NOTICES.read_text(encoding="utf-8")
    opening = text[:1500]

    assert "THIS PROGRAM USES THE Qt LIBRARIES AND PySide6" in opening
    assert "LESSER GENERAL PUBLIC LICENSE VERSION 3" in opening
    assert r"licenses\LGPL-3.0.txt" in opening and r"licenses\GPL-3.0.txt" in opening


def test_the_notices_name_the_qt_version_that_is_pinned_for_the_build():
    pins = tomllib.loads((PACKAGING.parents[1] / "pyproject.toml").read_text("utf-8"))
    pinned = next(
        p.split("==")[1]
        for p in pins["project"]["optional-dependencies"]["build"]
        if p.startswith("PySide6-Essentials==")
    )
    text = NOTICES.read_text(encoding="utf-8")

    assert f"Qt {pinned} " in text
    assert f"PySide6 / shiboken6 {pinned} " in text
    assert f"qtbase-everywhere-src-{pinned}.tar.xz" in text


@pytest.mark.parametrize(
    "component",
    [
        "PCRE2",
        "Data Compression Library (zlib)",
        "Freetype 2",
        "HarfBuzz-NG",
        "LibPNG",
        "Wintab API",
        "Unicode Character Database (UCD)",
        "OpenSSL 3.0.20",
        "Mersenne Twister",
        "expat",
        "mimalloc",
        "Microsoft Visual C++ runtime",
        "Bootloader Exception",
    ],
)
def test_the_notices_cover_the_components_found_in_the_shipped_binaries(component):
    assert component in NOTICES.read_text(encoding="utf-8")


def test_the_microsoft_runtime_files_can_be_removed_and_nothing_else(tmp_path):
    keep = [
        "OfflineDocTools.exe",
        "_internal/python314.dll",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/libssl-3.dll",
        "_internal/PySide6/msvcp_not_the_runtime.dll",
        "licenses/MSVCP140-note.txt",
    ]
    runtime = [
        "_internal/VCRUNTIME140.dll",
        "_internal/VCRUNTIME140_1.dll",
        "_internal/PySide6/MSVCP140.dll",
        "_internal/PySide6/MSVCP140_1.dll",
        "_internal/PySide6/MSVCP140_2.dll",
        "_internal/shiboken6/msvcp140.dll",
        "_internal/concrt140.dll",
    ]
    for name in keep + runtime:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")

    removed = policy.remove_ms_runtime(tmp_path)

    assert sorted(removed) == sorted(runtime)
    assert all((tmp_path / name).exists() for name in keep)
    assert not any((tmp_path / name).exists() for name in runtime)
    assert policy.remove_ms_runtime(tmp_path) == []
