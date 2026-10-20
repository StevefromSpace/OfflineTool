"""The update access token: validated on entry, protected at rest with Windows DPAPI."""

from __future__ import annotations

import sys

import pytest

from offline_doc_tools.updater import credentials as c

TOKEN = "github_pat_" + "A1b2C3d4E5" * 6


@pytest.mark.parametrize(
    "text",
    [TOKEN, "ghp_" + "x" * 36, "  " + TOKEN + "\r\n", "ghu_" + "Z9" * 20],
)
def test_plausible_tokens_are_accepted_and_trimmed(text):
    assert c.clean_token(text) == text.strip()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "has space inside the token 123456789012345",
        "x" * 300,
        TOKEN + "\x00",
        "tokén" * 6,
        None,
    ],
)
def test_implausible_tokens_are_refused(text):
    assert c.clean_token(text) is None


def fake_store(tmp_path):
    # A reversible stand-in for DPAPI, so the file logic is testable anywhere.
    return c.TokenStore(
        tmp_path / "token.bin",
        protect=lambda raw: b"PROT" + raw[::-1],
        unprotect=lambda blob: (
            blob[4:][::-1] if blob.startswith(b"PROT") else (_ for _ in ()).throw(OSError("bad"))
        ),
    )


def test_a_token_round_trips_and_is_not_stored_in_the_clear(tmp_path):
    store = fake_store(tmp_path)

    store.save(TOKEN)

    assert store.load() == TOKEN
    assert TOKEN.encode() not in (tmp_path / "token.bin").read_bytes()


def test_saving_an_invalid_token_raises_and_stores_nothing(tmp_path):
    store = fake_store(tmp_path)

    with pytest.raises(ValueError):
        store.save("nope")

    assert store.load() is None and not (tmp_path / "token.bin").exists()


def test_no_file_means_no_token(tmp_path):
    assert fake_store(tmp_path).load() is None


def test_a_corrupt_or_foreign_file_means_no_token(tmp_path):
    store = fake_store(tmp_path)
    (tmp_path / "token.bin").write_bytes(b"garbage")

    assert store.load() is None


def test_clearing_removes_the_token(tmp_path):
    store = fake_store(tmp_path)
    store.save(TOKEN)

    store.clear()
    store.clear()  # harmless twice

    assert store.load() is None and not (tmp_path / "token.bin").exists()


def test_saving_again_replaces_the_token(tmp_path):
    store = fake_store(tmp_path)
    store.save(TOKEN)
    store.save("ghp_" + "y" * 36)

    assert store.load() == "ghp_" + "y" * 36


def test_an_unwritable_location_raises_oserror(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = c.TokenStore(
        blocker / "token.bin", protect=lambda raw: raw, unprotect=lambda blob: blob
    )

    with pytest.raises(OSError):
        store.save(TOKEN)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows feature")
def test_real_dpapi_round_trip_and_tamper_detection(tmp_path):
    store = c.TokenStore(tmp_path / "token.bin")

    store.save(TOKEN)

    raw = (tmp_path / "token.bin").read_bytes()
    assert TOKEN.encode() not in raw and TOKEN.encode("utf-16-le") not in raw
    assert store.load() == TOKEN
    tampered = bytearray(raw)
    tampered[len(tampered) // 2] ^= 0xFF
    (tmp_path / "token.bin").write_bytes(bytes(tampered))
    assert store.load() is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows feature")
def test_dpapi_refuses_a_blob_made_with_different_entropy(tmp_path):
    other = c.dpapi_protect(b"secret", entropy=b"another application")

    with pytest.raises(OSError):
        c.dpapi_unprotect(other)
