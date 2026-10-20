r"""End-to-end test of the packaged self-update, on real bundles and real processes.

    python scripts\packaging_smoke\update_e2e.py --old DIR --new DIR --broken DIR

``DIR`` are built bundles (folders that hold OfflineDocTools.exe). OLD is installed, started, then
updated to NEW through the real UpdateService (signed manifest made with OpenSSL, package
downloaded from a local stand-in for GitHub, verified, staged, handed to the staged EXE as the
replacement helper). Then the same is tried with BROKEN, a release that exits at start-up, which
must be rolled back. Run in a temporary workspace; it kills only processes started from there.

What it does NOT cover: GitHub itself and the production key (see docs/UPDATES.md).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from offline_doc_tools.updater import credentials, github, installer, manifest  # noqa: E402
from offline_doc_tools.updater import service as svc  # noqa: E402

TOKEN = "github_pat_" + "E2eTest123" * 6
OBSOLETE = "_internal/obsolete-marker.txt"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        ("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""),
        flush=True,
    )
    if not ok:
        failures.append(name)


def digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def system_tool(*parts: str) -> str:
    return str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", *parts))


def kill_under(folder: Path) -> None:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
        f"$_.ExecutablePath.StartsWith('{folder}', 'OrdinalIgnoreCase') }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    shell = system_tool("WindowsPowerShell", "v1.0", "powershell.exe")
    found = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True
    ).stdout.split()
    for pid in found:
        subprocess.run([system_tool("taskkill.exe"), "/F", "/T", "/PID", pid], capture_output=True)


def openssl() -> str:
    found = shutil.which("openssl")
    if not found:
        raise SystemExit("openssl is needed to sign the test manifest")
    return found


def make_signer(work: Path):
    key = work / "test-key.pem"
    tool = openssl()
    subprocess.run([tool, "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    public = subprocess.run(
        [tool, "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout[-32:]

    def sign(data: bytes) -> bytes:
        message = work / "to-sign.bin"
        message.write_bytes(data)
        raw = subprocess.run(
            [tool, "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(message)],
            check=True,
            capture_output=True,
        ).stdout
        return base64.b64encode(raw)

    return public, sign


def zip_bundle(bundle: Path) -> bytes:
    target = bundle.parent / (bundle.name + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            archive.write(path, f"OfflineDocTools/{path.relative_to(bundle).as_posix()}")
    data = target.read_bytes()
    target.unlink()
    return data


class LocalGitHub:
    """Serves one release from memory, like ReleaseClient does for GitHub."""

    def __init__(self, tag: str, blobs: dict[str, bytes]) -> None:
        self.blobs = {i + 1: (name, data) for i, (name, data) in enumerate(blobs.items())}
        self.release = github.Release(
            tag, tuple(github.Asset(i, name, len(data)) for i, (name, data) in self.blobs.items())
        )

    def latest_release(self):
        return self.release

    def fetch_asset(self, asset_id, *, max_bytes, deadline=0):
        return self.blobs[asset_id][1]

    def download_asset(self, asset_id, target, *, max_bytes, expected_size, **_):
        target.write_bytes(self.blobs[asset_id][1])


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    check(f"waited for {what}", False, f"not within {timeout:g} s")
    return None


def scenario(
    name: str,
    old: Path,
    new: Path,
    work: Path,
    old_version: str,
    new_version: str,
    *,
    expect_ok: bool,
):
    print(f"\n=== {name}", flush=True)
    data = work / "data"
    os.environ["LOCALAPPDATA"] = str(data)
    apps = work / "apps"
    install = apps / "OfflineDocTools"
    shutil.copytree(old, install)
    (install / OBSOLETE).write_text("a file that the new version does not have")
    before = digest(install)
    app_log = data / "OfflineDocTools" / "logs" / "app.log"

    app = subprocess.Popen([str(install / "OfflineDocTools.exe")], cwd=str(install))
    wait_for(
        lambda: app_log.exists() and "Starting version" in app_log.read_text("utf-8", "replace"),
        60,
        "the old app to start",
    )
    check("the old version started", app.poll() is None)

    public, sign = make_signer(work)
    package = zip_bundle(new)
    raw = json.dumps(
        {
            "format": 1,
            "app": "OfflineDocTools",
            "channel": "stable",
            "version": new_version,
            "package": {
                "name": f"OfflineDocTools-{new_version}-win64.zip",
                "sha256": hashlib.sha256(package).hexdigest(),
                "size": len(package),
            },
            "updater_protocol": 1,
        },
        sort_keys=True,
    ).encode()
    release = {
        manifest.MANIFEST_ASSET: raw,
        manifest.SIGNATURE_ASSET: sign(raw),
        f"OfflineDocTools-{new_version}-win64.zip": package,
    }
    fake = LocalGitHub(f"v{new_version}", release)
    store = credentials.TokenStore(work / "token.bin", protect=lambda b: b, unprotect=lambda b: b)
    store.save(TOKEN)
    service = svc.UpdateService(
        repository="owner/private",
        keys=[public],
        token_store=store,
        installed_version=old_version,
        staging_dir=data / "OfflineDocTools" / svc.STAGING_NAME,
        client_factory=lambda repo, token: fake,
    )

    found = service.check()
    check(
        "the signed release is recognised as newer",
        found.status is svc.CheckStatus.NEWER,
        found.message,
    )
    staged = service.download_and_stage(found, install)
    check(
        "the real package passed verification and was staged",
        (staged.payload / "OfflineDocTools.exe").is_file(),
    )
    result_file = data / "OfflineDocTools" / svc.RESULT_NAME
    service.launch_helper(staged, install, result_file, parent_pid=app.pid)
    time.sleep(1.5)
    subprocess.run(
        [system_tool("taskkill.exe"), "/F", "/T", "/PID", str(app.pid)], capture_output=True
    )  # the app quits
    app.wait(timeout=30)

    result = wait_for(lambda: installer.read_result(result_file), 240, "the helper's result")
    if result is None:
        kill_under(work)
        return
    print("    result:", result["code"], "|", result["message"], flush=True)
    leftovers = sorted(p.name for p in apps.iterdir() if p.name != "OfflineDocTools")
    check("no backup or journal is left behind", leftovers == [], str(leftovers))
    update_log = data / "OfflineDocTools" / "logs" / "update.log"
    log_text = update_log.read_text("utf-8", "replace") if update_log.exists() else ""
    if expect_ok:
        check("the update was installed", result["ok"] is True and result["code"] == "installed")
        check("the installation is exactly the new version", digest(install) == digest(new))
        check("the obsolete file is gone", not (install / OBSOLETE).exists())
        check(
            "the NEW package's own helper did the replacement",
            "[new-helper]" in log_text,
            log_text[-300:],
        )
        started = wait_for(
            lambda: f"Starting version {new_version}" in app_log.read_text("utf-8", "replace"),
            30,
            "the new app's log line",
        )
        check("the new version is running", bool(started))
    else:
        check(
            "the failed update was rolled back",
            result["ok"] is False
            and result["rolled_back"] is True
            and result["code"] == "startup_failed",
        )
        check(
            "the old version is exactly restored, obsolete file included", digest(install) == before
        )
    kill_under(work)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--broken", type=Path, required=True)
    parser.add_argument("--old-version", default="0.1.0")
    parser.add_argument("--new-version", default="0.2.0")
    args = parser.parse_args()
    previous = os.environ.get("LOCALAPPDATA")
    root = Path(
        os.path.realpath(tempfile.mkdtemp(prefix="odte-"))
    )  # long form, as processes report
    try:
        for label, new, ok in (
            ("update to a newer version (and its own updater)", args.new, True),
            ("update to a broken version", args.broken, False),
        ):
            work = root / ("good" if ok else "bad")
            work.mkdir()
            scenario(label, args.old, new, work, args.old_version, args.new_version, expect_ok=ok)
    finally:
        kill_under(root)
        if previous is not None:
            os.environ["LOCALAPPDATA"] = previous
        shutil.rmtree(root, ignore_errors=True)
    print(
        "\nUPDATE E2E PASSED"
        if not failures
        else f"\nUPDATE E2E FAILED ({len(failures)}): {failures}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
