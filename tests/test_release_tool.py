"""The owner-side release tool: keys stay outside the repo; releases are verified before upload."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from offline_doc_tools.updater import manifest as mf
from updater_fixtures import bundle_files, make_zip

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release" / "release_tool.py"
_spec = importlib.util.spec_from_file_location("release_tool", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
sys.modules["release_tool"] = tool
_spec.loader.exec_module(tool)

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


def zip_file(tmp_path, version="0.2.0", files=None) -> Path:
    path = tmp_path / f"OfflineDocTools-{version}-win64.zip"
    path.write_bytes(make_zip(files if files is not None else bundle_files(version)))
    return path


@pytest.fixture
def key(tmp_path_factory):
    path = tmp_path_factory.mktemp("secure") / "release.pem"
    tool.keygen(path)
    return path


def test_keygen_prints_a_public_key_and_will_not_overwrite_a_key(tmp_path):
    path = tmp_path / "k.pem"

    public = tool.keygen(path)

    assert len(public) == 64 and bytes.fromhex(public)
    with pytest.raises(tool.ToolError):
        tool.keygen(path)


def test_the_private_key_cannot_be_created_inside_the_repository():
    with pytest.raises(tool.ToolError):
        tool.keygen(tool.ROOT / "dist" / "should-not-exist.pem")

    assert not (tool.ROOT / "dist" / "should-not-exist.pem").exists()


def test_a_release_is_written_and_accepted_by_the_applications_own_checks(tmp_path, key):
    package = zip_file(tmp_path)

    out = tool.release(package, key, None, tmp_path / "assets", check_version=False)

    assert sorted(p.name for p in out.iterdir()) == [mf.MANIFEST_ASSET, mf.SIGNATURE_ASSET]
    public = bytes.fromhex(tool.public_key_hex(key))
    parsed = mf.verify_manifest(
        (out / mf.MANIFEST_ASSET).read_bytes(), (out / mf.SIGNATURE_ASSET).read_text(), [public]
    )
    assert parsed.version == "0.2.0" and parsed.package_name == package.name


def test_a_minimum_installed_version_can_be_required(tmp_path, key):
    out = tool.release(zip_file(tmp_path), key, "0.1.5", tmp_path / "assets", check_version=False)

    raw = (out / mf.MANIFEST_ASSET).read_bytes()
    assert b'"min_installed_version": "0.1.5"' in raw


def test_the_private_key_never_appears_in_the_output(tmp_path, key):
    out = tool.release(zip_file(tmp_path), key, None, tmp_path / "assets", check_version=False)
    secret = key.read_bytes()

    assert all(secret not in p.read_bytes() for p in out.iterdir())


def test_a_wrongly_named_zip_is_refused(tmp_path, key):
    bad = tmp_path / "package.zip"
    bad.write_bytes(make_zip(bundle_files("0.2.0")))

    with pytest.raises(tool.ToolError):
        tool.release(bad, key, None, tmp_path / "assets", check_version=False)


def test_a_package_that_the_application_would_refuse_is_never_released(tmp_path, key):
    package = zip_file(tmp_path, files={**bundle_files("0.2.0"), "../../evil.txt": b"x"})

    with pytest.raises(tool.ToolError) as caught:
        tool.release(package, key, None, tmp_path / "assets", check_version=False)

    assert "package" in str(caught.value)


def test_verification_rejects_a_different_key_and_a_modified_package(tmp_path, key):
    package = zip_file(tmp_path)
    out = tool.release(package, key, None, tmp_path / "assets", check_version=False)
    other = tmp_path / "other.pem"
    other_public = tool.keygen(other)

    with pytest.raises(tool.ToolError):
        tool.verify(out / mf.MANIFEST_ASSET, out / mf.SIGNATURE_ASSET, package, other_public)
    package.write_bytes(package.read_bytes() + b"x")
    with pytest.raises(tool.ToolError):
        tool.verify(
            out / mf.MANIFEST_ASSET, out / mf.SIGNATURE_ASSET, package, tool.public_key_hex(key)
        )


def test_a_garbage_key_file_is_reported_not_crashed(tmp_path):
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key")

    with pytest.raises(tool.ToolError):
        tool.public_key_hex(junk)
    with pytest.raises(tool.ToolError):
        tool.sign(b"x", junk)


def test_the_command_line_runs_keygen_and_release(tmp_path, capsys):
    path = tmp_path / "cli.pem"
    assert tool.main(["keygen", "--key", str(path)]) == 0
    assert "TRUSTED_PUBLIC_KEYS" in capsys.readouterr().out

    code = tool.main(
        [
            "release",
            "--zip",
            str(zip_file(tmp_path, tool.source_version())),
            "--key",
            str(path),
            "--out",
            str(tmp_path / "a"),
        ]
    )

    assert code == 0 and "Upload these three files" in capsys.readouterr().out
    assert tool.main(["release", "--zip", str(tmp_path / "nope.zip"), "--key", str(path)]) == 1


def test_a_zip_whose_version_differs_from_the_source_is_refused(tmp_path, key):
    from offline_doc_tools import __version__

    other = "9.9.9" if __version__ != "9.9.9" else "9.9.8"

    with pytest.raises(tool.ToolError) as caught:
        tool.release(zip_file(tmp_path, other), key, None, tmp_path / "assets")

    assert "version" in str(caught.value)


def test_a_zip_of_the_sources_own_version_is_accepted(tmp_path, key):
    from offline_doc_tools import __version__

    out = tool.release(zip_file(tmp_path, __version__), key, None, tmp_path / "assets")

    assert (out / mf.MANIFEST_ASSET).exists()


def test_openssl_is_looked_up_on_the_path_only_never_in_the_current_folder(tmp_path, monkeypatch):
    (tmp_path / "openssl.exe").write_bytes(b"not really")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(tool.ToolError):
        tool.openssl()
