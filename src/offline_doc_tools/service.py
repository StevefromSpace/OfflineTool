"""Engine-agnostic conversion workflow: validate, convert a private copy, verify, place safely."""

from __future__ import annotations

import enum
import errno
import logging
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .docx_inspect import scan_linked_images
from .docx_sanitize import strip_external_references
from .engine import (
    ConversionError,
    ConversionFailedError,
    DocumentConverter,
    InvalidInputError,
    TemporaryFolderError,
    os_reason,
)

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = frozenset({".docx"})
_MAX_NAME_ATTEMPTS = 10_000
# A write that fails for lack of space (or quota) failed on the destination, not the source.
_OUT_OF_SPACE_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})
# A file name or path that is too long, or not allowed: Windows reports a name over 255
# characters as plain EINVAL; the winerror values are "invalid name", "bad path name" and
# "file name exceeds range".
_BAD_NAME_ERRNOS = frozenset({errno.EINVAL, errno.ENAMETOOLONG})
_BAD_NAME_WINERRORS = frozenset({123, 161, 206})
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
    linked_images_omitted: int = 0


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
            f"The file cannot be read ({os_reason(exc)}). It may be open in another program "
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
    try:
        with path.open("rb") as handle:
            header = handle.read(5)
    except OSError as exc:
        raise TemporaryFolderError.from_os_error("read the converted PDF from", exc) from exc
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
    # Opened first, so that a staged PDF we cannot read is reported as a temporary folder
    # problem rather than as a failure to write to the output folder, and creates no file.
    try:
        staged = staged_pdf.open("rb")
    except OSError as exc:
        raise TemporaryFolderError.from_os_error("read the converted PDF from", exc) from exc
    with staged:
        for attempt in range(_MAX_NAME_ATTEMPTS):
            name = f"{stem}.pdf" if attempt == 0 else f"{stem} ({attempt}).pdf"
            target = output_dir / name
            if _is_taken(target):
                continue
            try:
                destination = target.open("xb")
            except FileExistsError:
                continue
            except OSError as exc:
                problem = _output_problem(exc, output_dir)
                raise ConversionFailedError(
                    f"Could not write to the output folder ({problem})."
                ) from exc
            try:
                with destination:
                    shutil.copyfileobj(staged, destination)
            except BaseException as exc:
                # Only ever removes the file this function itself just created.
                removed = _remove_incomplete_output(target)
                if isinstance(exc, OSError):
                    problem = _output_problem(exc, output_dir)
                    left_behind = "" if removed else " An incomplete file may remain there."
                    raise ConversionFailedError(
                        f"Could not write the PDF to the output folder ({problem}).{left_behind}"
                    ) from exc
                raise
            return target
    raise ConversionFailedError("Could not find a free file name in the output folder.")


def _is_taken(target: Path) -> bool:
    try:
        return target.exists()
    except OSError:
        # Older Python versions raise for some errors instead of answering "no". Not being
        # able to tell is no reason to stop: the exclusive create that follows decides, and
        # reports what is really wrong.
        return False


def _is_folder(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:  # as in _is_taken
        return False


def _remove_incomplete_output(target: Path) -> bool:
    """Delete the incomplete file ``place_output`` itself created. False if that failed."""
    try:
        target.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not remove an incomplete output file")
        return False
    return True


def _output_problem(exc: OSError, output_dir: Path) -> str:
    """Why writing into the output folder failed: plain words, no path."""
    if exc.errno in _BAD_NAME_ERRNOS or getattr(exc, "winerror", None) in _BAD_NAME_WINERRORS:
        return "the file name or folder path is too long or not allowed"
    reason = os_reason(exc)
    if exc.errno == errno.ENOENT:
        if _is_folder(output_dir):
            # Without long-path support Windows reports a path that is too long as "not found".
            return f"{reason}; the file name or folder path may be too long"
        return f"{reason}; the folder may have been moved or deleted"
    return reason


def remove_temporary_tree(path: Path, description: str) -> None:
    """Delete a temporary directory; log (without the path) if anything is left behind."""
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        log.warning(
            "Could not fully remove a temporary %s; it may still contain document data. "
            "It is inside the system temporary folder and can be deleted manually.",
            description,
        )


def _copy_to_private(source: Path, private_copy: Path) -> None:
    """Copy ``source`` to ``private_copy``, telling a source that cannot be read from a
    temporary folder that cannot be written."""
    try:
        shutil.copyfile(source, private_copy)
    except OSError as exc:
        if exc.errno in _OUT_OF_SPACE_ERRNOS or _names(exc, private_copy):
            raise TemporaryFolderError.from_os_error("copy the document into", exc) from exc
        raise InvalidInputError(f"The file cannot be read ({os_reason(exc)}).") from exc


def _names(exc: OSError, path: Path) -> bool:
    """Whether ``exc`` names ``path`` as the file it failed on."""
    for name in (exc.filename, exc.filename2):
        try:
            if name is not None and Path(os.fsdecode(name)) == path:
                return True
        except (TypeError, ValueError):  # not a path, e.g. a file descriptor number
            continue
    return False


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

        Raises ``InvalidInputError`` if the output folder itself is unusable, and
        ``TemporaryFolderError`` if no workspace can be created in the temporary folder. Both
        are raised before any file is converted.
        """
        validate_output_dir(output_dir)
        sources = list(sources)
        total = len(sources)
        results: list[FileResult] = []
        log.info("Batch started: %d file(s)", total)

        try:
            workspace = Path(tempfile.mkdtemp(prefix="offline-doc-tools-"))
        except OSError as exc:
            raise TemporaryFolderError.from_os_error("create a workspace in", exc) from exc
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
            try:
                out_dir.mkdir(parents=True)
            except OSError as exc:
                raise TemporaryFolderError.from_os_error(
                    "create a workspace for this document in", exc
                ) from exc
            # The engine only ever sees a private copy with a plain ASCII name, so the
            # original cannot be modified or locked, and its name never reaches the engine.
            private_copy = job_dir / "source.docx"
            _copy_to_private(source, private_copy)
            # External references are stripped from the copy before the engine sees it: the
            # LibreOffice profile setting alone does not stop every linked image from being
            # fetched or embedded.
            # Counted first: afterwards the links are gone. Advisory only; never raises.
            linked_images_omitted = scan_linked_images(private_copy).omitted
            strip_external_references(private_copy)

            pdf = self._converter.convert_to_pdf(private_copy, out_dir)
            verify_pdf(pdf)
            target = place_output(pdf, output_dir, source.stem)
        except ConversionError as exc:
            # Only the category is logged: the message can contain the file's suffix or text
            # from the engine, and both are derived from the user's files.
            log.info("Conversion failed: %s", type(exc).__name__)
            return FileResult(source, FileStatus.FAILED, str(exc))
        except Exception as exc:
            log.error("Unexpected conversion error", exc_info=True)
            # An OSError's own text ends in the file name; keep the person's screen path-free too.
            detail = os_reason(exc) if isinstance(exc, OSError) else str(exc)
            return FileResult(source, FileStatus.FAILED, f"Unexpected error: {detail}")

        renamed = target.name != f"{source.stem}.pdf"
        message = f"Saved as {target.name} (name was already taken)" if renamed else ""
        return FileResult(
            source,
            FileStatus.SUCCEEDED,
            message,
            output=target,
            renamed=renamed,
            linked_images_omitted=linked_images_omitted,
        )
