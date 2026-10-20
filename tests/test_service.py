"""Conversion service tests, using a fake engine instead of LibreOffice."""

import errno
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from conftest import FAKE_PDF_BYTES, file_digest
from offline_doc_tools.engine import (
    ConversionError,
    ConversionFailedError,
    ConversionTimeoutError,
    InvalidInputError,
    TemporaryFolderError,
    os_reason,
)
from offline_doc_tools.service import (
    ConversionService,
    FileStatus,
    place_output,
    validate_output_dir,
    validate_source,
    verify_pdf,
)


class FakeConverter:
    """Writes a PDF into out_dir, or raises for documents whose text contains a marker."""

    def __init__(self, failures=None, pdf_bytes=FAKE_PDF_BYTES):
        self.failures = failures or {}
        self.pdf_bytes = pdf_bytes
        self.seen: list[Path] = []

    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        self.seen.append(source)
        with zipfile.ZipFile(source) as archive:
            text = archive.read("word/document.xml").decode("utf-8")
        for marker, exc in self.failures.items():
            if marker in text:
                raise exc
        pdf = out_dir / f"{source.stem}.pdf"
        pdf.write_bytes(self.pdf_bytes)
        return pdf


# --- validate_source -------------------------------------------------------


def test_valid_docx_passes(make_docx):
    validate_source(make_docx("ok.docx"))


def test_uppercase_extension_is_accepted(make_docx):
    validate_source(make_docx("LOUD.DOCX"))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(InvalidInputError, match="does not exist"):
        validate_source(tmp_path / "missing.docx")


def test_directory_rejected(tmp_path):
    folder = tmp_path / "folder.docx"
    folder.mkdir()
    with pytest.raises(InvalidInputError, match="not a file"):
        validate_source(folder)


@pytest.mark.parametrize("name", ["notes.txt", "old.doc", "sheet.xlsx", "noext"])
def test_unsupported_extension_rejected(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"data")
    with pytest.raises(InvalidInputError, match="Unsupported file type"):
        validate_source(path)


def test_empty_file_rejected(tmp_path):
    path = tmp_path / "empty.docx"
    path.write_bytes(b"")
    with pytest.raises(InvalidInputError, match="empty"):
        validate_source(path)


def test_non_zip_docx_rejected(tmp_path):
    path = tmp_path / "fake.docx"
    path.write_bytes(b"this is plain text, not a Word document")
    with pytest.raises(InvalidInputError, match="not a valid Word"):
        validate_source(path)


def test_word_lock_file_rejected(tmp_path):
    path = tmp_path / "~$report.docx"
    path.write_bytes(b"\x05owner")
    with pytest.raises(InvalidInputError, match="temporary lock file"):
        validate_source(path)


# --- validate_output_dir ---------------------------------------------------


def test_output_dir_must_exist(tmp_path):
    with pytest.raises(InvalidInputError, match="does not exist"):
        validate_output_dir(tmp_path / "nope")


def test_output_dir_must_be_directory(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("x")
    with pytest.raises(InvalidInputError, match="not a folder"):
        validate_output_dir(path)


# --- verify_pdf ------------------------------------------------------------


def test_verify_pdf_accepts_pdf(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(FAKE_PDF_BYTES)
    verify_pdf(pdf)


def test_verify_pdf_rejects_missing(tmp_path):
    with pytest.raises(ConversionFailedError, match="not created"):
        verify_pdf(tmp_path / "missing.pdf")


def test_verify_pdf_rejects_empty(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"")
    with pytest.raises(ConversionFailedError, match="empty"):
        verify_pdf(pdf)


def test_verify_pdf_rejects_non_pdf(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"<html>not a pdf</html>")
    with pytest.raises(ConversionFailedError, match="not a valid PDF"):
        verify_pdf(pdf)


# --- place_output ----------------------------------------------------------


def _staged_pdf(tmp_path: Path) -> Path:
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(FAKE_PDF_BYTES)
    return staged


def test_place_output_uses_original_stem(tmp_path, output_dir):
    target = place_output(_staged_pdf(tmp_path), output_dir, "Quarterly report")
    assert target == output_dir / "Quarterly report.pdf"
    assert target.read_bytes() == FAKE_PDF_BYTES


def test_place_output_never_overwrites(tmp_path, output_dir):
    existing = output_dir / "report.pdf"
    existing.write_bytes(b"precious existing file")
    (output_dir / "report (1).pdf").write_bytes(b"also precious")

    target = place_output(_staged_pdf(tmp_path), output_dir, "report")

    assert target == output_dir / "report (2).pdf"
    assert existing.read_bytes() == b"precious existing file"
    assert (output_dir / "report (1).pdf").read_bytes() == b"also precious"
    assert target.read_bytes() == FAKE_PDF_BYTES


def test_place_output_differing_case_never_clobbers(tmp_path, output_dir):
    # On case-insensitive filesystems (Windows) "REPORT.pdf" and "report.pdf" are the same file.
    (output_dir / "report.pdf").write_bytes(b"existing")
    target = place_output(_staged_pdf(tmp_path), output_dir, "REPORT")
    assert (output_dir / "report.pdf").read_bytes() == b"existing"
    assert target.read_bytes() == FAKE_PDF_BYTES


# --- ConversionService -----------------------------------------------------


def test_successful_conversion_creates_nonempty_pdf(make_docx, output_dir):
    source = make_docx("letter.docx")
    summary = ConversionService(FakeConverter()).convert_batch([source], output_dir)

    [result] = summary.results
    assert result.status is FileStatus.SUCCEEDED
    assert result.output == output_dir / "letter.pdf"
    assert result.output.stat().st_size > 0
    assert result.renamed is False
    assert summary.succeeded == 1 and summary.failed == 0


def test_unicode_and_spaces_in_names(make_docx, output_dir):
    source = make_docx("Résumé – Zoë 履歴書 v2.docx")
    summary = ConversionService(FakeConverter()).convert_batch([source], output_dir)
    assert summary.results[0].output == output_dir / "Résumé – Zoë 履歴書 v2.pdf"
    assert summary.results[0].output.is_file()


def test_engine_receives_private_copy_not_original(make_docx, output_dir):
    source = make_docx("orig.docx")
    converter = FakeConverter()
    ConversionService(converter).convert_batch([source], output_dir)

    [seen] = converter.seen
    assert seen != source
    assert seen.parent != source.parent
    assert seen.name == "source.docx"


def test_temporary_workspace_is_removed(make_docx, output_dir):
    converter = FakeConverter()
    ConversionService(converter).convert_batch([make_docx("a.docx")], output_dir)
    assert not converter.seen[0].exists()


def test_existing_output_is_renamed_not_overwritten(make_docx, output_dir):
    (output_dir / "memo.pdf").write_bytes(b"keep me")
    summary = ConversionService(FakeConverter()).convert_batch([make_docx("memo.docx")], output_dir)

    result = summary.results[0]
    assert result.output == output_dir / "memo (1).pdf"
    assert result.renamed is True
    assert (output_dir / "memo.pdf").read_bytes() == b"keep me"


def test_two_sources_with_same_name_both_kept(tmp_path, output_dir):
    from conftest import write_docx

    a = write_docx(tmp_path / "a" / "same.docx", "first")
    b = write_docx(tmp_path / "b" / "same.docx", "second")
    summary = ConversionService(FakeConverter()).convert_batch([a, b], output_dir)

    outputs = sorted(r.output.name for r in summary.results)
    assert outputs == ["same (1).pdf", "same.pdf"]


def test_source_files_are_preserved(make_docx, output_dir):
    good = make_docx("good.docx")
    bad = make_docx("bad.docx", text="FAIL-ME")
    before = {p: file_digest(p) for p in (good, bad)}
    listing_before = sorted(p.name for p in good.parent.iterdir())

    converter = FakeConverter(failures={"FAIL-ME": ConversionFailedError("boom")})
    summary = ConversionService(converter).convert_batch([good, bad], output_dir)
    assert [r.status for r in summary.results] == [FileStatus.SUCCEEDED, FileStatus.FAILED]

    assert {p: file_digest(p) for p in (good, bad)} == before
    assert sorted(p.name for p in good.parent.iterdir()) == listing_before


def test_batch_with_mixed_results(make_docx, tmp_path, output_dir):
    good1 = make_docx("one.docx")
    timeout = make_docx("slow.docx", text="SLOW")
    good2 = make_docx("two.docx")
    invalid = tmp_path / "in" / "notes.txt"
    invalid.write_text("plain")
    broken = make_docx("broken.docx", text="BROKEN")

    converter = FakeConverter(
        failures={
            "SLOW": ConversionTimeoutError("Conversion did not finish within 120 seconds."),
            "BROKEN": ConversionFailedError("LibreOffice exited with code 1."),
        }
    )
    summary = ConversionService(converter).convert_batch(
        [good1, timeout, good2, invalid, broken], output_dir
    )

    statuses = [r.status for r in summary.results]
    assert statuses == [
        FileStatus.SUCCEEDED,
        FileStatus.FAILED,
        FileStatus.SUCCEEDED,
        FileStatus.FAILED,
        FileStatus.FAILED,
    ]
    assert summary.succeeded == 2
    assert summary.failed == 3
    assert "120 seconds" in summary.results[1].message
    assert "Unsupported file type" in summary.results[3].message
    assert sorted(p.name for p in output_dir.iterdir()) == ["one.pdf", "two.pdf"]


def test_invalid_engine_output_is_reported_and_not_placed(make_docx, output_dir):
    converter = FakeConverter(pdf_bytes=b"")
    summary = ConversionService(converter).convert_batch([make_docx("x.docx")], output_dir)
    assert summary.results[0].status is FileStatus.FAILED
    assert "empty" in summary.results[0].message
    assert list(output_dir.iterdir()) == []


def test_unexpected_engine_exception_is_reported_not_swallowed(make_docx, output_dir):
    converter = FakeConverter(failures={"CRASH": RuntimeError("weird internal state")})
    summary = ConversionService(converter).convert_batch(
        [make_docx("c.docx", text="CRASH"), make_docx("d.docx")], output_dir
    )
    assert summary.results[0].status is FileStatus.FAILED
    assert "Unexpected error" in summary.results[0].message
    assert summary.results[1].status is FileStatus.SUCCEEDED


def test_invalid_output_dir_raises_before_converting(make_docx, tmp_path):
    converter = FakeConverter()
    with pytest.raises(InvalidInputError):
        ConversionService(converter).convert_batch([make_docx("a.docx")], tmp_path / "missing")
    assert converter.seen == []


def test_progress_callbacks(make_docx, output_dir):
    sources = [make_docx("a.docx"), make_docx("b.docx")]
    events = []
    ConversionService(FakeConverter()).convert_batch(
        sources,
        output_dir,
        on_file_started=lambda i, total, src: events.append(("start", i, total, src.name)),
        on_file_finished=lambda i, total, res: events.append(("done", i, total, res.status)),
    )
    assert events == [
        ("start", 0, 2, "a.docx"),
        ("done", 0, 2, FileStatus.SUCCEEDED),
        ("start", 1, 2, "b.docx"),
        ("done", 1, 2, FileStatus.SUCCEEDED),
    ]


def test_cancel_stops_before_next_file(make_docx, output_dir):
    sources = [make_docx("a.docx"), make_docx("b.docx"), make_docx("c.docx")]
    done = []
    summary = ConversionService(FakeConverter()).convert_batch(
        sources,
        output_dir,
        on_file_finished=lambda i, total, res: done.append(i),
        should_cancel=lambda: len(done) >= 1,
    )
    assert summary.cancelled is True
    assert len(summary.results) == 1
    assert summary.not_processed == (sources[1], sources[2])


# --- failure branches and cleanup ------------------------------------------


def test_reserved_windows_device_name_gets_safe_output_name(tmp_path, output_dir):
    target = place_output(_staged_pdf(tmp_path), output_dir, "con")
    assert target.name == "con_.pdf"
    assert target.read_bytes() == FAKE_PDF_BYTES


def test_place_output_removes_partial_file_on_copy_error(tmp_path, output_dir, monkeypatch):
    existing = output_dir / "doc.pdf"
    existing.write_bytes(b"precious")

    def broken_copy(src, dst):
        dst.write(b"%PDF-partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfileobj", broken_copy)
    with pytest.raises(ConversionFailedError, match="Could not write the PDF"):
        place_output(_staged_pdf(tmp_path), output_dir, "doc")

    assert sorted(p.name for p in output_dir.iterdir()) == ["doc.pdf"]
    assert existing.read_bytes() == b"precious"


def test_disk_full_is_reported_clearly_in_a_batch(make_docx, output_dir, monkeypatch):
    def broken_copy(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfileobj", broken_copy)
    summary = ConversionService(FakeConverter()).convert_batch([make_docx("a.docx")], output_dir)
    message = summary.results[0].message
    assert "Could not write the PDF to the output folder" in message
    assert "No space left on device" in message
    assert "Unexpected error" not in message


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("CON.notes", "CON_.notes.pdf"),
        ("conin$", "conin$_.pdf"),
        ("LPT0", "LPT0_.pdf"),
        ("aux ", "aux _.pdf"),
        ("console", "console.pdf"),
    ],
)
def test_reserved_device_name_variants(tmp_path, output_dir, stem, expected):
    target = place_output(_staged_pdf(tmp_path), output_dir, stem)
    assert target.name == expected


def test_output_dir_deleted_mid_batch_fails_remaining_files_cleanly(make_docx, tmp_path):
    output_dir = tmp_path / "vanishing"
    output_dir.mkdir()

    def remove_output_dir(index, total, result):
        if index == 0:
            for child in output_dir.iterdir():
                child.unlink()
            output_dir.rmdir()

    summary = ConversionService(FakeConverter()).convert_batch(
        [make_docx("a.docx"), make_docx("b.docx")], output_dir, on_file_finished=remove_output_dir
    )
    assert summary.results[0].status is FileStatus.SUCCEEDED
    assert summary.results[1].status is FileStatus.FAILED
    assert "Could not write to the output folder" in summary.results[1].message


def test_unreadable_source_is_rejected(make_docx, output_dir, monkeypatch):
    source = make_docx("locked.docx")

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfile", deny)
    summary = ConversionService(FakeConverter()).convert_batch([source], output_dir)
    assert summary.results[0].status is FileStatus.FAILED
    assert "cannot be read" in summary.results[0].message
    assert list(output_dir.iterdir()) == []


def test_each_job_folder_is_removed_before_the_next_file(make_docx, output_dir):
    converter = FakeConverter()
    remaining = []
    ConversionService(converter).convert_batch(
        [make_docx("a.docx"), make_docx("b.docx")],
        output_dir,
        on_file_finished=lambda i, n, res: remaining.append(converter.seen[i].parent.exists()),
    )
    assert remaining == [False, False]


def test_logs_contain_no_file_names_or_document_text(make_docx, output_dir, caplog):
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    good = make_docx("Zebra-Confidential-Name.docx", text="TOPSECRET-CONTENT")
    bad = make_docx("Okapi-Private-Name.docx", text="TOPSECRET-FAIL")
    converter = FakeConverter(failures={"TOPSECRET-FAIL": ConversionFailedError("boom")})

    ConversionService(converter).convert_batch([good, bad], output_dir)

    assert caplog.records, "expected the batch to be logged"
    for secret in ("Zebra-Confidential", "Okapi-Private", "TOPSECRET", str(good.parent)):
        assert secret not in caplog.text


# --- temporary folder problems: reported as such, never as raw OS errors ----------------

_USER = "Secret Person"  # stands in for the Windows user name inside a path


def _missing_folder_reason(folder: Path) -> str:
    """How this operating system words 'that folder does not exist'."""
    try:
        (folder / "probe").mkdir()
    except OSError as exc:
        return exc.strerror
    raise AssertionError("the folder unexpectedly exists")


def _assert_path_free(text: str, tmp_path: Path) -> None:
    assert _USER not in text
    assert str(tmp_path) not in text


def _single_result(make_docx, output_dir):
    summary = ConversionService(FakeConverter()).convert_batch([make_docx("a.docx")], output_dir)
    [result] = summary.results
    return result


def test_missing_temp_folder_is_reported_before_anything_is_converted(
    make_docx, output_dir, tmp_path, monkeypatch, caplog
):
    missing = tmp_path / _USER / "gone"
    reason = _missing_folder_reason(missing)
    monkeypatch.setattr(tempfile, "tempdir", str(missing))
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    converter, started = FakeConverter(), []

    with pytest.raises(ConversionError) as info:
        ConversionService(converter).convert_batch(
            [make_docx("a.docx")], output_dir, on_file_started=lambda *args: started.append(args)
        )

    message = str(info.value)
    assert not isinstance(info.value, OSError)
    assert "temporary folder" in message
    assert reason in message
    assert "Unexpected error" not in message
    assert converter.seen == [] and started == []
    assert caplog.records, "expected the problem to be logged"
    _assert_path_free(message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_unusable_default_temp_folders_are_reported_without_listing_them(
    make_docx, output_dir, tmp_path, monkeypatch, caplog
):
    # With no TEMP/TMP that works, Python raises "No usable temporary directory found in
    # [<every folder it tried>]": that text contains the user's profile path.
    candidates = [str(tmp_path / _USER / "a"), str(tmp_path / _USER / "b")]
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", lambda: candidates)
    caplog.set_level("DEBUG", logger="offline_doc_tools")

    with pytest.raises(ConversionError) as info:
        ConversionService(FakeConverter()).convert_batch([make_docx("a.docx")], output_dir)

    assert "temporary folder" in str(info.value)
    assert "No usable temporary directory found" in str(info.value)
    _assert_path_free(str(info.value), tmp_path)
    _assert_path_free(caplog.text, tmp_path)


@pytest.mark.parametrize(
    ("make_error", "reason"),
    [
        (
            lambda: PermissionError(
                errno.EACCES, "Permission denied", rf"C:\Users\{_USER}\AppData\Local\Temp\x"
            ),
            "Permission denied",
        ),
        (
            lambda: OSError(
                errno.ENOSPC, "No space left on device", rf"C:\Users\{_USER}\AppData\Local\Temp"
            ),
            "No space left on device",
        ),
    ],
    ids=["unwritable", "disk-full"],
)
def test_workspace_that_cannot_be_created_is_reported_clearly(
    make_docx, output_dir, tmp_path, monkeypatch, caplog, make_error, reason
):
    def refuse(*args, **kwargs):
        raise make_error()

    monkeypatch.setattr(tempfile, "mkdtemp", refuse)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    converter = FakeConverter()

    with pytest.raises(ConversionError) as info:
        ConversionService(converter).convert_batch([make_docx("a.docx")], output_dir)

    message = str(info.value)
    assert "temporary folder" in message and reason in message
    assert converter.seen == []
    _assert_path_free(message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_temp_folder_problems_have_their_own_error_type(
    make_docx, output_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "gone"))
    with pytest.raises(TemporaryFolderError) as info:
        ConversionService(FakeConverter()).convert_batch([make_docx("a.docx")], output_dir)
    assert isinstance(info.value, ConversionError)


def test_job_folder_failure_is_reported_as_a_temp_folder_problem(
    make_docx, output_dir, tmp_path, monkeypatch, caplog
):
    real_mkdir = Path.mkdir
    failed = []

    def full_disk_once(self, *args, **kwargs):
        if self.name == "out" and self.parent.name.startswith("job-") and not failed:
            failed.append(self)
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", full_disk_once)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    summary = ConversionService(FakeConverter()).convert_batch(
        [make_docx("a.docx"), make_docx("b.docx")], output_dir
    )

    first, second = summary.results
    assert first.status is FileStatus.FAILED
    assert "temporary folder" in first.message
    assert "No space left on device" in first.message
    assert "Unexpected error" not in first.message
    _assert_path_free(first.message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)
    assert second.status is FileStatus.SUCCEEDED  # the batch carries on
    assert not any("Unexpected conversion error" in r.getMessage() for r in caplog.records)


def _copy_error(kind: str, src: Path, dst: Path) -> OSError:
    locked = "The process cannot access the file because it is being used by another process"
    return {
        "disk-full": OSError(errno.ENOSPC, "No space left on device"),
        "destination-denied": PermissionError(errno.EACCES, "Permission denied", os.fspath(dst)),
        "destination-folder-gone": FileNotFoundError(
            errno.ENOENT, "The system cannot find the path specified", os.fspath(dst)
        ),
        "source-locked": PermissionError(errno.EACCES, locked, os.fspath(src)),
        "source-gone": FileNotFoundError(errno.ENOENT, "No such file", os.fspath(src)),
        "source-unnamed": PermissionError(errno.EACCES, "Permission denied"),
    }[kind]


@pytest.mark.parametrize("kind", ["disk-full", "destination-denied", "destination-folder-gone"])
def test_failure_writing_the_private_copy_blames_the_temp_folder(
    make_docx, output_dir, tmp_path, monkeypatch, caplog, kind
):
    def failing_copy(src, dst, **kwargs):
        raise _copy_error(kind, Path(src), Path(dst))

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfile", failing_copy)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    result = _single_result(make_docx, output_dir)

    assert result.status is FileStatus.FAILED
    assert "temporary folder" in result.message
    assert "cannot be read" not in result.message
    assert "Unexpected error" not in result.message
    _assert_path_free(result.message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)
    assert list(output_dir.iterdir()) == []


@pytest.mark.parametrize("kind", ["source-locked", "source-gone", "source-unnamed"])
def test_failure_reading_the_source_still_blames_the_source(
    make_docx, output_dir, tmp_path, monkeypatch, caplog, kind
):
    def failing_copy(src, dst, **kwargs):
        raise _copy_error(kind, Path(src), Path(dst))

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfile", failing_copy)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    result = _single_result(make_docx, output_dir)

    assert result.status is FileStatus.FAILED
    assert "The file cannot be read" in result.message
    assert "temporary folder" not in result.message
    _assert_path_free(result.message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_unreadable_converted_pdf_is_a_temp_folder_problem(tmp_path, monkeypatch):
    pdf = tmp_path / "result.pdf"
    pdf.write_bytes(FAKE_PDF_BYTES)
    real_open = Path.open

    def locked(self, mode="r", *args, **kwargs):
        if self == pdf:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    with pytest.raises(ConversionError) as info:
        verify_pdf(pdf)
    message = str(info.value)
    assert "temporary folder" in message and "Permission denied" in message
    _assert_path_free(message, tmp_path)


def test_unreadable_staged_pdf_is_a_temp_folder_problem_and_creates_no_output(
    tmp_path, output_dir, monkeypatch
):
    staged = _staged_pdf(tmp_path)
    real_open = Path.open

    def locked(self, mode="r", *args, **kwargs):
        if self == staged:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked)
    with pytest.raises(ConversionError) as info:
        place_output(staged, output_dir, "doc")

    message = str(info.value)
    assert "temporary folder" in message and "Permission denied" in message
    assert "output folder" not in message
    _assert_path_free(message, tmp_path)
    assert list(output_dir.iterdir()) == []


def test_unexpected_os_errors_are_reported_without_their_paths(make_docx, output_dir):
    error = PermissionError(errno.EACCES, "Permission denied", rf"C:\Users\{_USER}\x.pdf")
    converter = FakeConverter(failures={"CRASH": error})
    summary = ConversionService(converter).convert_batch(
        [make_docx("c.docx", text="CRASH")], output_dir
    )
    assert summary.results[0].status is FileStatus.FAILED
    assert "Permission denied" in summary.results[0].message
    assert _USER not in summary.results[0].message


# --- os_reason / TemporaryFolderError ---------------------------------------------------


def test_os_reason_uses_the_operating_systems_wording():
    exc = OSError(errno.ENOSPC, "No space left on device", rf"C:\Users\{_USER}\x")
    assert os_reason(exc) == "No space left on device"


def test_os_reason_falls_back_to_the_error_number_and_never_says_none():
    assert os_reason(OSError(errno.EACCES, None)) == os.strerror(errno.EACCES)
    for opaque in (OSError("opaque failure"), FileNotFoundError("Directory does not exist: x")):
        reason = os_reason(opaque)
        assert reason and "None" not in reason


def test_os_reason_drops_the_folders_listed_by_python_when_no_temp_folder_works():
    listing = rf"No usable temporary directory found in ['C:\Users\{_USER}\Temp', 'C:\TEMP']"
    reason = os_reason(FileNotFoundError(errno.ENOENT, listing))
    assert reason == "No usable temporary directory found"


@pytest.mark.parametrize(
    "text",
    [
        rf"cannot open C:\Users\{_USER}\x",
        f"cannot open C:/Users/{_USER}/x",
        f"//{_USER}/share unreachable",
    ],
)
def test_os_reason_never_passes_a_path_through(text):
    reason = os_reason(OSError(errno.ENOENT, text))
    assert _USER not in reason
    assert reason == os.strerror(errno.ENOENT)


def test_temporary_folder_error_message():
    error = TemporaryFolderError.from_os_error(
        "copy the document into",
        OSError(errno.ENOSPC, "No space left on device", rf"C:\Users\{_USER}\Temp\x"),
    )
    assert isinstance(error, ConversionFailedError)
    message = str(error)
    assert message.startswith(
        "Could not copy the document into the temporary folder (No space left on device)."
    )
    assert "free space" in message
    assert _USER not in message


# --- logs carry the error category, never text derived from the user's files -------------


def test_expected_failures_log_their_category_but_no_file_name_or_suffix(
    tmp_path, output_dir, caplog
):
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    folder = tmp_path / "Heron-Private-Folder"
    folder.mkdir()
    odd_suffix = folder / "Zebra-Private.qwxyz9"
    no_extension = folder / "Okapi-Private"
    lock_file = folder / "~$Ibis-Private.docx"
    missing = folder / "Lynx-Private.docx"
    for path in (odd_suffix, no_extension):
        path.write_bytes(b"data")
    lock_file.write_bytes(b"\x05owner")

    summary = ConversionService(FakeConverter()).convert_batch(
        [odd_suffix, no_extension, lock_file, missing], output_dir
    )

    # The person using the app still gets the full explanation ...
    messages = [r.message for r in summary.results]
    assert all(r.status is FileStatus.FAILED for r in summary.results)
    assert "Unsupported file type '.qwxyz9'" in messages[0]
    assert "(no extension)" in messages[1]
    assert "temporary lock file" in messages[2]
    assert "does not exist" in messages[3]
    # ... but the log only learns which kind of problem it was.
    assert "InvalidInputError" in caplog.text
    for secret in ("Zebra", "Okapi", "Ibis", "Lynx", "Private", "qwxyz9", str(tmp_path)):
        assert secret not in caplog.text


def test_engine_failure_messages_are_not_copied_into_the_log(make_docx, output_dir, caplog):
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    converter = FakeConverter(
        failures={
            "FAIL-A": ConversionFailedError("LibreOffice said Heron-Private.odt is broken"),
            "FAIL-B": ConversionTimeoutError("Okapi-Private did not finish"),
        }
    )
    summary = ConversionService(converter).convert_batch(
        [make_docx("a.docx", text="FAIL-A"), make_docx("b.docx", text="FAIL-B")], output_dir
    )
    assert "Heron-Private" in summary.results[0].message  # shown to the user ...
    assert "ConversionFailedError" in caplog.text
    assert "ConversionTimeoutError" in caplog.text
    assert "Private" not in caplog.text  # ... but not written to the log


# --- output-side file system errors: clear, actionable and free of paths ----------------


def _fail_creating_output(monkeypatch, make_error):
    """Make the exclusive create of the output PDF fail with ``make_error(path)``."""
    real_open = Path.open

    def failing_open(self, mode="r", *args, **kwargs):
        if mode == "xb":
            raise make_error(self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)


@pytest.mark.parametrize(
    ("make_error", "expected", "unwanted"),
    [
        (
            lambda p: PermissionError(errno.EACCES, "Permission denied", str(p)),
            ["Permission denied"],
            [],
        ),
        (
            lambda p: OSError(errno.ENOSPC, "No space left on device", str(p)),
            ["No space left on device"],
            [],
        ),
        # Windows reports a name over 255 characters as plain EINVAL.
        (
            lambda p: OSError(errno.EINVAL, "Invalid argument", str(p)),
            ["too long"],
            ["Invalid argument"],
        ),
        (
            lambda p: OSError(errno.ENAMETOOLONG, "File name too long", str(p)),
            ["too long"],
            [],
        ),
        # Without long-path support Windows reports an over-long path as "not found" although
        # the folder is still there.
        (
            lambda p: FileNotFoundError(errno.ENOENT, "No such file or directory", str(p)),
            ["No such file or directory", "too long"],
            [],
        ),
        (lambda p: OSError("opaque failure"), [], ["None"]),
    ],
    ids=[
        "permission-denied",
        "disk-full",
        "invalid-argument",
        "name-too-long",
        "path-too-long-reported-as-not-found",
        "no-reason-given",
    ],
)
def test_output_folder_errors_are_clear_and_path_free(
    make_docx, output_dir, tmp_path, monkeypatch, caplog, make_error, expected, unwanted
):
    _fail_creating_output(monkeypatch, make_error)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    result = _single_result(make_docx, output_dir)

    assert result.status is FileStatus.FAILED
    assert result.message.startswith("Could not write to the output folder")
    for text in expected:
        assert text in result.message
    for text in unwanted:
        assert text not in result.message
    assert "Unexpected error" not in result.message
    _assert_path_free(result.message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="winerror only exists on Windows")
def test_windows_filename_too_long_error_is_explained(make_docx, output_dir, monkeypatch):
    def too_long(path):
        return OSError(errno.ENOENT, "The filename or extension is too long", str(path), 206)

    _fail_creating_output(monkeypatch, too_long)
    result = _single_result(make_docx, output_dir)
    assert "too long" in result.message
    assert "Unexpected error" not in result.message


def test_output_folder_removed_mid_batch_says_so_without_naming_it(make_docx, tmp_path, caplog):
    output_dir = tmp_path / _USER / "vanishing"
    output_dir.mkdir(parents=True)

    def remove_output_dir(index, total, result):
        if index == 0:
            for child in output_dir.iterdir():
                child.unlink()
            output_dir.rmdir()

    caplog.set_level("DEBUG", logger="offline_doc_tools")
    summary = ConversionService(FakeConverter()).convert_batch(
        [make_docx("a.docx"), make_docx("b.docx")], output_dir, on_file_finished=remove_output_dir
    )

    message = summary.results[1].message
    assert message.startswith("Could not write to the output folder")
    assert "moved or deleted" in message
    _assert_path_free(message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_failed_cleanup_of_a_partial_output_does_not_hide_the_real_problem(
    make_docx, output_dir, tmp_path, monkeypatch, caplog
):
    def broken_copy(src, dst):
        dst.write(b"%PDF-partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    def locked_unlink(self, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfileobj", broken_copy)
    monkeypatch.setattr(Path, "unlink", locked_unlink)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    result = _single_result(make_docx, output_dir)

    assert result.status is FileStatus.FAILED
    assert "Could not write the PDF to the output folder" in result.message
    assert "No space left on device" in result.message
    assert "incomplete" in result.message  # the person is told a partial file may remain
    assert "Unexpected error" not in result.message
    _assert_path_free(result.message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_a_stat_error_on_a_candidate_name_does_not_stop_the_output(
    tmp_path, output_dir, monkeypatch
):
    # Before Python 3.13, Path.exists() raises for errors other than "not found".
    real_exists = Path.exists

    def denied(self, *args, **kwargs):
        if self.parent == output_dir:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", denied)
    target = place_output(_staged_pdf(tmp_path), output_dir, "doc")
    assert target == output_dir / "doc.pdf"
    assert target.read_bytes() == FAKE_PDF_BYTES
