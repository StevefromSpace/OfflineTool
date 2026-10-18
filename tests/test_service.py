"""Conversion service tests, using a fake engine instead of LibreOffice."""

import zipfile
from pathlib import Path

import pytest

from conftest import FAKE_PDF_BYTES, file_digest
from offline_doc_tools.engine import (
    ConversionFailedError,
    ConversionTimeoutError,
    InvalidInputError,
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
