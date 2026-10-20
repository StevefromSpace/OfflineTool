"""Count the pictures a .docx only links to, so the user can be told they were left out.

Read-only and advisory: it never raises and never changes anything. Only the structure verified
against the real LibreOffice is counted: an ``a:blip`` with an ``r:link`` attribute whose
relationship is an external image relationship, in any part of the package (this covers inline
pictures, headers and linked shape fills). Everything else is not counted, so a count of zero
proves nothing.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .docx_sanitize import (
    _A_NAMESPACES,
    _MAX_PART_BYTES,
    _R_NAMESPACES,
    _Budget,
    _expat_parser,
    _is_external,
    _owner_name,
    _parse_relationships,
    _read_limited,
)

log = logging.getLogger(__name__)

_IMAGE_TYPES = frozenset(f"{ns}/image" for ns in _R_NAMESPACES)
_BLIP_ELEMENTS = frozenset(f"{ns}}}blip" for ns in _A_NAMESPACES)
_LINK_ATTRIBUTES = frozenset(f"{ns}}}link" for ns in _R_NAMESPACES)


@dataclass(frozen=True)
class LinkedImageScan:
    omitted: int
    """Picture occurrences found that are only linked (and so left out of the PDF)."""
    complete: bool
    """False if some part could not be read, so ``omitted`` may be too low."""


def scan_linked_images(path: Path) -> LinkedImageScan:
    omitted = 0
    budget = _Budget()
    try:
        with zipfile.ZipFile(path) as archive:
            parts: dict[str, list[zipfile.ZipInfo]] = {}
            for info in archive.infolist():
                if not info.is_dir():
                    parts.setdefault(info.filename.lower(), []).append(info)
            for name, infos in parts.items():
                owner = _owner_name(name) if name.endswith(".rels") else None
                if owner is None:
                    continue
                root = _parse_relationships(archive, infos[0], budget)
                linked = {
                    child.get("Id")
                    for child in root
                    if child.get("Type") in _IMAGE_TYPES and _is_external(child)
                }
                linked.discard(None)
                if linked:
                    omitted += sum(
                        _count(archive, part, linked, budget) for part in parts.get(owner, [])
                    )
    except Exception as exc:
        # The text of an error can hold a path, so only its type is logged.
        log.info("Could not fully scan a document for linked images (%s)", type(exc).__name__)
        return LinkedImageScan(omitted, False)
    return LinkedImageScan(omitted, True)


def _count(
    archive: zipfile.ZipFile, part: zipfile.ZipInfo, linked: set[str], budget: _Budget
) -> int:
    count = 0

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal count
        if name in _BLIP_ELEMENTS:
            count += sum(
                key in _LINK_ATTRIBUTES and value.strip() in linked
                for key, value in attributes.items()
            )

    parser = _expat_parser(StartElementHandler=start)
    for chunk in _read_limited(archive, part, _MAX_PART_BYTES, budget):
        parser.Parse(chunk, False)
    parser.Parse(b"", True)
    return count
