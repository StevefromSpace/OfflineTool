"""Verify a downloaded update package and extract it into a staging folder.

Nothing is extracted until the file's size and SHA-256 equal the signed manifest and every archive
entry has passed the checks below. The archive is never trusted to name where files go: entries are
validated as relative paths under one ``OfflineDocTools`` folder and written one by one with
exclusive creation. The package is only ever extracted, never executed, here.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

from .manifest import Manifest

TOP = "OfflineDocTools"
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 1024**3
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_NAME_BYTES = 200
SPACE_MARGIN = 64 * 1024**2
_CHUNK = 1 << 20
_REQUIRED_FILES = ("OfflineDocTools.exe", "THIRD-PARTY-NOTICES.txt")
_REQUIRED_FOLDERS = ("_internal", "licenses")
_FORBIDDEN_SUFFIXES = frozenset(
    {".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".lnk", ".scr", ".com"}
    | {".msi", ".reg", ".hta", ".jar", ".url", ".pif", ".gadget"}
)
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")}
    | {f"LPT{i}" for i in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")}
)


class PackageError(Exception):
    """``kind``: size, hash, archive, space or filesystem. The text names no path."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def required_space(uncompressed: int) -> int:
    """Space for the extracted copy and for the copy that the helper installs from it."""
    return 2 * uncompressed + SPACE_MARGIN


def _free_bytes(path: Path) -> int:
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def _verify_file(package: Path, manifest: Manifest) -> None:
    try:
        if package.stat().st_size != manifest.package_size:
            raise PackageError("size")
        digest = hashlib.sha256()
        with package.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        raise PackageError("filesystem") from None
    if digest.hexdigest() != manifest.package_sha256:
        raise PackageError("hash")


def _check_name(name: str) -> tuple[tuple[str, ...], bool]:
    """The path components of an entry and whether it is a folder; PackageError if unsafe."""
    bad = PackageError("archive")
    if not name or len(name.encode("utf-8", "replace")) > MAX_NAME_BYTES:
        raise bad
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise bad
    if "\\" in name or ":" in name or name.startswith("/"):
        raise bad
    is_folder = name.endswith("/")
    parts = tuple((name[:-1] if is_folder else name).split("/"))
    for part in parts:
        if part in ("", ".", "..") or part.endswith((".", " ")) or len(part) > 255:
            raise bad
        if part.split(".")[0].strip().upper() in _RESERVED:
            raise bad
    if parts[0] != TOP:
        raise bad
    return parts, is_folder


def _inspect(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ENTRIES:
        raise PackageError("archive")
    entries, seen, folders, file_set, total = [], set(), set(), set(), 0
    for info in infos:
        parts, is_folder = _check_name(info.filename)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise PackageError("archive")  # symbolic link, device or other special file
        if info.flag_bits & 0x1 or info.compress_type not in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
        ):
            raise PackageError("archive")
        lowered = tuple(p.lower() for p in parts)
        if lowered in seen:
            raise PackageError("archive")  # same name twice, or differing only in case
        seen.add(lowered)
        for depth in range(1, len(lowered)):
            folders.add(lowered[:depth])
        if is_folder:
            folders.add(lowered)
        else:
            file_set.add(lowered)
            if (
                info.file_size > MAX_FILE_BYTES
                or Path(parts[-1]).suffix.lower() in _FORBIDDEN_SUFFIXES
            ):
                raise PackageError("archive")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise PackageError("archive")
        entries.append((info, parts, is_folder))
    if file_set & folders:
        raise PackageError("archive")  # a file with the name of a folder
    names = {"/".join(p) for p in file_set}
    wanted = {f"{TOP}/{n}".lower() for n in _REQUIRED_FILES}
    if not wanted <= names or not all(
        any(n.startswith(f"{TOP}/{folder}/".lower()) for n in names) for folder in _REQUIRED_FOLDERS
    ):
        raise PackageError("archive")
    return entries


def _uncompressed_total(entries) -> int:
    return sum(info.file_size for info, _, is_folder in entries if not is_folder)


def stage_package(
    package: Path,
    manifest: Manifest,
    destination: Path,
    *,
    free_bytes: Callable[[Path], int] = _free_bytes,
) -> Path:
    """Check ``package`` against the signed manifest and extract it to ``destination/TOP``.

    Returns the extracted folder. On any failure nothing is left behind in ``destination``."""
    _verify_file(package, manifest)
    try:
        with zipfile.ZipFile(package) as archive:
            entries = _inspect(archive)
            if free_bytes(destination) < required_space(_uncompressed_total(entries)):
                raise PackageError("space")
            return _extract(archive, entries, destination)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
        raise PackageError("archive") from None
    except OSError:
        raise PackageError("filesystem") from None


def _extract(archive, entries, destination: Path) -> Path:
    created = not destination.exists()
    root = destination / TOP
    try:
        if root.exists():
            shutil.rmtree(root)
        base = destination.resolve()
        for info, parts, is_folder in entries:
            target = destination.joinpath(*parts)
            if base not in target.resolve().parents and target.resolve() != base:
                raise PackageError("archive")
            if is_folder:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info) as source, target.open("xb") as sink:
                while chunk := source.read(_CHUNK):
                    written += len(chunk)
                    if written > info.file_size:
                        raise PackageError("archive")
                    sink.write(chunk)
            if written != info.file_size:
                raise PackageError("archive")
        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        if created:
            shutil.rmtree(destination, ignore_errors=True)
        raise
