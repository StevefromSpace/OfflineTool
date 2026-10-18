"""Shared test helpers. All documents are synthetic and generated at test time."""

from __future__ import annotations

import gc
import hashlib
import os
import time
import zipfile
from pathlib import Path

import pytest

# Qt widgets must never need a real display during tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>"""

FAKE_PDF_BYTES = b"%PDF-1.7\n% synthetic test pdf\n%%EOF\n"


def write_docx(path: Path, text: str = "Synthetic test document") -> Path:
    """Write a minimal but valid .docx file containing one paragraph."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", _DOCUMENT.format(text=text))
    return path


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def make_docx(tmp_path: Path):
    """Factory fixture: make_docx("name.docx") -> Path inside tmp_path/'in'."""

    def _make(name: str = "document.docx", text: str = "Synthetic test document") -> Path:
        return write_docx(tmp_path / "in" / name, text)

    return _make


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _dispose_qt_widgets(request):
    """Close and delete every window a Qt test created, deterministically.

    Otherwise test windows and workers left in reference cycles were finalised by Python's
    cyclic garbage collector at an arbitrary later moment, during another test's running
    conversion, which aborted the interpreter (seen with PySide6-Essentials 6.12 on
    Python 3.14; GC debug output showed an earlier test's MainWindow being collected then).
    """
    yield
    if "qapp" not in request.fixturenames:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    from offline_doc_tools.main_window import MainWindow

    still_running = []
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow) and widget.is_converting():
            # A test failed mid-conversion. Stop the batch and let the thread end; closing
            # now would open the confirmation dialog, and deleting the window would destroy
            # a running QThread (fatal in Qt). Pump events rather than block in
            # QThread.wait(): the thread only quits via a signal queued to this thread.
            widget._worker.cancel()
            deadline = time.monotonic() + 20
            while widget.is_converting() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
            if widget.is_converting():
                still_running.append(widget)
                continue
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    if still_running:
        pytest.fail("a conversion thread was still running after the test; window not deleted")


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path
