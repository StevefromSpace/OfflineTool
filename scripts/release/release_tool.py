r"""Owner-side tool for signed releases. Not part of any package: it needs the private key.

    python scripts\release\release_tool.py keygen --key C:\secure\offlinetool-release-key.pem
    python scripts\release\release_tool.py release ^
        --zip dist\release\OfflineDocTools-0.2.0-win64.zip ^
        --key C:\secure\offlinetool-release-key.pem [--min-installed 0.1.0]

``keygen`` makes an Ed25519 key with OpenSSL, refuses to overwrite one, and prints the PUBLIC key
as hex for ``TRUSTED_PUBLIC_KEYS`` in ``src/offline_doc_tools/updater/config.py``. The private key
is created wherever you say; keep it offline and outside the repository (``*.pem`` is ignored).

``release`` writes ``update-manifest.json`` and ``update-manifest.json.sig`` next to the ZIP's
version folder under ``dist\release-assets\<version>\``, then checks them with the same code the
application uses (signature, manifest rules, package size/hash, archive layout and safe paths).
Upload the three files (ZIP, manifest, signature) to a release of the PRIVATE release repository.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from offline_doc_tools.updater import manifest as mf  # noqa: E402
from offline_doc_tools.updater import package as pk  # noqa: E402

_ZIP = re.compile(r"OfflineDocTools-([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})-win64\.zip")


class ToolError(Exception):
    pass


def openssl() -> str:
    # An explicit path: without one, Windows also looks in the current folder first.
    found = shutil.which("openssl", path=os.environ.get("PATH", ""))
    if not found:
        raise ToolError("OpenSSL was not found on PATH (Git for Windows ships one).")
    return found


def public_key_hex(key: Path) -> str:
    out = subprocess.run(
        [openssl(), "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
        capture_output=True,
        check=False,
    )
    if out.returncode != 0 or len(out.stdout) < 32:
        raise ToolError("That file is not a usable Ed25519 private key.")
    return out.stdout[-32:].hex()


def keygen(key: Path) -> str:
    if key.exists():
        raise ToolError("That key file already exists; it will not be overwritten.")
    if ROOT in key.resolve().parents:
        raise ToolError("Keep the private key outside the repository.")
    key.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [openssl(), "genpkey", "-algorithm", "ed25519", "-out", str(key)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ToolError("OpenSSL could not create the key.")
    return public_key_hex(key)


def sign(data: bytes, key: Path) -> str:
    with tempfile.TemporaryDirectory() as work:
        message = Path(work) / "message.bin"
        message.write_bytes(data)
        out = subprocess.run(
            [openssl(), "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(message)],
            capture_output=True,
            check=False,
        )
    if out.returncode != 0 or len(out.stdout) != 64:
        raise ToolError("Signing failed (is that an Ed25519 private key?).")
    return base64.b64encode(out.stdout).decode("ascii")


def manifest_bytes(zip_path: Path, min_installed: str | None) -> tuple[bytes, str]:
    match = _ZIP.fullmatch(zip_path.name)
    if not match:
        raise ToolError("The ZIP must be named OfflineDocTools-<version>-win64.zip.")
    version = match.group(1)
    package = zip_path.read_bytes()
    data = {
        "format": mf.FORMAT,
        "app": mf.APP_ID,
        "channel": "stable",
        "version": version,
        "package": {
            "name": zip_path.name,
            "sha256": hashlib.sha256(package).hexdigest(),
            "size": len(package),
        },
        "updater_protocol": 1,
    }
    if min_installed:
        data["min_installed_version"] = min_installed
    return json.dumps(data, sort_keys=True, indent=2).encode("utf-8"), version


def source_version() -> str:
    text = (ROOT / "src" / "offline_doc_tools" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def release(
    zip_path: Path,
    key: Path,
    min_installed: str | None,
    out_root: Path,
    *,
    check_version: bool = True,
) -> Path:
    raw, version = manifest_bytes(zip_path, min_installed)
    if check_version and version != source_version():
        raise ToolError(
            f"The ZIP is version {version} but the source says {source_version()}: a client "
            "that installs it would report a different version and roll the update back."
        )
    out = out_root / version
    out.mkdir(parents=True, exist_ok=True)
    manifest_path, signature_path = out / mf.MANIFEST_ASSET, out / mf.SIGNATURE_ASSET
    manifest_path.write_bytes(raw)
    signature_path.write_text(sign(raw, key) + "\n", encoding="ascii")
    verify(manifest_path, signature_path, zip_path, public_key_hex(key))
    return out


def verify(manifest_path: Path, signature_path: Path, zip_path: Path, key_hex: str) -> mf.Manifest:
    """Check a release with the application's own code. Raises ToolError with the reason."""
    try:
        manifest = mf.verify_manifest(
            manifest_path.read_bytes(),
            signature_path.read_text(encoding="ascii"),
            [bytes.fromhex(key_hex)],
        )
        if zip_path.name != manifest.package_name:
            raise ToolError("The ZIP name differs from the one in the manifest.")
        with tempfile.TemporaryDirectory() as work:
            pk.stage_package(zip_path, manifest, Path(work) / "stage")
    except mf.ManifestError as exc:
        raise ToolError(f"The manifest or signature is not acceptable ({exc.kind}).") from None
    except pk.PackageError as exc:
        raise ToolError(f"The package is not acceptable ({exc.kind}).") from None
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("keygen", help="create the release signing key")
    gen.add_argument("--key", type=Path, required=True)
    rel = commands.add_parser("release", help="write and verify the signed manifest for a ZIP")
    rel.add_argument("--zip", type=Path, required=True, dest="zip_path")
    rel.add_argument("--key", type=Path, required=True)
    rel.add_argument("--min-installed", default=None)
    rel.add_argument("--out", type=Path, default=ROOT / "dist" / "release-assets")
    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            public = keygen(args.key)
            print(f"Private key written to {args.key}.")
            print("KEEP IT OFFLINE AND BACKED UP; never commit it.")
            print("Put this PUBLIC key into TRUSTED_PUBLIC_KEYS in updater/config.py:")
            print(f'    "{public}"')
        else:
            if args.min_installed and mf.parse_version(args.min_installed) is None:
                raise ToolError("--min-installed must look like 1.2.3.")
            out = release(args.zip_path, args.key, args.min_installed, args.out)
            print("Verified. Upload these three files to a release of the private repository:")
            for name in (args.zip_path, out / mf.MANIFEST_ASSET, out / mf.SIGNATURE_ASSET):
                print(f"    {name}")
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
