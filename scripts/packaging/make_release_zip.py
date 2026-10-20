r"""Pack the built bundle into a ZIP: ``python scripts\packaging\make_release_zip.py [--suffix X]``.

Run it after ``build_windows.py``. The ZIP holds the whole ``dist\OfflineDocTools\`` folder (never
only the EXE) under a top-level ``OfflineDocTools\`` folder, plus the repository's README.md and
docs/CLIENT-START-HERE.md (as START-HERE.md), which name the prerequisites. The bundle is checked
against bundle_policy first. The result is
``dist\release\OfflineDocTools-<version>-win64[-<suffix>].zip``; its path, size and SHA-256 are
printed. It is unsigned.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

import bundle_policy as policy

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "dist" / policy.BUNDLE_NAME
OUT_DIR = ROOT / "dist" / "release"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suffix", default="", help="added to the file name, e.g. update-check")
    args = parser.parse_args(argv)

    extras = {
        "README.md": ROOT / "README.md",
        "START-HERE.md": ROOT / "docs" / "CLIENT-START-HERE.md",
    }
    problems = [] if BUNDLE.is_dir() else [f"{BUNDLE} does not exist: build it first"]
    if not problems:
        problems = policy.find_violations(BUNDLE) + [
            f"missing: {name}" for name in policy.missing_files(BUNDLE)
        ]
    problems += [
        f"{name} source file is missing" for name, path in extras.items() if not path.is_file()
    ]
    if problems:
        print("\n".join(f"ZIP REFUSED: {p}" for p in problems))
        return 1

    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    name = f"{policy.BUNDLE_NAME}-{version}-win64{'-' + args.suffix if args.suffix else ''}.zip"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / name

    top = Path(policy.BUNDLE_NAME)
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:  # never overwrites
        for file in sorted(p for p in BUNDLE.rglob("*") if p.is_file()):
            archive.write(file, (top / file.relative_to(BUNDLE)).as_posix())
        for name, path in extras.items():
            archive.write(path, (top / name).as_posix())

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"ZIP: {target}\nSize: {target.stat().st_size} bytes\nSHA-256: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
