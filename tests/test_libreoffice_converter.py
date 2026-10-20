"""LibreOffice adapter tests. The subprocess is replaced by a fake runner."""

import errno
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from conftest import FAKE_PDF_BYTES
from offline_doc_tools.engine import (
    ConversionError,
    ConversionFailedError,
    ConversionTimeoutError,
    TemporaryFolderError,
)
from offline_doc_tools.libreoffice import (
    LibreOfficeConverter,
    ProcessResult,
    libreoffice_converter,
)


class FakeRunner:
    """Records calls and simulates LibreOffice writing a PDF into --outdir."""

    def __init__(self, returncode=0, write_pdf=True, stderr=b"", raise_exc=None):
        self.returncode = returncode
        self.write_pdf = write_pdf
        self.stderr = stderr
        self.raise_exc = raise_exc
        self.calls: list[tuple[list[str], float]] = []
        self.envs: list[dict | None] = []

    def __call__(self, args, timeout, env=None):
        self.calls.append((list(args), timeout))
        self.envs.append(env)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.write_pdf:
            outdir = Path(args[args.index("--outdir") + 1])
            source = Path(args[-1])
            (outdir / f"{source.stem}.pdf").write_bytes(FAKE_PDF_BYTES)
        return ProcessResult(self.returncode, b"", self.stderr)


@pytest.fixture
def work(tmp_path):
    source = tmp_path / "work" / "source.docx"
    source.parent.mkdir()
    source.write_bytes(b"PK fake")
    return source, tmp_path / "work"


def _converter(tmp_path, runner, timeout=120.0):
    return LibreOfficeConverter(
        Path(r"C:\LO\soffice.exe"),
        profile_root=tmp_path / "profile",
        timeout=timeout,
        runner=runner,
    )


def _profile_dir(args) -> Path:
    prefix = "-env:UserInstallation="
    [option] = [a for a in args if a.startswith(prefix)]
    path = Path(url2pathname(urlparse(option[len(prefix) :]).path))
    assert path.is_dir(), f"profile directory {path} was not created"
    return path


def test_builds_safe_argument_list(tmp_path, work):
    source, outdir = work
    runner = FakeRunner()
    _converter(tmp_path, runner, timeout=42).convert_to_pdf(source, outdir)

    args, timeout = runner.calls[0]
    assert timeout == 42
    assert args[0] == str(Path(r"C:\LO\soffice.exe"))
    profile_uri = args[1].removeprefix("-env:UserInstallation=")
    assert profile_uri.startswith((tmp_path / "profile").as_uri() + "/")
    assert "--headless" in args
    assert "--norestore" in args
    assert args[args.index("--convert-to") + 1] == "pdf:writer_pdf_Export"
    assert args[args.index("--outdir") + 1] == str(outdir)
    assert args[-1] == str(source)
    assert all(isinstance(a, str) for a in args)


def test_returns_produced_pdf(tmp_path, work):
    source, outdir = work
    pdf = _converter(tmp_path, FakeRunner()).convert_to_pdf(source, outdir)
    assert pdf == outdir / "source.pdf"
    assert pdf.read_bytes() == FAKE_PDF_BYTES


def test_timeout_raises_timeout_error(tmp_path, work):
    source, outdir = work
    runner = FakeRunner(raise_exc=subprocess.TimeoutExpired(cmd="soffice", timeout=5))
    with pytest.raises(ConversionTimeoutError, match="5 seconds"):
        _converter(tmp_path, runner, timeout=5).convert_to_pdf(source, outdir)


def test_nonzero_exit_raises_with_detail(tmp_path, work):
    source, outdir = work
    runner = FakeRunner(
        returncode=1, write_pdf=False, stderr=b"Error: source file could not be loaded\n"
    )
    with pytest.raises(ConversionFailedError, match="code 1") as info:
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)
    assert "could not be loaded" in str(info.value)


def test_nonzero_exit_fails_even_if_pdf_was_written(tmp_path, work):
    source, outdir = work
    with pytest.raises(ConversionFailedError):
        _converter(tmp_path, FakeRunner(returncode=81)).convert_to_pdf(source, outdir)


def test_exit_zero_without_output_raises(tmp_path, work):
    source, outdir = work
    with pytest.raises(ConversionFailedError, match="did not produce a PDF"):
        _converter(tmp_path, FakeRunner(write_pdf=False)).convert_to_pdf(source, outdir)


def test_profile_blocks_external_links_before_first_run(tmp_path, work):
    source, outdir = work
    seen = {}

    def runner(args, timeout, env=None):
        xcu = _profile_dir(args) / "user" / "registrymodifications.xcu"
        seen["xcu"] = xcu.read_text(encoding="utf-8") if xcu.exists() else None
        return FakeRunner()(args, timeout)

    _converter(tmp_path, runner).convert_to_pdf(source, outdir)

    assert seen["xcu"] is not None, "profile settings must exist before LibreOffice starts"
    assert '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse">' in seen["xcu"]
    assert "<value>true</value>" in seen["xcu"]


def test_fresh_profile_is_used_after_a_timeout(tmp_path, work):
    source, outdir = work
    calls = []

    def runner(args, timeout, env=None):
        calls.append(_profile_dir(args))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd="soffice", timeout=timeout)
        return FakeRunner()(args, timeout)

    converter = _converter(tmp_path, runner)
    with pytest.raises(ConversionTimeoutError):
        converter.convert_to_pdf(source, outdir)
    converter.convert_to_pdf(source, outdir)

    assert calls[0] != calls[1]
    assert (calls[1] / "user" / "registrymodifications.xcu").exists()


def test_profile_is_reused_between_successful_conversions(tmp_path, work):
    source, outdir = work
    runner = FakeRunner()
    converter = _converter(tmp_path, runner)
    converter.convert_to_pdf(source, outdir)
    converter.convert_to_pdf(source, outdir)
    assert runner.calls[0][0][1] == runner.calls[1][0][1]


def test_temp_paths_are_redacted_from_messages_and_logs(tmp_path, work, caplog):
    source, outdir = work
    home = str(Path.home())
    noisy = (
        f"convert {source} as a Writer document -> {outdir / 'source.pdf'}\n"
        f"Error: could not read {home}\\private\\thing.png\n"
    ).encode()
    runner = FakeRunner(returncode=1, write_pdf=False, stderr=noisy)

    caplog.set_level("DEBUG", logger="offline_doc_tools")
    with pytest.raises(ConversionFailedError) as info:
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)

    for text in (str(info.value), caplog.text):
        assert str(source.parent) not in text
        assert home not in text
    assert "Error: could not read" in str(info.value)


def test_successful_run_logs_exit_code_but_not_output(tmp_path, work, caplog):
    source, outdir = work
    caplog.set_level("INFO", logger="offline_doc_tools")

    def runner(args, timeout, env=None):
        FakeRunner()(args, timeout)
        return ProcessResult(0, f"convert {source} -> {outdir}".encode(), b"")

    _converter(tmp_path, runner).convert_to_pdf(source, outdir)
    assert "exited with code 0" in caplog.text
    assert str(source.parent) not in caplog.text


def test_executable_cannot_start(tmp_path, work):
    source, outdir = work
    runner = FakeRunner(raise_exc=FileNotFoundError(2, "No such file"))
    with pytest.raises(ConversionFailedError, match="Could not start LibreOffice"):
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)


def test_lost_hardening_setting_is_restored_before_the_next_run(tmp_path, work):
    source, outdir = work
    seen = []

    def runner(args, timeout, env=None):
        xcu = _profile_dir(args) / "user" / "registrymodifications.xcu"
        seen.append("BlockUntrustedRefererLinks" in xcu.read_text(encoding="utf-8"))
        # Simulate LibreOffice rewriting its settings file without our entry.
        xcu.write_text('<?xml version="1.0"?><oor:items/>', encoding="utf-8")
        return FakeRunner()(args, timeout)

    converter = _converter(tmp_path, runner)
    converter.convert_to_pdf(source, outdir)
    converter.convert_to_pdf(source, outdir)
    assert seen == [True, True]


def test_libreoffice_temp_files_are_confined_to_the_profile(tmp_path, work):
    source, outdir = work
    runner = FakeRunner()
    _converter(tmp_path, runner).convert_to_pdf(source, outdir)

    [env] = runner.envs
    profile_root = tmp_path / "profile"
    for name in ("TMP", "TEMP", "TMPDIR"):
        assert Path(env[name]).is_relative_to(profile_root)
        assert Path(env[name]).is_dir()
    assert env.get("PATH") == os.environ.get("PATH")  # rest of the environment is inherited


def test_uri_and_temp_dir_spellings_are_redacted(tmp_path, work, caplog):
    source, outdir = work
    temp_dir = tempfile.gettempdir()
    noisy = (
        f"loading {source.parent.as_uri()}/source.docx\n"
        f"lock in {temp_dir}\\lu1234.tmp\n"
        "Error: final line\n"
    ).encode()
    runner = FakeRunner(returncode=1, write_pdf=False, stderr=noisy)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    with pytest.raises(ConversionFailedError):
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)
    assert source.parent.as_uri() not in caplog.text
    assert temp_dir not in caplog.text


def test_long_output_is_redacted_before_it_is_truncated(tmp_path, work, caplog):
    source, outdir = work
    # Place a path so that keeping only the last 2000 characters would cut off its first
    # three characters ("C:\"), leaving an unredactable fragment with the user name in it.
    line = f"{source.parent / 'source.docx'}\n"
    noisy = (line + "z" * (2000 - len(line) + 3)).encode()
    runner = FakeRunner(returncode=1, write_pdf=False, stderr=noisy)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    with pytest.raises(ConversionFailedError):
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)
    assert str(source.parent)[3:] not in caplog.text


def test_libreoffice_crash_gets_an_explanatory_message(tmp_path, work):
    source, outdir = work
    runner = FakeRunner(returncode=0xC0000409, write_pdf=False)
    with pytest.raises(ConversionFailedError) as info:
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)
    message = str(info.value)
    assert "LibreOffice crashed" in message
    assert "temporary folder" in message


# --- temporary folder problems: reported as such, never as raw OS errors ----------------

_USER = "Secret Person"  # stands in for the Windows user name inside a path


def _missing_folder_reason(folder: Path) -> str:
    """How this operating system words 'that folder does not exist'."""
    try:
        (folder / "probe").mkdir()
    except OSError as exc:
        return exc.strerror
    raise AssertionError("the folder unexpectedly exists")


def _assert_path_free(text: str, tmp_path: Path) -> None:
    assert _USER not in text
    assert str(tmp_path) not in text


def test_missing_temp_folder_stops_the_converter_on_entry(tmp_path, monkeypatch, caplog):
    missing = tmp_path / _USER / "gone"
    reason = _missing_folder_reason(missing)
    monkeypatch.setattr(tempfile, "tempdir", str(missing))
    caplog.set_level("DEBUG", logger="offline_doc_tools")

    with (
        pytest.raises(ConversionError) as info,
        libreoffice_converter(Path(r"C:\LO\soffice.exe")),
    ):
        pytest.fail("no converter may be handed out without a place for its profiles")

    message = str(info.value)
    assert not isinstance(info.value, OSError)
    assert "temporary folder" in message
    assert reason in message
    assert caplog.records, "expected the problem to be logged"
    _assert_path_free(message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_unusable_default_temp_folders_are_not_listed_by_the_converter(
    tmp_path, monkeypatch, caplog
):
    # Python's own "No usable temporary directory found in [<every folder tried>]" error.
    candidates = [str(tmp_path / _USER / "a"), str(tmp_path / _USER / "b")]
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", lambda: candidates)
    caplog.set_level("DEBUG", logger="offline_doc_tools")

    with (
        pytest.raises(ConversionError) as info,
        libreoffice_converter(Path(r"C:\LO\soffice.exe")),
    ):
        pytest.fail("no converter may be handed out without a place for its profiles")

    assert "temporary folder" in str(info.value)
    _assert_path_free(str(info.value), tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_disk_full_when_creating_the_profile_folder(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device", rf"C:\Users\{_USER}\Temp\x")

    monkeypatch.setattr(tempfile, "mkdtemp", refuse)
    with (
        pytest.raises(ConversionError) as info,
        libreoffice_converter(Path(r"C:\LO\soffice.exe")),
    ):
        pytest.fail("no converter may be handed out without a place for its profiles")
    assert "temporary folder" in str(info.value)
    assert "No space left on device" in str(info.value)
    assert _USER not in str(info.value)


def _permission_denied(path):
    return PermissionError(errno.EACCES, "Permission denied", str(path))


def _disk_full(path):
    return OSError(errno.ENOSPC, "No space left on device", str(path))


@pytest.mark.parametrize(
    ("method", "name", "make_error", "reason"),
    [
        ("read_text", "registrymodifications.xcu", _permission_denied, "Permission denied"),
        ("write_text", "registrymodifications.xcu", _disk_full, "No space left on device"),
        ("mkdir", "user", _disk_full, "No space left on device"),
        ("mkdir", "tmp", _permission_denied, "Permission denied"),
    ],
    ids=["read-settings", "write-settings", "profile-folder", "libreoffice-temp-folder"],
)
def test_profile_preparation_failures_are_temp_folder_problems(
    tmp_path, work, monkeypatch, caplog, method, name, make_error, reason
):
    source, outdir = work
    real = getattr(Path, method)

    def failing(self, *args, **kwargs):
        if self.name == name:
            raise make_error(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)
    caplog.set_level("DEBUG", logger="offline_doc_tools")
    runner = FakeRunner()

    with pytest.raises(ConversionError) as info:
        _converter(tmp_path, runner).convert_to_pdf(source, outdir)

    message = str(info.value)
    assert not isinstance(info.value, OSError)
    assert "temporary folder" in message and reason in message
    assert "Could not start LibreOffice" not in message
    assert runner.calls == []  # LibreOffice is never started with a broken profile
    _assert_path_free(message, tmp_path)
    _assert_path_free(caplog.text, tmp_path)


def test_a_temp_folder_error_from_the_runner_is_not_blamed_on_libreoffice(tmp_path, work):
    source, outdir = work
    error = TemporaryFolderError("Could not create files in the temporary folder (x).")
    with pytest.raises(TemporaryFolderError) as info:
        _converter(tmp_path, FakeRunner(raise_exc=error)).convert_to_pdf(source, outdir)
    assert info.value is error
