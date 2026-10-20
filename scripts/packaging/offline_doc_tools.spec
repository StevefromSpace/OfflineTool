# PyInstaller spec for the Windows bundle. Run through scripts\packaging\build_windows.py, which
# provides the isolated build environment; the rules for what may be bundled are in
# bundle_policy.py.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parents[1]  # noqa: F821 (SPECPATH is provided by PyInstaller)
sys.path.insert(0, str(ROOT / "scripts" / "packaging"))
import bundle_policy as policy  # noqa: E402

analysis = Analysis(  # noqa: F821
    [str(ROOT / "scripts" / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    excludes=[*policy.EXCLUDED_QT_MODULES, *policy.EXCLUDED_STDLIB_MODULES],
)
# Fail closed: drop anything the policy rejects (Qt Network, OpenSSL, WebEngine, extra plugins).
analysis.binaries = [e for e in analysis.binaries if policy.keep_bundle_entry(e[0])]
analysis.datas = [e for e in analysis.datas if policy.keep_bundle_entry(e[0])]

exe = EXE(  # noqa: F821
    PYZ(analysis.pure),  # noqa: F821
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=policy.BUNDLE_NAME,
    console=False,
    upx=False,
)
COLLECT(  # noqa: F821
    exe,
    analysis.binaries,
    analysis.datas,
    upx=False,
    name=policy.BUNDLE_NAME,
)
