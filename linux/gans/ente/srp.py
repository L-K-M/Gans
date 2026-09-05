"""SRP-6a client for Ente, implemented directly because the common SRP libraries use the
RFC 2945 proof form while Ente uses the simple ``M1 = H(A | B | S)``. The math below is
byte-identical to the macOS build (``EnteSRP.swift``), which is verified against Ente's
server (``ente-io/go-srp``) and the CLI/web clients:

    group = RFC 5054 4096-bit, g = 5, H = SHA-256
    k  = H( PAD(N) | PAD(g) )
    x  = H( salt | H( I | ":" | P ) )          I = srpUserID string, P = 16-byte loginKey
    A  = g^a mod N
    u  = H( PAD(A) | PAD(B) )
    S  = (B - k·(g^x mod N))^(a + u·x) mod N
    K  = H( PAD(S) )
    M1 = H( A | B | S )                          A, B, S as minimal big-endian bytes
    M2 = H( A | M1 | K )

Padding to N's length (512 bytes) is applied only inside k (g), u (A, B), and K (S); the
values hashed into M1 are the minimal big-endian bytes that go on the wire, which is what
the server's ``CheckM1`` recomputes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from .. import b64

__all__ = ["EnteSRP", "Session", "SRPError"]

# RFC 5054 / RFC 3526 4096-bit MODP prime (big-endian hex).
_PRIME_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A92108011A723C12A787E6D7"
    "88719A10BDBA5B2699C327186AF4E23C1A946834B6150BDA2583E9CA2AD44CE8"
    "DBBBC2DB04DE8EF92E8EFC141FBECAA6287C59474E6BC05D99B2964FA090C3A2"
    "233BA186515BE7ED1F612970CEE2D7AFB81BDD762170481CD0069127D5B05AA9"
    "93B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934063199FFFFFFFFFFFFFFFF"
)

N = int(_PRIME_HEX, 16)
G = 5
BYTE_LENGTH = 512  # N is 4096 bits


class SRPError(Exception):
    BAD_SERVER_VALUE = "The server's SRP response was invalid."
    RANDOM_FAILED = "Couldn't generate secure random data for login."


def serialize(value: int) -> bytes:
    """Minimal big-endian bytes (empty for zero) — matches ``BigUInt.serialize()``."""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hash_int(data: bytes) -> int:
    return int.from_bytes(_hash(data), "big")


def _pad(value: int) -> bytes:
    """Left-zero-pads a value's big-endian bytes to N's byte length."""
    raw = serialize(value)
    if len(raw) >= BYTE_LENGTH:
        return raw
    return bytes(BYTE_LENGTH - len(raw)) + raw


def _multiplier() -> int:
    return _hash_int(_pad(N) + _pad(G))  # k = H(PAD(N) | PAD(g))


def _random_exponent() -> int:
    value = int.from_bytes(secrets.token_bytes(32), "big") % N
    return 1 if value == 0 else value


class Session:
    """In-flight handshake state between ``create-session`` and ``verify-session``."""

    def __init__(self, a: int, A: int, x: int):
        self._a = a
        self._A = A
        self._x = x
        # K = H(PAD(S)) kept as the raw 32-byte digest (an int would strip leading zeros
        # and make M2 verification fail ~1/256 of the time).
        self._cached_k: Optional[bytes] = None
        self.srp_a_base64 = b64.encode_standard(serialize(A))

    def compute_m1(self, server_b_base64: str) -> str:
        """Computes the client proof M1 from the server's public value B (base64)."""
        b_bytes = b64.decode_standard(server_b_base64)
        if b_bytes is None:
            raise SRPError(SRPError.BAD_SERVER_VALUE)
        B = int.from_bytes(b_bytes, "big")
        if B >= N or B % N == 0:  # the server always sends 0 < B < N; anything else is malformed
            raise SRPError(SRPError.BAD_SERVER_VALUE)

        k = _multiplier()
        u = _hash_int(_pad(self._A) + _pad(B))
        if u == 0:
            raise SRPError(SRPError.BAD_SERVER_VALUE)

        # S = (B - k·g^x)^(a + u·x) mod N, with modular subtraction to stay positive.
        gx = pow(G, self._x, N)
        kgx = (k * gx) % N
        base = (B % N + N - kgx) % N
        exponent = self._a + u * self._x
        S = pow(base, exponent, N)
        if S == 0:  # only reachable with a degenerate B; never derive the well-known key H(PAD(0))
            raise SRPError(SRPError.BAD_SERVER_VALUE)

        self._cached_k = _hash(_pad(S))

        # M1 hashes the minimal big-endian bytes of A, B, S (matches the server).
        m1 = _hash(serialize(self._A) + serialize(B) + serialize(S))
        return b64.encode_standard(m1)

    def verify_server_proof(self, m2_base64: str, m1_base64: str) -> bool:
        """Optionally verifies the server proof M2 = H(A | M1 | K) for mutual auth."""
        if self._cached_k is None:
            return False
        m1_bytes = b64.decode_standard(m1_base64)
        m2_bytes = b64.decode_standard(m2_base64)
        if m1_bytes is None or m2_bytes is None:
            return False
        expected = _hash(serialize(self._A) + m1_bytes + self._cached_k)
        return hmac.compare_digest(expected, m2_bytes)


class EnteSRP:
    @staticmethod
    def begin(identity: str, salt: bytes, login_key: bytes) -> Session:
        """Begins a handshake: derives x and the ephemeral A from the identity, salt, and
        the 16-byte login key."""
        inner = _hash(identity.encode("utf-8") + b":" + bytes(login_key))  # H(I | ":" | P)
        x = _hash_int(bytes(salt) + inner)                                   # H(salt | inner)
        a = _random_exponent()
        A = pow(G, a, N)
        return Session(a, A, x)
