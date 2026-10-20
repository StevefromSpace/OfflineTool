"""The contract between the conversion service and a conversion engine.

Exception messages are shown to end users, so they should be short and in plain language.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

_UNKNOWN_REASON = "unknown reason"
# CPython's tempfile appends every folder it tried to this message, and those contain the
# Windows user name.
_NO_TEMP_DIR_PREFIX = "No usable temporary directory found"
_TEMP_FOLDER_ADVICE = "Check that the temporary folder exists, is writable and has free space."


class ConversionError(Exception):
    """Base class for expected, user-reportable conversion problems."""


class InvalidInputError(ConversionError):
    """The input file or output folder cannot be used."""


class ConversionFailedError(ConversionError):
    """The conversion could not be completed: the engine failed, or what it produced (or the
    place it works in) was unusable."""


class ConversionTimeoutError(ConversionError):
    """The engine did not finish within the allowed time."""


class TemporaryFolderError(ConversionFailedError):
    """The system's temporary folder cannot be used: it is missing, read-only, full, ...

    Raised instead of the bare ``OSError`` so that the problem is not blamed on the document,
    and because an ``OSError``'s text contains a file name, which contains the Windows user
    name. Create it with ``from_os_error``.
    """

    @classmethod
    def from_os_error(cls, doing: str, exc: OSError) -> TemporaryFolderError:
        """``doing`` completes "Could not ... the temporary folder", e.g. "copy the document
        into". The message and the log line carry the operating system's reason, never a path.
        """
        reason = os_reason(exc)
        log.warning(
            "Temporary folder problem: could not %s the temporary folder (%s)", doing, reason
        )
        return cls(f"Could not {doing} the temporary folder ({reason}). {_TEMP_FOLDER_ADVICE}")


def os_reason(exc: OSError) -> str:
    """The operating system's own words for ``exc``: safe to show to users and to log.

    ``str(exc)`` ends in the file name, so it is never used. The reason is the error's
    ``strerror``; if that is missing, or has a path in it, the standard text for the error
    number is used instead.
    """
    reason = exc.strerror
    if reason and reason.startswith(_NO_TEMP_DIR_PREFIX):
        return _NO_TEMP_DIR_PREFIX
    if not reason or any(char in reason for char in "\\/:"):
        reason = _text_for_error_number(exc.errno)
    return reason


def _text_for_error_number(number: int | None) -> str:
    if not number:
        return _UNKNOWN_REASON
    try:
        return os.strerror(number)
    except (ValueError, OverflowError):
        return _UNKNOWN_REASON


class DocumentConverter(Protocol):
    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        """Convert ``source`` into a PDF inside ``out_dir`` and return the PDF's path.

        ``source`` is a private copy that the engine may read; ``out_dir`` is an empty,
        private directory. Raises ``ConversionError`` subclasses for expected failures.
        """
        ...
