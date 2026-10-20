"""The signed update manifest: what is believed, and what is refused."""

from __future__ import annotations

import base64
import json

import pytest

from ed25519_ref import public_key
from offline_doc_tools.updater import manifest as m
from updater_fixtures import OTHER_SEED, PUBLIC, manifest_dict, signed

KEYS = [PUBLIC]
NAME = "OfflineDocTools-0.2.0-win64.zip"


def verified(data, **kwargs):
    raw, sig = signed(data, **kwargs)
    return m.verify_manifest(raw, sig, KEYS)


def refused(raw: bytes, sig: str, keys=KEYS) -> str:
    with pytest.raises(m.ManifestError) as caught:
        m.verify_manifest(raw, sig, keys)
    return caught.value.kind


def package(name=NAME, sha="a" * 64, size=5):
    return {"package": {"name": name, "sha256": sha, "size": size}}


def test_a_valid_signed_manifest_is_accepted_and_exposes_only_validated_fields():
    result = verified(manifest_dict("0.2.0", b"package"))

    assert (result.version, result.package_name) == ("0.2.0", NAME)
    assert result.package_size == len(b"package") and len(result.package_sha256) == 64
    assert result.updater_protocol == 1 and result.min_installed_version is None


def test_a_signature_by_an_untrusted_key_is_refused():
    raw, sig = signed(manifest_dict(), seed=OTHER_SEED)

    assert refused(raw, sig) == "signature"


def test_any_one_of_several_trusted_keys_is_enough():
    raw, sig = signed(manifest_dict(), seed=OTHER_SEED)

    assert m.verify_manifest(raw, sig, [PUBLIC, public_key(OTHER_SEED)]).version == "0.2.0"


def test_no_trusted_key_means_not_configured_and_nothing_is_trusted():
    raw, sig = signed(manifest_dict())

    assert refused(raw, sig, keys=[]) == "unconfigured"


def test_a_manifest_changed_after_signing_is_refused():
    raw, sig = signed(manifest_dict("0.2.0"))

    assert refused(raw.replace(b"0.2.0", b"9.9.9"), sig) == "signature"


@pytest.mark.parametrize(
    "sig",
    [
        "",
        "not base64!!",
        base64.b64encode(b"short").decode(),
        base64.b64encode(bytes(65)).decode(),
        pytest.param("A" * 100000, id="huge"),
    ],
)
def test_a_malformed_signature_file_is_refused(sig):
    raw, _ = signed(manifest_dict())

    assert refused(raw, sig) == "signature"


def test_a_signature_with_surrounding_whitespace_is_still_read():
    raw, sig = signed(manifest_dict())

    assert m.verify_manifest(raw, f"\n{sig}\r\n", KEYS).version == "0.2.0"


def test_an_oversized_manifest_is_refused_before_anything_else():
    assert refused(b" " * (m.MAX_MANIFEST_BYTES + 1), "x") == "format"


@pytest.mark.parametrize(
    ("override", "kind"),
    [
        ({"format": 2}, "unsupported"),
        ({"format": 0}, "format"),
        ({"format": True}, "format"),
        ({"format": "1"}, "format"),
        ({"app": "SomethingElse"}, "format"),
        ({"channel": "beta"}, "format"),
        ({"channel": None}, "format"),
        ({"version": "1.2"}, "format"),
        ({"version": "v1.2.3"}, "format"),
        ({"version": "1.2.3-rc1"}, "format"),
        ({"version": "1.2.3\n"}, "format"),
        ({"version": "١.٢.٣"}, "format"),
        ({"version": 123}, "format"),
        ({"updater_protocol": 2}, "unsupported"),
        ({"updater_protocol": 0}, "unsupported"),
        ({"updater_protocol": "1"}, "format"),
        ({"min_installed_version": "x"}, "format"),
        ({"package": "x"}, "format"),
        (package(sha="ab"), "format"),
        (package(sha="A" * 64), "format"),
        (package(size=0), "format"),
        (package(size=True), "format"),
        (package(size=10**12), "format"),
        (package(name="OfflineDocTools-0.3.0-win64.zip"), "format"),
        (package(name="../" + NAME), "format"),
        (package(name="evil.exe"), "format"),
    ],
)
def test_a_correctly_signed_but_invalid_manifest_is_refused(override, kind):
    assert refused(*signed(manifest_dict(**override))) == kind


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', b"null", b"\xff\xfe", b"{"])
def test_signed_garbage_is_refused_as_a_format_error(body):
    assert refused(*signed(body)) == "format"


def test_unknown_extra_fields_do_not_matter_because_they_are_signed_too():
    assert verified(manifest_dict(notes="hello")).version == "0.2.0"


def test_versions_are_compared_as_numbers():
    assert m.parse_version("0.10.0") > m.parse_version("0.9.9")
    assert m.parse_version("1.0.0") > m.parse_version("0.99.99")
    assert m.parse_version("1.2") is None and m.parse_version("v1.2.3") is None
    assert m.parse_version("v1.2.3", allow_v=True) == (1, 2, 3)


def assess(installed="0.1.0", tag="v0.2.0", **overrides):
    return m.assess(verified(manifest_dict(**overrides)), installed, tag)


def test_a_newer_stable_release_is_offered():
    assert assess() is m.Applicability.NEWER


@pytest.mark.parametrize(
    ("installed", "version"), [("0.2.0", "0.2.0"), ("0.3.0", "0.2.0"), ("1.0.0", "0.9.9")]
)
def test_the_same_or_an_older_release_is_never_installed(installed, version):
    assert assess(installed, f"v{version}", version=version) is m.Applicability.UP_TO_DATE


def test_an_installation_below_the_minimum_must_update_manually_first():
    result = assess("0.1.0", min_installed_version="0.1.5")

    assert result is m.Applicability.MANUAL_UPDATE_NEEDED


def test_a_release_tag_that_disagrees_with_the_signed_version_is_refused():
    with pytest.raises(m.ManifestError) as caught:
        assess(tag="v0.9.0")

    assert caught.value.kind == "format"


def test_an_unparsable_installed_version_never_claims_an_update():
    with pytest.raises(m.ManifestError):
        assess(installed="dev")


def test_the_signature_covers_the_exact_bytes_not_the_parsed_content():
    data = manifest_dict()
    raw = json.dumps(data, sort_keys=True).encode()
    reordered = json.dumps(data, sort_keys=False, indent=2).encode()
    _, sig = signed(raw)

    assert reordered != raw and refused(reordered, sig) == "signature"
