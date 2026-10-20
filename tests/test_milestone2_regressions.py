"""Milestone 2 regression tests: the linked-image notice, reliability fixes and output safety.

Black-box tests against the milestone's contracts:

* C3  ``offline_doc_tools.docx_inspect.scan_linked_images`` counts the picture occurrences a
      document only *links* (they are left out of the PDF): the ten contract cases, the
      linked-shape-fill case, and a few clearly labelled extensions.
* C4  the results table says how many linked images were left out of a converted file.
* C5  a missing temporary folder is a plain ``ConversionError`` without paths; logs carry no
      file names, not even extensions; no expected failure reads "Unexpected error".
* Output safety: outputs are never overwritten, sources are never touched, the engine's private
      copy carries no external links except hyperlinks.

Everything is synthetic and offline. Documents are generated here, the engine is a fake, link
targets are never opened (the autouse fixture below records and blocks any network use), and new
modules are imported inside the tests, so a module that does not exist yet fails only the
tests that need it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import html
import logging
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET  # noqa: S405 (parses only synthetic test data)
import zipfile
import zlib
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import pytest

from conftest import FAKE_PDF_BYTES, file_digest, write_docx
from offline_doc_tools.engine import (
    ConversionError,
    ConversionFailedError,
    ConversionTimeoutError,
    InvalidInputError,
)

# --- constants ------------------------------------------------------------------------------

HTTP_LINK = "http://127.0.0.1:9/x.png"  # loopback "discard" port: nothing listens, never opened
HYPERLINK_TARGET = "http://127.0.0.1:9/page.html"
EMBEDDED_TARGET = "media/embedded.png"
EMBEDDED_SIZE = 2  # pixels; distinct sizes tell images apart in a real PDF

_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_REL = f"{_OFFICE_REL}/image"
HYPERLINK_REL = f"{_OFFICE_REL}/hyperlink"
HEADER_REL = f"{_OFFICE_REL}/header"
SETTINGS_REL = f"{_OFFICE_REL}/settings"
OLE_REL = f"{_OFFICE_REL}/oleObject"
TEMPLATE_REL = f"{_OFFICE_REL}/attachedTemplate"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_WML = "application/vnd.openxmlformats-officedocument.wordprocessingml"
_HEADER_OVERRIDE = f'<Override PartName="/word/header1.xml" ContentType="{_WML}.header+xml"/>'
_SETTINGS_OVERRIDE = f'<Override PartName="/word/settings.xml" ContentType="{_WML}.settings+xml"/>'
_NAMESPACES = " ".join(
    f'xmlns:{prefix}="{uri}"'
    for prefix, uri in {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "r": _OFFICE_REL,
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
        "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
        "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    }.items()
)

# Unique pieces of link targets. They must never show up in the UI, results or logs. (The
# window legitimately shows folder paths under tmp_path, so tests never assert on tmp_path.)
LINK_MARKERS = (
    "127.0.0.1:9",
    "Zebra-Beacon-q7",
    "SECRET-RECIPIENT-q7",
    "Okapi-Link-Target-q7",
    "Zebra-Fileserver-q7",
)


# --- a fake engine --------------------------------------------------------------------------


class _Relationship(NamedTuple):
    part: str
    type: str
    target: str
    external: bool


def _relationships(package: Path) -> list[_Relationship]:
    """Every relationship declared in the package's .rels parts (for what the engine was given)."""
    found: list[_Relationship] = []
    try:
        with zipfile.ZipFile(package) as archive:
            for name in archive.namelist():
                if not name.endswith(".rels"):
                    continue
                root = ET.fromstring(archive.read(name))  # noqa: S314 (synthetic test data)
                for element in root:
                    external = element.get("TargetMode") == "External"
                    found.append(
                        _Relationship(
                            name, element.get("Type", ""), element.get("Target", ""), external
                        )
                    )
    except (OSError, KeyError, zipfile.BadZipFile, zlib.error, ET.ParseError):
        pass
    return found


class FakeConverter:
    """A stand-in for LibreOffice that also records what it was handed.

    Writes ``<stem>.pdf`` into ``out_dir``, or raises for documents containing a failure
    marker. A private copy of the fake in tests/test_service.py, hardened so that an odd package
    (no word/document.xml, a damaged member) is a plain ConversionFailedError instead of a
    KeyError that the service would report as "Unexpected error". With ``echo=True`` the PDF
    carries the document's XML, so tests can tell which source produced which output.
    """

    def __init__(self, failures=None, pdf_bytes=FAKE_PDF_BYTES, echo=False):
        self.failures = failures or {}
        self.pdf_bytes = pdf_bytes
        self.echo = echo
        self.seen: list[Path] = []
        self.relationships: list[list[_Relationship]] = []
        self.package_names: list[list[str]] = []

    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        self.seen.append(source)
        try:
            with zipfile.ZipFile(source) as archive:
                self.package_names.append(archive.namelist())
                text = archive.read("word/document.xml").decode("utf-8", errors="replace")
        except (OSError, KeyError, zipfile.BadZipFile, zlib.error) as exc:
            raise ConversionFailedError("The document could not be read.") from exc
        self.relationships.append(_relationships(source))
        for marker, failure in self.failures.items():
            if marker in text:
                raise failure
        pdf = out_dir / f"{source.stem}.pdf"
        pdf.write_bytes(self.pdf_bytes + text.encode("utf-8") if self.echo else self.pdf_bytes)
        return pdf


# --- synthetic documents --------------------------------------------------------------------


def _png(size: int) -> bytes:
    """An opaque grey square PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\x00" + b"\x80\x80\x80" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _rel(rel_id: str, rel_type: str, target: str, *, external: bool = False) -> str:
    mode = ' TargetMode="External"' if external else ""
    escaped = html.escape(target, quote=True)
    return f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{escaped}"{mode}/>'


def _rels_part(rels: Sequence[str]) -> str:
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    return f'{_XML_DECL}<Relationships xmlns="{package_ns}">{"".join(rels)}</Relationships>'


def _picture(number: int, attribute: str, rel_id: str) -> str:
    """One inline DrawingML picture; ``attribute`` is ``embed`` or ``link``."""
    return (
        "<w:p><w:r><w:drawing><wp:inline>"
        '<wp:extent cx="952500" cy="952500"/>'
        f'<wp:docPr id="{number}" name="Picture {number}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="p{number}.png"/><pic:cNvPicPr/>'
        f'</pic:nvPicPr><pic:blipFill><a:blip r:{attribute}="{rel_id}"/>'
        "<a:stretch><a:fillRect/></a:stretch></pic:blipFill>"
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _shape_fill(number: int, rel_id: str, *, wrapped: bool = False) -> str:
    """A rectangle whose fill is the image linked as ``rel_id`` (wps:spPr/a:blipFill/a:blip).

    ``wrapped`` puts the drawing in ``mc:AlternateContent``, the way Word writes shapes.
    """
    drawing = (
        '<w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="1" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        '<wp:extent cx="952500" cy="952500"/><wp:wrapNone/>'
        f'<wp:docPr id="{number}" name="Shape {number}"/><wp:cNvGraphicFramePr/>'
        '<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
        'wordprocessingShape"><wps:wsp><wps:cNvSpPr/><wps:spPr>'
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:blipFill><a:blip r:link="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></a:blipFill>'
        "</wps:spPr><wps:bodyPr/></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing>"
    )
    if wrapped:
        drawing = (
            f'<mc:AlternateContent><mc:Choice Requires="wps">{drawing}</mc:Choice>'
            "<mc:Fallback><w:pict/></mc:Fallback></mc:AlternateContent>"
        )
    return f"<w:p><w:r>{drawing}</w:r></w:p>"


def _docx(
    path: Path,
    *,
    body: str = "",
    rels: Sequence[str] = (),
    parts: dict[str, str] | None = None,
    overrides: Sequence[str] = (),
    document_xml: str | bytes | None = None,
    text: str = "Synthetic test document",
) -> Path:
    """Write a complete .docx: content types, package relationships and the main part."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if document_xml is None:
        document_xml = (
            f"{_XML_DECL}<w:document {_NAMESPACES}><w:body>"
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>{body}</w:body></w:document>"
        )
    content_types = (
        f'{_XML_DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f'<Override PartName="/word/document.xml" ContentType="{_WML}.document.main+xml"/>'
        f"{''.join(overrides)}</Types>"
    )
    package_rels = _rels_part([_rel("rId1", f"{_OFFICE_REL}/officeDocument", "word/document.xml")])
    contents = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": package_rels,
        "word/document.xml": document_xml,
        "word/_rels/document.xml.rels": _rels_part(rels),
        **(parts or {}),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        if any(EMBEDDED_TARGET in rel for rel in rels):
            archive.writestr(f"word/{EMBEDDED_TARGET}", _png(EMBEDDED_SIZE))
    return path


def _linked_doc(
    path: Path, targets: Sequence[str], *, embedded: int = 0, text: str = "Linked images"
) -> Path:
    """A document with ``embedded`` embedded pictures and one *linked* picture per target."""
    rels = [_rel("rId9", IMAGE_REL, EMBEDDED_TARGET)] if embedded else []
    pictures = [_picture(number, "embed", "rId9") for number in range(1, embedded + 1)]
    for offset, target in enumerate(targets):
        rels.append(_rel(f"rId{10 + offset}", IMAGE_REL, target, external=True))
        pictures.append(_picture(100 + offset, "link", f"rId{10 + offset}"))
    return _docx(path, body="".join(pictures), rels=rels, text=text)


def _header_doc(
    path: Path, header_targets: Sequence[str], body_targets: Sequence[str] = ()
) -> Path:
    """Linked pictures in word/header1.xml, referenced once from the document's sectPr.

    ``body_targets`` adds linked pictures to the main part as well.
    """
    header_rels = [
        _rel(f"rId{n}", IMAGE_REL, target, external=True)
        for n, target in enumerate(header_targets, start=1)
    ]
    header_pictures = "".join(
        _picture(n, "link", f"rId{n}") for n in range(1, len(header_targets) + 1)
    )
    body_rels = [
        _rel(f"rId{30 + n}", IMAGE_REL, target, external=True)
        for n, target in enumerate(body_targets)
    ]
    body_pictures = "".join(
        _picture(50 + n, "link", f"rId{30 + n}") for n in range(len(body_targets))
    )
    return _docx(
        path,
        body=(
            f"{body_pictures}"
            '<w:sectPr><w:headerReference w:type="default" r:id="rId20"/></w:sectPr>'
        ),
        rels=[_rel("rId20", HEADER_REL, "header1.xml"), *body_rels],
        parts={
            "word/header1.xml": f"{_XML_DECL}<w:hdr {_NAMESPACES}>{header_pictures}</w:hdr>",
            "word/_rels/header1.xml.rels": _rels_part(header_rels),
        },
        overrides=[_HEADER_OVERRIDE],
    )


def _hyperlink_doc(path: Path) -> Path:
    link = '<w:p><w:hyperlink r:id="rId7"><w:r><w:t>a web page</w:t></w:r></w:hyperlink></w:p>'
    rels = [_rel("rId7", HYPERLINK_REL, HYPERLINK_TARGET, external=True)]
    return _docx(path, body=link, rels=rels)


def _unreferenced_link_doc(path: Path) -> Path:
    """An external image relationship that no picture uses."""
    return _docx(path, rels=[_rel("rId10", IMAGE_REL, HTTP_LINK, external=True)])


def _shape_fill_doc(path: Path, targets: Sequence[str], *, wrapped: bool = False) -> Path:
    rels = [
        _rel(f"rId{10 + n}", IMAGE_REL, target, external=True) for n, target in enumerate(targets)
    ]
    body = "".join(
        _shape_fill(200 + n, f"rId{10 + n}", wrapped=wrapped) for n in range(len(targets))
    )
    return _docx(path, body=body, rels=rels)


def _kitchen_sink_doc(path: Path, tmp_path: Path) -> Path:
    """Every kind of external link the sanitizer must strip, plus one hyperlink it must keep."""
    local_image = (tmp_path / "never-opened" / "picture.png").as_uri()
    body = (
        _picture(1, "embed", "rId9")
        + _picture(2, "link", "rId10")
        + _shape_fill(3, "rId11", wrapped=True)
        + '<w:p><w:hyperlink r:id="rId12"><w:r><w:t>a web page</w:t></w:r></w:hyperlink></w:p>'
        + '<w:sectPr><w:headerReference w:type="default" r:id="rId20"/></w:sectPr>'
    )
    rels = [
        _rel("rId9", IMAGE_REL, EMBEDDED_TARGET),
        _rel("rId10", IMAGE_REL, HTTP_LINK, external=True),
        _rel("rId11", IMAGE_REL, local_image, external=True),
        _rel("rId12", HYPERLINK_REL, HYPERLINK_TARGET, external=True),
        _rel("rId13", OLE_REL, "file:///C:/Zebra-Linked-Object.xlsx", external=True),
        _rel("rId20", HEADER_REL, "header1.xml"),
        _rel("rId30", SETTINGS_REL, "settings.xml"),
    ]
    parts = {
        "word/header1.xml": (
            f"{_XML_DECL}<w:hdr {_NAMESPACES}>{_picture(4, 'link', 'rId1')}</w:hdr>"
        ),
        "word/_rels/header1.xml.rels": _rels_part(
            [_rel("rId1", IMAGE_REL, "http://127.0.0.1:9/header.png", external=True)]
        ),
        "word/settings.xml": (
            f'{_XML_DECL}<w:settings {_NAMESPACES}><w:attachedTemplate r:id="rId1"/></w:settings>'
        ),
        "word/_rels/settings.xml.rels": _rels_part(
            [_rel("rId1", TEMPLATE_REL, "http://127.0.0.1:9/template.dotm", external=True)]
        ),
    }
    return _docx(
        path, body=body, rels=rels, parts=parts, overrides=[_HEADER_OVERRIDE, _SETTINGS_OVERRIDE]
    )


def _marked_link_targets(tmp_path: Path) -> list[str]:
    """Targets that each carry a unique marker (never created, never opened)."""
    return [
        "http://127.0.0.1:9/Zebra-Beacon-q7/pixel.png?recipient=SECRET-RECIPIENT-q7",
        (tmp_path / "Okapi-Link-Target-q7" / "private.png").as_uri(),
        "file://Zebra-Fileserver-q7/share/picture.png",
    ]


def _corrupt_member(package: Path, member: str, target: Path) -> Path:
    """Copy ``package`` to ``target`` with one byte flipped inside ``member``'s stored data."""
    with zipfile.ZipFile(package) as archive:
        info = archive.getinfo(member)
    data = bytearray(package.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", data, info.header_offset + 26)
    start = info.header_offset + 30 + name_length + extra_length
    data[start + info.compress_size // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    return target


# --- small helpers --------------------------------------------------------------------------


def _scan(path: Path):
    from offline_doc_tools.docx_inspect import scan_linked_images

    return scan_linked_images(path)


def _result(path: Path) -> tuple[int, bool]:
    scan = _scan(path)
    return scan.omitted, scan.complete


def _run_batch(sources, output_dir: Path, converter=None, **callbacks):
    from offline_doc_tools.service import ConversionService

    service = ConversionService(converter if converter is not None else FakeConverter())
    return service.convert_batch(list(sources), output_dir, **callbacks)


def _names(summary) -> list[str]:
    return [result.status.name for result in summary.results]


def _tree_state(folder: Path) -> dict[str, tuple[str, int] | None]:
    """Every name under ``folder`` with its content digest and modification time."""
    state: dict[str, tuple[str, int] | None] = {}
    for path in sorted(folder.rglob("*")):
        key = path.relative_to(folder).as_posix()
        state[key] = None if path.is_dir() else (file_digest(path), path.stat().st_mtime_ns)
    return state


def _count_pattern(count: int) -> re.Pattern[str]:
    return re.compile(rf"(?<!\d){count}(?!\d)")


def _leaked_locations(text: str, *locations: Path) -> list[str]:
    """Fragments of the given locations, the home folder or any drive path found in ``text``."""
    needles = []
    for location in (*locations, Path.home()):
        needles += [str(location), location.as_posix(), location.name]
    lowered = text.lower()
    leaked = [needle for needle in needles if len(needle) >= 4 and needle.lower() in lowered]
    if re.search(r"[A-Za-z]:[\\/]", text):
        leaked.append("<a drive-letter path>")
    return leaked


def _missing_temp_folder(tmp_path: Path, monkeypatch) -> Path:
    """Point the system temporary folder at a folder that does not exist."""
    missing = tmp_path / "Zebra-Person-q7" / "Missing-Temp-q7"
    monkeypatch.setattr(tempfile, "tempdir", str(missing))
    return missing


@pytest.fixture(autouse=True)
def _no_network_access(monkeypatch):
    """Record and block any attempt to open a connection or resolve a host name.

    Recording (not only raising) means a swallowed exception cannot hide an attempt.
    """
    attempts: list[str] = []

    def blocked(what: str):
        def refuse(*args, **kwargs):
            attempts.append(what)
            raise OSError(f"network access is not allowed in tests ({what})")

        return refuse

    monkeypatch.setattr(socket.socket, "connect", blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
    yield
    assert not attempts, f"the code under test used the network: {attempts}"


# --- Qt helpers (imported lazily) -------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _window(converter=None, executable: Path | None = None):
    """A MainWindow that converts with ``converter`` (a fake) or, given ``executable``, for real."""
    from offline_doc_tools.main_window import MainWindow

    if executable is not None:
        return MainWindow(find_executable=lambda: executable)

    @contextmanager
    def make_converter(_executable):
        yield converter if converter is not None else FakeConverter()

    return MainWindow(find_executable=lambda: Path("soffice"), make_converter=make_converter)


def _convert_in_window(sources, output_dir: Path, converter=None, **window_options):
    timeout = window_options.pop("timeout", 20.0)
    window = _window(converter, **window_options)
    window.add_files(sources)
    window.set_output_dir(output_dir)
    window.start_conversion()
    _wait_until(lambda: not window.is_converting(), timeout)
    return window


def _rows(window) -> list[tuple[str, str, str]]:
    table = window.results_table
    return [
        (table.item(row, 0).text(), table.item(row, 1).text(), table.item(row, 2).text())
        for row in range(table.rowCount())
    ]


def _all_ui_text(window) -> str:
    """Every text the window can show: labels, buttons, cells, list items and all tooltips."""
    from PySide6.QtWidgets import QWidget

    chunks = [window.windowTitle()]
    for widget in [window, *window.findChildren(QWidget)]:
        chunks += [widget.toolTip(), widget.statusTip(), widget.whatsThis()]
        text = getattr(widget, "text", None)
        if callable(text):
            chunks.append(text())
    table = window.results_table
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                chunks += [item.text(), item.toolTip(), item.statusTip()]
    for row in range(window.file_list.count()):
        item = window.file_list.item(row)
        chunks += [item.text(), item.toolTip()]
    return "\n".join(chunk for chunk in chunks if chunk)


# =============================================================================================
# C3 - scan_linked_images
# =============================================================================================


def test_linked_image_scan_is_a_frozen_dataclass_of_omitted_and_complete():
    from offline_doc_tools.docx_inspect import LinkedImageScan

    assert dataclasses.is_dataclass(LinkedImageScan)
    assert [field.name for field in dataclasses.fields(LinkedImageScan)] == ["omitted", "complete"]
    scan = LinkedImageScan(omitted=2, complete=True)
    assert scan == LinkedImageScan(2, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scan.omitted = 3  # type: ignore[misc]


@pytest.mark.parametrize("kind", ["http", "file"])
def test_scan_counts_a_linked_picture(tmp_path, kind):  # contract cases 1 and 2
    target = HTTP_LINK if kind == "http" else (tmp_path / "never-opened" / "p.png").as_uri()
    source = _linked_doc(tmp_path / "in" / "Linked – Zoë 履歴書.docx", [target])
    assert _result(source) == (1, True)


def test_scan_ignores_embedded_pictures(tmp_path):  # contract case 3
    source = _linked_doc(tmp_path / "in" / "embedded.docx", [], embedded=2)
    assert _result(source) == (0, True)


def test_scan_counts_only_the_linked_pictures_of_a_mixed_document(tmp_path):
    targets = [HTTP_LINK, (tmp_path / "gone" / "p.png").as_uri(), "http://127.0.0.1:9/c.png"]
    source = _linked_doc(tmp_path / "in" / "mixed.docx", targets, embedded=2)
    assert _result(source) == (3, True)


def test_scan_counts_a_linked_picture_in_a_header_part(tmp_path):  # contract case 4
    source = _header_doc(tmp_path / "in" / "header.docx", [HTTP_LINK])
    assert _result(source) == (1, True)


def test_scan_adds_up_the_main_part_and_the_header_part(tmp_path):
    # The sum of contract cases "body" and "header".
    source = _header_doc(
        tmp_path / "in" / "both.docx",
        [HTTP_LINK],
        body_targets=[(tmp_path / "gone" / "p.png").as_uri()],
    )
    assert _result(source) == (2, True)


def test_scan_of_a_document_without_images(tmp_path):  # contract case 5
    # conftest's minimal document has no relationships part at all.
    source = write_docx(tmp_path / "in" / "plain.docx")
    assert _result(source) == (0, True)


@pytest.mark.parametrize(
    "payload",
    [b"this is plain text, not a Word document", bytes(range(256)) * 8, b""],
    ids=["plain-text", "binary-garbage", "empty-file"],
)
def test_scan_of_a_file_that_is_no_package_is_incomplete_not_an_error(tmp_path, payload):
    # Contract case 6. No assertion on ``omitted``: there is nothing to count.
    source = tmp_path / "in" / "damaged.docx"
    source.parent.mkdir()
    source.write_bytes(payload)
    assert _scan(source).complete is False


def test_scan_ignores_an_external_image_relationship_that_nothing_uses(tmp_path):  # case 7
    source = _unreferenced_link_doc(tmp_path / "in" / "unused.docx")
    assert _result(source) == (0, True)


def test_scan_counts_picture_occurrences_not_relationships(tmp_path):  # contract case 8
    rels = [_rel("rId10", IMAGE_REL, HTTP_LINK, external=True)]
    body = _picture(1, "link", "rId10") + _picture(2, "link", "rId10")
    source = _docx(tmp_path / "in" / "shared.docx", body=body, rels=rels)
    assert _result(source) == (2, True)


def test_scan_ignores_external_hyperlinks(tmp_path):  # contract case 9
    source = _hyperlink_doc(tmp_path / "in" / "hyperlink.docx")
    assert _result(source) == (0, True)


@pytest.mark.parametrize("kind", ["linked", "header", "shape-fill", "damaged", "empty"])
def test_scan_leaves_the_source_and_its_folder_untouched(tmp_path, kind):  # contract case 10
    folder = tmp_path / "in"
    if kind == "linked":
        source = _linked_doc(folder / "d.docx", [HTTP_LINK], embedded=1)
    elif kind == "header":
        source = _header_doc(folder / "d.docx", [HTTP_LINK])
    elif kind == "shape-fill":
        source = _shape_fill_doc(folder / "d.docx", [HTTP_LINK], wrapped=True)
    else:
        source = folder / "d.docx"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"garbage" if kind == "damaged" else b"")
    (folder / "neighbour.txt").write_text("another file in the same folder")
    before = _tree_state(folder)

    _scan(source)

    assert _tree_state(folder) == before


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain-drawing", "alternate-content"])
def test_scan_counts_a_linked_image_used_as_a_shape_fill(tmp_path, wrapped):
    source = _shape_fill_doc(tmp_path / "in" / "fill.docx", [HTTP_LINK], wrapped=wrapped)
    assert _result(source) == (1, True)


def test_scan_counts_pictures_and_shape_fills_together(tmp_path):
    rels = [
        _rel("rId9", IMAGE_REL, EMBEDDED_TARGET),
        _rel("rId10", IMAGE_REL, HTTP_LINK, external=True),
        _rel("rId11", IMAGE_REL, (tmp_path / "gone" / "fill.png").as_uri(), external=True),
    ]
    body = (
        _picture(1, "embed", "rId9")
        + _picture(2, "link", "rId10")
        + _shape_fill(3, "rId11", wrapped=True)
    )
    source = _docx(tmp_path / "in" / "pictures-and-fills.docx", body=body, rels=rels)
    assert _result(source) == (2, True)


# --- extensions beyond the numbered cases (same definition of "linked image") -----------------


def test_extension_scan_follows_namespaces_not_prefixes(tmp_path):
    # Producers other than Word choose their own prefixes; only the namespaces are fixed.
    document = (
        f"{_XML_DECL}"
        '<x:document xmlns:x="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        f'xmlns:q="{_OFFICE_REL}" '
        'xmlns:d="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:v="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:p="http://schemas.openxmlformats.org/drawingml/2006/picture"><x:body><x:p><x:r>'
        "<x:drawing><v:inline><d:graphic><d:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><p:pic><p:blipFill>'
        '<d:blip q:link="rId10"/></p:blipFill></p:pic></d:graphicData></d:graphic>'
        "</v:inline></x:drawing></x:r></x:p></x:body></x:document>"
    )
    rels = [_rel("rId10", IMAGE_REL, HTTP_LINK, external=True)]
    source = _docx(tmp_path / "in" / "prefixes.docx", rels=rels, document_xml=document)
    assert _result(source) == (1, True)


def test_extension_scan_needs_an_image_relationship_type(tmp_path):
    # The definition says "an image relationship type": a picture whose link resolves to an
    # external *hyperlink* relationship is not a linked image.
    rels = [_rel("rId10", HYPERLINK_REL, HYPERLINK_TARGET, external=True)]
    source = _docx(tmp_path / "in" / "odd.docx", body=_picture(1, "link", "rId10"), rels=rels)
    assert _result(source) == (0, True)


@pytest.mark.parametrize(
    "kind", ["truncated-zip", "corrupt-member", "not-well-formed-xml", "invalid-utf8"]
)
def test_extension_scan_of_a_damaged_package_is_incomplete_not_an_error(tmp_path, kind):
    folder = tmp_path / "in"
    good = _linked_doc(folder / "good.docx", [HTTP_LINK])
    damaged = folder / "damaged.docx"
    if kind == "truncated-zip":
        damaged.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    elif kind == "corrupt-member":
        _corrupt_member(good, "word/document.xml", damaged)
    else:
        # An external image relationship exists, so the main part must be read to count; a
        # scanner that cannot read it cannot call its answer complete.
        rels = [_rel("rId10", IMAGE_REL, HTTP_LINK, external=True)]
        if kind == "not-well-formed-xml":
            document = "<w:document><w:body><w:p>"
        else:
            document = b'<?xml version="1.0"?><w:document>\xff\xfe</w:document>'
        _docx(damaged, document_xml=document, rels=rels)

    assert _scan(damaged).complete is False


def test_extension_scan_of_a_zip_that_is_no_word_document_does_not_raise(tmp_path):
    source = tmp_path / "in" / "zip-but-not-word.docx"
    source.parent.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("hello.txt", "just a zip")
    _scan(source)


# =============================================================================================
# Service level: the notice must never change what a conversion does
# =============================================================================================


def test_omitted_linked_images_do_not_change_a_successful_conversion(tmp_path, output_dir):
    plain = write_docx(tmp_path / "in" / "Plain-Notes.docx", "PLAIN")
    linked = _linked_doc(
        tmp_path / "in" / "Linked-Notes.docx",
        [HTTP_LINK, (tmp_path / "gone" / "p.png").as_uri()],
        embedded=1,
    )

    summary = _run_batch([plain, linked], output_dir)

    assert _names(summary) == ["SUCCEEDED", "SUCCEEDED"]
    plain_result, linked_result = summary.results
    assert linked_result.output == output_dir / "Linked-Notes.pdf"
    assert linked_result.output.read_bytes() == FAKE_PDF_BYTES
    assert linked_result.renamed is False
    assert plain_result.renamed is False
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.cancelled is False


@pytest.mark.parametrize("kind", ["not-well-formed-xml", "invalid-utf8"])
def test_a_document_the_scan_cannot_read_still_converts(tmp_path, output_dir, kind):
    # Scanning is advisory: trouble reading a part must not turn a conversion into a failure.
    if kind == "not-well-formed-xml":
        document = "<w:document><w:body><w:p>unfinished"
    else:
        document = b'<?xml version="1.0"?><w:document>\xff\xfe</w:document>'
    source = _docx(tmp_path / "in" / "Hard-To-Scan.docx", document_xml=document)

    summary = _run_batch([source], output_dir)

    assert _names(summary) == ["SUCCEEDED"], summary.results[0].message
    assert summary.results[0].output.read_bytes() == FAKE_PDF_BYTES


def test_a_malformed_relationships_part_never_reads_as_an_unexpected_error(tmp_path, output_dir):
    broken = _docx(tmp_path / "in" / "broken-rels.docx", text="BROKEN")
    with zipfile.ZipFile(broken) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["word/_rels/document.xml.rels"] = b"<Relationships><Relationship Id="
    with zipfile.ZipFile(broken, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    good = write_docx(tmp_path / "in" / "good.docx", "GOOD")
    before = _tree_state(broken.parent)

    summary = _run_batch([broken, good], output_dir)

    # Whether such a file converts or is refused is the implementation's call; being reported
    # as an "Unexpected error", or stopping the batch, is not.
    assert not summary.results[0].message.startswith("Unexpected error")
    assert _names(summary)[1] == "SUCCEEDED"
    assert _tree_state(broken.parent) == before


def test_the_engine_only_ever_sees_hyperlinks_as_external_relationships(tmp_path, output_dir):
    """The private copy handed to the engine has every non-hyperlink external relationship
    removed (so nothing can be fetched or embedded), keeps hyperlinks and embedded images, and
    the source stays byte-identical."""
    source = _kitchen_sink_doc(tmp_path / "in" / "Sanitize-Me.docx", tmp_path)
    before = _tree_state(source.parent)
    in_source = [rel for rel in _relationships(source) if rel.external]
    assert len(in_source) == 6  # the fixture really contains what the sanitizer must remove
    converter = FakeConverter()

    summary = _run_batch([source], output_dir, converter)

    assert _names(summary) == ["SUCCEEDED"], summary.results[0].message
    [seen] = converter.relationships
    external = [(rel.type, rel.target) for rel in seen if rel.external]
    assert external == [(HYPERLINK_REL, HYPERLINK_TARGET)]
    assert any(rel.type == IMAGE_REL and rel.target == EMBEDDED_TARGET for rel in seen)
    assert f"word/{EMBEDDED_TARGET}" in converter.package_names[0]
    assert _tree_state(source.parent) == before


def test_link_targets_never_reach_the_results_or_the_logs(tmp_path, output_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")
    targets = _marked_link_targets(tmp_path)
    source = _linked_doc(tmp_path / "in" / "Marked-Links.docx", targets, embedded=1)

    summary = _run_batch([source], output_dir)

    assert _names(summary) == ["SUCCEEDED"]
    assert caplog.records, "expected the batch to be logged"
    shown = repr(summary) + "\n" + caplog.text
    for marker in LINK_MARKERS:
        assert marker not in shown


# =============================================================================================
# C4 - the results table reports linked images that were left out
# =============================================================================================


@pytest.mark.parametrize("count", [1, 3])
def test_details_say_how_many_linked_images_were_left_out(qapp, tmp_path, output_dir, count):
    targets = [HTTP_LINK, (tmp_path / "gone" / "p.png").as_uri(), "http://127.0.0.1:9/b.png"]
    source = _linked_doc(tmp_path / "in" / "Holiday-Photos.docx", targets[:count], embedded=1)

    window = _convert_in_window([source], output_dir)

    [(name, result, details)] = _rows(window)
    assert name == "Holiday-Photos.docx"
    assert result == "Converted"
    assert "linked image" in details.lower()
    assert _count_pattern(count).search(details), details
    assert "Holiday-Photos.pdf" in details  # the usual "Saved as ..." information stays
    assert (output_dir / "Holiday-Photos.pdf").is_file()


def test_details_count_linked_shape_fills(qapp, tmp_path, output_dir):
    targets = [HTTP_LINK, (tmp_path / "gone" / "fill.png").as_uri()]
    source = _shape_fill_doc(tmp_path / "in" / "Banner-Shapes.docx", targets, wrapped=True)

    window = _convert_in_window([source], output_dir)

    [(_, result, details)] = _rows(window)
    assert result == "Converted"
    assert "linked image" in details.lower()
    assert _count_pattern(2).search(details), details


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda path: write_docx(path, "PLAIN"), id="no-images"),
        pytest.param(lambda path: _linked_doc(path, [], embedded=2), id="embedded-only"),
        pytest.param(_unreferenced_link_doc, id="unreferenced-external-image"),
        pytest.param(_hyperlink_doc, id="external-hyperlink"),
    ],
)
def test_details_carry_no_notice_when_nothing_was_left_out(qapp, tmp_path, output_dir, build):
    source = build(tmp_path / "in" / "Nothing-Missing.docx")

    window = _convert_in_window([source], output_dir)

    [(_, result, details)] = _rows(window)
    assert result == "Converted"
    assert details.startswith("Saved as")
    assert "linked image" not in details.lower()
    assert "linked image" not in _all_ui_text(window).lower()


def test_each_row_reports_its_own_count_in_a_mixed_batch(qapp, tmp_path, output_dir):
    targets = [HTTP_LINK, (tmp_path / "gone" / "p.png").as_uri(), "http://127.0.0.1:9/b.png"]
    first = _linked_doc(tmp_path / "in" / "First-Linked.docx", targets, embedded=1)
    second = _linked_doc(tmp_path / "in" / "Second-Embedded.docx", [], embedded=2)
    third = _linked_doc(tmp_path / "in" / "Third-Failing.docx", targets[:1], text="FAIL")
    converter = FakeConverter(failures={"FAIL": ConversionFailedError("Damaged document.")})

    window = _convert_in_window([first, second, third], output_dir, converter)

    rows = _rows(window)
    assert [row[1] for row in rows] == ["Converted", "Converted", "Failed"]
    assert "linked image" in rows[0][2].lower()
    assert _count_pattern(3).search(rows[0][2]), rows[0][2]
    assert "linked image" not in rows[1][2].lower()
    assert "Damaged document." in rows[2][2]
    assert "2 converted" in window.summary_label.text()
    assert "1 failed" in window.summary_label.text()


def test_the_notice_does_not_replace_the_renamed_output_information(qapp, tmp_path, output_dir):
    (output_dir / "Collision-Doc.pdf").write_bytes(b"precious existing file")
    source = _linked_doc(tmp_path / "in" / "Collision-Doc.docx", [HTTP_LINK] * 3, embedded=1)

    window = _convert_in_window([source], output_dir)

    [(_, result, details)] = _rows(window)
    assert result == "Converted"
    assert "Collision-Doc (1).pdf" in details
    assert "linked image" in details.lower()
    assert _count_pattern(3).search(details), details
    assert (output_dir / "Collision-Doc.pdf").read_bytes() == b"precious existing file"


def test_link_targets_never_appear_in_the_window_or_the_logs(qapp, tmp_path, output_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")
    targets = _marked_link_targets(tmp_path)
    linked = _linked_doc(tmp_path / "in" / "Marked-Pictures.docx", targets, embedded=1)
    fills = _shape_fill_doc(tmp_path / "in" / "Marked-Fills.docx", targets, wrapped=True)

    window = _convert_in_window([linked, fills], output_dir)

    rows = _rows(window)
    assert [row[1] for row in rows] == ["Converted", "Converted"]
    assert all("linked image" in row[2].lower() for row in rows)
    assert caplog.records, "expected the batch to be logged"
    shown = _all_ui_text(window) + "\n" + caplog.text
    for marker in LINK_MARKERS:
        assert marker not in shown


# =============================================================================================
# C5 - reliability
# =============================================================================================


def test_missing_temp_folder_is_a_conversion_error_that_names_the_temporary_folder(
    make_docx, output_dir, tmp_path, monkeypatch
):
    source = make_docx("a.docx")
    missing = _missing_temp_folder(tmp_path, monkeypatch)
    converter = FakeConverter()

    with pytest.raises(ConversionError) as caught:
        _run_batch([source], output_dir, converter)

    message = str(caught.value)
    assert "temporary folder" in message.lower()
    assert not message.startswith("Unexpected error")
    assert _leaked_locations(message, missing, tmp_path) == []
    assert converter.seen == []
    assert list(output_dir.iterdir()) == []


def test_missing_temp_folder_leaves_no_path_in_the_logs(
    make_docx, output_dir, tmp_path, monkeypatch, caplog
):
    source = make_docx("a.docx")
    missing = _missing_temp_folder(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")

    with contextlib.suppress(Exception):  # the exception type has its own test
        _run_batch([source], output_dir)

    assert _leaked_locations(caplog.text, missing, tmp_path) == []


def test_entering_the_libreoffice_converter_without_a_temp_folder_is_a_conversion_error(
    tmp_path, monkeypatch
):
    from offline_doc_tools.libreoffice import libreoffice_converter

    missing = _missing_temp_folder(tmp_path, monkeypatch)

    with pytest.raises(ConversionError) as caught, libreoffice_converter(Path("soffice.exe")):
        pytest.fail("the converter must not be usable without a temporary folder")

    message = str(caught.value)
    assert "temporary folder" in message.lower()
    assert not message.startswith("Unexpected error")
    assert _leaked_locations(message, missing, tmp_path) == []


def test_libreoffice_converter_without_a_temp_folder_leaves_no_path_in_the_logs(
    tmp_path, monkeypatch, caplog
):
    from offline_doc_tools.libreoffice import libreoffice_converter

    missing = _missing_temp_folder(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")

    with contextlib.suppress(Exception), libreoffice_converter(Path("soffice.exe")):
        pass

    assert _leaked_locations(caplog.text, missing, tmp_path) == []


@pytest.mark.parametrize("failing_step", ["batch-workspace", "converter-setup"])
def test_window_explains_a_missing_temporary_folder_plainly(
    qapp, make_docx, output_dir, tmp_path, monkeypatch, caplog, failing_step
):
    from offline_doc_tools.main_window import MainWindow

    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")
    if failing_step == "batch-workspace":
        window = _window()  # fake engine: only the service's own workspace fails
    else:
        # The real libreoffice_converter fails on entering, before any LibreOffice would start.
        window = MainWindow(find_executable=lambda: Path("soffice.exe"))
    window.add_files([make_docx("a.docx")])
    window.set_output_dir(output_dir)
    missing = _missing_temp_folder(tmp_path, monkeypatch)

    window.start_conversion()
    _wait_until(lambda: not window.is_converting())

    text = window.summary_label.text()
    assert "could not start" in text
    assert "temporary folder" in text.lower()
    assert "unexpected error" not in text.lower()
    assert _leaked_locations(text, missing, tmp_path) == []
    assert _leaked_locations(caplog.text, missing, tmp_path) == []


@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("Zebra-Secret-Name.txt", ".txt"),
        ("Okapi-Old-Report.doc", ".doc"),
        ("Lynx-Sheet.xlsx", ".xlsx"),
    ],
)
def test_logs_hold_neither_the_name_nor_the_extension_of_an_unsupported_file(
    tmp_path, output_dir, caplog, name, extension
):
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")
    unsupported = tmp_path / "in" / name
    unsupported.parent.mkdir()
    unsupported.write_bytes(b"plain text, not a Word document")
    good = write_docx(tmp_path / "in" / "Plain-Good.docx")

    summary = _run_batch([unsupported, good], output_dir)

    result = summary.results[0]
    assert result.status.name == "FAILED"
    assert "unsupported" in result.message.lower()  # the person still gets the explanation
    assert _names(summary) == ["FAILED", "SUCCEEDED"]
    assert caplog.records, "expected the batch to be logged"
    assert Path(name).stem not in caplog.text
    # ".doc" must not trip over the legitimate ".docx" of a generic hint.
    assert not re.search(re.escape(extension) + r"(?![A-Za-z0-9])", caplog.text, re.IGNORECASE)


def _raw_file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "in" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _doc(tmp_path: Path, name: str, text: str = "Synthetic test document") -> Path:
    return write_docx(tmp_path / "in" / name, text)


def _scn_missing_source(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [tmp_path / "in" / "gone.docx"], {}


def _scn_folder_named_like_a_document(tmp_path, output_dir, monkeypatch):
    folder = tmp_path / "in" / "folder.docx"
    folder.mkdir(parents=True)
    return FakeConverter(), [folder], {}


def _scn_unsupported_type(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [_raw_file(tmp_path, "notes.txt", b"plain")], {}


def _scn_no_extension(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [_raw_file(tmp_path, "README", b"plain")], {}


def _scn_empty_document(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [_raw_file(tmp_path, "empty.docx", b"")], {}


def _scn_not_a_zip(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [_raw_file(tmp_path, "fake.docx", b"not a Word document")], {}


def _scn_word_lock_file(tmp_path, output_dir, monkeypatch):
    return FakeConverter(), [_raw_file(tmp_path, "~$report.docx", b"\x05owner")], {}


def _scn_engine_failed(tmp_path, output_dir, monkeypatch):
    converter = FakeConverter(failures={"FAIL": ConversionFailedError("Damaged document.")})
    return converter, [_doc(tmp_path, "a.docx", "FAIL")], {}


def _scn_engine_timeout(tmp_path, output_dir, monkeypatch):
    failure = ConversionTimeoutError("Conversion did not finish within 120 seconds.")
    return FakeConverter(failures={"SLOW": failure}), [_doc(tmp_path, "a.docx", "SLOW")], {}


def _scn_engine_rejected_input(tmp_path, output_dir, monkeypatch):
    failure = InvalidInputError("The engine cannot use this document.")
    return FakeConverter(failures={"BAD": failure}), [_doc(tmp_path, "a.docx", "BAD")], {}


def _scn_engine_wrote_an_empty_pdf(tmp_path, output_dir, monkeypatch):
    return FakeConverter(pdf_bytes=b""), [_doc(tmp_path, "a.docx")], {}


def _scn_engine_wrote_no_pdf_at_all(tmp_path, output_dir, monkeypatch):
    return FakeConverter(pdf_bytes=b"<html>not a pdf</html>"), [_doc(tmp_path, "a.docx")], {}


class _ProducesNothing:
    def convert_to_pdf(self, source: Path, out_dir: Path) -> Path:
        return out_dir / "never-written.pdf"


def _scn_engine_produced_nothing(tmp_path, output_dir, monkeypatch):
    return _ProducesNothing(), [_doc(tmp_path, "a.docx")], {}


def _scn_source_cannot_be_copied(tmp_path, output_dir, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfile", deny)
    return FakeConverter(), [_doc(tmp_path, "a.docx")], {}


def _scn_disk_full_while_placing_the_pdf(tmp_path, output_dir, monkeypatch):
    def broken_copy(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("offline_doc_tools.service.shutil.copyfileobj", broken_copy)
    return FakeConverter(), [_doc(tmp_path, "a.docx")], {}


def _scn_output_folder_removed_mid_batch(tmp_path, output_dir, monkeypatch):
    def remove_output_dir(index, total, result):
        if index == 0:
            for child in output_dir.iterdir():
                child.unlink()
            output_dir.rmdir()

    sources = [_doc(tmp_path, "a.docx"), _doc(tmp_path, "b.docx")]
    return FakeConverter(), sources, {"on_file_finished": remove_output_dir}


def _adapter(tmp_path: Path, runner):
    from offline_doc_tools.libreoffice import LibreOfficeConverter

    return LibreOfficeConverter(
        Path("soffice.exe"), profile_root=tmp_path / "profile", runner=runner
    )


def _scn_libreoffice_cannot_start(tmp_path, output_dir, monkeypatch):
    def runner(args, timeout, env=None):
        raise OSError(2, "The system cannot find the file specified")

    return _adapter(tmp_path, runner), [_doc(tmp_path, "a.docx")], {}


def _scn_libreoffice_timeout(tmp_path, output_dir, monkeypatch):
    def runner(args, timeout, env=None):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=5)

    return _adapter(tmp_path, runner), [_doc(tmp_path, "a.docx")], {}


def _scn_libreoffice_exit_code(tmp_path, output_dir, monkeypatch):
    from offline_doc_tools.libreoffice import ProcessResult

    def runner(args, timeout, env=None):
        return ProcessResult(1, b"", b"Error: source file could not be loaded\n")

    return _adapter(tmp_path, runner), [_doc(tmp_path, "a.docx")], {}


def _scn_libreoffice_exit_zero_without_pdf(tmp_path, output_dir, monkeypatch):
    from offline_doc_tools.libreoffice import ProcessResult

    def runner(args, timeout, env=None):
        return ProcessResult(0, b"", b"")

    return _adapter(tmp_path, runner), [_doc(tmp_path, "a.docx")], {}


_FAILURE_SCENARIOS = [
    _scn_missing_source,
    _scn_folder_named_like_a_document,
    _scn_unsupported_type,
    _scn_no_extension,
    _scn_empty_document,
    _scn_not_a_zip,
    _scn_word_lock_file,
    _scn_engine_failed,
    _scn_engine_timeout,
    _scn_engine_rejected_input,
    _scn_engine_wrote_an_empty_pdf,
    _scn_engine_wrote_no_pdf_at_all,
    _scn_engine_produced_nothing,
    _scn_source_cannot_be_copied,
    _scn_disk_full_while_placing_the_pdf,
    _scn_output_folder_removed_mid_batch,
    _scn_libreoffice_cannot_start,
    _scn_libreoffice_timeout,
    _scn_libreoffice_exit_code,
    _scn_libreoffice_exit_zero_without_pdf,
]


@pytest.mark.parametrize(
    "scenario", _FAILURE_SCENARIOS, ids=lambda scenario: scenario.__name__.removeprefix("_scn_")
)
def test_expected_failures_never_read_as_unexpected_errors(
    tmp_path, output_dir, monkeypatch, caplog, scenario
):
    caplog.set_level(logging.DEBUG, logger="offline_doc_tools")
    converter, sources, callbacks = scenario(tmp_path, output_dir, monkeypatch)

    summary = _run_batch(sources, output_dir, converter, **callbacks)

    failed = [result for result in summary.results if result.status.name == "FAILED"]
    assert failed, "the scenario was meant to produce a failed file"
    for result in failed:
        assert result.message.strip()
        assert not result.message.startswith("Unexpected error"), result.message
    assert not [record for record in caplog.records if record.exc_info], (
        "an expected failure logged a traceback"
    )


# =============================================================================================
# Output safety
# =============================================================================================

_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{number}" for device in ("COM", "LPT") for number in range(10)}
)


def _is_device_name(file_name: str) -> bool:
    """Would Windows read ``file_name`` as a device (``CON``, ``CON.txt``, ``aux .pdf``)?"""
    return file_name.partition(".")[0].rstrip(" ").upper() in _DEVICE_NAMES


def _can_create(folder: Path, name: str) -> bool:
    """True if ``name`` is an ordinary file name on this system.

    Some Windows versions resolve ``CON.docx`` to the console device instead of a file. The
    exclusive create writes nothing, so probing such a name is harmless.
    """
    folder.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError:
        return False
    os.close(descriptor)
    ordinary = name in {entry.name for entry in folder.iterdir()}
    if ordinary:
        (folder / name).unlink()
    return ordinary


def _with_stem(real: Path, stem: str) -> Path:
    """``real`` itself, reporting ``stem`` as its stem. Only used where the OS cannot have a
    file with that name; the service names its output after ``source.stem`` alone."""
    alias = type("_StemAlias", (type(real),), {"stem": property(lambda _self: stem)})
    return alias(real)


def _source_with_stem(tmp_path: Path, stem: str, text: str) -> Path:
    folder = tmp_path / "in"
    if _can_create(folder, f"{stem}.docx"):
        return write_docx(folder / f"{stem}.docx", text)
    return _with_stem(write_docx(folder / "device-name-stand-in.docx", text), stem)


def test_batch_never_overwrites_existing_outputs_even_for_case_variants(tmp_path, output_dir):
    sources = [
        write_docx(tmp_path / "in" / "a" / "Report.docx", "MARK-ALPHA"),
        write_docx(tmp_path / "in" / "b" / "REPORT.docx", "MARK-BRAVO"),
        write_docx(tmp_path / "in" / "c" / "report.docx", "MARK-CHARLIE"),
    ]
    existing = {
        output_dir / "report.pdf": b"EXISTING-LOWER-CASE",
        output_dir / "Report (1).pdf": b"EXISTING-NUMBERED",
    }
    for path, data in existing.items():
        path.write_bytes(data)

    summary = _run_batch(sources, output_dir, FakeConverter(echo=True))

    assert _names(summary) == ["SUCCEEDED"] * 3
    for path, data in existing.items():
        assert path.read_bytes() == data
    new_files = [path for path in output_dir.iterdir() if path not in existing]
    assert len(new_files) == 3
    contents = [path.read_bytes() for path in new_files]
    markers = (b"MARK-ALPHA", b"MARK-BRAVO", b"MARK-CHARLIE")
    for marker in markers:
        assert sum(marker in data for data in contents) == 1, marker
    for result, marker in zip(summary.results, markers, strict=True):
        assert marker in result.output.read_bytes()


def test_a_second_batch_never_overwrites_the_outputs_of_the_first(tmp_path, output_dir):
    source = write_docx(tmp_path / "in" / "Minutes.docx", "MARK-FIRST")
    first = _run_batch([source], output_dir, FakeConverter(echo=True)).results[0]
    first_bytes = first.output.read_bytes()

    write_docx(source, "MARK-SECOND")  # same name, new content
    second = _run_batch([source], output_dir, FakeConverter(echo=True)).results[0]

    assert second.status.name == "SUCCEEDED"
    assert second.output != first.output
    assert second.renamed is True
    assert first.output.read_bytes() == first_bytes
    assert b"MARK-FIRST" in first_bytes
    assert b"MARK-SECOND" in second.output.read_bytes()
    assert len(list(output_dir.iterdir())) == 2


@pytest.mark.parametrize("stem", ["CON", "con", "Nul", "COM1", "lpt9", "AUX.report"])
def test_device_named_sources_get_safe_output_names_and_never_clobber_each_other(
    tmp_path, output_dir, stem
):
    source = _source_with_stem(tmp_path, stem, "MARK-DEVICE")
    converter = FakeConverter(echo=True)

    first = _run_batch([source], output_dir, converter).results[0]
    first_bytes = first.output.read_bytes()
    second = _run_batch([source], output_dir, converter).results[0]

    for result in (first, second):
        assert result.status.name == "SUCCEEDED", result.message
        assert not _is_device_name(result.output.name), result.output.name
        assert result.output.is_file()
        assert b"MARK-DEVICE" in result.output.read_bytes()
    assert first.output.name.lower() != second.output.name.lower()
    assert first.output.read_bytes() == first_bytes
    assert sorted(entry.name for entry in output_dir.iterdir()) == sorted(
        [first.output.name, second.output.name]
    )


def test_sources_and_their_folder_are_untouched_by_a_batch(tmp_path, output_dir):
    folder = tmp_path / "in"
    sources = [
        write_docx(folder / "good.docx", "GOOD"),
        _linked_doc(
            folder / "linked.docx", [HTTP_LINK, (tmp_path / "x" / "y.png").as_uri()], embedded=1
        ),
        _shape_fill_doc(folder / "fills.docx", [HTTP_LINK], wrapped=True),
        _header_doc(folder / "header.docx", [HTTP_LINK]),
        _kitchen_sink_doc(folder / "kitchen-sink.docx", tmp_path),
        write_docx(folder / "bad.docx", "FAIL-ME"),
        _raw_file(tmp_path, "notes.txt", b"plain"),
        _raw_file(tmp_path, "empty.docx", b""),
        _raw_file(tmp_path, "garbage.docx", b"not a zip"),
        _raw_file(tmp_path, "~$lock.docx", b"\x05owner"),
    ]
    before = _tree_state(folder)
    converter = FakeConverter(failures={"FAIL-ME": ConversionFailedError("boom")})

    summary = _run_batch(sources, output_dir, converter)

    assert len(summary.results) == len(sources)
    assert summary.succeeded == 5
    assert _tree_state(folder) == before


def test_sources_and_their_folder_are_untouched_by_scanning_them(tmp_path):
    folder = tmp_path / "in"
    sources = [
        write_docx(folder / "good.docx", "GOOD"),
        _linked_doc(folder / "linked.docx", [HTTP_LINK], embedded=1),
        _shape_fill_doc(folder / "fills.docx", [HTTP_LINK], wrapped=True),
        _header_doc(folder / "header.docx", [HTTP_LINK]),
        _kitchen_sink_doc(folder / "kitchen-sink.docx", tmp_path),
        _raw_file(tmp_path, "notes.txt", b"plain"),
        _raw_file(tmp_path, "empty.docx", b""),
        _raw_file(tmp_path, "garbage.docx", b"not a zip"),
    ]
    before = _tree_state(folder)

    for source in sources:
        _scan(source)

    assert _tree_state(folder) == before


# =============================================================================================
# One real-LibreOffice test: the notice agrees with what the real engine leaves out
# =============================================================================================


@pytest.mark.integration
def test_notice_matches_what_the_real_libreoffice_leaves_out(qapp, tmp_path, output_dir):
    """The number the window reports equals the pictures and shape fills missing from the real
    PDF. Only the image stored in the document may appear in it."""
    from offline_doc_tools.libreoffice import find_soffice

    soffice = find_soffice()
    if soffice is None:
        pytest.skip("LibreOffice is not installed")

    picture_file, fill_file = tmp_path / "linked-picture.png", tmp_path / "linked-fill.png"
    picture_file.write_bytes(_png(3))
    fill_file.write_bytes(_png(4))
    rels = [
        _rel("rId9", IMAGE_REL, EMBEDDED_TARGET),
        _rel("rId10", IMAGE_REL, picture_file.as_uri(), external=True),
        _rel("rId11", IMAGE_REL, fill_file.as_uri(), external=True),
        _rel("rId12", IMAGE_REL, HTTP_LINK, external=True),  # loopback port 9: nothing listens
    ]
    body = (
        _picture(1, "embed", "rId9")
        + _picture(2, "link", "rId10")
        + _shape_fill(3, "rId11", wrapped=True)
        + _picture(4, "link", "rId12")
    )
    source = _docx(
        tmp_path / "in" / "Real-Engine-Check.docx", body=body, rels=rels, text="Real engine check"
    )
    before = _tree_state(source.parent)

    window = _convert_in_window([source], output_dir, executable=soffice, timeout=240.0)

    [(_, result, details)] = _rows(window)
    assert result == "Converted", details
    assert "linked image" in details.lower()
    assert _count_pattern(3).search(details), details
    pdf = (output_dir / "Real-Engine-Check.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-")
    assert f"/Width {EMBEDDED_SIZE}".encode() in pdf  # the embedded image is kept
    assert b"/Width 3" not in pdf  # the linked picture is left out
    assert b"/Width 4" not in pdf  # the linked shape fill is left out
    assert _scan(source).omitted == 3
    assert _tree_state(source.parent) == before
