"""What the owner must set before this build can update itself.

Both values are public information and belong in the source of the build that is delivered:

* ``UPDATE_REPOSITORY``: ``owner/name`` of the PRIVATE GitHub repository whose releases carry the
  signed updates. Empty means updates are not configured and nothing is ever requested.
* ``TRUSTED_PUBLIC_KEYS``: hex of the raw 32-byte Ed25519 public keys whose signatures are accepted
  (more than one allows a key change). The matching private key stays with the owner, offline, and
  is never part of the repository or of any package. Empty means nothing can be trusted.

Without both, the updater stays off. See docs/UPDATES.md.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

UPDATE_REPOSITORY = ""
TRUSTED_PUBLIC_KEYS: tuple[str, ...] = ()

_HEX_KEY = re.compile(r"[0-9a-f]{64}")


def trusted_keys(keys: Sequence[str] | None = None) -> tuple[bytes, ...]:
    """The configured keys as bytes; entries that are not 64 hex digits are ignored."""
    source = TRUSTED_PUBLIC_KEYS if keys is None else keys
    return tuple(bytes.fromhex(k) for k in source if isinstance(k, str) and _HEX_KEY.fullmatch(k))


def repository() -> str:
    return UPDATE_REPOSITORY


def is_configured(repo: str | None = None, keys: Sequence[str] | None = None) -> bool:
    return bool(UPDATE_REPOSITORY if repo is None else repo) and bool(trusted_keys(keys))
