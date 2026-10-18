"""Engine-agnostic conversion workflow: validate, convert a private copy, verify, place safely."""

from __future__ import annotations

import enum
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .engine import (
    ConversionError,
    ConversionFailedError,
    DocumentConverter,
    InvalidInputError,
)

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".docx"})
_MAX_NAME_ATTEMPTS = 10_000
# Windows device names: "CON.pdf" (and on older Windows "CON.anything.pdf") may refer to the
# console device rather than a file.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in range(10)}
)


class FileStatus(enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class FileResult:
    source: Path
    status: FileStatus
    message: str = ""
    output: Path | None = None
    renamed: bool = False


@dataclass(frozen=True)
class BatchSummary:
    results: tuple[FileResult, ...]
    not_processed: tuple[Path, ...] = ()
    cancelled: bool = False

    @property
    def succeeded(self) -> int:
        return sum(r.status is FileStatus.SUCCEEDED for r in self.results)

    @property
    def failed(self) -> int:
        return sum(r.status is FileStatus.FAILED for r in self.results)


def validate_source(path: Path) -> None:
    """Raise ``InvalidInputError`` unless ``path`` looks like a readable .docx document."""
    if not path.exists():
        raise InvalidInputError("The file does not exist. It may have been moved or deleted.")
    if not path.is_file():
        raise InvalidInputError("This is not a file.")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        shown = f"'{path.suffix}'" if path.suffix else "(no extension)"
        raise InvalidInputError(
            f"Unsupported file type {shown}. Only Word .docx files are supported."
        )
    if path.name.startswith("~$"):
        raise InvalidInputError(
            "This is a temporary lock file created by Word while a document is open, "
            "not a document."
        )
    try:
        with path.open("rb") as handle:
            header = handle.read(4)
    except OSError as exc:
        raise InvalidInputError(
            f"The file cannot be read ({exc.strerror}). It may be open in another program "
            "or you may not have permission to read it."
        ) from exc
    if not header:
        raise InvalidInputError("The file is empty.")
    if not zipfile.is_zipfile(path):
        raise InvalidInputError(
            "This is not a valid Word .docx file. It may be damaged, password-protected, "
            "or an older .doc file renamed to .docx."
        )


def validate_output_dir(path: Path) -> None:
    if not path.exists():
        raise InvalidInputError("The output folder does not exist.")
    if not path.is_dir():
        raise InvalidInputError("The output location is not a folder.")


def verify_pdf(path: Path) -> None:
    """Raise ``ConversionFailedError`` unless ``path`` is a non-empty file with a PDF header."""
    if not path.is_file():
        raise ConversionFailedError("The PDF file was not created.")
    with path.open("rb") as handle:
        header = handle.read(5)
    if not header:
        raise ConversionFailedError("The PDF produced by the converter is empty.")
    if header != b"%PDF-":
        raise ConversionFailedError("The file produced by the converter is not a valid PDF.")


def place_output(staged_pdf: Path, output_dir: Path, stem: str) -> Path:
    """Copy ``staged_pdf`` to ``output_dir/<stem>.pdf`` without ever overwriting a file.

    If the name is taken, ``<stem> (1).pdf``, ``<stem> (2).pdf``, ... are tried. Exclusive
    creation makes this safe even if another program creates a file at the same moment.
    """
    first, dot, rest = stem.partition(".")
    if first.rstrip(" ").upper() in _RESERVED_NAMES:
        stem = f"{first}_{dot}{rest}"
    for attempt in range(_MAX_NAME_ATTEMPTS):
        name = f"{stem}.pdf" if attempt == 0 else f"{stem} ({attempt}).pdf"
        target = output_dir / name
        if target.exists():
            continue
        try:
            destination = target.open("xb")
        except FileExistsError:
            continue
        except OSError as exc:
            raise ConversionFailedError(
                f"Could not write to the output folder ({exc.strerror})."
            ) from exc
        try:
            with destination, staged_pdf.open("rb") as source:
                shutil.copyfileobj(source, destination)
        except BaseException as exc:
            # Only ever removes the file this function itself just created.
            target.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ConversionFailedError(
                    f"Could not write the PDF to the output folder ({exc.strerror})."
                ) from exc
            raise
        return target
    raise ConversionFailedError("Could not find a free file name in the output folder.")


def remove_temporary_tree(path: Path, description: str) -> None:
    """Delete a temporary directory; log (without the path) if anything is left behind."""
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        log.warning(
            "Could not fully remove a temporary %s; it may still contain document data. "
            "It is inside the system temporary folder and can be deleted manually.",
            description,
        )


class ConversionService:
    def __init__(self, converter: DocumentConverter) -> None:
        self._converter = converter

    def convert_batch(
        self,
        sources: Iterable[Path],
        output_dir: Path,
        *,
        on_file_started: Callable[[int, int, Path], None] | None = None,
        on_file_finished: Callable[[int, int, FileResult], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> BatchSummary:
        """Convert ``sources`` one by one. Per-file problems become FAILED results.

        Raises ``InvalidInputError`` only if the output folder itself is unusable.
        """
        validate_output_dir(output_dir)
        sources = list(sources)
        total = len(sources)
        results: list[FileResult] = []
        log.info("Batch started: %d file(s)", total)

        workspace = Path(tempfile.mkdtemp(prefix="offline-doc-tools-"))
        try:
            for index, source in enumerate(sources):
                if should_cancel is not None and should_cancel():
                    log.info("Batch cancelled after %d of %d file(s)", index, total)
                    return BatchSummary(tuple(results), tuple(sources[index:]), cancelled=True)
                if on_file_started is not None:
                    on_file_started(index, total, source)
                result = self._convert_one(source, output_dir, workspace / f"job-{index}")
                results.append(result)
                log.info("File %d/%d: %s", index + 1, total, result.status.value)
                if on_file_finished is not None:
                    on_file_finished(index, total, result)
        finally:
            remove_temporary_tree(workspace, "batch workspace")

        return BatchSummary(tuple(results))

    def _convert_one(self, source: Path, output_dir: Path, job_dir: Path) -> FileResult:
        try:
            return self._convert_in(source, output_dir, job_dir)
        finally:
            # Delete this document's private copy now rather than at the end of the batch.
            remove_temporary_tree(job_dir, "job folder")

    def _convert_in(self, source: Path, output_dir: Path, job_dir: Path) -> FileResult:
        try:
            validate_source(source)
            out_dir = job_dir / "out"
            out_dir.mkdir(parents=True)
            # The engine only ever sees a private copy with a plain ASCII name, so the
            # original cannot be modified or locked, and its name never reaches the engine.
            private_copy = job_dir / "source.docx"
            try:
                shutil.copyfile(source, private_copy)
            except OSError as exc:
                raise InvalidInputError(f"The file cannot be read ({exc.strerror}).") from exc

            pdf = self._converter.convert_to_pdf(private_copy, out_dir)
            verify_pdf(pdf)
            target = place_output(pdf, output_dir, source.stem)
        except ConversionError as exc:
            log.info("Conversion failed: %s: %s", type(exc).__name__, exc)
            return FileResult(source, FileStatus.FAILED, str(exc))
        except Exception as exc:
            log.error("Unexpected conversion error", exc_info=True)
            return FileResult(source, FileStatus.FAILED, f"Unexpected error: {exc}")

        renamed = target.name != f"{source.stem}.pdf"
        message = f"Saved as {target.name} (name was already taken)" if renamed else ""
        return FileResult(source, FileStatus.SUCCEEDED, message, output=target, renamed=renamed)
