"""Headless smoke tests of the main window (QT_QPA_PLATFORM=offscreen)."""

import gc
import threading
import time
import weakref
from contextlib import contextmanager, nullcontext
from pathlib import Path

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from offline_doc_tools.engine import ConversionFailedError
from offline_doc_tools.main_window import MainWindow
from test_service import FakeConverter


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _window(converter=None, executable=Path("soffice")):
    @contextmanager
    def make_converter(_executable):
        yield converter or FakeConverter()

    return MainWindow(find_executable=lambda: executable, make_converter=make_converter)


def test_convert_disabled_until_files_and_folder_chosen(qapp, make_docx, output_dir):
    window = _window()
    assert not window.convert_button.isEnabled()

    window.add_files([make_docx("a.docx")])
    assert not window.convert_button.isEnabled()

    window.set_output_dir(output_dir)
    assert window.convert_button.isEnabled()


def test_duplicate_files_are_listed_once(qapp, make_docx):
    window = _window()
    doc = make_docx("a.docx")
    window.add_files([doc, doc])
    window.add_files([doc])
    assert window.file_list.count() == 1


def test_missing_libreoffice_blocks_conversion(qapp, make_docx, output_dir):
    window = _window(executable=None)
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)

    assert not window.convert_button.isEnabled()
    assert window.engine_banner.isVisibleTo(window)
    assert "LibreOffice" in window.engine_banner.text()


def test_end_to_end_batch_reports_results(qapp, make_docx, output_dir):
    converter = FakeConverter(failures={"FAIL": ConversionFailedError("Damaged document.")})
    window = _window(converter)
    window.add_files([make_docx("good.docx"), make_docx("bad.docx", text="FAIL")])
    window.set_output_dir(output_dir)

    window.start_conversion()
    assert window.is_converting()
    _wait_until(lambda: not window.is_converting())

    assert window.results_table.rowCount() == 2
    assert window.results_table.item(0, 1).text() == "Converted"
    assert window.results_table.item(0, 2).text() == "Saved as good.pdf"
    assert window.results_table.item(1, 1).text() == "Failed"
    assert "Damaged document." in window.results_table.item(1, 2).text()
    assert "1 converted" in window.summary_label.text()
    assert "1 failed" in window.summary_label.text()
    assert window.progress_bar.value() == 2
    assert window.open_output_button.isEnabled()
    assert (output_dir / "good.pdf").is_file()
    window.close()


class BlockingConverter(FakeConverter):
    """Waits for a signal before each conversion and records the converting thread."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.started = threading.Event()
        self.thread_ids: list[int] = []

    def convert_to_pdf(self, source, out_dir):
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        assert self.release.wait(10), "test never released the converter"
        return super().convert_to_pdf(source, out_dir)


def test_initial_progress_shows_no_bogus_total(qapp):
    window = _window()
    assert "100" not in window.progress_bar.text()


def test_conversion_runs_off_the_gui_thread(qapp, make_docx, output_dir):
    converter = BlockingConverter()
    window = _window(converter)
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)

    window.start_conversion()
    _wait_until(converter.started.is_set)
    QApplication.processEvents()  # GUI thread still serves events while converting
    converter.release.set()
    _wait_until(lambda: not window.is_converting())

    assert converter.thread_ids and converter.thread_ids[0] != threading.get_ident()


def test_cancel_stops_after_current_file(qapp, make_docx, output_dir):
    converter = BlockingConverter()
    window = _window(converter)
    window.add_files([make_docx(f"{n}.docx") for n in "abc"])
    window.set_output_dir(output_dir)

    window.start_conversion()
    _wait_until(converter.started.is_set)
    window.cancel_button.click()
    converter.release.set()
    _wait_until(lambda: not window.is_converting())

    assert window.results_table.rowCount() == 1
    assert "2 not processed (cancelled)" in window.summary_label.text()


def test_batch_that_cannot_start_is_reported(qapp, make_docx, tmp_path):
    output_dir = tmp_path / "gone"
    output_dir.mkdir()
    window = _window()
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)
    output_dir.rmdir()

    window.start_conversion()
    _wait_until(lambda: not window.is_converting())

    assert "could not start" in window.summary_label.text()
    assert "does not exist" in window.summary_label.text()


def test_closing_during_conversion_waits_then_closes(qapp, make_docx, output_dir, monkeypatch):
    monkeypatch.setattr(
        "offline_doc_tools.main_window.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    converter = BlockingConverter()
    window = _window(converter)
    window.add_files([make_docx("a.docx"), make_docx("b.docx")])
    window.set_output_dir(output_dir)
    window.show()

    window.start_conversion()
    _wait_until(converter.started.is_set)
    assert window.close() is False  # refused while a conversion is running
    assert window.isVisible()
    converter.release.set()
    _wait_until(lambda: not window.is_converting())
    _wait_until(lambda: not window.isVisible())

    assert window.results_table.rowCount() == 1  # the second file was cancelled


def test_libreoffice_removed_after_start_up_is_detected(qapp, make_docx, output_dir):
    found = [Path("soffice")]
    window = MainWindow(
        find_executable=lambda: found[0], make_converter=lambda exe: nullcontext(FakeConverter())
    )
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)
    found[0] = None

    window.start_conversion()

    assert not window.is_converting()
    assert window.engine_banner.isVisibleTo(window)
    assert not window.convert_button.isEnabled()


def test_file_names_are_shown_as_plain_text(qapp):
    window = _window()
    for label in (window.status_label, window.summary_label, window.engine_banner):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_worker_and_thread_are_freed_without_the_garbage_collector(qapp, make_docx, output_dir):
    window = _window()
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)

    gc.disable()  # destruction must not depend on the cyclic GC running at some later time
    try:
        window.start_conversion()
        worker_ref = weakref.ref(window._worker)
        thread = window._thread
        _wait_until(lambda: not window.is_converting())
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        assert worker_ref() is None
        assert not shiboken6.isValid(thread)
    finally:
        gc.enable()
