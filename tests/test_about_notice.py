"""The window itself tells the user that Qt is used under the LGPL and where the notices are.

Without this, the LGPL notice existed only in a text file beside the executable.
"""

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from offline_doc_tools import __version__
from offline_doc_tools.main_window import MainWindow, about_text


def test_the_text_names_qt_pyside_the_licence_and_where_the_notices_are():
    text = about_text()

    for needed in ("Qt", "PySide6", "LGPL", "THIRD-PARTY-NOTICES.txt", "licenses", __version__):
        assert needed in text


def test_the_text_is_plain_because_the_message_box_renders_rich_text():
    assert "<" not in about_text()


def test_the_button_shows_the_notice(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "about", lambda *args: shown.append(args[1:]))
    window = MainWindow(find_executable=lambda: Path("soffice"))

    window.about_button.click()

    assert shown == [("About Offline Doc Tools", about_text())]
