"""Builders shared by the updater tests: signed manifests, fake bundles, release packages."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

from ed25519_ref import public_key, sign

SEED = bytes(range(7, 39))
OTHER_SEED = bytes(range(100, 132))
PUBLIC = public_key(SEED)
TOP = "OfflineDocTools"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_dict(version="0.2.0", package_bytes: bytes = b"zip-bytes", **overrides) -> dict:
    data = {
        "format": 1,
        "app": "OfflineDocTools",
        "channel": "stable",
        "version": version,
        "package": {
            "name": f"OfflineDocTools-{version}-win64.zip",
            "sha256": sha256(package_bytes),
            "size": len(package_bytes),
        },
        "updater_protocol": 1,
    }
    data.update(overrides)
    return data


def signed(data: dict | bytes, seed: bytes = SEED) -> tuple[bytes, str]:
    raw = data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode()
    return raw, base64.b64encode(sign(seed, raw)).decode()


def bundle_files(version: str, *, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """The files of a minimal fake bundle, relative to the OfflineDocTools folder."""
    files = {
        "OfflineDocTools.exe": f"EXE {version}".encode(),
        "THIRD-PARTY-NOTICES.txt": b"notices",
        "README.md": b"readme",
        "licenses/LGPL-3.0.txt": b"lgpl",
        "_internal/python314.dll": f"PY {version}".encode(),
        "_internal/PySide6/Qt6Core.dll": b"qt",
    }
    files.update(extra or {})
    return files


def make_zip(files: dict[str, bytes], top: str | None = TOP) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(f"{top}/{name}" if top else name, data)
    return buffer.getvalue()


def write_bundle(root: Path, version: str, **kwargs) -> Path:
    for name, data in bundle_files(version, **kwargs).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root
