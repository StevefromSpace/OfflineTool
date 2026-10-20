"""Staging a downloaded package: nothing is extracted until every check has passed."""

from __future__ import annotations

import io
import zipfile

import pytest

from offline_doc_tools.updater import manifest as m
from offline_doc_tools.updater import package as pk
from updater_fixtures import TOP, bundle_files, make_zip, manifest_dict, signed

GOOD = bundle_files("0.2.0")


def manifest_for(data: bytes, version="0.2.0") -> m.Manifest:
    raw, sig = signed(manifest_dict(version, data))
    from updater_fixtures import PUBLIC

    return m.verify_manifest(raw, sig, [PUBLIC])


def write_package(tmp_path, data: bytes, name="pkg.zip"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def stage(tmp_path, data: bytes, **kwargs):
    package = write_package(tmp_path, data)
    return pk.stage_package(package, manifest_for(data), tmp_path / "stage", **kwargs)


def kind_of(tmp_path, data: bytes, **kwargs) -> str:
    with pytest.raises(pk.PackageError) as caught:
        stage(tmp_path, data, **kwargs)
    return caught.value.kind


def raw_zip(entries: list[tuple[str, bytes]], *, patch: dict[bytes, bytes] | None = None) -> bytes:
    """A zip with exact entry names (ZipInfo keeps '..', '/' and ':'). ``patch`` swaps bytes of
    equal length afterwards, e.g. to put a backslash into a name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(zipfile.ZipInfo(name), data)
    out = buffer.getvalue()
    for old, new in (patch or {}).items():
        assert len(old) == len(new)
        out = out.replace(old, new)
    return out


def good_entries() -> list[tuple[str, bytes]]:
    return [(f"{TOP}/{name}", data) for name, data in GOOD.items()]


def test_a_good_package_is_extracted_exactly(tmp_path):
    payload = stage(tmp_path, make_zip(GOOD))

    assert payload == tmp_path / "stage" / TOP
    found = {
        p.relative_to(payload).as_posix(): p.read_bytes() for p in payload.rglob("*") if p.is_file()
    }
    assert found == GOOD


def test_a_package_that_differs_from_the_signed_hash_is_refused_before_it_is_opened(tmp_path):
    data = make_zip(GOOD)
    package = write_package(tmp_path, data)
    manifest = manifest_for(
        make_zip(bundle_files("0.2.0", extra={"x": b"y"}))
    )  # signed for another file

    with pytest.raises(pk.PackageError) as caught:
        pk.stage_package(package, manifest, tmp_path / "stage")

    assert caught.value.kind in ("size", "hash")
    assert not (tmp_path / "stage").exists()


def test_a_package_of_the_right_size_but_other_content_is_refused(tmp_path):
    data = make_zip(GOOD)
    tampered = bytearray(data)
    tampered[len(tampered) // 2] ^= 0xFF
    package = write_package(tmp_path, bytes(tampered))

    with pytest.raises(pk.PackageError) as caught:
        pk.stage_package(package, manifest_for(data), tmp_path / "stage")

    assert caught.value.kind == "hash" and not (tmp_path / "stage").exists()


def test_a_truncated_download_is_refused(tmp_path):
    data = make_zip(GOOD)
    package = write_package(tmp_path, data[: len(data) // 2])

    with pytest.raises(pk.PackageError) as caught:
        pk.stage_package(package, manifest_for(data), tmp_path / "stage")

    assert caught.value.kind == "size"


def test_a_missing_download_is_a_filesystem_error(tmp_path):
    with pytest.raises(pk.PackageError) as caught:
        pk.stage_package(tmp_path / "gone.zip", manifest_for(b"x"), tmp_path / "stage")

    assert caught.value.kind == "filesystem"


@pytest.mark.parametrize(
    "name",
    [
        f"{TOP}/../evil.txt",
        f"{TOP}/sub/../../evil.txt",
        "/abs/evil.txt",
        "C:/evil.txt",
        f"{TOP}/C:evil.txt",
        f"{TOP}/file.txt:stream",
        f"{TOP}//double.txt",
        f"{TOP}/./dot.txt",
        f"{TOP}/trailing.",
        f"{TOP}/trailing ",
        f"{TOP}/CON",
        f"{TOP}/nul.txt",
        f"{TOP}/_internal/COM1.dll",
        f"{TOP}/lpt9",
        f"{TOP}/CONIN$",
        f"{TOP}/conout$.txt",
        f"{TOP}/COM\u00b9",
        f"{TOP}/LPT\u00b2.txt",
        f"{TOP}/COM\u00b3.dll",
        f"{TOP}/bad\x01name",
        f"{TOP}/{'a' * 300}",
        "OtherTop/file.txt",
        "file-at-root.txt",
    ],
)
def test_hostile_entry_names_are_refused_and_nothing_is_extracted(tmp_path, name):
    data = raw_zip([*good_entries(), (name, b"bad")])

    assert kind_of(tmp_path, data) == "archive"
    assert not (tmp_path / "stage").exists() and not (tmp_path / "evil.txt").exists()


def test_a_backslash_traversal_is_refused(tmp_path):
    # Python on Windows turns "\\" into "/" when it reads a name, so this arrives as "../evil.txt".
    entries = [*good_entries(), (f"{TOP}/XX/evil.txt", b"bad")]
    data = raw_zip(entries, patch={b"XX/": b"..\\"})

    assert kind_of(tmp_path, data) == "archive" and not (tmp_path / "evil.txt").exists()


def test_symbolic_links_are_refused(tmp_path):
    link = zipfile.ZipInfo(f"{TOP}/link")
    link.external_attr = 0o120777 << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in good_entries():
            archive.writestr(name, data)
        archive.writestr(link, "C:/Windows")

    assert kind_of(tmp_path, buffer.getvalue()) == "archive"


@pytest.mark.parametrize(
    "extension", [".bat", ".cmd", ".ps1", ".vbs", ".js", ".lnk", ".scr", ".msi", ".reg"]
)
def test_script_and_shortcut_files_are_refused(tmp_path, extension):
    data = raw_zip([*good_entries(), (f"{TOP}/helper{extension}", b"bad")])

    assert kind_of(tmp_path, data) == "archive"


def test_names_that_differ_only_in_case_are_refused(tmp_path):
    data = raw_zip([*good_entries(), (f"{TOP}/readme.MD", b"x")])

    assert kind_of(tmp_path, data) == "archive"


def test_a_file_and_a_folder_with_the_same_name_are_refused(tmp_path):
    data = raw_zip([*good_entries(), (f"{TOP}/_internal", b"x")])

    assert kind_of(tmp_path, data) == "archive"


def test_encrypted_entries_are_refused(tmp_path):
    data = bytearray(make_zip(GOOD))
    # Set the "encrypted" flag bit in the first central directory entry (offset 8 of the header).
    index = bytes(data).find(b"PK\x01\x02")
    data[index + 8] |= 0x01

    assert kind_of(tmp_path, bytes(data)) == "archive"


@pytest.mark.parametrize(
    "missing",
    [
        "OfflineDocTools.exe",
        "THIRD-PARTY-NOTICES.txt",
        "_internal/python314.dll",
        "licenses/LGPL-3.0.txt",
    ],
)
def test_a_package_without_a_required_part_is_refused(tmp_path, missing):
    files = dict(GOOD)
    if missing.endswith("python314.dll"):
        files = {k: v for k, v in files.items() if not k.startswith("_internal/")}
    elif missing.startswith("licenses/"):
        files = {k: v for k, v in files.items() if not k.startswith("licenses/")}
    else:
        files.pop(missing)

    assert kind_of(tmp_path, make_zip(files)) == "archive"


def test_two_top_level_folders_are_refused(tmp_path):
    data = raw_zip([*good_entries(), ("Another/file.txt", b"x")])

    assert kind_of(tmp_path, data) == "archive"


@pytest.mark.parametrize("junk", [b"not a zip at all", b"PK\x03\x04" + b"\0" * 50])
def test_malformed_archives_are_refused(tmp_path, junk):
    assert kind_of(tmp_path, junk) in ("archive", "size")


def test_too_many_entries_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "MAX_ENTRIES", 3)

    assert kind_of(tmp_path, make_zip(GOOD)) == "archive"


def test_a_file_larger_than_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "MAX_FILE_BYTES", 5)

    assert kind_of(tmp_path, make_zip(GOOD)) == "archive"


def test_a_total_larger_than_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "MAX_TOTAL_BYTES", 20)

    assert kind_of(tmp_path, make_zip(GOOD)) == "archive"


def test_too_little_free_disk_space_is_reported_and_nothing_is_written(tmp_path):
    kind = kind_of(tmp_path, make_zip(GOOD), free_bytes=lambda _path: 10)

    assert kind == "space" and not (tmp_path / "stage").exists()


def test_a_failure_while_extracting_removes_what_was_written(tmp_path, monkeypatch):
    data = make_zip(GOOD)
    real_open = zipfile.ZipFile.open
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", flaky)

    assert kind_of(tmp_path, data) == "filesystem"
    assert not (tmp_path / "stage").exists()


def test_staging_over_an_existing_folder_replaces_it(tmp_path):
    (tmp_path / "stage" / TOP).mkdir(parents=True)
    (tmp_path / "stage" / TOP / "stale.txt").write_text("old")

    payload = stage(tmp_path, make_zip(GOOD))

    assert not (payload / "stale.txt").exists() and (payload / "OfflineDocTools.exe").exists()


def test_required_space_covers_the_extracted_copy_and_the_installed_copy():
    assert pk.required_space(1000) > 2 * 1000
    assert pk.required_space(0) > 0
