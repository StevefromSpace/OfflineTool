"""A small client for the releases of a PRIVATE GitHub repository.

Authentication is a read-only token that the user entered (see credentials.py). Everything in a
response other than the plain facts used below is ignored, in particular every URL in it: asset
addresses are built here from the repository name and the numeric asset id. The token goes to
``api.github.com`` only. An asset download answers with one redirect to a short-lived signed URL
on a ``*.githubusercontent.com`` host; exactly that one redirect is followed, over HTTPS, without
the token. No request ever carries a document name, a path, a version or other data about the user.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import credentials

try:
    import ssl
except ImportError:  # a Python or bundle without TLS support
    ssl = None  # type: ignore[assignment]

API = "https://api.github.com"
TIMEOUT_SECONDS = 15
MAX_JSON_BYTES = 1024 * 1024
MAX_ASSETS = 200
MAX_RETRY_AFTER = 3600
DEADLINE_SMALL = 60.0
DEADLINE_PACKAGE = 3600.0
_CHUNK = 64 * 1024
_REPOSITORY = re.compile(r"[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}")
_TAG = re.compile(r"v?[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
_HEADERS = {"User-Agent": "OfflineDocTools-updater", "X-GitHub-Api-Version": "2022-11-28"}

_MESSAGES = {
    "auth": "The update access token was refused or has expired. Enter a new token.",
    "denied": "Access to the update repository was refused.",
    "not_found": (
        "The update repository or release was not found, or this token has no access to it."
    ),
    "rate_limited": "GitHub is limiting requests right now. Try again later.",
    "network": "The update server could not be reached. Check the internet connection.",
    "bad_answer": "The update server sent an unexpected answer, so it was ignored.",
    "tls": "This build cannot make secure connections, so it cannot check for updates.",
    "too_large": "The update is larger than allowed, so it was not downloaded.",
    "cancelled": "The download was cancelled.",
    "filesystem": "The update could not be saved to disk.",
}


class UpdateError(Exception):
    """``kind`` is one of the keys of _MESSAGES. The text names no URL, path or token."""

    def __init__(self, kind: str, retry_after: int | None = None) -> None:
        super().__init__(_MESSAGES[kind])
        self.kind = kind
        self.message = _MESSAGES[kind]
        self.retry_after = retry_after


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class Release:
    tag: str
    assets: tuple[Asset, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # a redirect becomes an HTTPError that this module inspects itself


def _build_opener() -> urllib.request.OpenerDirector:
    if ssl is None:
        raise UpdateError("tls")
    context = ssl.create_default_context()  # verifies certificates and host names
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _from_http_error(exc: urllib.error.HTTPError) -> UpdateError:
    code, headers = exc.code, exc.headers
    exc.close()
    if code == 401:
        return UpdateError("auth")
    if code in (403, 429):
        retry = headers.get("retry-after") if headers is not None else None
        remaining = headers.get("x-ratelimit-remaining") if headers is not None else None
        if code == 429 or retry or remaining == "0":
            wait = int(retry) if retry and retry.isascii() and retry.isdigit() else None
            return UpdateError("rate_limited", min(wait, MAX_RETRY_AFTER) if wait else None)
        return UpdateError("denied")
    if code == 404:
        return UpdateError("not_found")
    if code >= 500:
        return UpdateError("network")
    return UpdateError("bad_answer")


def _redirect_target(location: object) -> str:
    """The signed download URL if (and only if) it is an HTTPS URL on a GitHub content host."""
    bad = UpdateError("bad_answer")
    if not isinstance(location, str) or not location:
        raise bad
    if any(ord(c) <= 32 or ord(c) == 127 for c in location):
        raise bad
    try:
        parts = urlsplit(location)
        port = parts.port
    except ValueError:
        raise bad from None
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        raise bad
    if not host.endswith(".githubusercontent.com") or host == ".githubusercontent.com":
        raise bad
    return location


class ReleaseClient:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        opener_factory: Callable[[], urllib.request.OpenerDirector] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository or "") or set(repository.split("/")[0]) == {"."}:
            raise ValueError("invalid repository name")
        if set(repository.split("/")[1]) == {"."}:
            raise ValueError("invalid repository name")
        cleaned = credentials.clean_token(token)
        if cleaned is None:
            raise ValueError("invalid token")
        self._repository = repository
        self._token = cleaned
        self._opener_factory = opener_factory or _build_opener
        self._clock = clock

    # --- requests ----------------------------------------------------------------------------

    def _open(self, url: str, headers: dict[str, str]):
        request = urllib.request.Request(url, headers={**_HEADERS, **headers})  # noqa: S310
        try:
            return self._opener_factory().open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            raise exc  # classified by the caller, which knows whether a redirect is expected
        except UpdateError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise UpdateError("network") from None

    def _authorised(self, url: str, accept: str):
        return self._open(url, {"Authorization": f"Bearer {self._token}", "Accept": accept})

    def _read(
        self, reply, limit: int, deadline: float, cancel=None, sink=None, progress=None, total=None
    ):
        started, done, buffer = self._clock(), 0, bytearray()
        while True:
            if cancel is not None and cancel():
                raise UpdateError("cancelled")
            if self._clock() - started > deadline:
                raise UpdateError("network")
            try:
                chunk = reply.read(_CHUNK)
            except (OSError, http.client.HTTPException):
                raise UpdateError("network") from None
            if not chunk:
                return bytes(buffer), done
            done += len(chunk)
            if done > limit:
                raise UpdateError("too_large")
            if total is not None and done > total:
                raise UpdateError("bad_answer")
            if sink is None:
                buffer += chunk
            else:
                try:
                    sink.write(chunk)
                except OSError:
                    raise UpdateError("filesystem") from None
            if progress is not None:
                progress(done, total or done)

    # --- the release -------------------------------------------------------------------------

    def latest_release(self) -> Release:
        url = f"{API}/repos/{self._repository}/releases/latest"
        try:
            with self._authorised(url, "application/vnd.github+json") as reply:
                body, _ = self._read(reply, MAX_JSON_BYTES, DEADLINE_SMALL)
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc) from None
        except UpdateError as exc:
            raise UpdateError("bad_answer" if exc.kind == "too_large" else exc.kind) from None
        return self._parse_release(body)

    @staticmethod
    def _parse_release(body: bytes) -> Release:
        bad = UpdateError("bad_answer")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise bad from None
        if not isinstance(data, dict):
            raise bad
        tag, assets = data.get("tag_name"), data.get("assets")
        if not isinstance(tag, str) or not _TAG.fullmatch(tag):
            raise bad
        if data.get("draft") is not False or data.get("prerelease") is not False:
            raise bad  # only published, stable releases are ever considered
        if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
            raise bad
        parsed = []
        for item in assets:
            if not isinstance(item, dict):
                raise bad
            asset_id, name, size = item.get("id"), item.get("name"), item.get("size")
            if not _is_int(asset_id) or asset_id < 1 or not _is_int(size) or size < 0:
                raise bad
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 255
                or "/" in name
                or "\\" in name
            ):
                raise bad
            parsed.append(Asset(asset_id, name, size))
        return Release(tag, tuple(parsed))

    # --- assets ------------------------------------------------------------------------------

    def _asset_reply(self, asset_id: int):
        if type(asset_id) is not int or asset_id < 1:
            raise ValueError("invalid asset id")
        url = f"{API}/repos/{self._repository}/releases/assets/{asset_id}"
        try:
            return self._authorised(url, "application/octet-stream")
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise _from_http_error(exc) from None
            location = exc.headers.get("Location") if exc.headers is not None else None
            exc.close()
        target = _redirect_target(location)
        try:
            return self._open(target, {"Accept": "application/octet-stream"})  # no token
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc) from None

    def fetch_asset(
        self, asset_id: int, *, max_bytes: int, deadline: float = DEADLINE_SMALL
    ) -> bytes:
        with self._asset_reply(asset_id) as reply:
            data, _ = self._read(reply, max_bytes, deadline)
        return data

    def download_asset(
        self,
        asset_id: int,
        target: Path,
        *,
        max_bytes: int,
        expected_size: int,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        deadline: float = DEADLINE_PACKAGE,
    ) -> None:
        part = target.with_name(target.name + ".part")
        try:
            with self._asset_reply(asset_id) as reply:
                try:
                    sink = part.open("wb")
                except OSError:
                    raise UpdateError("filesystem") from None
                with sink:
                    _, done = self._read(
                        reply, max_bytes, deadline, cancel, sink, progress, expected_size
                    )
            if done != expected_size:
                raise UpdateError("bad_answer")
            part.replace(target)
        except BaseException as exc:
            part.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise UpdateError("filesystem") from None
            raise
