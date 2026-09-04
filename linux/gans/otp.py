"""Generates one-time codes for TOTP (RFC 6238), HOTP (RFC 4226), and Steam Guard.
Pure and deterministic given a counter/time, so it's covered by RFC test vectors."""

from __future__ import annotations

import hashlib
import hmac
import math
import struct
import time as _time
from enum import Enum
from typing import Optional

__all__ = ["OTPAlgorithm", "code", "totp", "steam", "seconds_remaining"]


class OTPAlgorithm(Enum):
    """Hash algorithm for the HMAC step of an OTP."""

    SHA1 = "SHA1"
    SHA256 = "SHA256"
    SHA512 = "SHA512"

    @classmethod
    def lenient(cls, value: Optional[str]) -> "OTPAlgorithm":
        upper = (value or "").upper()
        if upper == "SHA256":
            return cls.SHA256
        if upper == "SHA512":
            return cls.SHA512
        return cls.SHA1

    @property
    def digestmod(self):
        return {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}[self.value]


_STEAM_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"


def _hmac(counter: int, secret: bytes, algorithm: OTPAlgorithm) -> bytes:
    """HMAC of an 8-byte big-endian counter under ``secret``."""
    return hmac.new(bytes(secret), struct.pack(">Q", counter), algorithm.digestmod).digest()


def _truncate(digest: bytes) -> int:
    """RFC 4226 dynamic truncation → a 31-bit integer."""
    offset = digest[-1] & 0x0F
    return ((digest[offset] & 0x7F) << 24) | (digest[offset + 1] << 16) | (digest[offset + 2] << 8) | digest[offset + 3]


def _now() -> float:
    return _time.time()


def code(secret: bytes, counter: int, digits: int, algorithm: OTPAlgorithm) -> str:
    """A counter-based code (HOTP, and the engine behind TOTP)."""
    # Clamp to a safe range: RFC 4226/6238 use 6–8 digits, and the macOS build keeps
    # 10^digits within UInt32. A hostile/malformed otpauth URI can set any digit count,
    # so defend here regardless of what the parser allowed.
    digits = min(max(int(digits), 1), 9)
    number = _truncate(_hmac(counter, secret, algorithm)) % (10 ** digits)
    return f"{number:0{digits}d}"


def totp(secret: bytes, time: Optional[float] = None, period: int = 30, digits: int = 6,
         algorithm: OTPAlgorithm = OTPAlgorithm.SHA1) -> str:
    """A time-based code (TOTP) at a given instant (epoch seconds; now when omitted)."""
    instant = _now() if time is None else time
    counter = int(math.floor(instant / float(period)))
    return code(secret, counter, digits, algorithm)


def steam(secret: bytes, time: Optional[float] = None, period: int = 30) -> str:
    """A Steam Guard code: 5 chars over the Steam alphabet, SHA1, period 30."""
    instant = _now() if time is None else time
    counter = int(math.floor(instant / float(period)))
    value = _truncate(_hmac(counter, secret, OTPAlgorithm.SHA1))
    result = []
    for _ in range(5):
        result.append(_STEAM_ALPHABET[value % len(_STEAM_ALPHABET)])
        value //= len(_STEAM_ALPHABET)
    return "".join(result)


def seconds_remaining(time: Optional[float] = None, period: int = 30) -> int:
    """Seconds remaining in the current TOTP window (for the countdown ring)."""
    instant = _now() if time is None else time
    return period - (int(instant) % period)
