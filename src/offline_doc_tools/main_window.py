"""Main application window. UI only: conversion work is delegated to ConversionWorker."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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

from . import __version__
from .engine import DocumentConverter
from .libreoffice import find_soffice, libreoffice_converter, missing_libreoffice_message
from .service import BatchSummary, FileResult, FileStatus
from .updater.runtime import UpdateRuntime
from .updater.service import CheckResult, CheckStatus, UpdateFailure, cleanup_staging
from .worker import BackgroundTask, ConversionWorker

log = logging.getLogger(__name__)

MakeConverter = Callable[[Path], AbstractContextManager[DocumentConverter]]
AskToken = Callable[[QWidget], str | None]
Confirm = Callable[[QWidget, str, str], bool]


def _ask_token_dialog(parent: QWidget) -> str | None:
    text, accepted = QInputDialog.getText(
        parent,
        "Update access",
        "Paste the update access token you were given:",
        QLineEdit.EchoMode.Password,
    )
    return text if accepted else None


def _confirm_dialog(parent: QWidget, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes


def about_text() -> str:
    """Plain text on purpose: ``QMessageBox.about`` renders anything that looks like rich text."""
    return (
        f"Offline Doc Tools {__version__}\n\n"
        "Converts Word (.docx) documents to PDF on this computer, using the LibreOffice you have "
        "installed. Your documents are not sent anywhere.\n\n"
        "This program uses the Qt toolkit (QtCore, QtGui and QtWidgets) through PySide6 (Qt for "
        "Python), under the GNU Lesser General Public License version 3 (LGPL-3.0). Qt is not part "
        "of this program's own code: it is supplied as separate library files in the program "
        "folder.\n\n"
        "The licence texts, the notices for the other third-party software and the web addresses "
        "from which the Qt and PySide6 source code can be obtained are in "
        "THIRD-PARTY-NOTICES.txt and the licenses folder, next to OfflineDocTools.exe."
    )


_COL_FILE, _COL_RESULT, _COL_DETAILS = range(3)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


class MainWindow(QMainWindow):
    def __init__(
        self,
        find_executable: Callable[[], Path | None] = find_soffice,
        make_converter: MakeConverter = libreoffice_converter,
        updates: UpdateRuntime | None = None,
        ask_token: AskToken = _ask_token_dialog,
        confirm: Confirm = _confirm_dialog,
        quit_app: Callable[[], None] = QApplication.quit,
    ) -> None:
        super().__init__()
        self._updates = updates
        self._ask_token = ask_token
        self._confirm = confirm
        self._quit_app = quit_app
        self._update_task: BackgroundTask | None = None
        self._found: CheckResult | None = None
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
        if self._updates is not None:
            try:  # whatever goes wrong here must never stop documents being converted
                self.update_label.setText(self._updates.startup_message() or "")
            except Exception:
                log.error("Could not read the last update result", exc_info=False)

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
        self.about_button = QPushButton("About and licences…")
        self.about_button.clicked.connect(self._show_about)
        bottom.addWidget(self.summary_label, 1)
        bottom.addWidget(self.about_button)
        bottom.addWidget(self.open_output_button)
        layout.addLayout(bottom)

        # The only features that use the network, and only when a button here is clicked.
        # Hidden in a build that cannot update (no private repository or key set in config.py),
        # so a client is never offered a button that can only say "not configured".
        self.updates_box = QWidget()
        updates_row = QHBoxLayout(self.updates_box)
        updates_row.setContentsMargins(0, 0, 0, 0)
        self.update_button = QPushButton("Check for updates")
        self.update_button.setToolTip(
            "Asks the private release repository for the latest version. Nothing about you or "
            "your documents is sent. Nothing is installed without your confirmation."
        )
        self.update_button.clicked.connect(self._check_for_updates)
        self.token_button = QPushButton("Update access…")
        self.token_button.setToolTip("Enter or replace the access token used for updates")
        self.token_button.clicked.connect(self._enter_token)
        self.update_label = QLabel("")
        self.update_label.setTextFormat(Qt.TextFormat.PlainText)
        self.update_label.setWordWrap(True)
        self.install_button = QPushButton("Download and install…")
        self.install_button.clicked.connect(self._install_update)
        self.install_button.hide()
        updates_row.addWidget(self.update_button)
        updates_row.addWidget(self.token_button)
        updates_row.addWidget(self.update_label, 1)
        updates_row.addWidget(self.install_button)
        layout.addWidget(self.updates_box)
        if self._updates is None or not self._updates.service.configured():
            self.updates_box.hide()

        self.setCentralWidget(root)

    def _show_about(self) -> None:
        QMessageBox.about(self, "About Offline Doc Tools", about_text())

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

    # --- updates (every step is started by a click) ----------------------------

    def _updating(self) -> bool:
        return self._update_task is not None

    def _run_update_task(self, work, handler, text: str) -> None:
        self.install_button.hide()
        self._found = None
        self.update_label.setText(text)
        task = BackgroundTask(work)
        task.done.connect(handler, Qt.ConnectionType.QueuedConnection)
        task.progress.connect(self._on_update_progress, Qt.ConnectionType.QueuedConnection)
        self._update_task = task
        self._update_controls()
        task.start()

    def _on_update_progress(self, done: int, total: int) -> None:
        if self._updating() and total:
            self.update_label.setText(f"Downloading the update… {done * 100 // total}%")

    def _check_for_updates(self) -> None:
        if self._updating() or self.is_converting():
            return
        if self._updates is None:
            self.update_label.setText("Updates are not available in this build.")
            return
        service = self._updates.service
        if service.configured() and not service.has_token():
            self._enter_token(then_check=True)
            return
        self._run_update_task(
            lambda task: service.check(), self._on_check_done, "Checking for updates…"
        )

    def _on_check_done(self, result: object, error: object) -> None:
        self._update_task = None
        if error is not None or not isinstance(result, CheckResult):
            self.update_label.setText("The update check failed unexpectedly.")
        else:
            self.update_label.setText(result.message)
            if result.status is CheckStatus.NEWER:
                self._found = result
                self.install_button.show()
        self._update_controls()

    def _enter_token(self, then_check: bool = False) -> None:
        if self._updating() or self._updates is None:
            return
        text = self._ask_token(self)
        if not text:
            return
        try:
            self._updates.service.save_token(text)
        except ValueError:
            self.update_label.setText("That does not look like a GitHub access token.")
            return
        except OSError:
            self.update_label.setText("The token could not be stored on this computer.")
            return
        self.update_label.setText("Update access saved.")
        if then_check is True:
            self._check_for_updates()

    def _install_update(self) -> None:
        found, runtime = self._found, self._updates
        if found is None or runtime is None or self._updating() or self.is_converting():
            return
        if runtime.install_dir is None:
            self.update_label.setText("Updates can only be installed in the packaged application.")
            return
        question = (
            f"Install version {found.version}? Offline Doc Tools will close, update itself and "
            "start again. If the new version does not start, the current one is restored."
        )
        if not self._confirm(self, "Install update", question):
            return
        install_dir, service = runtime.install_dir, runtime.service
        self._run_update_task(
            lambda task: service.download_and_stage(
                found, install_dir, progress=task.progress.emit, cancel=task.cancelled
            ),
            self._on_install_done,
            "Downloading the update…",
        )

    def _on_install_done(self, staged: object, error: object) -> None:
        self._update_task = None
        runtime = self._updates
        if error is not None or staged is None or runtime is None or runtime.install_dir is None:
            message = str(error) if isinstance(error, UpdateFailure) else "The update failed."
            self.update_label.setText(message)
            self._update_controls()
            return
        try:
            runtime.service.launch_helper(staged, runtime.install_dir, runtime.result_file)
        except OSError:
            cleanup_staging(runtime.staging_dir)
            self.update_label.setText("The update could not be started. Nothing was changed.")
            self._update_controls()
            return
        self.update_label.setText("Installing the update. The application will restart.")
        self._quit_app()

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
            if result.linked_images_omitted:
                count = result.linked_images_omitted
                details += (
                    f". {count} linked image{'s were' if count != 1 else ' was'} left out of "
                    "the PDF (linked images are never fetched)"
                )
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
        updating = self._updating()
        for widget in (self.add_button, self.clear_button, self.choose_output_button):
            widget.setEnabled(not busy)
        self.remove_button.setEnabled(not busy and bool(self._files))
        for widget in (self.update_button, self.token_button, self.install_button):
            widget.setEnabled(not busy and not updating)
        self.convert_button.setEnabled(
            not busy
            and not updating
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
