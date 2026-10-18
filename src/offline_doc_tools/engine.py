"""The contract between the conversion service and a conversion engine.

Exception messages are shown to end users, so they should be short and in plain language.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ConversionError(Exception):
    """Base class for expected, user-reportable conversion problems."""


class InvalidInputError(ConversionError):
    """The input file or output folder cannot be used."""


class ConversionFailedError(ConversionError):
    """The engine ran but did not produce a usable PDF."""


class ConversionTimeoutError(ConversionError):
    """The engine did not finish within the allowed time."""


class DocumentConverter(Protocol):
    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        """Convert ``source`` into a PDF inside ``out_dir`` and return the PDF's path.

        ``source`` is a private copy that the engine may read; ``out_dir`` is an empty,
        private directory. Raises ``ConversionError`` subclasses for expected failures.
        """
        ...
