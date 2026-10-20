"""Strip external references out of a private copy of a .docx.

LibreOffice must never be handed a relationship that points outside the package: it would fetch
a linked image from the network or embed a linked local file. This module removes every such
relationship from the OPC relationship parts (``*.rels``) before the engine sees the copy.

The one exception is a hyperlink relationship that its owning part uses only as a hyperlink:
that is a clickable link in the PDF, not something the engine fetches. Everything else that is
external, or whose target is not a plain package-local path, is removed.

Two kinds of content make LibreOffice load things no relationship mentions (probed with
LibreOffice 26.2.1), so a package holding either is refused instead:

* a ``w:altChunk``, an embedded HTML/RTF/text section: it is imported whatever type its
  relationship has, and the images and style sheets it names are fetched or embedded;
* an SVG picture that names files outside the document: they are embedded in the PDF.
  LibreOffice recognises SVG by its content, also gzip-compressed, whatever the member is called.

Fail closed: a package that cannot be fully judged (damaged, encrypted, a DOCTYPE, too large, an
unexpected structure) is refused, never passed on. Nothing here opens a network connection or
touches a file other than the one it is given and its own temporary file beside it.

Not covered, and untested: nested packages (embedded OLE objects), field codes other than the
ones probed, and anything else LibreOffice might load from a part without a relationship.
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import os
import re
import shutil
import tempfile
import warnings
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree as ET  # noqa: S405 (a DOCTYPE is rejected before parsing)
from xml.parsers import expat  # noqa: S407
from xml.sax.saxutils import quoteattr

from .engine import ConversionFailedError, TemporaryFolderError

log = logging.getLogger(__name__)

_REFUSAL = "The document could not be checked for external references, so it was not converted."
_REFUSAL_CHUNK = (
    "The document contains an embedded HTML or text section that can load external content, "
    "so it was not converted."
)
_REFUSAL_SVG = (
    "The document contains a picture (SVG) that refers to external files, so it was not converted."
)

_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_RELATIONSHIP = f"{{{_PKG_NS}}}Relationship"
_RELATIONSHIPS = f"{{{_PKG_NS}}}Relationships"

_R_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_HYPERLINK_TYPES = frozenset(f"{ns}/hyperlink" for ns in _R_NAMESPACES)
_W_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
_A_NAMESPACES = (
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://purl.oclc.org/ooxml/drawingml/main",
)
# Names as expat reports them with namespace_separator="}": "uri}local".
_HYPERLINK_ELEMENTS = frozenset(
    [f"{ns}}}hyperlink" for ns in _W_NAMESPACES]
    + [f"{ns}}}{local}" for ns in _A_NAMESPACES for local in ("hlinkClick", "hlinkHover")]
)
_ALTCHUNK_ELEMENTS = frozenset(f"{ns}}}altChunk" for ns in _W_NAMESPACES)
_ID_ATTRIBUTES = frozenset(f"{ns}}}id" for ns in _R_NAMESPACES)

_MAX_RELS_BYTES = 10 * 1024 * 1024
_MAX_PART_BYTES = 256 * 1024 * 1024
_MAX_INSPECT_BYTES = 512 * 1024 * 1024  # all XML one document may make us parse
_MAX_ENTRIES = 100_000
_MAX_REWRITE_BYTES = 2 * 1024**3
_FORCE_ZIP64_FROM = 1 << 30
_CHUNK = 1 << 20
# shortcut: LibreOffice found "<svg" 2 KB into a file but not 9 KB in; revisit if that changes.
_SVG_SNIFF_BYTES = 16 * 1024
_GZIP_ERRORS = (gzip.BadGzipFile, EOFError, zlib.error)

_OWNER = re.compile(r"(?:(.*)/)?_rels/(.+)\.rels")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_UNC = re.compile(r"[\\/]{2}")
_BLANKS = re.compile(r"[\x00-\x20\x7f]+")
# A CSS url() or @import that does not point inside the picture ("#id" or a data: URI).
_SVG_OUTSIDE = re.compile(r"url\(\s*+['\"]?+\s*+(?!#|data:)|@import", re.IGNORECASE)


class _Refused(Exception):
    """The package cannot be judged or must not be passed on. The message is shown to the user,
    so it holds no paths."""

    def __init__(self, message: str = _REFUSAL) -> None:
        super().__init__(message)


class _Budget:
    """How much XML one document may make us read: it bounds the time a hostile file can take."""

    def __init__(self) -> None:
        self.remaining = _MAX_INSPECT_BYTES

    def spend(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise _Refused


class _Rels(NamedTuple):
    index: int
    owner: str | None
    root: ET.Element
    external: list[ET.Element]
    internal: list[ET.Element]
    candidates: set[str]  # ids of external hyperlinks that might be kept


def strip_external_references(path: Path) -> int:
    """Rewrite the .docx at `path` IN PLACE. `path` is always a PRIVATE COPY in the app's
    temporary workspace, never a user's file. Returns the number of relationships removed;
    0 means the file was left byte-for-byte untouched."""
    try:
        removed = _strip(Path(path))
    except _Refused as refusal:
        log.info("Document refused: %s", refusal)
        raise ConversionFailedError(str(refusal)) from None
    except OSError as exc:
        raise TemporaryFolderError.from_os_error("read or update the document in", exc) from exc
    except Exception as exc:
        # Includes BadZipFile, zlib.error and bugs: all mean "cannot be judged". Only the
        # type is logged because the text of an error can hold a path.
        log.info("Document refused: %s while checking for external references", type(exc).__name__)
        raise ConversionFailedError(_REFUSAL) from None
    if removed:
        log.info("Removed %d external references", removed)
    return removed


def _strip(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_ENTRIES or any(info.flag_bits & 0x1 for info in infos):
            raise _Refused
        by_name: dict[str, list[zipfile.ZipInfo]] = {}
        for info in infos:
            if not info.is_dir():
                by_name.setdefault(info.filename.lower(), []).append(info)

        budget = _Budget()
        found: list[_Rels] = []
        for index, info in enumerate(infos):
            if info.is_dir():
                continue
            if info.filename.lower().endswith(".rels"):
                found.append(_read_relationships(archive, index, info, budget))
            else:
                _refuse_svg_naming_outside_files(archive, info, budget)
        used_as_links = _scan_owning_parts(archive, found, by_name, budget)

        removed = 0
        changes: dict[int, bytes] = {}
        for item in found:
            kept = _kept(item, used_as_links)
            if len(kept) != len(item.root):
                removed += len(item.root) - len(kept)
                changes[item.index] = _serialize(item.root, kept)
        if not removed:
            return 0
        if sum(info.file_size for info in infos) > _MAX_REWRITE_BYTES:
            raise _Refused
    _rewrite(path, changes)
    return removed


# --- reading ----------------------------------------------------------------------------------


def _read_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
    budget: _Budget,
    gzipped: bool = False,
):
    """Yield the member's bytes in chunks (inflated first if it is gzip), refusing one that is,
    or turns out to be, too big."""
    if info.file_size > limit:
        raise _Refused
    total = 0
    with archive.open(info) as member:
        stream = gzip.GzipFile(fileobj=member) if gzipped else member
        while chunk := stream.read(_CHUNK):
            total += len(chunk)
            budget.spend(len(chunk))
            if total > limit:
                raise _Refused
            yield chunk


def _no_doctype(*_args) -> None:
    raise _Refused


def _expat_parser(**handlers):
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartDoctypeDeclHandler = _no_doctype
    parser.EntityDeclHandler = _no_doctype
    for name, handler in handlers.items():
        setattr(parser, name, handler)
    return parser


def _parse_relationships(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget
) -> ET.Element:
    data = b"".join(_read_limited(archive, info, _MAX_RELS_BYTES, budget))
    _expat_parser().Parse(data, True)  # well-formedness and DOCTYPE check only
    root = ET.fromstring(data)  # noqa: S314 (no DTD: checked above)
    if root.tag != _RELATIONSHIPS:
        raise _Refused
    for child in root:
        if child.tag != _RELATIONSHIP or len(child):
            raise _Refused
    return root


def _owner_name(rels_name: str) -> str | None:
    """Lower-cased name of the part that owns a relationships file; None for the package's
    own relationships and for files that are not in a ``_rels`` folder."""
    match = _OWNER.fullmatch(rels_name.lower())
    if match is None:
        return None
    folder, leaf = match.groups()
    return f"{folder}/{leaf}" if folder else leaf


def _is_external(relationship: ET.Element) -> bool:
    if (relationship.get("TargetMode") or "").strip().lower() == "external":
        return True
    # Browsers ignore tabs, newlines and padding inside a URL, so a target is judged without.
    target = _BLANKS.sub("", relationship.get("Target") or "")
    return bool(_SCHEME.match(target) or _UNC.match(target))


def _read_relationships(
    archive: zipfile.ZipFile, index: int, info: zipfile.ZipInfo, budget: _Budget
) -> _Rels:
    root = _parse_relationships(archive, info, budget)
    external: list[ET.Element] = []
    internal: list[ET.Element] = []
    for child in root:
        (external if _is_external(child) else internal).append(child)
    counts = Counter(child.get("Id") for child in root)
    candidates = {
        child.get("Id")
        for child in external
        if child.get("Type") in _HYPERLINK_TYPES
        and child.get("Id")
        and counts[child.get("Id")] == 1
    }
    return _Rels(index, _owner_name(info.filename), root, external, internal, candidates)


# --- the parts that own relationships ---------------------------------------------------------


def _scan_owner(
    archive: zipfile.ZipFile, owners: list[zipfile.ZipInfo], wanted: set[str], budget: _Budget
) -> set[str]:
    """Read every copy of an owning part once. Refuses a w:altChunk. Returns the ids from
    `wanted` that the part uses only as hyperlinks."""
    as_link: set[str] = set()
    otherwise: set[str] = set()

    def start(name: str, attributes: dict[str, str]) -> None:
        if name in _ALTCHUNK_ELEMENTS:
            raise _Refused(_REFUSAL_CHUNK)
        if not wanted:
            return
        is_link = name in _HYPERLINK_ELEMENTS
        for key, value in attributes.items():
            value = value.strip()
            if value in wanted:
                (as_link if is_link and key in _ID_ATTRIBUTES else otherwise).add(value)

    for owner in owners:
        parser = _expat_parser(StartElementHandler=start)
        for chunk in _read_limited(archive, owner, _MAX_PART_BYTES, budget):
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    return as_link - otherwise


def _scan_owning_parts(
    archive: zipfile.ZipFile,
    found: list[_Rels],
    by_name: dict[str, list[zipfile.ZipInfo]],
    budget: _Budget,
) -> dict[str, set[str]]:
    """Read each owning part once, however many relationships files point at it. A part is only
    read if a hyperlink's fate depends on it or it has an internal relationship (a w:altChunk
    could use it). Whether that target exists is not judged here: another consumer may resolve
    the name differently, so a target this module cannot find must not skip the scan."""
    wanted: dict[str, set[str]] = {}
    for item in found:
        if item.owner in by_name and (item.candidates or item.internal):
            wanted.setdefault(item.owner, set()).update(item.candidates)
    return {
        owner: _scan_owner(archive, by_name[owner], ids, budget) for owner, ids in wanted.items()
    }


def _kept(item: _Rels, used_as_links: dict[str, set[str]]) -> list[ET.Element]:
    links = used_as_links.get(item.owner, set()) & item.candidates
    drop = {id(child) for child in item.external if child.get("Id") not in links}
    return [child for child in item.root if id(child) not in drop]


# --- pictures ---------------------------------------------------------------------------------


def _refuse_svg_naming_outside_files(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget
) -> None:
    """LibreOffice recognises an SVG picture by its content, whatever the member is called, also
    when it is gzip-compressed, and embeds the files it names. Refuse such a picture."""
    try:
        with archive.open(info) as member:
            head = member.read(_SVG_SNIFF_BYTES)
        gzipped = head[:2] == b"\x1f\x8b"
        if gzipped:
            with archive.open(info) as member, gzip.GzipFile(fileobj=member) as inflated:
                head = inflated.read(_SVG_SNIFF_BYTES)
    except _GZIP_ERRORS as exc:
        # LibreOffice has been seen decoding a damaged gzip trailer that Python's gzip module
        # rejects; treat "cannot read this" as "cannot judge this", not as "safe to pass on".
        raise _Refused from exc
    if b"<svg" not in head.lower():
        return

    tail = ""

    def start(name: str, attributes: dict[str, str]) -> None:
        is_link = name.rsplit("}", 1)[-1] == "a"
        for key, value in attributes.items():
            names_a_file = key.rsplit("}", 1)[-1] == "href" and not is_link
            if _SVG_OUTSIDE.search(value) or (
                names_a_file and not value.strip().lower().startswith(("#", "data:"))
            ):
                raise _Refused(_REFUSAL_SVG)

    def text(data: str) -> None:
        nonlocal tail
        window = tail + data  # a token can straddle two pieces of text
        if _SVG_OUTSIDE.search(window):
            raise _Refused(_REFUSAL_SVG)
        tail = window[-16:]

    parser = _expat_parser(StartElementHandler=start, CharacterDataHandler=text)
    try:
        for chunk in _read_limited(archive, info, _MAX_PART_BYTES, budget, gzipped):
            parser.Parse(chunk, False)
    except _GZIP_ERRORS as exc:  # damage met after the sniff window (BadGzipFile is an OSError)
        raise _Refused from exc
    parser.Parse(b"", True)


# --- writing ----------------------------------------------------------------------------------


def _serialize(root: ET.Element, kept: list[ET.Element]) -> bytes:
    """A relationships part with the default namespace and only the kept relationships."""

    def attributes(element: ET.Element) -> str:
        text = ""
        namespaces: dict[str, str] = {}
        for key, value in element.attrib.items():
            if key.startswith("{"):
                uri, local = key[1:].split("}", 1)
                prefix = namespaces.setdefault(uri, f"ns{len(namespaces) + 1}")
                key = f"{prefix}:{local}"
            text += f" {key}={quoteattr(value)}"
        return "".join(f" xmlns:{p}={quoteattr(u)}" for u, p in namespaces.items()) + text

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
        f'<Relationships xmlns="{_PKG_NS}"{attributes(root)}>',
        *(f"<Relationship{attributes(child)}/>" for child in kept),
        "</Relationships>",
    ]
    return "".join(lines).encode("utf-8")


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A fresh header with the same name, time, compression and attributes. The extra field is
    dropped: it can hold a ZIP64 record that the writer adds again."""
    name = info.filename
    if not info.flag_bits & 0x800 and not name.isascii():
        # Python reads a name without the UTF-8 flag as cp437. Most producers write UTF-8 and
        # leave the flag off; undo the misreading or the part would get a different name.
        with contextlib.suppress(UnicodeError):
            name = name.encode("cp437").decode("utf-8")
    new = zipfile.ZipInfo(name, info.date_time)
    new.compress_type = info.compress_type
    new.external_attr = info.external_attr
    new.internal_attr = info.internal_attr
    new.create_system = info.create_system
    new.comment = info.comment
    new.file_size = info.file_size
    return new


def _rewrite(path: Path, changes: dict[int, bytes]) -> None:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".odt-", suffix=".tmp")
    temporary = Path(name)
    try:
        with (
            os.fdopen(descriptor, "wb") as handle,
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(handle, "w") as target,
            warnings.catch_warnings(),
        ):
            # Duplicate member names are kept as they are, so that no copy of a part is lost.
            warnings.filterwarnings("ignore", message="Duplicate name", category=UserWarning)
            target.comment = source.comment
            for index, info in enumerate(source.infolist()):
                new = _copy_info(info)
                if info.is_dir():
                    new.CRC = new.compress_size = 0  # mkdir() leaves these unset for a ZipInfo
                    target.mkdir(new)
                elif index in changes:
                    target.writestr(new, changes[index])
                else:
                    with (
                        source.open(info) as member,
                        target.open(
                            new, "w", force_zip64=info.file_size >= _FORCE_ZIP64_FROM
                        ) as out,
                    ):
                        shutil.copyfileobj(member, out, _CHUNK)
                new.external_attr = info.external_attr  # open() turns a 0 into 0o600 << 16
        temporary.replace(path)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise _Refused from exc
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
