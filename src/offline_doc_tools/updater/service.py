"""The update workflow, without any GUI: check, download, verify, stage, hand over to the helper.

Order matters and is the security design:

1. The release listing is untrusted. Only the signed manifest (checked first, against the public
   keys built into the application) says what the update is.
2. The package is downloaded to a staging folder *next to* the installation, then its size and
   SHA-256 are compared with the signed manifest and its archive is validated, before extraction.
3. Only then is the *staged* new executable started with ``--apply-update``; it replaces the
   installation after this application has exited (see helper.py and installer.py).

Nothing here is needed to convert documents; every failure becomes a message, never an exception
for the caller. Messages name no path, URL or token.
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import manifest as mf
from . import package as pk
from .credentials import TokenStore
from .github import ReleaseClient, UpdateError

log = logging.getLogger(__name__)

STAGING_NAME = "update-staging"
RESULT_NAME = "update-result.json"
_MAX_SIGNATURE_BYTES = 1024


class CheckStatus(enum.Enum):
    NOT_CONFIGURED = "not configured"
    NO_TOKEN = "no token"  # noqa: S105 (a state name, not a secret)
    UP_TO_DATE = "up to date"
    NEWER = "newer version available"
    MANUAL_UPDATE_NEEDED = "manual update needed"
    FAILED = "failed"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    message: str
    version: str | None = None
    manifest: mf.Manifest | None = None
    package_asset_id: int | None = None
    kind: str | None = None


@dataclass(frozen=True)
class StagedUpdate:
    payload: Path
    manifest: mf.Manifest


class UpdateFailure(Exception):
    """A download or staging step failed; ``str(exc)`` is safe to show."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


_MANIFEST_MESSAGES = {
    "signature": "The update's signature could not be verified, so it was ignored.",
    "format": "The update information is not valid, so it was ignored.",
    "unsupported": "This update needs a newer updater and must be installed manually.",
    "unconfigured": "Updates are not configured in this build.",
}
_PACKAGE_MESSAGES = {
    "size": "The downloaded update has the wrong size, so it was discarded.",
    "hash": "The downloaded update does not match its signature, so it was discarded.",
    "archive": "The downloaded update is not a valid package, so it was discarded.",
    "space": "There is not enough free disk space for the update.",
    "filesystem": "The update could not be saved or unpacked on this disk.",
}


def cleanup_staging(staging_dir: Path) -> None:
    """Remove an earlier update's staging folder (it cannot be removed by the helper that ran
    from it). Failures are ignored: it is retried later."""
    if staging_dir.name == STAGING_NAME:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _retry_text(error: UpdateError) -> str:
    if error.kind == "rate_limited" and error.retry_after:
        minutes = max(1, round(error.retry_after / 60))
        return f"GitHub is limiting requests right now. Try again in about {minutes} minute(s)."
    return error.message


class UpdateService:
    def __init__(
        self,
        *,
        repository: str,
        keys: Sequence[bytes],
        token_store: TokenStore,
        installed_version: str,
        staging_dir: Path,
        client_factory: Callable[[str, str], ReleaseClient] = ReleaseClient,
        popen: Callable = subprocess.Popen,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        if staging_dir.name != STAGING_NAME:
            raise ValueError("unexpected staging folder")  # it is deleted recursively
        self._staging = staging_dir
        self._repository = repository
        self._keys = tuple(keys)
        self._store = token_store
        self._installed = installed_version
        self._client_factory = client_factory
        self._popen = popen
        self._free_bytes = free_bytes
        self._client: ReleaseClient | None = None

    # --- configuration and token -------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self._repository) and bool(self._keys)

    def has_token(self) -> bool:
        return self._store.load() is not None

    def save_token(self, text: str) -> None:
        """Raises ValueError for text that cannot be a token and OSError if it cannot be kept."""
        self._store.save(text)

    # --- check -------------------------------------------------------------------------------

    def check(self) -> CheckResult:
        if not self.configured():
            return CheckResult(CheckStatus.NOT_CONFIGURED, _MANIFEST_MESSAGES["unconfigured"])
        token = self._store.load()
        if token is None:
            return CheckResult(
                CheckStatus.NO_TOKEN, "Enter the update access token to check for updates."
            )
        try:
            self._client = self._client_factory(self._repository, token)
            return self._check(self._client)
        except UpdateError as exc:
            return CheckResult(CheckStatus.FAILED, _retry_text(exc), kind=exc.kind)
        except mf.ManifestError as exc:
            return CheckResult(CheckStatus.FAILED, _MANIFEST_MESSAGES[exc.kind], kind=exc.kind)
        except Exception as exc:  # a bug must not take the application down
            log.error("Update check failed unexpectedly: %s", type(exc).__name__)
            return CheckResult(
                CheckStatus.FAILED, "The update check failed unexpectedly.", kind="bug"
            )

    def _check(self, client: ReleaseClient) -> CheckResult:
        release = client.latest_release()
        by_name = {asset.name: asset for asset in release.assets}
        manifest_asset = by_name.get(mf.MANIFEST_ASSET)
        signature_asset = by_name.get(mf.SIGNATURE_ASSET)
        if manifest_asset is None or signature_asset is None:
            return CheckResult(
                CheckStatus.FAILED,
                "This release has no signed update manifest, so it was ignored.",
                kind="no_manifest",
            )
        if (
            manifest_asset.size > mf.MAX_MANIFEST_BYTES
            or signature_asset.size > _MAX_SIGNATURE_BYTES
        ):
            raise mf.ManifestError("format")
        raw = client.fetch_asset(manifest_asset.id, max_bytes=mf.MAX_MANIFEST_BYTES)
        signature = client.fetch_asset(signature_asset.id, max_bytes=_MAX_SIGNATURE_BYTES)
        manifest = mf.verify_manifest(raw, signature.decode("ascii", "replace"), self._keys)
        verdict = mf.assess(manifest, self._installed, release.tag)
        if verdict is mf.Applicability.UP_TO_DATE:
            return CheckResult(
                CheckStatus.UP_TO_DATE,
                f"You have the latest version ({self._installed}).",
                version=manifest.version,
            )
        if verdict is mf.Applicability.MANUAL_UPDATE_NEEDED:
            return CheckResult(
                CheckStatus.MANUAL_UPDATE_NEEDED,
                f"Version {manifest.version} is available but must be installed manually "
                f"because this version is too old for an automatic update.",
                version=manifest.version,
            )
        package = by_name.get(manifest.package_name)
        if package is None or package.size != manifest.package_size:
            raise mf.ManifestError("format")  # the release does not hold the package it announces
        return CheckResult(
            CheckStatus.NEWER,
            f"Version {manifest.version} is available (you have {self._installed}).",
            version=manifest.version,
            manifest=manifest,
            package_asset_id=package.id,
        )

    # --- download and stage ------------------------------------------------------------------

    def download_and_stage(
        self,
        found: CheckResult,
        install_dir: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> StagedUpdate:
        """Download, verify and extract the update next to ``install_dir``. Raises UpdateFailure."""
        if found.status is not CheckStatus.NEWER or found.manifest is None or self._client is None:
            raise UpdateFailure("There is no verified update to download.", "state")
        manifest = found.manifest
        root = self._staging
        shutil.rmtree(root, ignore_errors=True)
        try:
            root.mkdir(parents=True)
            free = (self._free_bytes or (lambda p: shutil.disk_usage(p).free))(root)
            if free < manifest.package_size + pk.SPACE_MARGIN:
                raise UpdateFailure(_PACKAGE_MESSAGES["space"], "space")
            archive = root / "package.zip"
            self._client.download_asset(
                found.package_asset_id,
                archive,
                max_bytes=manifest.package_size,
                expected_size=manifest.package_size,
                progress=progress,
                cancel=cancel,
            )
            payload = pk.stage_package(archive, manifest, root / "payload")
            archive.unlink(missing_ok=True)
            return StagedUpdate(payload, manifest)
        except UpdateFailure:
            shutil.rmtree(root, ignore_errors=True)
            raise
        except UpdateError as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise UpdateFailure(_retry_text(exc), exc.kind) from None
        except pk.PackageError as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise UpdateFailure(_PACKAGE_MESSAGES[exc.kind], exc.kind) from None
        except OSError:
            shutil.rmtree(root, ignore_errors=True)
            raise UpdateFailure(_PACKAGE_MESSAGES["filesystem"], "filesystem") from None

    # --- hand over ---------------------------------------------------------------------------

    def launch_helper(
        self,
        staged: StagedUpdate,
        install_dir: Path,
        result_file: Path,
        parent_pid: int | None = None,
    ) -> None:
        """Start the staged executable as the replacement helper. The caller then exits."""
        helper_exe = staged.payload / "OfflineDocTools.exe"
        command = [
            str(helper_exe),
            "--apply-update",
            "--protocol", str(staged.manifest.updater_protocol),
            "--install-dir", str(install_dir),
            "--payload", str(staged.payload),
            "--parent-pid", str(parent_pid or os.getpid()),
            "--result-file", str(result_file),
            "--from-version", self._installed,
            "--to-version", staged.manifest.version,
        ]  # fmt: skip
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        self._popen(
            command,
            cwd=str(staged.payload),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
