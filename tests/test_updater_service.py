"""The update workflow end to end, with a fake GitHub and real manifests, packages and staging."""

from __future__ import annotations

import subprocess

import pytest

from ed25519_ref import public_key
from offline_doc_tools.updater import credentials as cred
from offline_doc_tools.updater import github as gh
from offline_doc_tools.updater import manifest as mf
from offline_doc_tools.updater import service as svc
from updater_fixtures import (
    OTHER_SEED,
    PUBLIC,
    TOP,
    bundle_files,
    make_zip,
    manifest_dict,
    signed,
    write_bundle,
)

TOKEN = "github_pat_" + "K3m4N5b6V7" * 6
REPO = "owner/private-releases"


class FakeClient:
    """Stands in for ReleaseClient: serves a scripted release and its asset bytes."""

    def __init__(self, release: gh.Release, blobs: dict[int, bytes], error=None):
        self.release, self.blobs, self.error = release, blobs, error
        self.fetched: list[int] = []
        self.downloaded: list[int] = []

    def latest_release(self):
        if self.error:
            raise self.error
        return self.release

    def fetch_asset(self, asset_id, *, max_bytes, deadline=0):
        self.fetched.append(asset_id)
        data = self.blobs[asset_id]
        if len(data) > max_bytes:
            raise gh.UpdateError("too_large")
        return data

    def download_asset(
        self, asset_id, target, *, max_bytes, expected_size, progress=None, cancel=None, deadline=0
    ):
        self.downloaded.append(asset_id)
        if self.error and self.error.kind != "ok":
            raise self.error
        data = self.blobs[asset_id]
        if cancel and cancel():
            raise gh.UpdateError("cancelled")
        target.write_bytes(data)
        if progress:
            progress(len(data), len(data))


class World:
    def __init__(
        self,
        tmp_path,
        *,
        installed="0.1.0",
        new="0.2.0",
        files=None,
        seed=None,
        tag=None,
        **manifest_overrides,
    ):
        self.package = make_zip(files if files is not None else bundle_files(new))
        data = manifest_dict(new, self.package, **manifest_overrides)
        raw, sig = signed(data, **({"seed": seed} if seed else {}))
        self.raw, self.sig = raw, sig.encode()
        self.tag = tag or f"v{new}"
        self.install = write_bundle(tmp_path / "apps" / "OfflineDocTools", installed)
        self.staging = tmp_path / "data" / svc.STAGING_NAME
        self.store = cred.TokenStore(
            tmp_path / "token.bin", protect=lambda b: b, unprotect=lambda b: b
        )
        self.store.save(TOKEN)
        self.installed = installed
        self.new = new
        self.client: FakeClient | None = None
        self.launched: list = []

    def assets(self, *, drop=(), package_size=None):
        items = [
            gh.Asset(1, mf.MANIFEST_ASSET, len(self.raw)),
            gh.Asset(2, mf.SIGNATURE_ASSET, len(self.sig)),
            gh.Asset(3, f"OfflineDocTools-{self.new}-win64.zip", package_size or len(self.package)),
        ]
        return tuple(a for a in items if a.name not in drop)

    def service(
        self,
        *,
        drop=(),
        package_size=None,
        blobs=None,
        keys=None,
        error=None,
        free=10**12,
        token=True,
    ):
        release = gh.Release(self.tag, self.assets(drop=drop, package_size=package_size))
        self.client = FakeClient(
            release, blobs or {1: self.raw, 2: self.sig, 3: self.package}, error
        )
        if not token:
            self.store.clear()

        def popen(command, **kwargs):
            self.launched.append((command, kwargs))

        return svc.UpdateService(
            repository=REPO,
            keys=[PUBLIC] if keys is None else keys,
            token_store=self.store,
            installed_version=self.installed,
            staging_dir=self.staging,
            client_factory=lambda repo, tok: self.client,
            popen=popen,
            free_bytes=lambda _p: free,
        )


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- check ------------------------------------------------------------------------------------


def test_a_newer_signed_release_is_found(world):
    result = world.service().check()

    assert result.status is svc.CheckStatus.NEWER and result.version == "0.2.0"
    assert result.package_asset_id == 3 and result.manifest.package_name.endswith("win64.zip")
    assert world.client.fetched == [1, 2] and world.client.downloaded == []  # nothing big yet


def test_the_same_version_is_up_to_date(tmp_path):
    w = World(tmp_path, installed="0.2.0")

    assert w.service().check().status is svc.CheckStatus.UP_TO_DATE


def test_an_older_release_is_not_offered(tmp_path):
    w = World(tmp_path, installed="0.3.0", new="0.2.0")

    assert w.service().check().status is svc.CheckStatus.UP_TO_DATE


def test_a_too_old_installation_must_update_manually(tmp_path):
    w = World(tmp_path, min_installed_version="0.1.5")

    result = w.service().check()

    assert result.status is svc.CheckStatus.MANUAL_UPDATE_NEEDED and "manually" in result.message


def test_without_repository_or_keys_nothing_is_requested(world):
    for kwargs in ({"keys": []},):
        service = world.service(**kwargs)
        assert service.check().status is svc.CheckStatus.NOT_CONFIGURED
    assert world.client.fetched == []

    unconfigured = svc.UpdateService(
        repository="", keys=[PUBLIC], token_store=world.store, installed_version="0.1.0",
        staging_dir=world.staging,
        client_factory=lambda *a: pytest.fail("no client may be created"),
    )  # fmt: skip
    assert unconfigured.check().status is svc.CheckStatus.NOT_CONFIGURED


def test_without_a_token_the_user_is_asked_for_one_and_nothing_is_requested(world):
    result = world.service(token=False).check()

    assert result.status is svc.CheckStatus.NO_TOKEN and world.client.fetched == []


def test_a_release_without_a_manifest_is_ignored(world):
    result = world.service(drop=(mf.MANIFEST_ASSET,)).check()

    assert result.status is svc.CheckStatus.FAILED and "manifest" in result.message


def test_a_release_without_a_signature_is_ignored(world):
    assert world.service(drop=(mf.SIGNATURE_ASSET,)).check().status is svc.CheckStatus.FAILED


def test_a_manifest_signed_by_another_key_is_ignored(tmp_path):
    w = World(tmp_path, seed=OTHER_SEED)

    result = w.service().check()

    assert result.status is svc.CheckStatus.FAILED and result.kind == "signature"
    assert w.client.downloaded == []


def test_a_signature_over_a_modified_manifest_is_ignored(world):
    tampered = world.raw.replace(b"0.2.0", b"0.9.9")

    result = world.service(blobs={1: tampered, 2: world.sig, 3: world.package}).check()

    assert result.kind == "signature"


def test_an_unsupported_manifest_format_is_reported_as_manual(tmp_path):
    w = World(tmp_path, format=2)

    result = w.service().check()

    assert result.kind == "unsupported" and "manually" in result.message


def test_a_release_tag_that_disagrees_with_the_signed_version_is_ignored(tmp_path):
    w = World(tmp_path, tag="v0.9.0")

    assert w.service().check().status is svc.CheckStatus.FAILED


def test_a_release_that_lacks_the_announced_package_is_ignored(world):
    result = world.service(drop=("OfflineDocTools-0.2.0-win64.zip",)).check()

    assert result.status is svc.CheckStatus.FAILED


def test_a_package_whose_listed_size_differs_from_the_signed_size_is_ignored(world):
    assert world.service(package_size=12345).check().status is svc.CheckStatus.FAILED


def test_an_enormous_manifest_asset_is_refused_before_it_is_downloaded(world):
    big = gh.Release(
        "v0.2.0", (gh.Asset(1, mf.MANIFEST_ASSET, 10**9), gh.Asset(2, mf.SIGNATURE_ASSET, 88))
    )
    service = world.service()
    world.client.release = big

    assert service.check().status is svc.CheckStatus.FAILED and world.client.fetched == []


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("auth", "token"),
        ("denied", "refused"),
        ("not_found", "no access"),
        ("network", "reached"),
        ("tls", "secure"),
        ("bad_answer", "unexpected"),
    ],
)
def test_github_failures_become_messages_not_exceptions(world, kind, words):
    result = world.service(error=gh.UpdateError(kind)).check()

    assert result.status is svc.CheckStatus.FAILED and result.kind == kind
    assert words in result.message and TOKEN not in result.message


def test_a_rate_limit_message_says_when_to_try_again(world):
    result = world.service(error=gh.UpdateError("rate_limited", retry_after=600)).check()

    assert "10 minute" in result.message


def test_an_unexpected_bug_never_escapes_the_check(world):
    service = world.service()
    world.client.latest_release = lambda: 1 / 0

    result = service.check()

    assert result.status is svc.CheckStatus.FAILED and "unexpectedly" in result.message


# --- download and stage ----------------------------------------------------------------------


def stage(world, **kwargs):
    service = world.service()
    found = service.check()
    return service, found, service.download_and_stage(found, world.install, **kwargs)


def test_a_verified_package_is_staged_next_to_the_installation(world):
    seen = []

    service, found, staged = stage(world, progress=lambda d, t: seen.append((d, t)))

    root = world.staging
    assert staged.payload == root / "payload" / TOP
    assert (staged.payload / "OfflineDocTools.exe").read_bytes() == b"EXE 0.2.0"
    assert not (root / "package.zip").exists() and seen
    assert world.client.downloaded == [3]


def test_a_package_that_does_not_match_the_signed_hash_is_discarded(world):
    tampered = bytearray(world.package)
    tampered[len(tampered) // 2] ^= 0xFF
    service = world.service(blobs={1: world.raw, 2: world.sig, 3: bytes(tampered)})
    found = service.check()

    with pytest.raises(svc.UpdateFailure) as caught:
        service.download_and_stage(found, world.install)

    assert caught.value.kind == "hash" and not (world.staging).exists()


def test_a_malicious_archive_with_a_valid_signature_is_still_refused(tmp_path):
    files = {**bundle_files("0.2.0"), "../../evil.txt": b"x"}
    w = World(tmp_path, files=files)
    service = w.service()
    found = service.check()

    with pytest.raises(svc.UpdateFailure) as caught:
        service.download_and_stage(found, w.install)

    assert caught.value.kind == "archive" and not (tmp_path / "evil.txt").exists()
    assert not (w.staging).exists()


def test_an_interrupted_download_leaves_nothing_behind(world):
    service = world.service()
    found = service.check()
    world.client.error = gh.UpdateError("network")

    with pytest.raises(svc.UpdateFailure) as caught:
        service.download_and_stage(found, world.install)

    assert caught.value.kind == "network" and not (world.staging).exists()


def test_a_cancelled_download_leaves_nothing_behind(world):
    service = world.service()
    found = service.check()

    with pytest.raises(svc.UpdateFailure) as caught:
        service.download_and_stage(found, world.install, cancel=lambda: True)

    assert caught.value.kind == "cancelled" and not (world.staging).exists()


def test_too_little_disk_space_stops_before_downloading(world):
    service = world.service(free=10)
    found = service.check()

    with pytest.raises(svc.UpdateFailure) as caught:
        service.download_and_stage(found, world.install)

    assert caught.value.kind == "space" and world.client.downloaded == []


def test_only_a_verified_newer_check_result_can_be_downloaded(world):
    service = world.service(keys=[public_key(OTHER_SEED)])
    failed = service.check()

    with pytest.raises(svc.UpdateFailure):
        service.download_and_stage(failed, world.install)

    assert world.client.downloaded == []


def test_a_stale_staging_folder_is_replaced_not_reused(world):
    stale = world.staging
    (stale / "payload" / TOP).mkdir(parents=True)
    (stale / "payload" / TOP / "old.txt").write_text("old")

    _, _, staged = stage(world)

    assert not (staged.payload / "old.txt").exists()


def test_cleanup_removes_a_finished_staging_folder(world):
    (world.staging / "payload").mkdir(parents=True)

    svc.cleanup_staging(world.staging)

    assert not (world.staging).exists()
    svc.cleanup_staging(world.staging)  # nothing left: still fine


# --- hand over -------------------------------------------------------------------------------


def test_the_helper_is_the_staged_executable_started_outside_the_installation(world):
    service, found, staged = stage(world)
    result_file = world.install.parent / "result.json"

    service.launch_helper(staged, world.install, result_file)

    command, kwargs = world.launched[0]
    assert command[0] == str(staged.payload / "OfflineDocTools.exe")
    assert command[1] == "--apply-update" and kwargs["cwd"] == str(staged.payload)
    pairs = dict(zip(command[2::2], command[3::2], strict=True))
    assert pairs["--install-dir"] == str(world.install) and pairs["--payload"] == str(
        staged.payload
    )
    assert pairs["--from-version"] == "0.1.0" and pairs["--to-version"] == "0.2.0"
    assert pairs["--protocol"] == "1" and pairs["--result-file"] == str(result_file)
    assert pairs["--parent-pid"].isdigit()
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert str(world.install) not in str(kwargs["cwd"])


def test_the_parent_process_to_wait_for_can_be_named(world):
    service, found, staged = stage(world)

    service.launch_helper(staged, world.install, world.install.parent / "r.json", parent_pid=4321)

    command, _ = world.launched[0]
    assert command[command.index("--parent-pid") + 1] == "4321"


def test_staging_is_kept_out_of_the_installations_folder_and_checked_by_name(world, tmp_path):
    service, found, staged = stage(world)

    assert world.staging in staged.payload.parents
    assert world.install.parent not in staged.payload.parents
    with pytest.raises(ValueError):
        svc.UpdateService(
            repository=REPO, keys=[PUBLIC], token_store=world.store, installed_version="0.1.0",
            staging_dir=tmp_path / "Documents",
        )  # fmt: skip
