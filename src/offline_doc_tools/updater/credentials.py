"""Storage of the update access token, protected for the current Windows user with DPAPI.

The token is a read-only GitHub token for the private release repository. It is typed in by the
user (never built into the application), kept only in an encrypted file under the user's profile,
and sent to ``api.github.com`` only. It is never logged.
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import sys
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,255}")
_ENTROPY = b"OfflineDocTools update token v1"
_UI_FORBIDDEN = 0x1


def clean_token(text: object) -> str | None:
    """The token with surrounding whitespace removed, or None if it cannot be a GitHub token."""
    if not isinstance(text, str):
        return None
    token = text.strip()
    return token if _TOKEN.fullmatch(token) else None


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _run_dpapi(function_name: str, data: bytes, entropy: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("Windows data protection is not available")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, keep_alive = _blob(data)
    extra, keep_extra = _blob(entropy)
    out = _Blob()
    function = getattr(crypt32, function_name)
    ok = function(
        ctypes.byref(source),
        None,
        ctypes.byref(extra),
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(out),
    )
    del keep_alive, keep_extra
    if not ok:
        raise OSError("Windows data protection failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_protect(data: bytes, entropy: bytes = _ENTROPY) -> bytes:
    # CryptProtectData(in, description, entropy, reserved, prompt, flags, out)
    return _run_dpapi("CryptProtectData", data, entropy)


def dpapi_unprotect(blob: bytes, entropy: bytes = _ENTROPY) -> bytes:
    return _run_dpapi("CryptUnprotectData", blob, entropy)


class TokenStore:
    def __init__(
        self,
        path: Path,
        protect: Callable[[bytes], bytes] = dpapi_protect,
        unprotect: Callable[[bytes], bytes] = dpapi_unprotect,
    ) -> None:
        self._path = path
        self._protect = protect
        self._unprotect = unprotect

    def save(self, token: str) -> None:
        """Raises ValueError for something that is not a token, OSError if it cannot be stored."""
        cleaned = clean_token(token)
        if cleaned is None:
            raise ValueError("not a token")
        blob = self._protect(cleaned.encode("ascii"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_bytes(blob)
        temporary.replace(self._path)

    def load(self) -> str | None:
        try:
            return clean_token(self._unprotect(self._path.read_bytes()).decode("ascii"))
        except (OSError, ValueError):
            return None

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)
