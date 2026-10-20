"""What may, and what may not, end up in the Windows bundle.

One set of rules, three users: the PyInstaller spec uses it to filter PyInstaller's file lists,
``build_windows.py`` uses it to check the finished bundle, and the tests pin the rules down.
Standard library only, because the spec runs inside the build venv and the build script under
whatever Python the developer happens to use.

Why so strict: the app imports only QtCore, QtGui and QtWidgets. Its only network use is the
click-started update check (update_check.py), which needs Python's ``ssl`` and so OpenSSL 3 and
nothing else. Every extra Qt module, plugin or TLS library in the bundle is code to patch, a
licence notice to carry and a possible network path, so anything not listed here is rejected
(fail closed).
"""

from __future__ import annotations

import re
from pathlib import Path

#: Name of the bundle folder (and of the executable inside it).
BUNDLE_NAME = "OfflineDocTools"

#: Folder PyInstaller 6 puts everything except the executable into.
CONTENTS_DIR = "_internal"

#: Licence texts the bundle must carry in ``licenses\``, verbatim from scripts/packaging/licenses.
#: Qt for Python and the Qt libraries are used under the LGPL-3.0, whose section 4(b) requires a
#: copy of it and of the GPL-3.0 it incorporates to accompany the program. The OpenSSL 3 libraries
#: that Python's ssl module needs are Apache-2.0, which requires a copy of the licence.
REQUIRED_LICENCE_TEXTS = ("LGPL-3.0.txt", "GPL-3.0.txt", "OpenSSL-Apache-2.0.txt")

#: Third-party notices shipped beside the EXE, so that the LGPL notice (Qt and PySide6 are used) is
#: the first thing seen in the bundle folder. Verbatim upstream text; its header names the sources.
ROOT_NOTICE = "THIRD-PARTY-NOTICES.txt"

#: What the update check's HTTPS support needs, as patterns below the contents folder. Without
#: them every check would fail as if the user were offline, so a bundle lacking them is rejected.
REQUIRED_TLS_FILES = ("_ssl.pyd", "libssl-3*.dll", "libcrypto-3*.dll")

#: Qt modules the app imports (lower case, as in Qt6<Name>.dll / Qt<Name>.pyd).
ALLOWED_QT_MODULES = frozenset({"core", "gui", "widgets"})

#: The only Qt plugins that ship, as "<plugin type>/<file name>" in lower case.
#: qwindows is the Windows platform plugin and qmodernwindowsstyle the Windows 11 look. qoffscreen
#: costs 112 KiB and lets the bundle be smoke-tested without a display (QT_QPA_PLATFORM=offscreen).
#: There are deliberately no image-format plugins (JPEG, TIFF, WebP, SVG ... carry their own
#: licence notices) and no TLS or network-information plugins: the app loads no images.
ALLOWED_QT_PLUGINS = frozenset(
    {
        "platforms/qwindows.dll",
        "platforms/qoffscreen.dll",
        "styles/qmodernwindowsstyle.dll",
    }
)

#: Qt modules for the PyInstaller analysis to ignore. PySide6/__init__.py contains a
#: ``from . import QtNetwork`` that static analysis cannot tell is conditional, which is how a
#: default build ends up with Qt Network, OpenSSL and the TLS plugins. This list is not
#: exhaustive on purpose; the check of the finished bundle is.
EXCLUDED_QT_MODULES = tuple(
    f"PySide6.{name}"
    for name in (
        "Qt3DAnimation",
        "Qt3DCore",
        "Qt3DExtras",
        "Qt3DInput",
        "Qt3DLogic",
        "Qt3DRender",
        "QtAsyncio",
        "QtAxContainer",
        "QtBluetooth",
        "QtCanvasPainter",
        "QtCharts",
        "QtConcurrent",
        "QtDBus",
        "QtDataVisualization",
        "QtDesigner",
        "QtGraphs",
        "QtGraphsWidgets",
        "QtHelp",
        "QtHttpServer",
        "QtLabsStyleKit",
        "QtLocation",
        "QtMultimedia",
        "QtMultimediaWidgets",
        "QtNetwork",
        "QtNetworkAuth",
        "QtNfc",
        "QtOpenGL",
        "QtOpenGLWidgets",
        "QtPdf",
        "QtPdfWidgets",
        "QtPositioning",
        "QtPrintSupport",
        "QtQml",
        "QtQuick",
        "QtQuick3D",
        "QtQuickControls2",
        "QtQuickTest",
        "QtQuickWidgets",
        "QtRemoteObjects",
        "QtScxml",
        "QtSensors",
        "QtSerialBus",
        "QtSerialPort",
        "QtSpatialAudio",
        "QtSql",
        "QtStateMachine",
        "QtSvg",
        "QtSvgWidgets",
        "QtTest",
        "QtTextToSpeech",
        "QtUiTools",
        "QtWebChannel",
        "QtWebEngineCore",
        "QtWebEngineQuick",
        "QtWebEngineWidgets",
        "QtWebSockets",
        "QtWebView",
        "QtXml",
    )
)

#: Standard-library modules for the PyInstaller analysis to ignore, all optional imports:
#: - _hashlib: a second user of libcrypto; without it ``hashlib`` falls back to its built-in
#:   implementations. (``ssl`` and ``_ssl`` are deliberately bundled for the update check.)
#: - _decimal: embeds libmpdec, whose BSD notice Python's LICENSE.txt does not carry. ``decimal``
#:   falls back to the pure-Python ``_pydecimal``, which only ``random`` -> ``statistics``
#:   -> ``fractions`` would ever load.
EXCLUDED_STDLIB_MODULES = ("_hashlib", "_decimal")

_PYSIDE_TREES = ("pyside6", "shiboken6")
_PLUGIN_PATH = re.compile(r"(?:^|/)plugins/([^/]+)/([^/]+)$")
_QT_LIBRARY = re.compile(r"^qt6?(?P<module>[a-z0-9]+)\.(?:dll|pyd)$")
_MSVC_RUNTIME = re.compile(r"^(?:msvcp140(?:_\d)?|vcruntime140(?:_\d)?|concrt140)\.dll$")
# OpenSSL other than the 3.x libraries the update check uses, and _hashlib.
_OPENSSL = re.compile(r"^(?:lib(?:ssl|crypto)-(?!3(?:-x64)?\.dll$)[\w-]+\.dll|_hashlib\.pyd)$")

#: Rules for a file name wherever it appears: (pattern, reason).
_ANYWHERE = (
    (re.compile(r"^qt6?network"), "Qt Network: the app never opens network connections"),
    (re.compile(r"^qt6?webengine|^qtwebengineprocess\.exe$|\.pak$"), "Qt WebEngine (Chromium)"),
    (_OPENSSL, "OpenSSL other than the 3.x libraries of the update check, or _hashlib"),
    (re.compile(r"^_decimal\.pyd$"), "libmpdec: its licence notice is not shipped (see policy)"),
    (re.compile(r"^(?:soffice|libreoffice)"), "LibreOffice is installed separately, never bundled"),
)


def _parts(path: str) -> list[str]:
    """Lower-case path components, using "/" whatever separator the caller had."""
    return [p for p in path.replace("\\", "/").lower().split("/") if p not in ("", ".")]


def _binding_tree_reason(parts: list[str]) -> str | None:
    """Rules for everything below PySide6/ and shiboken6/ (Qt's own files)."""
    name = parts[-1]
    plugin = _PLUGIN_PATH.search("/".join(parts))
    if plugin:
        if f"{plugin[1]}/{plugin[2]}" in ALLOWED_QT_PLUGINS:
            return None
        return f"Qt plugin {plugin[1]}/{plugin[2]} is not needed by the app"
    if len(parts) > 2:  # a sub-folder other than plugins: translations, qml, resources ...
        return f"unexpected Qt data folder {parts[1]}/ (the app loads none)"
    if _MSVC_RUNTIME.match(name):
        return None
    library = _QT_LIBRARY.match(name)
    if library:
        if library["module"] in ALLOWED_QT_MODULES:
            return None
        return f"Qt module {library['module']} is not used by the app"
    if name in ("pyside6.abi3.dll", "shiboken6.abi3.dll", "shiboken.pyd"):
        return None
    if name == "opengl32sw.dll":
        return "software OpenGL (20 MiB): QtWidgets paints without OpenGL"
    return f"{name} is not on the list of files the app needs"


def unwanted_reason(path: str) -> str | None:
    """Why ``path`` (relative to the bundle's contents folder) must not be bundled, or None."""
    parts = _parts(path)
    if not parts:
        return None
    if parts[0] in _PYSIDE_TREES and len(parts) > 1:
        reason = _binding_tree_reason(parts)
        if reason:
            return reason
    name = parts[-1]
    for pattern, reason in _ANYWHERE:
        if pattern.search(name):
            return reason
    library = _QT_LIBRARY.match(name)
    if library and library["module"] not in ALLOWED_QT_MODULES and "plugins" not in parts:
        return f"Qt module {library['module']} is not used by the app"
    return None


def keep_bundle_entry(path: str) -> bool:
    """For the spec: keep this file-list entry? (``path`` is its destination in the bundle)."""
    return unwanted_reason(path) is None


def remove_ms_runtime(root: Path) -> list[str]:
    """Delete the Microsoft Visual C++ runtime DLLs from a bundle (relative paths returned).

    For a build that relies on the user having installed Microsoft's *Visual C++ Redistributable
    (x64)*, instead of redistributing these files itself. See docs/DISTRIBUTION.md."""
    removed = []
    for file in sorted(root.rglob("*.dll")):
        if _MSVC_RUNTIME.match(file.name.lower()):
            removed.append(file.relative_to(root).as_posix())
            file.unlink()
    return removed


def find_violations(root: Path) -> list[str]:
    """Walk a finished bundle and describe every file the policy rejects."""
    problems = []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(root)
        if relative.parts[0] == CONTENTS_DIR:
            relative = Path(*relative.parts[1:])
        elif relative.parts[0] == "licenses":
            continue
        reason = unwanted_reason(relative.as_posix())
        if reason:
            problems.append(f"{file.relative_to(root).as_posix()}: {reason}")
    return problems


def missing_files(root: Path) -> list[str]:
    """Files that must be in a good bundle (positive counterpart of find_violations)."""
    contents = root / CONTENTS_DIR
    required = [
        root / f"{BUNDLE_NAME}.exe",
        contents / "python3.dll",
        contents / "base_library.zip",
        *(contents / "PySide6" / f"Qt6{m.capitalize()}.dll" for m in sorted(ALLOWED_QT_MODULES)),
        *(contents / "PySide6" / f"Qt{m.capitalize()}.pyd" for m in sorted(ALLOWED_QT_MODULES)),
        contents / "shiboken6" / "shiboken6.abi3.dll",
        *(contents / "PySide6" / "plugins" / p for p in sorted(ALLOWED_QT_PLUGINS)),
        *(root / "licenses" / name for name in REQUIRED_LICENCE_TEXTS),
        root / ROOT_NOTICE,
    ]
    missing = [p.relative_to(root).as_posix() for p in required if not p.is_file()]
    missing += [
        f"{CONTENTS_DIR}/{pattern}"
        for pattern in REQUIRED_TLS_FILES
        if not any(contents.glob(pattern))
    ]
    return missing
