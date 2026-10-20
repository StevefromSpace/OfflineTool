"""The updater as the application uses it: real configuration, real token store, real folders."""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from . import config, installer
from .credentials import TokenStore
from .helper import MARKER_ENV
from .service import RESULT_NAME, STAGING_NAME, UpdateService, cleanup_staging


def data_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "OfflineDocTools"
    return Path.home() / ".local" / "state" / "offline-doc-tools"


@dataclass
class UpdateRuntime:
    service: UpdateService
    install_dir: Path | None  # None when not running as the packaged application
    result_file: Path
    staging_dir: Path

    def startup_message(self) -> str | None:
        """The outcome of the last update, once; also removes that update's staging folder.

        Only when a result exists (the helper has finished with the staging folder) and this
        copy was not started by a helper that is still waiting for it."""
        if os.environ.get(MARKER_ENV):
            return None
        result = installer.read_result(self.result_file)
        if result is None:
            return None
        cleanup_staging(self.staging_dir)
        with contextlib.suppress(OSError):
            self.result_file.unlink()
        prefix = f"Update to {result.get('to')}: " if result.get("ok") else "Update failed: "
        return prefix + str(result["message"])


def build_runtime() -> UpdateRuntime:
    data = data_directory()
    service = UpdateService(
        repository=config.repository(),
        keys=config.trusted_keys(),
        token_store=TokenStore(data / "update-token.bin"),
        installed_version=__version__,
        staging_dir=data / STAGING_NAME,
    )
    frozen = bool(getattr(sys, "frozen", False))
    return UpdateRuntime(
        service,
        Path(sys.executable).parent if frozen else None,
        data / RESULT_NAME,
        data / STAGING_NAME,
    )
