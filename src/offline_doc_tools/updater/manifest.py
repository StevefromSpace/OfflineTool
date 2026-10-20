"""The signed update manifest: the only thing from a release that is believed.

``update-manifest.json`` and its detached Ed25519 signature ``update-manifest.json.sig`` (base64)
are release assets. Everything else GitHub returns is treated as untrusted. The manifest binds the
application identity, the version, the package file name, its SHA-256 and size, and the updater
protocol that the package's own helper speaks.
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from . import ed25519

APP_ID = "OfflineDocTools"
FORMAT = 1
#: Helper protocols this application can hand an update to. A package whose helper speaks any
#: other protocol is refused: that update has to be installed by hand.
SUPPORTED_PROTOCOLS = frozenset({1})
MANIFEST_ASSET = "update-manifest.json"
SIGNATURE_ASSET = "update-manifest.json.sig"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 1024**3

_VERSION = re.compile(r"([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Version = tuple[int, int, int]


class ManifestError(Exception):
    """``kind``: unconfigured, signature, format or unsupported. The text names no input."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class Applicability(enum.Enum):
    NEWER = "newer"
    UP_TO_DATE = "up to date"
    MANUAL_UPDATE_NEEDED = "manual update needed"


@dataclass(frozen=True)
class Manifest:
    version: str
    package_name: str
    package_sha256: str
    package_size: int
    updater_protocol: int
    min_installed_version: str | None


def parse_version(text: object, *, allow_v: bool = False) -> Version | None:
    if not isinstance(text, str):
        return None
    if allow_v and text.startswith("v"):
        text = text[1:]
    match = _VERSION.fullmatch(text)
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _signature_bytes(text: str) -> bytes:
    try:
        raw = base64.b64decode(text.strip(), validate=True) if len(text) < 1024 else b""
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 64:
        raise ManifestError("signature")
    return raw


def verify_manifest(raw: bytes, signature_text: str, trusted_keys: Sequence[bytes]) -> Manifest:
    """Check the signature over the exact bytes of ``raw``, then validate the content."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("format")
    if not trusted_keys:
        raise ManifestError("unconfigured")
    signature = _signature_bytes(signature_text)
    if not any(ed25519.verify(key, raw, signature) for key in trusted_keys):
        raise ManifestError("signature")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ManifestError("format") from None
    return _validate(data)


def _validate(data: object) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError("format")
    if _is_int(data.get("format")) and data["format"] > FORMAT:
        raise ManifestError("unsupported")
    if data.get("format") != FORMAT or not _is_int(data.get("format")):
        raise ManifestError("format")
    if data.get("app") != APP_ID or data.get("channel") != "stable":
        raise ManifestError("format")
    version = data.get("version")
    if parse_version(version) is None:
        raise ManifestError("format")
    protocol = data.get("updater_protocol")
    if not _is_int(protocol):
        raise ManifestError("format")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ManifestError("unsupported")
    minimum = data.get("min_installed_version")
    if minimum is not None and parse_version(minimum) is None:
        raise ManifestError("format")
    package = data.get("package")
    if not isinstance(package, dict):
        raise ManifestError("format")
    name, sha, size = package.get("name"), package.get("sha256"), package.get("size")
    if name != f"{APP_ID}-{version}-win64.zip":
        raise ManifestError("format")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ManifestError("format")
    if not _is_int(size) or not 0 < size <= MAX_PACKAGE_BYTES:
        raise ManifestError("format")
    return Manifest(version, name, sha, size, protocol, minimum)


def assess(manifest: Manifest, installed: str, tag: str | None) -> Applicability:
    """Whether the signed release should be offered for the installed version."""
    have = parse_version(installed)
    if have is None:
        raise ManifestError("format")
    if tag is not None and parse_version(tag, allow_v=True) != parse_version(manifest.version):
        raise ManifestError("format")  # the release tag disagrees with the signed version
    if parse_version(manifest.version) <= have:
        return Applicability.UP_TO_DATE
    minimum = (
        parse_version(manifest.min_installed_version) if manifest.min_installed_version else None
    )
    if minimum is not None and have < minimum:
        return Applicability.MANUAL_UPDATE_NEEDED
    return Applicability.NEWER
