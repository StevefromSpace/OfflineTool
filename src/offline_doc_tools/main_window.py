"""Main application window. UI only: conversion work is delegated to ConversionWorker."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .engine import DocumentConverter
from .libreoffice import find_soffice, libreoffice_converter, missing_libreoffice_message
from .service import BatchSummary, FileResult, FileStatus
from .worker import ConversionWorker

MakeConverter = Callable[[Path], AbstractContextManager[DocumentConverter]]

_COL_FILE, _COL_RESULT, _COL_DETAILS = range(3)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


class MainWindow(QMainWindow):
    def __init__(
        self,
        find_executable: Callable[[], Path | None] = find_soffice,
        make_converter: MakeConverter = libreoffice_converter,
    ) -> None:
        super().__init__()
        self._find_executable = find_executable
        self._make_converter = make_converter
        self._executable: Path | None = None
        self._files: list[Path] = []
        self._output_dir: Path | None = None
        self._thread: QThread | None = None
        self._worker: ConversionWorker | None = None
        self._close_when_done = False

        self.setWindowTitle("Offline Doc Tools: Word to PDF")
        self.resize(820, 640)
        self._build_ui()
        self._check_engine()
        self._update_controls()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        self.engine_box = QWidget()
        engine_layout = QHBoxLayout(self.engine_box)
        engine_layout.setContentsMargins(0, 0, 0, 0)
        self.engine_banner = QLabel()
        self.engine_banner.setTextFormat(Qt.TextFormat.PlainText)
        self.engine_banner.setWordWrap(True)
        self.engine_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.engine_banner.setStyleSheet(
            "QLabel { background: #fff4e5; color: #663c00; border: 1px solid #ffb74d;"
            " padding: 8px; }"
        )
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self._recheck_engine)
        engine_layout.addWidget(self.engine_banner, 1)
        engine_layout.addWidget(recheck, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.engine_box)

        files_group = QGroupBox("Word documents (.docx)")
        files_layout = QVBoxLayout(files_group)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        files_layout.addWidget(self.file_list)
        file_buttons = QHBoxLayout()
        self.add_button = QPushButton("Add files…")
        self.add_button.clicked.connect(self._choose_files)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear_files)
        for button in (self.add_button, self.remove_button, self.clear_button):
            file_buttons.addWidget(button)
        file_buttons.addStretch()
        files_layout.addLayout(file_buttons)
        layout.addWidget(files_group)

        output_group = QGroupBox("Output folder")
        output_layout = QHBoxLayout(output_group)
        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("Choose where the PDF files will be saved")
        self.choose_output_button = QPushButton("Choose…")
        self.choose_output_button.clicked.connect(self._choose_output_dir)
        output_layout.addWidget(self.output_edit, 1)
        output_layout.addWidget(self.choose_output_button)
        layout.addWidget(output_group)

        actions = QHBoxLayout()
        self.convert_button = QPushButton("Convert to PDF")
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self.start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Stops after the document currently being converted")
        self.cancel_button.clicked.connect(self._cancel_conversion)
        actions.addWidget(self.convert_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v of %m")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        # Plain text: file names must never be interpreted as rich text/HTML.
        self.status_label = QLabel("")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)

        self.results_table = QTableWidget(0, 3)
        self.results_table.setHorizontalHeaderLabels(["File", "Result", "Details"])
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.verticalHeader().setVisible(False)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(_COL_FILE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_COL_RESULT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_DETAILS, QHeaderView.ResizeMode.Stretch)
        self.results_table.setColumnWidth(_COL_FILE, 240)
        layout.addWidget(self.results_table, 1)

        bottom = QHBoxLayout()
        self.summary_label = QLabel("")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-weight: bold;")
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.clicked.connect(self._open_output_dir)
        bottom.addWidget(self.summary_label, 1)
        bottom.addWidget(self.open_output_button)
        layout.addLayout(bottom)

        self.setCentralWidget(root)

    # --- public API (also used by tests) --------------------------------------

    def add_files(self, paths: Iterable[Path]) -> None:
        known = {_path_key(p) for p in self._files}
        for path in paths:
            key = _path_key(path)
            if key in known:
                continue
            known.add(key)
            self._files.append(path)
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            self.file_list.addItem(item)
        self._update_controls()

    def set_output_dir(self, path: Path) -> None:
        self._output_dir = path
        self.output_edit.setText(str(path))
        self._update_controls()

    def is_converting(self) -> bool:
        return self._thread is not None

    def start_conversion(self) -> None:
        if self.is_converting() or not self._files or self._output_dir is None:
            return
        # Re-check in case LibreOffice was removed or installed since start-up.
        self._check_engine()
        if self._executable is None:
            self._update_controls()
            return

        executable = self._executable
        files = list(self._files)
        self.results_table.setRowCount(0)
        self.summary_label.setText("")
        self.summary_label.setStyleSheet("font-weight: bold;")
        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        thread = QThread(self)
        worker = ConversionWorker(files, self._output_dir, lambda: self._make_converter(executable))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.file_started.connect(self._on_file_started)
        worker.file_finished.connect(self._on_file_finished)
        worker.finished.connect(self._on_batch_finished)
        worker.failed.connect(self._on_batch_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)

        self._thread, self._worker = thread, worker
        self._update_controls()
        thread.start()

    # --- engine detection -----------------------------------------------------

    def _check_engine(self) -> None:
        self._executable = self._find_executable()
        if self._executable is None:
            self.engine_banner.setText(missing_libreoffice_message())
            self.engine_box.show()
        else:
            self.engine_box.hide()

    def _recheck_engine(self) -> None:
        self._check_engine()
        self._update_controls()

    # --- user actions ---------------------------------------------------------

    def _choose_files(self) -> None:
        start = str(self._files[-1].parent) if self._files else ""
        names, _ = QFileDialog.getOpenFileNames(
            self, "Select Word documents", start, "Word documents (*.docx)"
        )
        self.add_files(Path(name) for name in names)

    def _remove_selected(self) -> None:
        rows = sorted((self.file_list.row(i) for i in self.file_list.selectedItems()), reverse=True)
        for row in rows:
            self.file_list.takeItem(row)
            del self._files[row]
        self._update_controls()

    def _clear_files(self) -> None:
        self.file_list.clear()
        self._files.clear()
        self._update_controls()

    def _choose_output_dir(self) -> None:
        start = str(self._output_dir) if self._output_dir else ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if chosen:
            self.set_output_dir(Path(chosen))

    def _open_output_dir(self) -> None:
        if self._output_dir is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_dir))):
            self.status_label.setText("Could not open the output folder.")

    def _cancel_conversion(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling after the current document…")

    # --- worker signals -------------------------------------------------------

    def _on_file_started(self, index: int, total: int, name: str) -> None:
        self.status_label.setText(f"Converting {index + 1} of {total}: {name}")
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self._set_row(row, name, "Converting…", "")

    def _on_file_finished(self, index: int, total: int, result: FileResult) -> None:
        row = self.results_table.rowCount() - 1
        if result.status is FileStatus.SUCCEEDED:
            details = f"Saved as {result.output.name}"
            if result.renamed:
                details += " (a file with the original name already existed)"
            self._set_row(row, result.source.name, "Converted", details)
            self.results_table.item(row, _COL_DETAILS).setToolTip(str(result.output))
        else:
            self._set_row(row, result.source.name, "Failed", result.message)
            self.results_table.item(row, _COL_RESULT).setForeground(Qt.GlobalColor.red)
        self.results_table.item(row, _COL_FILE).setToolTip(str(result.source))
        self.progress_bar.setValue(index + 1)

    def _on_batch_finished(self, summary: BatchSummary) -> None:
        parts = [f"{summary.succeeded} converted", f"{summary.failed} failed"]
        if summary.cancelled:
            parts.append(f"{len(summary.not_processed)} not processed (cancelled)")
        self.summary_label.setText("Finished: " + ", ".join(parts) + ".")
        self.status_label.setText("")

    def _on_batch_failed(self, message: str) -> None:
        self.summary_label.setStyleSheet("font-weight: bold; color: #b00020;")
        self.summary_label.setText(f"Conversion could not start: {message}")
        self.status_label.setText("")

    def _on_thread_finished(self) -> None:
        if self._thread is not None:
            self._thread.wait()  # returns at once: the thread has finished its event loop
            self._thread.deleteLater()
        # The worker has a single owner, this Python reference. Dropping it deletes the worker
        # here on the GUI thread, after its thread has stopped.
        self._thread = self._worker = None
        self._update_controls()
        if self._close_when_done:
            self.close()

    # --- helpers --------------------------------------------------------------

    def _set_row(self, row: int, name: str, result: str, details: str) -> None:
        for column, text in ((_COL_FILE, name), (_COL_RESULT, result), (_COL_DETAILS, details)):
            item = QTableWidgetItem(text)
            if column == _COL_DETAILS:
                item.setToolTip(text)
            self.results_table.setItem(row, column, item)

    def _update_controls(self) -> None:
        busy = self.is_converting()
        for widget in (self.add_button, self.clear_button, self.choose_output_button):
            widget.setEnabled(not busy)
        self.remove_button.setEnabled(not busy and bool(self._files))
        self.convert_button.setEnabled(
            not busy
            and bool(self._files)
            and self._output_dir is not None
            and self._executable is not None
        )
        self.cancel_button.setEnabled(busy)
        self.open_output_button.setEnabled(
            self._output_dir is not None and self._output_dir.is_dir()
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        if not self.is_converting():
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "Conversion in progress",
            "A conversion is running. Stop after the current document and close?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._close_when_done = True
            self._cancel_conversion()
        event.ignore()
