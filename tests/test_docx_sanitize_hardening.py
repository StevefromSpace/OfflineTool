"""Hardening tests for the sanitizer: content that is not a relationship, and resource limits.

LibreOffice 26.2.1 was probed with a loopback server (18 Oct 2026): it imports an ``altChunk``
HTML part whatever its relationship type is and fetches the images and style sheets in it, and it
embeds a local file named by an SVG picture, whatever the picture is called and also when the SVG
is gzip-compressed or sits beside a PNG fallback. The relationship rules alone do not stop either.

Every package is synthetic and built at test time. Nothing here needs LibreOffice or the network.
"""

from __future__ import annotations

import gzip
import io
import struct
import zipfile

import pytest

from offline_doc_tools import docx_sanitize
from offline_doc_tools.docx_inspect import scan_linked_images
from offline_doc_tools.docx_sanitize import strip_external_references
from offline_doc_tools.service import ConversionService, FileStatus
from test_docx_sanitize import (
    _CONTENT_TYPES,
    DOCUMENT_RELS,
    HYPERLINK,
    IMAGE,
    OFFICE_DOCUMENT,
    R_NS,
    R_STRICT,
    ROOT_RELS,
    W_NS,
    W_STRICT,
    WEB_URL,
    assert_refused,
    external,
    hyperlink_use,
    make_docx,
    picture_use,
    rel,
    rel_ids,
    rels_part,
    write_zip,
    xml_part,
)
from test_service import FakeConverter

AFCHUNK = f"{R_NS}/aFChunk"
CHUNK_HTML = b"<html><body><img src='http://x.invalid/a.png'></body></html>"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def svg(inner: str) -> bytes:
    return f"<svg {SVG_NS} width='10' height='10'>{inner}</svg>".encode()


def damaged_trailer(picture: bytes) -> bytes:
    """A valid gzip header and payload (so the content decodes fine) with a corrupted 8-byte
    trailer, the shape LibreOffice has been seen decoding despite Python's gzip rejecting it."""
    data = bytearray(gzip.compress(picture))
    data[-1] ^= 0xFF
    return bytes(data)


def untouched(path) -> None:
    before = path.read_bytes()
    assert strip_external_references(path) == 0
    assert path.read_bytes() == before


# --- an embedded HTML chunk (w:altChunk) -------------------------------------------------------


@pytest.mark.parametrize(
    "rel_type",
    [AFCHUNK, f"{R_STRICT}/aFChunk", IMAGE, HYPERLINK, "http://example.invalid/custom", ""],
)
@pytest.mark.parametrize("mode", [None, "Internal"])
def test_an_embedded_html_chunk_is_refused_whatever_its_relationship_type(tmp_path, rel_type, mode):
    path = make_docx(
        tmp_path,
        rels=[rel("rId50", rel_type, "afchunk.html", mode)],
        body='<w:altChunk r:id="rId50"/>',
        extra=[("word/afchunk.html", CHUNK_HTML)],
    )

    error = assert_refused(path, tmp_path)

    assert "embedded" in str(error)


@pytest.mark.parametrize(("w", "r"), [(W_NS, R_NS), (W_STRICT, R_STRICT)])
def test_the_chunk_element_is_found_in_either_namespace_and_in_other_parts(tmp_path, w, r):
    path = make_docx(
        tmp_path,
        extra=[
            ("word/header1.xml", xml_part("hdr", '<w:p><w:altChunk r:id="rId1"/></w:p>', w=w, r=r)),
            ("word/_rels/header1.xml.rels", rels_part(rel("rId1", AFCHUNK, "chunk.html"))),
            ("word/chunk.html", CHUNK_HTML),
        ],
    )

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "target",
    [
        "afchunk.html",
        "%61fchunk.html",
        "AFCHUNK.HTML",
        "./afchunk.html",
        "sub/../afchunk.html",
        "/word/afchunk.html",
        "..\\word\\afchunk.html",
        "afchunk.html#part",
        "afchunk.html?x=1",
        "  afchunk.html  ",
    ],
)
def test_a_chunk_is_found_however_its_relationship_spells_the_target(tmp_path, target):
    path = make_docx(
        tmp_path,
        rels=[rel("rId50", AFCHUNK, target)],
        body='<w:altChunk r:id="rId50"/>',
        extra=[("word/afchunk.html", CHUNK_HTML)],
    )

    assert_refused(path, tmp_path)


@pytest.mark.parametrize("hostile", ["chunk", "svg", "damaged-gzip"])
def test_a_refused_document_never_reaches_the_engine_and_the_batch_goes_on(tmp_path, hostile):
    bad = {
        "chunk": dict(
            rels=[rel("rId50", AFCHUNK, "afchunk.html")],
            body='<w:altChunk r:id="rId50"/>',
            extra=[("word/afchunk.html", CHUNK_HTML)],
        ),
        "svg": dict(extra=[("word/media/image1.svg", svg('<image href="file:///C:/a.png"/>'))]),
        "damaged-gzip": dict(
            extra=[("word/media/image1.svgz", damaged_trailer(svg("<rect/>")))],
        ),
    }[hostile]
    bad_path = make_docx(tmp_path / "bad", **bad)
    good_path = make_docx(tmp_path / "good")
    original = bad_path.read_bytes()
    converter = FakeConverter()
    out = tmp_path / "out"
    out.mkdir()

    summary = ConversionService(converter).convert_batch([bad_path, good_path], out)

    first, second = summary.results
    assert first.status is FileStatus.FAILED and "temporary folder" not in first.message.lower()
    assert second.status is FileStatus.SUCCEEDED
    assert len(converter.seen) == 1  # only the good document was handed to the engine
    assert bad_path.read_bytes() == original  # the user's file is untouched


@pytest.mark.parametrize(
    ("target", "member"),
    [
        ("../../chunk.html", "chunk.html"),  # more ".." than there are folders
        ("chunk%2Ehtml", "word/chunk%2Ehtml"),  # a member whose name really holds a "%"
        ("missing.html", "word/other.html"),  # a part that this spelling cannot reach
    ],
)
def test_a_chunk_is_refused_even_when_this_reading_of_the_target_finds_no_part(
    tmp_path, target, member
):
    # Another consumer may resolve the target to a member that this module's reading misses.
    path = make_docx(
        tmp_path,
        rels=[rel("rId50", AFCHUNK, target)],
        body='<w:altChunk r:id="rId50"/>',
        extra=[(member, CHUNK_HTML)],
    )

    assert_refused(path, tmp_path)


@pytest.mark.parametrize("newlines", [0, 5, 40])
def test_a_css_url_followed_by_a_lot_of_whitespace_is_still_found(tmp_path, newlines):
    style = "<style>.a{background:url(" + "\n" * newlines + "file:///C:/x.png)}</style>"
    path = make_docx(tmp_path, extra=[("word/media/image1.svg", svg(style))])

    assert_refused(path, tmp_path)


def test_a_chunk_relationship_that_nothing_uses_is_harmless(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId50", AFCHUNK, "afchunk.html")],
        extra=[("word/afchunk.html", CHUNK_HTML)],
    )

    untouched(path)


def test_an_external_chunk_relationship_is_removed_like_any_other(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId50", AFCHUNK, "http://x.invalid/chunk.html", "External")],
        body='<w:altChunk r:id="rId50"/>',
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == []


def test_a_part_that_a_relationship_points_at_must_be_readable(tmp_path):
    # Its relationships lead to a part that exists, so a chunk could be hiding in it.
    path = make_docx(
        tmp_path,
        rels=[rel("rId3", IMAGE, "media/a.png")],
        extra=[("word/media/a.png", b"png")],
    )
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i)) for i in archive.infolist()]
    write_zip(path, [(n, b"<broken" if n == "word/document.xml" else d) for n, d in members])

    assert_refused(path, tmp_path)


# --- SVG pictures that name files outside the document ------------------------------------------

EXTERNAL_SVGS = [
    pytest.param(svg('<image xlink:href="file:///C:/private/a.png"/>'), id="xlink-file"),
    pytest.param(svg('<image href="http://x.invalid/a.png"/>'), id="plain-href-http"),
    pytest.param(svg('<image xlink:href="  FILE:///C:/private/a.png"/>'), id="padded-upper-case"),
    pytest.param(svg('<use href="other.svg#a"/>'), id="use-another-file"),
    pytest.param(svg('<rect style="fill:url(file:///C:/a.png)"/>'), id="css-url-in-attribute"),
    pytest.param(svg("<style>@import url(http://x.invalid/a.css);</style>"), id="css-import"),
    pytest.param(svg("<style>.a{background:url('http://x.invalid/a.png')}</style>"), id="css-url"),
]
CONTAINERS = [
    pytest.param("word/media/image1.svg", False, id="svg"),
    pytest.param("word/media/image1.png", False, id="svg-named-png"),
    pytest.param("word/media/image1.svgz", True, id="gzip"),
    pytest.param("word/media/image1.png", True, id="gzip-named-png"),
]


@pytest.mark.parametrize("name, compressed", CONTAINERS)
@pytest.mark.parametrize("picture", EXTERNAL_SVGS)
def test_an_svg_that_names_outside_files_is_refused_whatever_it_is_called(
    tmp_path, picture, name, compressed
):
    data = gzip.compress(picture) if compressed else picture
    path = make_docx(
        tmp_path, rels=[rel("rId60", IMAGE, name.removeprefix("word/"))], extra=[(name, data)]
    )

    error = assert_refused(path, tmp_path)

    assert "SVG" in str(error)


def test_an_svg_that_starts_after_some_xml_comments_is_still_examined(tmp_path):
    picture = (
        b"<?xml version='1.0'?><!--" + b"x" * 3000 + b"-->" + svg('<image href="file:///a.png"/>')
    )
    path = make_docx(tmp_path, extra=[("word/media/image1.svg", picture)])

    assert_refused(path, tmp_path)


def test_an_svg_with_a_doctype_is_refused(tmp_path):
    picture = b'<!DOCTYPE svg SYSTEM "file:///C:/private/x.dtd">' + svg("<rect/>")
    path = make_docx(tmp_path, extra=[("word/media/image1.svg", picture)])

    assert_refused(path, tmp_path)


def test_a_damaged_trailer_after_the_sniff_window_is_refused_as_an_unreadable_document(tmp_path):
    # Longer than _SVG_SNIFF_BYTES, so the damage is only met while the whole picture is read.
    picture = svg('<image href="file:///C:/a.png"/><!--' + "x" * 20_000 + "-->")
    path = make_docx(tmp_path, extra=[("word/media/image1.svgz", damaged_trailer(picture))])

    error = assert_refused(path, tmp_path)

    assert "temporary folder" not in str(error).lower()


@pytest.mark.parametrize(
    "style",
    [
        "<style>@im&#112;ort 'http://x.invalid/a.css';</style>",
        "<style>.a{background:ur&#108;('http://x.invalid/a.png')}</style>",
    ],
)
def test_a_css_token_split_across_text_events_is_still_found(tmp_path, style):
    path = make_docx(tmp_path, extra=[("word/media/image1.svg", svg(style))])

    assert_refused(path, tmp_path)


def test_an_svg_longer_than_the_part_limit_is_refused_also_when_compressed(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_sanitize, "_MAX_PART_BYTES", 5000)
    picture = svg("<!--" + "x" * 20_000 + "-->")
    path = make_docx(tmp_path, extra=[("word/media/image1.png", gzip.compress(picture))])

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\x1f\x8b" + b"not really gzip", id="malformed-header"),
        pytest.param(
            damaged_trailer(svg('<image xlink:href="file:///C:/private/a.png"/>')),
            id="good-header-damaged-trailer",
        ),
    ],
)
def test_a_gzip_member_python_cannot_fully_read_is_refused_not_passed_through(tmp_path, data):
    """A gzip member Python's own gzip module cannot verify must not be treated as harmless: it
    is refused like any other package that cannot be fully judged, not passed on unexamined."""
    path = make_docx(tmp_path, extra=[("word/media/image1.emz", data)])

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "picture",
    [
        pytest.param(svg('<use href="#a"/><g id="a"><rect/></g>'), id="local-reference"),
        pytest.param(svg('<image xlink:href="data:image/png;base64,AAAA"/>'), id="data-uri"),
        pytest.param(svg('<rect fill="url(#g)" style="stroke:url( \'#g\' )"/>'), id="local-paint"),
        pytest.param(svg('<a xlink:href="http://x.invalid/"><rect/></a>'), id="plain-hyperlink"),
        pytest.param(svg("<style>.a{fill:red}</style>"), id="style-without-urls"),
    ],
)
@pytest.mark.parametrize("compressed", [False, True])
def test_an_svg_without_outside_references_is_left_alone(tmp_path, picture, compressed):
    data = gzip.compress(picture) if compressed else picture
    path = make_docx(
        tmp_path,
        rels=[rel("rId2", IMAGE, "media/embedded.png"), rel("rId60", IMAGE, "media/image1.svg")],
        extra=[("word/media/embedded.png", b"png"), ("word/media/image1.svg", data)],
    )

    untouched(path)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(gzip.compress(b"\x01\x00\x00\x00" * 100), id="gzip-of-a-metafile"),
        pytest.param(b"\x89PNG\r\n" + bytes(range(256)) * 20, id="binary-picture"),
        pytest.param(b"", id="empty"),
    ],
)
def test_members_that_are_not_svg_are_not_examined_further(tmp_path, data):
    path = make_docx(tmp_path, extra=[("word/media/image1.emz", data)])

    untouched(path)


# --- limits on the work a document can cause ----------------------------------------------------


def test_copies_of_a_relationships_file_do_not_make_the_owning_part_be_read_again(
    tmp_path, monkeypatch
):
    copies = 30
    members = [
        ("[Content_Types].xml", _CONTENT_TYPES),
        (ROOT_RELS, rels_part(rel("rId1", OFFICE_DOCUMENT, "word/document.xml"))),
        (
            "word/document.xml",
            xml_part("document", "".join(hyperlink_use(f"rId{n}") for n in range(copies))),
        ),
        *[
            (DOCUMENT_RELS, rels_part(rel(f"rId{n}", HYPERLINK, WEB_URL, "External")))
            for n in range(copies)
        ],
    ]
    path = tmp_path / "job-0" / "source.docx"
    with pytest.warns(UserWarning, match="Duplicate"):
        write_zip(path, members)
    opened = []
    original = zipfile.ZipFile.open

    def counting_open(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", counting_open)

    assert strip_external_references(path) == 0

    # Once to look at its start, once to read it: not once per copy of the relationships file.
    assert opened.count("word/document.xml") <= 2


def test_a_package_with_too_many_entries_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_sanitize, "_MAX_ENTRIES", 5)
    path = make_docx(tmp_path, extra=[(f"customXml/item{n}.xml", b"<a/>") for n in range(5)])

    assert_refused(path, tmp_path)


def test_xml_beyond_the_inspection_budget_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_sanitize, "_MAX_INSPECT_BYTES", 2000)
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External"), rel("rId6", IMAGE, "media/a.png")],
        body=hyperlink_use("rId5") + "<w:p/>" * 500,
        extra=[("word/media/a.png", b"png")],
    )

    assert_refused(path, tmp_path)


def test_a_part_that_turns_out_longer_than_its_header_says_is_stopped(tmp_path, monkeypatch):
    path = make_docx(
        tmp_path, rels=[rel("rId5", HYPERLINK, WEB_URL, "External")], body=hyperlink_use("rId5")
    )
    monkeypatch.setattr(docx_sanitize, "_MAX_PART_BYTES", 5000)
    long_but_valid = xml_part("document", hyperlink_use("rId5") + "<w:p/>" * 3000)
    original = zipfile.ZipFile.open

    def lying_open(self, name, *args, **kwargs):
        if getattr(name, "filename", name) == "word/document.xml":
            return io.BytesIO(long_but_valid)
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", lying_open)

    # Valid XML, so only the limit can stop it: the hyperlink would otherwise be kept.
    assert_refused(path, tmp_path)


def _mark_encrypted(path, member: str) -> None:
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    data[info.header_offset + 6] |= 0x01  # local header
    central = data.find(b"PK\x01\x02")
    while central != -1:
        if bytes(data[central + 46 : central + 46 + len(member)]) == member.encode():
            data[central + 8] |= 0x01  # central directory
            break
        central = data.find(b"PK\x01\x02", central + 4)
    path.write_bytes(bytes(data))


def test_an_encrypted_member_is_refused_even_when_nothing_needs_removing(tmp_path):
    path = make_docx(tmp_path, extra=[("word/media/secret.bin", b"data")])
    _mark_encrypted(path, "word/media/secret.bin")

    assert_refused(path, tmp_path)


# --- fidelity of a rewrite -----------------------------------------------------------------------


def test_a_rewrite_keeps_the_bytes_of_names_stored_as_utf_8_without_the_flag(tmp_path):
    name = "word/media/Bild-\u00e4\u00f6.png"
    path = make_docx(tmp_path, rels=[external("rId2")], extra=[(name, b"PNGDATA")])
    data = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # clear the UTF-8 flag
        position = data.find(signature)
        while position != -1:
            flags = struct.unpack_from("<H", data, position + offset)[0]
            struct.pack_into("<H", data, position + offset, flags & ~0x800)
            position = data.find(signature, position + 4)
    path.write_bytes(bytes(data))
    assert name.encode() in path.read_bytes()

    assert strip_external_references(path) == 1

    assert name.encode() in path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        assert archive.read(name) == b"PNGDATA"


# --- the linked-image scan -----------------------------------------------------------------------


def test_the_scan_counts_only_links_that_resolve_to_external_images(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[external("rId2"), rel("rId3", HYPERLINK, WEB_URL, "External")],
        body=picture_use('r:link="rId2"') + picture_use('r:link="rId3"'),
    )

    scan = scan_linked_images(path)

    assert (scan.omitted, scan.complete) == (1, True)
