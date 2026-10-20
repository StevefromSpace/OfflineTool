"""Reference Ed25519 *signer* for tests only (RFC 8032 section 5.1.6). The application ships no
signing code; releases are signed with OpenSSL. Built on the verifier's own curve arithmetic, and
checked against the RFC test vectors and against OpenSSL in test_updater_ed25519.py."""

from __future__ import annotations

import hashlib

from offline_doc_tools.updater import ed25519 as ed


def _compress(point) -> bytes:
    zinv = pow(point[2], ed._P - 2, ed._P)
    x, y = point[0] * zinv % ed._P, point[1] * zinv % ed._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    return _compress(ed._mul(_expand(seed)[0], ed._G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(ed._mul(a, ed._G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed._L
    encoded_r = _compress(ed._mul(r, ed._G))
    k = int.from_bytes(hashlib.sha512(encoded_r + pub + message).digest(), "little") % ed._L
    return encoded_r + ((r + k * a) % ed._L).to_bytes(32, "little")
