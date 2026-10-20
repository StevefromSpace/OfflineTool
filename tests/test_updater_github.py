"""The private GitHub Releases client: authentication, redirects, limits, hostile metadata."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from offline_doc_tools.updater import github as gh

REPO = "someowner/offline-releases"
TOKEN = "github_pat_" + "Q7w8E9r0T1" * 6
API = "https://api.github.com"
LATEST = f"{API}/repos/{REPO}/releases/latest"
SIGNED_HOST = (
    "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sig=abc"
)


class Reply:
    def __init__(self, body: bytes = b"", status: int = 200, chunk: int = 65536):
        self._stream = io.BytesIO(body)
        self.status = status
        self._chunk = chunk
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(min(size, self._chunk) if size > 0 else self._chunk)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def http_error(code: int, headers: dict[str, str] | None = None, url: str = LATEST):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "x", message, io.BytesIO(b"{}"))


class Opener:
    """Replays scripted outcomes in order and records every request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def client(*outcomes, **kwargs):
    opener = Opener(*outcomes)
    return gh.ReleaseClient(REPO, TOKEN, opener_factory=lambda: opener, **kwargs), opener


def release_json(**overrides) -> bytes:
    data = {
        "tag_name": "v0.2.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"id": 11, "name": "update-manifest.json", "size": 300},
            {"id": 12, "name": "update-manifest.json.sig", "size": 88},
            {"id": 13, "name": "OfflineDocTools-0.2.0-win64.zip", "size": 26_000_000},
        ],
    }
    data.update(overrides)
    return json.dumps(data).encode()


def failure_kind(callable_):
    with pytest.raises(gh.UpdateError) as caught:
        callable_()
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    return caught.value.kind


# --- the release listing ---------------------------------------------------------------------


def test_the_latest_release_is_read_with_the_token_and_nothing_else_identifying():
    c, opener = client(Reply(release_json()))

    release = c.latest_release()

    assert release.tag == "v0.2.0"
    assert [(a.id, a.name, a.size) for a in release.assets][0] == (11, "update-manifest.json", 300)
    request = opener.requests[0]
    assert request.full_url == LATEST and request.data is None
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("User-agent") == "OfflineDocTools-updater"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert opener.timeouts == [gh.TIMEOUT_SECONDS]


def test_urls_inside_the_release_are_ignored():
    hostile = release_json(
        assets=[
            {
                "id": 11,
                "name": "update-manifest.json",
                "size": 1,
                "url": "https://evil.example/steal",
                "browser_download_url": "http://evil.example/x",
            }
        ],
        html_url="https://evil.example",
    )
    c, _ = client(Reply(hostile))

    release = c.latest_release()

    assert "evil" not in repr(release)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        release_json(tag_name=5),
        release_json(tag_name="latest"),
        release_json(tag_name="v1.2"),
        release_json(draft=True),
        release_json(prerelease=True),
        release_json(assets="x"),
        release_json(assets=[{"id": "1", "name": "a", "size": 1}]),
        release_json(assets=[{"id": 0, "name": "a", "size": 1}]),
        release_json(assets=[{"id": True, "name": "a", "size": 1}]),
        release_json(assets=[{"id": 1, "name": "a/b", "size": 1}]),
        release_json(assets=[{"id": 1, "name": "a", "size": -1}]),
        pytest.param(
            release_json(assets=[{"id": i, "name": f"f{i}", "size": 1} for i in range(1, 400)]),
            id="too-many-assets",
        ),
        pytest.param(b" " * (gh.MAX_JSON_BYTES + 10), id="huge-json"),
    ],
)
def test_invalid_release_metadata_is_refused(body):
    c, _ = client(Reply(body))

    assert failure_kind(c.latest_release) == "bad_answer"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (http_error(401), "auth"),
        (http_error(403, {"x-ratelimit-remaining": "0"}), "rate_limited"),
        (http_error(403, {"retry-after": "120"}), "rate_limited"),
        (http_error(429), "rate_limited"),
        (http_error(403), "denied"),
        (http_error(404), "not_found"),
        (http_error(500), "network"),
        (http_error(502), "network"),
        (http_error(301, {"Location": "https://evil.example"}), "bad_answer"),
        (http_error(302), "bad_answer"),
        (http_error(418), "bad_answer"),
        (urllib.error.URLError("no route"), "network"),
        (TimeoutError("slow"), "network"),
        (ConnectionResetError("reset"), "network"),
        (OSError("unreachable"), "network"),
    ],
)
def test_failures_are_classified_without_leaking_anything(error, kind):
    c, _ = client(error)

    assert failure_kind(c.latest_release) == kind


def test_the_rate_limit_wait_is_reported_and_capped():
    c, _ = client(http_error(429, {"retry-after": "120"}))
    with pytest.raises(gh.UpdateError) as caught:
        c.latest_release()
    assert caught.value.retry_after == 120

    c, _ = client(http_error(429, {"retry-after": "999999"}))
    with pytest.raises(gh.UpdateError) as caught:
        c.latest_release()
    assert caught.value.retry_after == gh.MAX_RETRY_AFTER


def test_a_python_without_tls_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(gh, "ssl", None)
    c = gh.ReleaseClient(REPO, TOKEN)

    assert failure_kind(c.latest_release) == "tls"


@pytest.mark.parametrize(
    "repo", ["", "noslash", "a/b/c", "../x/y", "a b/c", "a/" + "b" * 200, "a/b?x=1"]
)
def test_an_invalid_repository_name_is_refused(repo):
    with pytest.raises(ValueError):
        gh.ReleaseClient(repo, TOKEN)


@pytest.mark.parametrize("token", ["", "short", "has space 12345678901234567890", None])
def test_an_invalid_token_is_refused(token):
    with pytest.raises(ValueError):
        gh.ReleaseClient(REPO, token)


# --- downloading assets ----------------------------------------------------------------------


def asset_flow(body: bytes, location: str = SIGNED_HOST, first=None):
    return [first or http_error(302, {"Location": location}, f"{API}/x"), Reply(body)]


def test_a_small_asset_is_fetched_through_exactly_one_redirect_without_the_token():
    c, opener = client(*asset_flow(b"manifest-bytes"))

    data = c.fetch_asset(11, max_bytes=1000)

    assert data == b"manifest-bytes"
    first, second = opener.requests
    assert first.full_url == f"{API}/repos/{REPO}/releases/assets/11"
    assert first.get_header("Accept") == "application/octet-stream"
    assert first.get_header("Authorization") == f"Bearer {TOKEN}"
    assert second.full_url == SIGNED_HOST
    assert second.get_header("Authorization") is None
    assert TOKEN not in str(second.headers)


def test_a_direct_answer_from_the_api_host_is_accepted():
    c, opener = client(Reply(b"direct"))

    assert c.fetch_asset(11, max_bytes=100) == b"direct"
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/x",
        "https://evil.example/x",
        "https://githubusercontent.com.evil.example/x",
        "https://evilgithubusercontent.com/x",
        "https://user@release-assets.githubusercontent.com/x",
        "https://release-assets.githubusercontent.com:8443/x",
        "ftp://release-assets.githubusercontent.com/x",
        "//release-assets.githubusercontent.com/x",
        "/relative/path",
        "https://release-assets.githubusercontent.com/x\r\nX-Evil: 1",
        "",
    ],
)
def test_a_redirect_to_anywhere_but_a_github_content_host_is_refused_before_following(location):
    c, opener = client(http_error(302, {"Location": location}, f"{API}/x"))

    assert failure_kind(lambda: c.fetch_asset(11, max_bytes=100)) == "bad_answer"
    assert len(opener.requests) == 1  # the second request was never made


def test_a_missing_location_is_refused():
    c, _ = client(http_error(302, {}, f"{API}/x"))

    assert failure_kind(lambda: c.fetch_asset(11, max_bytes=100)) == "bad_answer"


def test_a_second_redirect_is_refused():
    c, opener = client(
        http_error(302, {"Location": SIGNED_HOST}, f"{API}/x"),
        http_error(302, {"Location": "https://evil.example/y"}, SIGNED_HOST),
    )

    assert failure_kind(lambda: c.fetch_asset(11, max_bytes=100)) == "bad_answer"
    assert len(opener.requests) == 2


def test_a_revoked_token_on_the_asset_request_is_an_auth_error():
    c, _ = client(http_error(401, url=f"{API}/x"))

    assert failure_kind(lambda: c.fetch_asset(11, max_bytes=100)) == "auth"


@pytest.mark.parametrize("asset_id", [0, -1, True, "11", 1.5, None])
def test_only_positive_integer_asset_ids_are_requested(asset_id):
    c, opener = client()

    with pytest.raises(ValueError):
        c.fetch_asset(asset_id, max_bytes=100)

    assert opener.requests == []


def test_an_asset_larger_than_allowed_is_refused_while_reading():
    c, _ = client(*asset_flow(b"x" * 101))

    assert failure_kind(lambda: c.fetch_asset(11, max_bytes=100)) == "too_large"


def test_downloading_to_a_file_checks_the_size_and_reports_progress(tmp_path):
    body = b"p" * 5000
    c, _ = client(*asset_flow(body))
    seen = []

    c.download_asset(
        13,
        tmp_path / "pkg.zip",
        max_bytes=10_000,
        expected_size=5000,
        progress=lambda d, t: seen.append((d, t)),
    )

    assert (tmp_path / "pkg.zip").read_bytes() == body
    assert seen and seen[-1] == (5000, 5000)
    assert not (tmp_path / "pkg.zip.part").exists()


@pytest.mark.parametrize("body", [b"p" * 4999, b"p" * 5001])
def test_a_download_of_the_wrong_size_leaves_no_file(tmp_path, body):
    c, _ = client(*asset_flow(body))

    kind = failure_kind(
        lambda: c.download_asset(13, tmp_path / "pkg.zip", max_bytes=10_000, expected_size=5000)
    )

    assert kind in ("bad_answer", "too_large")
    assert list(tmp_path.iterdir()) == []


def test_a_cancelled_download_leaves_no_file(tmp_path):
    c, _ = client(*asset_flow(b"p" * 5000))
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 0

    kind = failure_kind(
        lambda: c.download_asset(
            13, tmp_path / "pkg.zip", max_bytes=10_000, expected_size=5000, cancel=cancel
        )
    )

    assert kind == "cancelled" and list(tmp_path.iterdir()) == []


def test_an_interrupted_download_leaves_no_file(tmp_path):
    class Dropping(Reply):
        def read(self, size=-1):
            if self._stream.tell() >= 1000:
                raise ConnectionResetError("dropped")
            return super().read(size)

    c, _ = client(
        http_error(302, {"Location": SIGNED_HOST}, f"{API}/x"), Dropping(b"p" * 5000, chunk=500)
    )

    kind = failure_kind(
        lambda: c.download_asset(13, tmp_path / "pkg.zip", max_bytes=10_000, expected_size=5000)
    )

    assert kind == "network" and list(tmp_path.iterdir()) == []


def test_a_download_that_drips_too_slowly_is_abandoned(tmp_path):
    ticks = iter(range(0, 100_000, 100))
    c, _ = client(*asset_flow(b"p" * 5000), clock=lambda: next(ticks))

    kind = failure_kind(lambda: c.fetch_asset(13, max_bytes=10_000, deadline=10))

    assert kind == "network"


def test_a_failure_to_write_the_file_is_a_filesystem_error(tmp_path):
    c, _ = client(*asset_flow(b"p" * 100))

    kind = failure_kind(
        lambda: c.download_asset(
            13, tmp_path / "no" / "such" / "pkg.zip", max_bytes=1000, expected_size=100
        )
    )

    assert kind == "filesystem"


@pytest.mark.parametrize("retry_after", ["\u00b2", "\u0663", "12\u00b2", "-5", "1e3", ""])
def test_an_odd_retry_after_value_is_ignored_not_a_crash(retry_after):
    c, _ = client(http_error(429, {"retry-after": retry_after}))

    with pytest.raises(gh.UpdateError) as caught:
        c.latest_release()

    assert caught.value.kind == "rate_limited" and caught.value.retry_after is None
