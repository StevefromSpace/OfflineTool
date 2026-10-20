"""The application entry point hands --apply-update to the replacement helper, before any GUI."""

from __future__ import annotations

import sys

from offline_doc_tools import app
from offline_doc_tools.updater import helper


def test_apply_update_runs_the_helper_and_never_creates_a_window(monkeypatch):
    seen = []
    monkeypatch.setattr(helper, "main", lambda argv: seen.append(list(argv)) or 7)
    monkeypatch.setattr(app, "QApplication", None)  # would raise if the GUI path were taken
    monkeypatch.setattr(sys, "argv", ["OfflineDocTools.exe", "--apply-update", "--protocol", "1"])

    assert app.main() == 7
    assert seen == [["--protocol", "1"]]
