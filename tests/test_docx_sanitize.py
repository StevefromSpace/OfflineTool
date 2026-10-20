"""Unit tests for ``strip_external_references``: the sanitizer that runs on the private copy.

Every package is synthetic and built at test time. Nothing here needs LibreOffice, the network
or a file outside ``tmp_path``.
"""

from __future__ import annotations

import errno
import functools
import io
import logging
import os
import random
import socket
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from offline_doc_tools import docx_sanitize
from offline_doc_tools.docx_sanitize import strip_external_references
from offline_doc_tools.engine import ConversionFailedError, TemporaryFolderError

PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
R_STRICT = "http://purl.oclc.org/ooxml/officeDocument/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
A_STRICT = "http://purl.oclc.org/ooxml/drawingml/main"

IMAGE = f"{R_NS}/image"
HYPERLINK = f"{R_NS}/hyperlink"
HYPERLINK_STRICT = f"{R_STRICT}/hyperlink"
OFFICE_DOCUMENT = f"{R_NS}/officeDocument"
HEADER = f"{R_NS}/header"
SETTINGS = f"{R_NS}/settings"
NUMBERING = f"{R_NS}/numbering"
ATTACHED_TEMPLATE = f"{R_NS}/attachedTemplate"
OLE_OBJECT = f"{R_NS}/oleObject"

DOCUMENT_RELS = "word/_rels/document.xml.rels"
ROOT_RELS = "_rels/.rels"
XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
FIXED_TIME = (2021, 5, 17, 13, 24, 36)
WEB_URL = "https://example.invalid/page"

_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" '
    b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/></Types>'
)


# --- builders -------------------------------------------------------------------------------


def rel(rel_id: str, rel_type: str, target: str, mode: str | None = None) -> str:
    mode_attribute = "" if mode is None else f" TargetMode={quoteattr(mode)}"
    return (
        f"<Relationship Id={quoteattr(rel_id)} Type={quoteattr(rel_type)} "
        f"Target={quoteattr(target)}{mode_attribute}/>"
    )


def rels_part(*rels: str, ns: str = PKG_NS) -> bytes:
    return (
        f'{XML_DECLARATION.decode()}\n<Relationships xmlns="{ns}">{"".join(rels)}</Relationships>'
    ).encode()


def xml_part(root: str, body: str = "", *, w: str = W_NS, r: str = R_NS, a: str = A_NS) -> bytes:
    return (
        f"{XML_DECLARATION.decode()}"
        f'<w:{root} xmlns:w="{w}" xmlns:r="{r}" xmlns:a="{a}" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f"{body}</w:{root}>"
    ).encode()


def write_zip(path: Path, members: Iterable[tuple[str, bytes]]) -> Path:
    """Write a package whose members differ in compression, attributes and (for directories)
    kind, so that a rewrite that normalises any of them is noticed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index, (name, data) in enumerate(members):
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED if index % 2 else zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 << 16) if index % 3 else 0
            if name.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
                # Python 3.14.5's mkdir() leaves these unset for a ZipInfo.
                info.CRC = info.compress_size = info.file_size = 0
                archive.mkdir(info)
            else:
                wanted = info.external_attr
                archive.writestr(info, data)
                info.external_attr = wanted  # writestr() turns a 0 into 0o600 << 16
    return path


def make_docx(
    tmp_path: Path,
    *,
    rels: Iterable[str] = (),
    body: str = "",
    root_rels: Iterable[str] = (),
    extra: Iterable[tuple[str, bytes]] = (),
    name: str = "job-0/source.docx",
) -> Path:
    members = [
        ("[Content_Types].xml", _CONTENT_TYPES),
        (ROOT_RELS, rels_part(rel("rId1", OFFICE_DOCUMENT, "word/document.xml"), *root_rels)),
        ("word/document.xml", xml_part("document", body)),
        (DOCUMENT_RELS, rels_part(*rels)),
        *extra,
    ]
    return write_zip(tmp_path / name, members)


def part_with_rels(
    part_name: str, body: str, *rels: str, root: str = "hdr"
) -> list[tuple[str, bytes]]:
    folder, _, leaf = part_name.rpartition("/")
    return [
        (part_name, xml_part(root, body)),
        (f"{folder}/_rels/{leaf}.rels", rels_part(*rels)),
    ]


def hyperlink_use(rel_id: str) -> str:
    return f'<w:p><w:hyperlink r:id="{rel_id}"><w:r><w:t>a link</w:t></w:r></w:hyperlink></w:p>'


def picture_use(attribute: str) -> str:
    return (
        "<w:p><w:r><w:drawing><a:graphic><a:graphicData><wps:wsp><wps:spPr><a:blipFill>"
        f"<a:blip {attribute}/>"
        "</a:blipFill></wps:spPr></wps:wsp></a:graphicData></a:graphic></w:drawing></w:r></w:p>"
    )


def external(rel_id: str, rel_type: str = IMAGE, target: str = "http://example.invalid/a.png"):
    return rel(rel_id, rel_type, target, "External")


# --- readers --------------------------------------------------------------------------------


def parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)  # noqa: S314 - synthetic test data


def relationships(path: Path, name: str = DOCUMENT_RELS):
    with zipfile.ZipFile(path) as archive:
        root = parse(archive.read(name))
    return [(e.get("Id"), e.get("Type"), e.get("Target"), e.get("TargetMode")) for e in root]


def rel_ids(path: Path, name: str = DOCUMENT_RELS) -> list[str]:
    return [item[0] for item in relationships(path, name)]


def member_summary(path: Path):
    with zipfile.ZipFile(path) as archive:
        return [
            (
                i.filename,
                i.date_time,
                i.compress_type,
                i.external_attr,
                i.create_system,
                i.comment,
                i.is_dir(),
            )
            for i in archive.infolist()
        ]


def member_bytes(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        return [(i.filename, b"" if i.is_dir() else archive.read(i)) for i in archive.infolist()]


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def tree_state(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def assert_plain_refusal(error: BaseException, path: Path, tmp_path: Path) -> None:
    message = str(error)
    assert "external" in message.lower()
    for private in (str(path), path.name, str(tmp_path), tmp_path.name, Path.home().name):
        assert private not in message


def assert_refused(path: Path, tmp_path: Path) -> ConversionFailedError:
    """The package is refused, with a path-free message, and nothing is changed or left behind."""
    before = path.read_bytes()
    folder_before = listing(path.parent)
    with pytest.raises(ConversionFailedError) as caught:
        strip_external_references(path)
    assert not isinstance(caught.value, TemporaryFolderError)
    assert_plain_refusal(caught.value, path, tmp_path)
    assert path.read_bytes() == before
    assert listing(path.parent) == folder_before
    return caught.value


# --- external relationships are removed -----------------------------------------------------


def test_an_external_relationship_is_removed_and_the_embedded_one_kept(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId1", IMAGE, "media/image1.png"), external("rId2")],
    )

    assert strip_external_references(path) == 1

    assert relationships(path) == [("rId1", IMAGE, "media/image1.png", None)]


@pytest.mark.parametrize(
    "rel_type",
    [
        IMAGE,
        OLE_OBJECT,
        ATTACHED_TEMPLATE,
        f"{R_NS}/frame",
        f"{R_NS}/subDocument",
        f"{R_NS}/externalLink",
        f"{R_STRICT}/image",
        "http://example.invalid/some/custom/type",
        "",
    ],
)
def test_external_relationships_of_every_type_are_removed(tmp_path, rel_type):
    path = make_docx(tmp_path, rels=[external("rId2", rel_type)], body=picture_use('r:link="rId2"'))

    assert strip_external_references(path) == 1

    assert relationships(path) == []


def test_an_external_relationship_without_a_type_or_target_is_removed(tmp_path):
    path = make_docx(
        tmp_path,
        rels=['<Relationship Id="rId2" TargetMode="External"/>', '<Relationship Id="rId3"/>'],
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId3"]


@pytest.mark.parametrize("mode", ["External", "external", "EXTERNAL", " External "])
def test_target_mode_is_matched_ignoring_case_and_padding(tmp_path, mode):
    # A local-looking target proves it is the mode, not the target, that makes it external.
    path = make_docx(tmp_path, rels=[rel("rId2", IMAGE, "media/image1.png", mode)])

    assert strip_external_references(path) == 1

    assert relationships(path) == []


NON_LOCAL_TARGETS = [
    "http://example.invalid/a.png",
    "HTTPS://example.invalid/a.png",
    "ftp://example.invalid/a.png",
    "file:///C:/private/a.png",
    "file://server/share/a.png",
    "C:\\private\\a.png",
    "C:/private/a.png",
    "c:a.png",
    "\\\\server\\share\\a.png",
    "//server/share/a.png",
    "/\\server/share/a.png",
    "\\/server/share/a.png",
    " http://example.invalid/a.png",
    "\thttp://example.invalid/a.png",
    "\nfile:///C:/private/a.png",
    "ht\ttp://example.invalid/a.png",
    "mailto:someone@example.invalid",
    "a+b-c.d:rest",
]


@pytest.mark.parametrize("mode", [None, "Internal", "bogus"])
@pytest.mark.parametrize("target", NON_LOCAL_TARGETS)
def test_non_local_targets_are_removed_whatever_the_target_mode(tmp_path, target, mode):
    path = make_docx(
        tmp_path,
        rels=[rel("rId1", IMAGE, "media/image1.png"), rel("rId2", IMAGE, target, mode)],
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId1"]


LOCAL_TARGETS = [
    "media/image1.png",
    "../media/image1.png",
    "/word/media/image1.png",
    "\\word\\media\\image1.png",
    "#bookmark",
    "header1.xml",
    "media/my picture.png",
    "media/a:b.png",
    "",
]


@pytest.mark.parametrize("mode", [None, "Internal"])
@pytest.mark.parametrize("target", LOCAL_TARGETS)
def test_local_targets_are_left_alone(tmp_path, target, mode):
    path = make_docx(tmp_path, rels=[rel("rId2", IMAGE, target, mode)])
    before = path.read_bytes()

    assert strip_external_references(path) == 0

    assert path.read_bytes() == before


def test_the_count_adds_up_across_every_part(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[external("rId2"), external("rId3"), rel("rId4", IMAGE, "media/image1.png")],
        root_rels=[external("rId9", f"{R_NS}/thumbnail")],
        extra=part_with_rels("word/header1.xml", "", external("rId1"), external("rId2")),
    )

    assert strip_external_references(path) == 5

    assert rel_ids(path) == ["rId4"]
    assert rel_ids(path, "word/_rels/header1.xml.rels") == []
    assert rel_ids(path, ROOT_RELS) == ["rId1"]


def test_duplicate_ids_or_missing_ids_cannot_keep_a_hyperlink(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[
            rel("rId5", HYPERLINK, WEB_URL, "External"),
            rel("rId5", HYPERLINK, WEB_URL, "External"),
            rel(" rId6", HYPERLINK, WEB_URL, "External"),
            rel("rId6 ", HYPERLINK, WEB_URL, "External"),
            f'<Relationship Type="{HYPERLINK}" Target="{WEB_URL}" TargetMode="External"/>',
        ],
        body=hyperlink_use("rId5") + hyperlink_use("rId6"),
    )

    assert strip_external_references(path) == 5

    assert relationships(path) == []


# --- hyperlinks: the single exception --------------------------------------------------------


@pytest.mark.parametrize("hyperlink_type", [HYPERLINK, HYPERLINK_STRICT])
@pytest.mark.parametrize(
    ("w", "r", "a"),
    [(W_NS, R_NS, A_NS), (W_STRICT, R_STRICT, A_STRICT), (W_STRICT, R_NS, A_STRICT)],
)
@pytest.mark.parametrize(
    "use",
    [
        '<w:hyperlink r:id="rId5"><w:r><w:t>x</w:t></w:r></w:hyperlink>',
        '<wp:docPr id="1" name="x"><a:hlinkClick r:id="rId5"/></wp:docPr>',
        '<wp:docPr id="1" name="x"><a:hlinkHover r:id="rId5"/></wp:docPr>',
        '<w:hyperlink r:id="rId5"/><a:hlinkClick r:id="rId5"/><a:hlinkHover r:id="rId5"/>',
    ],
)
def test_a_hyperlink_that_is_only_used_as_a_hyperlink_is_kept(
    tmp_path, hyperlink_type, w, r, a, use
):
    path = tmp_path / "job-0" / "source.docx"
    write_zip(
        path,
        [
            ("[Content_Types].xml", _CONTENT_TYPES),
            (ROOT_RELS, rels_part(rel("rId1", OFFICE_DOCUMENT, "word/document.xml"))),
            ("word/document.xml", xml_part("document", use, w=w, r=r, a=a)),
            (DOCUMENT_RELS, rels_part(rel("rId5", hyperlink_type, WEB_URL, "External"))),
        ],
    )
    before = path.read_bytes()

    assert strip_external_references(path) == 0

    assert path.read_bytes() == before


def test_a_kept_hyperlink_survives_while_other_relationships_are_removed(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[
            rel("rId5", HYPERLINK, WEB_URL, "External"),
            external("rId6"),
            rel("rId7", HYPERLINK, "file:///C:/private/doc.docx", "External"),
        ],
        body=hyperlink_use("rId5") + hyperlink_use("rId7") + picture_use('r:link="rId6"'),
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId5", "rId7"]


@pytest.mark.parametrize(
    "use",
    [
        picture_use('r:link="rId5"'),
        picture_use('r:embed="rId5"'),
        picture_use('r:embed="rId1" r:link="rId5"'),
        '<w:pict><v:shape><v:imagedata r:id="rId5"/></v:shape></w:pict>',
        '<w:pict><v:shape><v:imagedata r:href="rId5"/></v:shape></w:pict>',
        '<w:pict><v:rect><v:fill r:id="rId5" type="frame"/></v:rect></w:pict>',
        '<w:pict><v:shape><v:imagedata r:pict="rId5"/></v:shape></w:pict>',
        '<w:object><o:OLEObject r:id="rId5"/></w:object>',
        '<w:footerReference w:type="default" r:id="rId5"/>',
        '<w:hyperlink r:embed="rId5"/>',
        '<w:hyperlink r:link="rId5"/>',
        '<a:hlinkMouseOver r:id="rId5"/>',
        '<a:hlinkClick r:link="rId5"/>',
        '<x:hyperlink xmlns:x="urn:example:other" r:id="rId5"/>',
        '<w:hyperlink xmlns:q="urn:example:other" q:id="rId5"/>',
        '<w:hyperlink xmlns:q="urn:example:other" r:id="rId1" q:id="rId5"/>',
        # Beyond the brief, chosen to err towards removal: an Id in an attribute of any other
        # namespace (VML's o:relid) or of none is a reference too.
        '<v:imagedata o:relid="rId5"/>',
        '<w:hyperlink w:anchor="rId5"/>',
        '<w:something plain="rId5"/>',
        # Padding does not hide a reference.
        '<a:blip r:link=" rId5 "/>',
        '<a:blip r:link="\trId5\n"/>',
    ],
)
def test_a_hyperlink_with_any_other_use_is_removed(tmp_path, use):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External")],
        body=use,
    )

    assert strip_external_references(path) == 1

    assert relationships(path) == []


@pytest.mark.parametrize(
    "body",
    [
        hyperlink_use("rId5") + picture_use('r:link="rId5"'),
        picture_use('r:link="rId5"') + hyperlink_use("rId5"),
        '<w:hyperlink r:id="rId5"/><a:blip r:embed="rId5"/><a:hlinkClick r:id="rId5"/>',
    ],
)
def test_a_hyperlink_referenced_both_ways_is_removed(tmp_path, body):
    path = make_docx(tmp_path, rels=[rel("rId5", HYPERLINK, WEB_URL, "External")], body=body)

    assert strip_external_references(path) == 1

    assert relationships(path) == []


def test_another_relationship_that_is_used_as_a_picture_does_not_stop_a_hyperlink_being_kept(
    tmp_path,
):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External"), external("rId6")],
        body=hyperlink_use("rId5") + picture_use('r:link="rId6"'),
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId5"]


@pytest.mark.parametrize("body", ["", hyperlink_use("rId4"), picture_use('r:link="rId4"')])
def test_a_hyperlink_that_nothing_references_is_removed(tmp_path, body):
    path = make_docx(tmp_path, rels=[rel("rId5", HYPERLINK, WEB_URL, "External")], body=body)

    assert strip_external_references(path) == 1

    assert relationships(path) == []


@pytest.mark.parametrize(
    "rel_type",
    [
        IMAGE,
        OLE_OBJECT,
        ATTACHED_TEMPLATE,
        f"{R_NS}/Hyperlink",
        f"{HYPERLINK}/",
        f"{HYPERLINK}x",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlinks",
    ],
)
def test_only_a_relationship_of_exactly_the_hyperlink_type_is_kept(tmp_path, rel_type):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", rel_type, WEB_URL, "External")],
        body=hyperlink_use("rId5"),
    )

    assert strip_external_references(path) == 1

    assert relationships(path) == []


def test_a_hyperlink_with_an_internal_mode_url_target_follows_the_same_rule(tmp_path):
    # The exception does not look at TargetMode: a hyperlink-typed relationship that is only
    # used as a hyperlink stays, an unused one goes.
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "Internal"), rel("rId6", HYPERLINK, WEB_URL)],
        body=hyperlink_use("rId5"),
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId5"]


def test_hyperlinks_are_checked_against_the_part_that_owns_the_relationships(tmp_path):
    # rId1 is a hyperlink in document.xml, but it is the header's relationship that is external.
    path = make_docx(
        tmp_path,
        rels=[rel("rId1", HYPERLINK, WEB_URL, "External")],
        body=hyperlink_use("rId1"),
        extra=part_with_rels(
            "word/header1.xml",
            picture_use('r:link="rId1"'),
            rel("rId1", HYPERLINK, WEB_URL, "External"),
        ),
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path) == ["rId1"]
    assert rel_ids(path, "word/_rels/header1.xml.rels") == []


def test_a_hyperlink_in_a_header_is_kept_when_the_header_uses_it_as_one(tmp_path):
    path = make_docx(
        tmp_path,
        extra=part_with_rels(
            "word/header1.xml",
            hyperlink_use("rId1") + hyperlink_use("rId2"),
            rel("rId1", HYPERLINK, WEB_URL, "External"),
            external("rId2", HYPERLINK, "file:///C:/x.docx"),
            external("rId3"),
        ),
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path, "word/_rels/header1.xml.rels") == ["rId1", "rId2"]


def test_a_hyperlink_is_removed_when_the_owning_part_is_missing(tmp_path):
    path = make_docx(
        tmp_path,
        body=hyperlink_use("rId1"),
        extra=[
            ("word/_rels/header9.xml.rels", rels_part(rel("rId1", HYPERLINK, WEB_URL, "External")))
        ],
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path, "word/_rels/header9.xml.rels") == []


@pytest.mark.parametrize(
    "rels_name",
    [
        "word/header1.xml.rels",  # not in a _rels folder
        "word/_rels/.rels",  # a part-less relationships file anywhere but the root
        "word/sub/_rels/.rels",
        "header1.xml.rels",
        "_rels/_rels/.rels",
    ],
)
def test_relationships_files_without_an_owning_part_lose_their_hyperlinks_too(tmp_path, rels_name):
    path = make_docx(
        tmp_path,
        body=hyperlink_use("rId1"),
        extra=[
            ("word/header1.xml", xml_part("hdr", hyperlink_use("rId1"))),
            ("header1.xml", xml_part("hdr", hyperlink_use("rId1"))),
            (rels_name, rels_part(rel("rId1", HYPERLINK, WEB_URL, "External"), external("rId2"))),
        ],
    )

    assert strip_external_references(path) == 2

    assert rel_ids(path, rels_name) == []


def test_the_owning_part_is_found_whatever_the_case_of_the_names(tmp_path):
    path = make_docx(
        tmp_path,
        extra=[
            ("Word/Header1.XML", xml_part("hdr", hyperlink_use("rId1"))),
            (
                "word/_RELS/header1.xml.RELS",
                rels_part(rel("rId1", HYPERLINK, WEB_URL, "External"), external("rId2")),
            ),
        ],
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path, "word/_RELS/header1.xml.RELS") == ["rId1"]


def test_a_reference_in_any_duplicate_of_the_owning_part_counts(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External")],
        body=hyperlink_use("rId5"),
    )
    with zipfile.ZipFile(path, "a") as archive, pytest.warns(UserWarning, match="Duplicate"):
        archive.writestr("word/document.xml", xml_part("document", picture_use('r:link="rId5"')))

    assert strip_external_references(path) == 1

    assert relationships(path) == []


def test_every_duplicate_of_a_relationships_file_is_inspected(tmp_path):
    path = make_docx(tmp_path, rels=[rel("rId1", IMAGE, "media/image1.png")])
    with zipfile.ZipFile(path, "a") as archive, pytest.warns(UserWarning, match="Duplicate"):
        archive.writestr(DOCUMENT_RELS, rels_part(external("rId2")))

    assert strip_external_references(path) == 1

    with zipfile.ZipFile(path) as archive:
        contents = [archive.read(i) for i in archive.infolist() if i.filename == DOCUMENT_RELS]
    assert len(contents) == 2
    assert [len(parse(c)) for c in contents] == [1, 0]


# --- the other kinds of relationships files ---------------------------------------------------


def test_root_relationships_lose_every_external_one_even_a_used_hyperlink(tmp_path):
    path = make_docx(
        tmp_path,
        body=hyperlink_use("rId9"),
        root_rels=[
            external("rId8", f"{R_NS}/thumbnail"),
            rel("rId9", HYPERLINK, WEB_URL, "External"),
            rel("rId10", IMAGE, "docProps/thumbnail.jpeg"),
        ],
    )

    assert strip_external_references(path) == 2

    assert rel_ids(path, ROOT_RELS) == ["rId1", "rId10"]


@pytest.mark.parametrize(
    ("owner", "rels_name", "owner_root"),
    [
        ("word/header1.xml", "word/_rels/header1.xml.rels", "hdr"),
        ("word/footer1.xml", "word/_rels/footer1.xml.rels", "ftr"),
        ("word/footnotes.xml", "word/_rels/footnotes.xml.rels", "footnotes"),
        ("word/endnotes.xml", "word/_rels/endnotes.xml.rels", "endnotes"),
        ("word/settings.xml", "word/_rels/settings.xml.rels", "settings"),
        ("word/webSettings.xml", "word/_rels/webSettings.xml.rels", "webSettings"),
        ("word/numbering.xml", "word/_rels/numbering.xml.rels", "numbering"),
        ("word/comments.xml", "word/_rels/comments.xml.rels", "comments"),
        ("word/glossary/document.xml", "word/glossary/_rels/document.xml.rels", "document"),
        ("word/charts/chart1.xml", "word/charts/_rels/chart1.xml.rels", "chartSpace"),
        ("word/diagrams/data1.xml", "word/diagrams/_rels/data1.xml.rels", "dataModel"),
        ("custom/deep/er/thing.xml", "custom/deep/er/_rels/thing.xml.rels", "thing"),
    ],
)
def test_relationships_of_every_part_are_inspected(tmp_path, owner, rels_name, owner_root):
    path = make_docx(
        tmp_path,
        extra=[
            (owner, xml_part(owner_root, hyperlink_use("rId2"))),
            (
                rels_name,
                rels_part(
                    rel("rId1", IMAGE, "../media/image1.png"),
                    rel("rId2", HYPERLINK, WEB_URL, "External"),
                    external("rId3", ATTACHED_TEMPLATE, "http://example.invalid/t.dotm"),
                    external("rId4", IMAGE, "file:///C:/private/a.png"),
                ),
            ),
        ],
    )

    assert strip_external_references(path) == 2

    assert rel_ids(path, rels_name) == ["rId1", "rId2"]


def test_relationships_files_with_an_upper_case_extension_are_inspected(tmp_path):
    path = make_docx(
        tmp_path, extra=[("word/_rels/numbering.xml.RELS", rels_part(external("rId1")))]
    )

    assert strip_external_references(path) == 1

    assert rel_ids(path, "word/_rels/numbering.xml.RELS") == []


def test_a_relationships_file_encoded_as_utf_16_is_inspected_and_rewritten_as_utf_8(tmp_path):
    utf16 = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        f'<Relationships xmlns="{PKG_NS}">{external("rId2")}{rel("rId3", IMAGE, "m/é.png")}'
        "</Relationships>"
    ).encode("utf-16")
    path = make_docx(tmp_path)
    path = write_zip(
        path,
        [
            ("[Content_Types].xml", _CONTENT_TYPES),
            (ROOT_RELS, rels_part(rel("rId1", OFFICE_DOCUMENT, "word/document.xml"))),
            ("word/document.xml", xml_part("document")),
            (DOCUMENT_RELS, utf16),
        ],
    )

    assert strip_external_references(path) == 1

    with zipfile.ZipFile(path) as archive:
        assert archive.read(DOCUMENT_RELS).startswith(XML_DECLARATION)
    assert relationships(path) == [("rId3", IMAGE, "m/é.png", None)]


# --- nothing to do: the file is not touched ---------------------------------------------------


def test_a_package_without_external_references_is_left_byte_for_byte_untouched(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External"), rel("rId6", IMAGE, "media/a.png")],
        body=hyperlink_use("rId5"),
    )
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns

    assert strip_external_references(path) == 0

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == stamp
    assert listing(path.parent) == ["source.docx"]


def test_a_second_call_removes_nothing_and_changes_nothing(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External"), external("rId6")],
        body=hyperlink_use("rId5"),
        root_rels=[external("rId9")],
    )
    assert strip_external_references(path) == 2
    once = path.read_bytes()

    assert strip_external_references(path) == 0

    assert path.read_bytes() == once


# --- what a rewrite keeps ---------------------------------------------------------------------


def _rich_package(tmp_path: Path) -> Path:
    big = random.Random(7).randbytes(300_000)  # noqa: S311 (deterministic test data)
    return make_docx(
        tmp_path,
        rels=[rel("rId1", IMAGE, "media/image1.png"), external("rId2")],
        extra=[
            ("word/media/", b""),
            ("word/media/image1.png", big),
            ("word/media/empty.bin", b""),
            ("word/media/Bild-\u00e4\u00f6.png", b"\x89PNG-unicode-name"),
            ("docProps/core.xml", b"<core>" + b"x" * 5000 + b"</core>"),
            ("word/_rels/footer1.xml.rels", rels_part(rel("rId1", IMAGE, "media/image1.png"))),
            ("customXml/item1.xml", b"<a/>"),
        ],
    )


def test_a_rewrite_keeps_names_order_times_compression_and_attributes(tmp_path):
    path = _rich_package(tmp_path)
    before = member_summary(path)
    with zipfile.ZipFile(path) as archive:
        archive_comment = archive.comment
    assert {item[2] for item in before} == {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
    assert 0 in {item[3] for item in before}

    assert strip_external_references(path) == 1

    assert member_summary(path) == before
    with zipfile.ZipFile(path) as archive:
        assert archive.comment == archive_comment
        assert archive.testzip() is None


def test_a_rewrite_keeps_the_content_of_every_other_member(tmp_path):
    path = _rich_package(tmp_path)
    before = dict(member_bytes(path))

    assert strip_external_references(path) == 1

    after = dict(member_bytes(path))
    assert list(after) == list(before)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {DOCUMENT_RELS}


def test_a_rewrite_keeps_the_archive_comment(tmp_path):
    path = make_docx(tmp_path, rels=[external("rId2")])
    with zipfile.ZipFile(path, "a") as archive:
        archive.comment = b"a comment"

    assert strip_external_references(path) == 1

    with zipfile.ZipFile(path) as archive:
        assert archive.comment == b"a comment"


def test_only_the_changed_relationships_files_are_rewritten(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[external("rId2")],
        extra=[("word/_rels/footer1.xml.rels", rels_part(rel("rId1", IMAGE, "media/a.png")))],
    )
    before = dict(member_bytes(path))

    strip_external_references(path)

    after = dict(member_bytes(path))
    assert after["word/_rels/footer1.xml.rels"] == before["word/_rels/footer1.xml.rels"]
    assert after[ROOT_RELS] == before[ROOT_RELS]
    assert after[DOCUMENT_RELS] != before[DOCUMENT_RELS]


def test_the_rewritten_relationships_file_is_utf_8_with_a_declaration_and_the_default_namespace(
    tmp_path,
):
    path = make_docx(
        tmp_path,
        rels=[rel("rId1", IMAGE, 'media/a "b" & <c>.png'), external("rId2")],
    )

    strip_external_references(path)

    with zipfile.ZipFile(path) as archive:
        data = archive.read(DOCUMENT_RELS)
    assert data.startswith(XML_DECLARATION)
    text = data.decode("utf-8")
    assert f'<Relationships xmlns="{PKG_NS}">' in text
    assert "ns0" not in text
    assert ":Relationship" not in text
    assert relationships(path) == [("rId1", IMAGE, 'media/a "b" & <c>.png', None)]


def test_the_rewrite_keeps_every_attribute_of_the_relationships_it_keeps(tmp_path):
    keep = (
        f'<Relationship Id="rId1" Type="{IMAGE}" Target="media/a.png" TargetMode="Internal" '
        'x-extra="1"/>'
    )
    path = make_docx(tmp_path, rels=[keep, external("rId2")])

    strip_external_references(path)

    with zipfile.ZipFile(path) as archive:
        [kept] = parse(archive.read(DOCUMENT_RELS))
    assert kept.attrib == {
        "Id": "rId1",
        "Type": IMAGE,
        "Target": "media/a.png",
        "TargetMode": "Internal",
        "x-extra": "1",
    }


def test_a_rewrite_that_removes_everything_leaves_a_valid_empty_relationships_element(tmp_path):
    path = make_docx(tmp_path, rels=[external("rId2")])

    strip_external_references(path)

    with zipfile.ZipFile(path) as archive:
        root = parse(archive.read(DOCUMENT_RELS))
    assert root.tag == f"{{{PKG_NS}}}Relationships"
    assert len(root) == 0


def test_large_members_are_forced_to_zip64_and_stay_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_sanitize, "_FORCE_ZIP64_FROM", 1000)
    path = _rich_package(tmp_path)
    before = dict(member_bytes(path))
    sizes = {i.filename: i.file_size for i in zipfile.ZipFile(path).infolist()}

    assert strip_external_references(path) == 1

    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        versions = {i.filename: i.extract_version for i in archive.infolist()}
    assert dict(member_bytes(path))["word/media/image1.png"] == before["word/media/image1.png"]
    assert versions["word/media/image1.png"] >= 45  # ZIP64_VERSION
    assert sizes["word/media/image1.png"] >= 1000
    assert versions["customXml/item1.xml"] < 45  # small members are not forced


def test_a_package_over_the_rewrite_limit_is_refused_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_sanitize, "_MAX_REWRITE_BYTES", 1000)
    path = _rich_package(tmp_path)

    assert_refused(path, tmp_path)


# --- fail closed ------------------------------------------------------------------------------


@pytest.mark.parametrize("content", [b"", b"this is not a zip file", b"PK\x03\x04" + b"\0" * 10])
def test_a_file_that_is_not_a_zip_package_is_refused(tmp_path, content):
    path = tmp_path / "job-0" / "source.docx"
    path.parent.mkdir()
    path.write_bytes(content)

    assert_refused(path, tmp_path)


def test_a_truncated_package_is_refused(tmp_path):
    path = make_docx(tmp_path, rels=[external("rId2")])
    path.write_bytes(path.read_bytes()[:-30])

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"   ",
        b"<Relationships",
        b"<Relationships xmlns='" + PKG_NS.encode() + b"'><Relationship></Relationships>",
        b'<?xml version="1.0"?><a/><b/>',
        b"\xff\xfe\x00 not xml at all",
        # Valid XML, but not a relationships file.
        b"<Relationships/>",
        b'<Other xmlns="' + PKG_NS.encode() + b'"/>',
        (
            f'<Relationships xmlns="urn:example:other"><Relationship Id="rId1" '
            f'Type="{IMAGE}" Target="http://example.invalid/a.png"/></Relationships>'
        ).encode(),
        # A Relationship that is not a plain child of the root cannot be judged.
        (
            f'<Relationships xmlns="{PKG_NS}"><Group><Relationship Id="rId1" Type="{IMAGE}" '
            'Target="http://example.invalid/a.png"/></Group></Relationships>'
        ).encode(),
        (
            f'<Relationships xmlns="{PKG_NS}"><Relationship xmlns="urn:example:other" '
            f'Id="rId1" Type="{IMAGE}" Target="http://example.invalid/a.png"/></Relationships>'
        ).encode(),
        # Undecodable text.
        b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="'
        + PKG_NS.encode()
        + b'"><Relationship Id="r\xff"/></Relationships>',
        b'<?xml version="1.0" encoding="x-no-such-encoding"?><Relationships xmlns="'
        + PKG_NS.encode()
        + b'"/>',
    ],
)
def test_a_malformed_or_undecodable_relationships_file_is_refused(tmp_path, content):
    path = make_docx(
        tmp_path,
        extra=[("word/_rels/footer1.xml.rels", content)],
    )

    assert_refused(path, tmp_path)


def test_a_malformed_relationships_file_is_refused_even_if_others_were_fine(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[external("rId2")],
        extra=[("word/_rels/header1.xml.rels", b"<broken")],
    )

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "doctype",
    [
        '<!DOCTYPE Relationships [<!ENTITY x "y">]>',
        "<!DOCTYPE Relationships>",
        '<!DOCTYPE Relationships SYSTEM "file:///C:/private/x.dtd">',
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>',
    ],
)
def test_a_relationships_file_with_a_doctype_is_refused(tmp_path, doctype):
    content = (f'<?xml version="1.0"?>{doctype}<Relationships xmlns="{PKG_NS}"/>').encode()
    path = make_docx(tmp_path, extra=[("word/_rels/footer1.xml.rels", content)])

    assert_refused(path, tmp_path)


def test_a_relationships_file_over_the_size_limit_is_refused(tmp_path):
    padding = b" " * (10 * 1024 * 1024 + 1)
    content = rels_part(rel("rId1", IMAGE, "a.png")) + padding
    path = make_docx(tmp_path, extra=[("word/_rels/footer1.xml.rels", content)])

    assert_refused(path, tmp_path)


def test_a_relationships_file_just_inside_the_size_limit_is_accepted(tmp_path):
    content = rels_part(rel("rId1", IMAGE, "a.png")) + b" " * (10 * 1024 * 1024 - 400)
    assert len(content) < 10 * 1024 * 1024
    path = make_docx(tmp_path, extra=[("word/_rels/footer1.xml.rels", content)])
    before = path.read_bytes()

    assert strip_external_references(path) == 0

    assert path.read_bytes() == before


def test_a_part_that_never_ends_is_stopped_by_the_streaming_limit(tmp_path, monkeypatch):
    # The size in the zip headers is honest here, so make zipfile hand back more than it
    # declared, as it would for a lying header.
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External")],
        body=hyperlink_use("rId5"),
    )
    monkeypatch.setattr(docx_sanitize, "_MAX_PART_BYTES", 5000)
    original = zipfile.ZipFile.open

    def open_member(self, name, *args, **kwargs):
        if getattr(name, "filename", name) == "word/document.xml":
            return io.BytesIO(b" " * 20_000)
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)

    assert_refused(path, tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        b"<w:document",
        b"",
        b"\x00\x01\x02 binary",
        b'<?xml version="1.0"?><a/><b/>',
        b'<?xml version="1.0" encoding="UTF-8"?><a b="\xff"/>',
        b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>',
        b"<!DOCTYPE a><a/>",
    ],
)
def test_an_owning_part_that_must_be_read_but_cannot_be_is_refused(tmp_path, content):
    path = make_docx(tmp_path, rels=[rel("rId5", HYPERLINK, WEB_URL, "External")])
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i)) for i in archive.infolist()]
    members = [(n, content if n == "word/document.xml" else d) for n, d in members]
    write_zip(path, members)

    assert_refused(path, tmp_path)


def test_an_owning_part_over_the_size_limit_is_refused(tmp_path, monkeypatch):
    path = make_docx(
        tmp_path,
        rels=[rel("rId5", HYPERLINK, WEB_URL, "External")],
        body=hyperlink_use("rId5") + "<w:p/>" * 5000,
    )
    monkeypatch.setattr(docx_sanitize, "_MAX_PART_BYTES", 10_000)

    assert_refused(path, tmp_path)


def test_an_owning_part_is_not_read_unless_something_depends_on_it(tmp_path):
    # Its content cannot change the outcome, so a damaged document.xml does not stop a document
    # whose only relationship is an external one that is removed whatever the part says.
    path = make_docx(tmp_path, rels=[external("rId2")])
    with zipfile.ZipFile(path) as archive:
        members = [(i.filename, archive.read(i)) for i in archive.infolist()]
    members = [(n, b"<broken" if n == "word/document.xml" else d) for n, d in members]
    write_zip(path, members)

    assert strip_external_references(path) == 1


def test_a_missing_owning_part_is_not_an_error(tmp_path):
    path = make_docx(
        tmp_path,
        extra=[
            ("word/_rels/ghost.xml.rels", rels_part(rel("rId1", HYPERLINK, WEB_URL, "External")))
        ],
    )

    assert strip_external_references(path) == 1


def test_a_corrupt_inspected_member_is_refused(tmp_path):
    path = make_docx(tmp_path, rels=[external("rId2")])
    assert_refused(_flip_a_stored_byte(path, DOCUMENT_RELS), tmp_path)


def test_a_corrupt_member_that_is_only_copied_is_refused_and_leaves_no_temporary_file(tmp_path):
    path = make_docx(
        tmp_path,
        rels=[external("rId2")],
        extra=[("word/media/image1.png", random.Random(3).randbytes(50_000))],  # noqa: S311 (deterministic test data)
    )
    assert_refused(_flip_a_stored_byte(path, "word/media/image1.png"), tmp_path)


def _flip_a_stored_byte(path: Path, member: str) -> Path:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    data = bytearray(path.read_bytes())
    name_length = int.from_bytes(data[info.header_offset + 26 : info.header_offset + 28], "little")
    extra_length = int.from_bytes(data[info.header_offset + 28 : info.header_offset + 30], "little")
    start = info.header_offset + 30 + name_length + extra_length
    data[start + max(info.compress_size // 2, 0)] ^= 0xFF
    path.write_bytes(bytes(data))
    return path


def test_an_encrypted_member_is_refused(tmp_path):
    path = make_docx(tmp_path, rels=[external("rId2")], extra=[("word/media/secret.bin", b"data")])
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/media/secret.bin")
    # Set the "encrypted" bit in the local header and in the central directory.
    data[info.header_offset + 6] |= 0x01
    central = bytes(data).rfind(b"PK\x01\x02" + b"\0" * 0)
    while central != -1:
        name_start = central + 46
        if bytes(data[name_start : name_start + len(info.filename)]) == info.filename.encode():
            data[central + 8] |= 0x01
            break
        central = bytes(data).rfind(b"PK\x01\x02", 0, central)
    path.write_bytes(bytes(data))

    assert_refused(path, tmp_path)


# --- operating system problems ---------------------------------------------------------------


def _disk_full(path: Path) -> OSError:
    return OSError(errno.ENOSPC, "No space left on device", str(path))


def assert_temporary_folder_problem(path: Path, tmp_path: Path, before: bytes, folder: list[str]):
    with pytest.raises(TemporaryFolderError) as caught:
        strip_external_references(path)
    message = str(caught.value)
    assert "temporary folder" in message
    for private in (str(path), path.name, str(tmp_path), Path.home().name):
        assert private not in message
    assert path.read_bytes() == before
    assert listing(path.parent) == folder


def test_a_write_error_is_a_temporary_folder_error_and_leaves_no_temporary_file(
    tmp_path, monkeypatch
):
    path = _rich_package(tmp_path)
    before, folder = path.read_bytes(), listing(path.parent)
    calls = []
    original = zipfile.ZipFile.open

    def fail_on_the_third_member(self, name, mode="r", *args, **kwargs):
        if mode == "w":
            calls.append(name)
            if len(calls) == 3:
                raise _disk_full(path)
        return original(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_on_the_third_member)

    assert_temporary_folder_problem(path, tmp_path, before, folder)
    assert len(calls) == 3  # the temporary file had real content when the error came


def test_an_error_while_writing_the_changed_relationships_file_is_handled_the_same_way(
    tmp_path, monkeypatch
):
    path = make_docx(tmp_path, rels=[external("rId2")])
    before, folder = path.read_bytes(), listing(path.parent)

    def fail(self, *args, **kwargs):
        raise _disk_full(path)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)

    assert_temporary_folder_problem(path, tmp_path, before, folder)


def test_failing_to_replace_the_file_is_a_temporary_folder_error_and_cleans_up(
    tmp_path, monkeypatch
):
    path = make_docx(tmp_path, rels=[external("rId2")])
    before, folder = path.read_bytes(), listing(path.parent)

    def fail(source, destination):
        raise PermissionError(errno.EACCES, "Access is denied", str(destination))

    monkeypatch.setattr(os, "replace", fail)

    assert_temporary_folder_problem(path, tmp_path, before, folder)


def test_failing_to_create_the_temporary_file_is_a_temporary_folder_error(tmp_path, monkeypatch):
    path = make_docx(tmp_path, rels=[external("rId2")])
    before, folder = path.read_bytes(), listing(path.parent)

    def fail(*args, **kwargs):
        raise _disk_full(path)

    monkeypatch.setattr(docx_sanitize.tempfile, "mkstemp", fail)

    assert_temporary_folder_problem(path, tmp_path, before, folder)


def test_a_read_error_is_a_temporary_folder_error(tmp_path):
    missing = tmp_path / "job-0" / "source.docx"
    missing.parent.mkdir()

    with pytest.raises(TemporaryFolderError) as caught:
        strip_external_references(missing)

    assert str(tmp_path) not in str(caught.value)
    assert missing.name not in str(caught.value)


def test_an_unexpected_failure_is_reported_as_a_refusal_not_a_crash(tmp_path, monkeypatch):
    path = make_docx(tmp_path, rels=[external("rId2")])

    def explode(*args, **kwargs):
        raise ZeroDivisionError("boom " + str(path))

    monkeypatch.setattr(docx_sanitize, "_serialize", explode)

    error = assert_refused(path, tmp_path)
    assert "boom" not in str(error)


# --- isolation and logging --------------------------------------------------------------------


def _record_paths(monkeypatch, recorded: list[str]) -> None:
    def spy(owner, name, count):
        original = getattr(owner, name)

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            for argument in args[:count]:
                if isinstance(argument, str | bytes | os.PathLike):
                    recorded.append(os.fsdecode(argument))
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    for name, count in (
        ("open", 1),
        ("stat", 1),
        ("lstat", 1),
        ("scandir", 1),
        ("listdir", 1),
        ("replace", 2),
        ("rename", 2),
        ("remove", 1),
        ("unlink", 1),
    ):
        spy(os, name, count)
    spy(io, "open", 1)
    spy(__import__("builtins"), "open", 1)


def test_no_file_other_than_the_given_one_is_touched_and_no_target_is_followed(
    tmp_path, monkeypatch
):
    linked = tmp_path / "linked" / "picture.png"
    linked.parent.mkdir()
    linked.write_bytes(b"linked picture")
    bystander = tmp_path / "work" / "job-0" / "bystander.txt"
    bystander.parent.mkdir(parents=True)
    bystander.write_text("do not touch")
    outside = tmp_path / "work" / "other.docx"
    make_docx(tmp_path, rels=[external("rId2")], name="work/other.docx")
    path = make_docx(
        tmp_path,
        rels=[
            external("rId2", IMAGE, linked.as_uri()),
            external("rId3", IMAGE, "http://127.0.0.1:9/pixel.png"),
            external("rId4", IMAGE, "\\\\localhost\\share\\pixel.png"),
            rel("rId5", HYPERLINK, linked.as_uri(), "External"),
            rel("rId6", IMAGE, str(linked), "External"),
        ],
        body=hyperlink_use("rId5"),
        name="work/job-0/source.docx",
    )
    state = {k: v for k, v in tree_state(tmp_path).items() if not k.endswith("source.docx")}
    recorded: list[str] = []

    def no_network(*args, **kwargs):
        raise AssertionError("the sanitizer must never use the network")

    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "connect", no_network)
        patch.setattr(socket, "getaddrinfo", no_network)
        _record_paths(patch, recorded)
        assert strip_external_references(path) == 4

    assert recorded, "the spy saw nothing"
    folder = path.parent.resolve()
    for name in recorded:
        assert Path(name).resolve().parent == folder, (
            "touched a file outside the private copy's folder"
        )
    after = {k: v for k, v in tree_state(tmp_path).items() if not k.endswith("source.docx")}
    assert after == state
    assert outside.exists()
    assert listing(path.parent) == ["bystander.txt", "source.docx"]


def test_the_log_contains_counts_only(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = make_docx(
        tmp_path,
        rels=[
            external("rIdSecret-q7", IMAGE, "http://example.invalid/Zebra-q7/pixel.png"),
            external("rId3", IMAGE, "file:///C:/Users/Okapi-q7/private.png"),
        ],
    )

    assert strip_external_references(path) == 2

    assert "Removed 2 external references" in caplog.text
    for secret in (
        "Zebra-q7",
        "Okapi-q7",
        "rIdSecret",
        "example.invalid",
        str(tmp_path),
        Path.home().name,
    ):
        assert secret not in caplog.text


def test_the_log_of_a_refusal_contains_no_path_or_id(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = make_docx(
        tmp_path,
        extra=[("word/_rels/footer1.xml.rels", b"<broken rIdSecret-q7=")],
    )

    assert_refused(path, tmp_path)

    for secret in ("rIdSecret", "source.docx", str(tmp_path), Path.home().name, "footer1"):
        assert secret not in caplog.text


def test_nothing_is_logged_as_removed_when_nothing_was(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = make_docx(tmp_path, rels=[rel("rId1", IMAGE, "media/a.png")])

    strip_external_references(path)

    assert "Removed" not in caplog.text
