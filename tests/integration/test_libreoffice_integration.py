"""Runs the real LibreOffice. Skipped automatically when LibreOffice is not installed.

Deselect explicitly with:  pytest -m "not integration"
"""

import http.server
import shutil
import struct
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path

import pytest

from conftest import file_digest, write_docx
from offline_doc_tools.libreoffice import find_soffice, libreoffice_converter
from offline_doc_tools.service import ConversionService, FileStatus


def _png(size: int) -> bytes:
    """An opaque grey square PNG; distinct sizes tell images apart in the PDF."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    rows = b"".join(b"\x00" + b"\x80\x80\x80" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


_EMBEDDED_SIZE, _LOCAL_LINK_SIZE, _REMOTE_LINK_SIZE = 2, 3, 5
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

SOFFICE = find_soffice()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(SOFFICE is None, reason="LibreOffice is not installed"),
]


def test_real_batch_conversion(tmp_path):
    sources_dir = tmp_path / "Source folder ünïcødé"
    good = write_docx(sources_dir / "Résumé – Zoë 履歴書.docx", "Synthetic integration test text")
    second = write_docx(sources_dir / "plain name.docx", "Second synthetic document")
    broken = sources_dir / "broken.docx"
    broken.write_bytes(b"not a zip file at all")
    # Passes our ZIP check, so it reaches LibreOffice, which must fail cleanly.
    not_word = sources_dir / "zip but not word.docx"
    with zipfile.ZipFile(not_word, "w") as archive:
        archive.writestr("hello.txt", "just a zip")
    output_dir = tmp_path / "Output folder"
    output_dir.mkdir()
    (output_dir / "plain name.pdf").write_bytes(b"pre-existing file that must survive")

    digests = {p: file_digest(p) for p in (good, second, broken, not_word)}
    listing = sorted(p.name for p in sources_dir.iterdir())

    with libreoffice_converter(SOFFICE, timeout=180) as converter:
        summary = ConversionService(converter).convert_batch(
            [good, broken, not_word, second], output_dir
        )

    by_name = {r.source.name: r for r in summary.results}
    for name in ("Résumé – Zoë 履歴書.docx", "plain name.docx"):
        result = by_name[name]
        assert result.status is FileStatus.SUCCEEDED, result.message
        assert result.output.stat().st_size > 0
        assert result.output.read_bytes().startswith(b"%PDF-")

    assert by_name["plain name.docx"].output.name == "plain name (1).pdf"
    assert (output_dir / "plain name.pdf").read_bytes() == b"pre-existing file that must survive"
    assert by_name["broken.docx"].status is FileStatus.FAILED
    assert by_name["zip but not word.docx"].status is FileStatus.FAILED
    # Depending on the LibreOffice version it exits non-zero or exits 0 without output.
    message = by_name["zip but not word.docx"].message
    assert "LibreOffice could not convert" in message or "did not produce a PDF" in message
    assert sorted(p.name for p in output_dir.iterdir()) == [
        "Résumé – Zoë 履歴書.pdf",
        "plain name (1).pdf",
        "plain name.pdf",
    ]

    # Sources untouched, and no LibreOffice lock files left next to them.
    assert {p: file_digest(p) for p in digests} == digests
    assert sorted(p.name for p in sources_dir.iterdir()) == listing


def _picture(picture_id: int, rel_id: str, mode: str = "link") -> str:
    return (
        "<w:p><w:r><w:drawing><wp:inline>"
        '<wp:extent cx="952500" cy="952500"/>'
        f'<wp:docPr id="{picture_id}" name="Picture {picture_id}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="p{picture_id}.png"/><pic:cNvPicPr/>'
        f'</pic:nvPicPr><pic:blipFill><a:blip r:{mode}="{rel_id}"/>'
        "<a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _write_docx_with_images(path, linked_targets):
    """A .docx with one embedded image plus images *linked* by TargetMode="External"."""
    rels = f'<Relationship Id="rId9" Type="{_IMAGE_REL}" Target="media/embedded.png"/>'
    rels += "".join(
        f'<Relationship Id="rId{n}" Type="{_IMAGE_REL}" Target="{target}" TargetMode="External"/>'
        for n, target in enumerate(linked_targets, start=10)
    )
    pictures = _picture(9, "rId9", mode="embed") + "".join(
        _picture(n, f"rId{n}") for n in range(10, 10 + len(linked_targets))
    )
    # Start from a valid minimal document, then swap in the body and its relationships.
    write_docx(path)
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b'<Default Extension="xml"',
        b'<Default Extension="png" ContentType="image/png"/><Default Extension="xml"',
    )
    parts["word/media/embedded.png"] = _png(_EMBEDDED_SIZE)
    parts["word/_rels/document.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships '
        f'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
        "</Relationships>"
    ).encode()
    parts["word/document.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>'
        f"<w:p><w:r><w:t>Linked images</w:t></w:r></w:p>{pictures}</w:body></w:document>"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path


def test_linked_images_are_neither_fetched_nor_embedded(tmp_path):
    """Regression test: without the hardened profile LibreOffice fetched the URL (a network
    request / tracking beacon) and embedded the linked local file into the PDF. Two documents
    share one profile, so the setting must also survive LibreOffice's first run."""
    requests: list[str] = []
    remote_png = _png(_REMOTE_LINK_SIZE)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(remote_png)))
            self.end_headers()
            self.wfile.write(remote_png)

        def log_message(self, *args):
            pass

    # Loopback only: stands in for a remote server without needing a network.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    local_image = tmp_path / "local-private-image.png"
    local_image.write_bytes(_png(_LOCAL_LINK_SIZE))
    targets = [f"http://127.0.0.1:{server.server_address[1]}/beacon.png", local_image.as_uri()]
    sources = [
        _write_docx_with_images(tmp_path / "in" / f"linked-{n}.docx", targets) for n in (1, 2)
    ]
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    try:
        with libreoffice_converter(SOFFICE, timeout=180) as converter:
            summary = ConversionService(converter).convert_batch(sources, output_dir)
    finally:
        server.shutdown()

    assert requests == []
    for result in summary.results:
        assert result.status is FileStatus.SUCCEEDED, result.message
        pdf = result.output.read_bytes()
        assert f"/Width {_EMBEDDED_SIZE}".encode() in pdf  # embedded image kept
        assert f"/Width {_LOCAL_LINK_SIZE}".encode() not in pdf  # linked local file left out
        assert f"/Width {_REMOTE_LINK_SIZE}".encode() not in pdf


def test_conversion_recovers_after_a_real_timeout(tmp_path, monkeypatch, request):
    source = write_docx(tmp_path / "in" / "doc.docx")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # Route the app's (and LibreOffice's) temporary folders somewhere we can inspect. Kept
    # short and under the real temp folder: LibreOffice 26.2.1 crashes (0xC0000409) when its
    # profile path is as deep as pytest's tmp_path.
    system_temp = Path(tempfile.mkdtemp(prefix="odt-it-"))
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    request.addfinalizer(lambda: shutil.rmtree(system_temp, ignore_errors=True))

    with libreoffice_converter(SOFFICE, timeout=0.5) as converter:
        service = ConversionService(converter)
        first = service.convert_batch([source], output_dir).results[0]
        assert list(system_temp.glob("offline-doc-tools-*/job-*")) == []  # no copy left
        converter.timeout = 180
        second = service.convert_batch([source], output_dir).results[0]

    assert first.status is FileStatus.FAILED
    assert "did not finish within 0.5 seconds" in first.message
    assert second.status is FileStatus.SUCCEEDED, second.message
    assert second.output.read_bytes().startswith(b"%PDF-")
    assert list(system_temp.iterdir()) == []  # workspaces and profiles all removed
