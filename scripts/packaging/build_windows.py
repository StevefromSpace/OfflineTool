r"""Build the Windows bundle: ``python scripts\packaging\build_windows.py`` (Windows, Python 3.11+).

Creates ``build\packaging-venv`` with the exact pins of the ``build`` extra in pyproject.toml
(pip downloads them: the only network use of the build), runs PyInstaller with the committed
spec, adds the third-party licence texts and checks the finished bundle against bundle_policy.
The result is ``dist\OfflineDocTools\``. It is unsigned and has no installer.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import bundle_policy as policy

ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / "build" / "packaging-venv"
DIST = ROOT / "dist"
LICENCE_TEXTS = ROOT / "scripts" / "packaging" / "licenses"
LICENCE_NAMES = ("licen", "copying", "notice")
# Distributions whose licence texts the bundle carries (their code is in it).
SHIPPED = ("pyside6_essentials", "shiboken6")


def run(*args: str) -> None:
    subprocess.run(args, check=True, cwd=ROOT)  # noqa: S603 (argument list, no shell)


def build_pins() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]["build"]


def licence_files(site_packages: Path) -> list[Path]:
    found = []
    for dist_info in site_packages.glob("*.dist-info"):
        if dist_info.name.split("-")[0].lower().replace("-", "_") in SHIPPED:
            found += [
                p
                for p in dist_info.rglob("*")
                if p.is_file() and p.name.lower().startswith(LICENCE_NAMES)
            ]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--vc-runtime-prerequisite",
        action="store_true",
        help="leave Microsoft's C++ runtime DLLs out; the PC needs the Redistributable",
    )
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.version_info < (3, 11):
        print("The bundle is built on Windows with Python 3.11 or newer.")
        return 1
    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        run(sys.executable, "-m", "venv", str(VENV))
    run(str(python), "-m", "pip", "install", "--disable-pip-version-check", *build_pins())
    shutil.rmtree(DIST / policy.BUNDLE_NAME, ignore_errors=True)
    run(
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        str(ROOT / "scripts" / "packaging" / "offline_doc_tools.spec"),
    )

    bundle = DIST / policy.BUNDLE_NAME
    licences = bundle / "licenses"
    licences.mkdir(exist_ok=True)
    for source in licence_files(VENV / "Lib" / "site-packages"):
        shutil.copy2(source, licences / f"{source.parent.parent.name.split('-')[0]}-{source.name}")
    # The wheels ship only Qt's commercial-licence notice; the LGPL-3.0 they are used under, and
    # the GPL-3.0 it incorporates, come from the repository (no download at build time).
    for name in policy.REQUIRED_LICENCE_TEXTS:
        shutil.copy2(LICENCE_TEXTS / name, licences / name)
    shutil.copy2(LICENCE_TEXTS / policy.ROOT_NOTICE, bundle / policy.ROOT_NOTICE)
    # The interpreter's own licence (PyInstaller bundles the Python runtime).
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if candidate.is_file():
            shutil.copy2(candidate, licences / "Python-LICENSE.txt")
            break

    if args.vc_runtime_prerequisite:
        removed = policy.remove_ms_runtime(bundle)
        print(f"Left out {len(removed)} Microsoft runtime files; the PC needs the Redistributable.")

    problems = policy.find_violations(bundle) + [
        f"missing: {name}" for name in policy.missing_files(bundle)
    ]
    if not any(licences.iterdir()):
        problems.append("no licence texts were found to ship")
    for problem in problems:
        print(f"BUNDLE PROBLEM: {problem}")
    print(f"Bundle: {bundle}" if not problems else "Bundle rejected.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
