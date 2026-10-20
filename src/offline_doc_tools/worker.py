"""Runs a conversion batch off the GUI thread and reports progress through Qt signals."""

from __future__ import annotations

import contextlib
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


class BackgroundTask(QObject):
    """Runs ``work(task)`` once on a daemon thread; reports ``done(result, error)``.

    A plain thread, not a QThread: closing the window while a request is still running must never
    wait for the network or tear down a running QThread. ``work`` may emit ``progress`` and should
    look at ``cancelled()``. Exactly one of result and error is meaningful; an exception becomes
    ``error`` (and is logged by type only, never with its text)."""

    progress = Signal(int, int)  # done, total
    done = Signal(object, object)  # result, error (an Exception or None)

    def __init__(self, work: Callable[[BackgroundTask], object]) -> None:
        super().__init__()
        self._work = work
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        result: object = None
        error: Exception | None = None
        try:
            result = self._work(self)
        except Exception as exc:
            log.error("Background task failed unexpectedly: %s", type(exc).__name__)
            error = exc
        with contextlib.suppress(RuntimeError):  # the window was closed meanwhile
            self.done.emit(result, error)
