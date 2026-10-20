"""The Ed25519 verifier: RFC 8032 vectors, malformed input, and an independent OpenSSL check."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from ed25519_ref import public_key, sign
from offline_doc_tools.updater import ed25519

# RFC 8032 section 7.1, TEST 1 and TEST 2.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]
SEED = bytes(range(32))
PUB = public_key(SEED)
MSG = b'{"format": 1}'


@pytest.mark.parametrize(("seed", "pub", "msg", "sig"), VECTORS)
def test_rfc_8032_vectors_verify_and_the_reference_signer_reproduces_them(seed, pub, msg, sig):
    seed, pub, msg, sig = (bytes.fromhex(x) for x in (seed, pub, msg, sig))

    assert public_key(seed) == pub
    assert sign(seed, msg) == sig
    assert ed25519.verify(pub, msg, sig)


def test_a_good_signature_verifies_and_any_change_to_it_does_not():
    sig = sign(SEED, MSG)

    assert ed25519.verify(PUB, MSG, sig)
    assert not ed25519.verify(PUB, MSG + b" ", sig)
    for index in (0, 31, 32, 63):
        broken = bytearray(sig)
        broken[index] ^= 1
        assert not ed25519.verify(PUB, MSG, bytes(broken)), index


def test_a_signature_by_another_key_is_rejected():
    other = sign(bytes(range(1, 33)), MSG)

    assert not ed25519.verify(PUB, MSG, other)


@pytest.mark.parametrize("size", [0, 31, 33, 63, 65])
def test_wrong_lengths_are_rejected(size):
    assert not ed25519.verify(PUB, MSG, b"\0" * size)
    assert not ed25519.verify(b"\0" * size, MSG, sign(SEED, MSG))


def test_a_non_canonical_s_value_is_rejected():
    sig = sign(SEED, MSG)
    s = int.from_bytes(sig[32:], "little")
    malleable = sig[:32] + (s + ed25519._L).to_bytes(32, "little")

    assert len(malleable) == 64 and not ed25519.verify(PUB, MSG, malleable)


def test_non_canonical_or_invalid_point_encodings_are_rejected():
    sig = sign(SEED, MSG)
    non_canonical_y = (ed25519._P + 1).to_bytes(32, "little")  # y >= p
    not_on_the_curve = (2).to_bytes(32, "little")

    assert not ed25519.verify(non_canonical_y, MSG, sig)
    assert not ed25519.verify(PUB, MSG, non_canonical_y + sig[32:])
    assert not ed25519.verify(not_on_the_curve, MSG, sig)


def test_a_small_order_public_key_is_rejected_whatever_the_signature():
    identity = (1).to_bytes(32, "little")  # the neutral element, order 1
    forged = identity + bytes(32)  # R = identity, S = 0 satisfies the bare equation for any message

    assert not ed25519.verify(identity, b"anything", forged)


OPENSSL = shutil.which("openssl")


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_signatures_made_by_openssl_verify(tmp_path):
    key = tmp_path / "k.pem"
    message = tmp_path / "m.bin"
    message.write_bytes(MSG)
    subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    raw_public = subprocess.run(
        [OPENSSL, "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout[-32:]
    signature = subprocess.run(
        [OPENSSL, "pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(message)],
        check=True,
        capture_output=True,
    ).stdout

    assert len(signature) == 64 and ed25519.verify(raw_public, MSG, signature)
    assert not ed25519.verify(raw_public, MSG + b"x", signature)
