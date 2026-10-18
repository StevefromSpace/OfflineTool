"""Runs a conversion batch off the GUI thread and reports progress through Qt signals."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .engine import ConversionError, DocumentConverter
from .service import ConversionService

log = logging.getLogger(__name__)

ConverterFactory = Callable[[], AbstractContextManager[DocumentConverter]]


class ConversionWorker(QObject):
    """Move to a ``QThread`` and connect ``QThread.started`` to ``run``."""

    file_started = Signal(int, int, str)  # index, total, file name
    file_finished = Signal(int, int, object)  # index, total, FileResult
    finished = Signal(object)  # BatchSummary
    failed = Signal(str)  # the batch could not run at all

    def __init__(
        self, sources: Sequence[Path], output_dir: Path, converter_factory: ConverterFactory
    ) -> None:
        super().__init__()
        self._sources = list(sources)
        self._output_dir = output_dir
        self._converter_factory = converter_factory
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Stop after the document currently being converted. Safe to call from any thread."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        try:
            with self._converter_factory() as converter:
                summary = ConversionService(converter).convert_batch(
                    self._sources,
                    self._output_dir,
                    on_file_started=lambda i, n, src: self.file_started.emit(i, n, src.name),
                    on_file_finished=lambda i, n, res: self.file_finished.emit(i, n, res),
                    should_cancel=self._cancel.is_set,
                )
        except ConversionError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            log.error("Batch failed unexpectedly", exc_info=True)
            self.failed.emit(f"Unexpected error: {exc}")
            return
        self.finished.emit(summary)
