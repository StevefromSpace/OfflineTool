from contextlib import contextmanager

from offline_doc_tools.engine import ConversionFailedError
from offline_doc_tools.service import FileStatus
from offline_doc_tools.worker import ConversionWorker
from test_service import FakeConverter


@contextmanager
def _factory_for(converter):
    yield converter


def _collect(worker):
    events = {"started": [], "finished_file": [], "done": [], "failed": []}
    worker.file_started.connect(lambda i, n, name: events["started"].append((i, n, name)))
    worker.file_finished.connect(lambda i, n, res: events["finished_file"].append((i, n, res)))
    worker.finished.connect(lambda summary: events["done"].append(summary))
    worker.failed.connect(lambda msg: events["failed"].append(msg))
    return events


def test_worker_emits_progress_and_summary(qapp, make_docx, output_dir):
    sources = [make_docx("a.docx"), make_docx("b.docx", text="FAIL")]
    converter = FakeConverter(failures={"FAIL": ConversionFailedError("bad document")})
    worker = ConversionWorker(sources, output_dir, lambda: _factory_for(converter))
    events = _collect(worker)

    worker.run()

    assert [e[:2] for e in events["started"]] == [(0, 2), (1, 2)]
    assert events["started"][0][2] == "a.docx"
    assert [e[2].status for e in events["finished_file"]] == [
        FileStatus.SUCCEEDED,
        FileStatus.FAILED,
    ]
    [summary] = events["done"]
    assert summary.succeeded == 1 and summary.failed == 1
    assert events["failed"] == []


def test_worker_cancel_skips_remaining(qapp, make_docx, output_dir):
    sources = [make_docx("a.docx"), make_docx("b.docx")]
    worker = ConversionWorker(sources, output_dir, lambda: _factory_for(FakeConverter()))
    events = _collect(worker)
    worker.file_finished.connect(lambda *_: worker.cancel())

    worker.run()

    [summary] = events["done"]
    assert summary.cancelled is True
    assert len(summary.results) == 1


def test_worker_reports_setup_failure(qapp, make_docx, tmp_path):
    worker = ConversionWorker(
        [make_docx("a.docx")], tmp_path / "missing-out", lambda: _factory_for(FakeConverter())
    )
    events = _collect(worker)

    worker.run()

    assert events["done"] == []
    [message] = events["failed"]
    assert "does not exist" in message
