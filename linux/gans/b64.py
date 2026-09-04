"""Base64 helpers. Ente uses **standard** base64 (with padding) for every crypto field
(salts, nonces, encrypted blobs, SRP A/B/M values) and **URL-safe** base64 for the auth
token it issues. Mixing the two silently corrupts bytes, so they're kept distinct here."""

from __future__ import annotations

import base64
import binascii
import re

_STANDARD_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


def _pad(string: str) -> str:
    """Appends ``=`` until the length is a multiple of 4 so input that arrived without
    padding decodes."""
    remainder = len(string) % 4
    return string if remainder == 0 else string + "=" * (4 - remainder)


def decode_standard(string: str) -> bytes | None:
    """Decodes a standard-base64 string, tolerating missing padding. ``None`` on garbage."""
    padded = _pad(string)
    if not _STANDARD_ALPHABET.match(padded):
        return None
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def encode_standard(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def decode_url_safe(string: str) -> bytes | None:
    """Decodes a URL-safe base64 string (``-``/``_``, optional padding)."""
    return decode_standard(string.replace("-", "+").replace("_", "/"))


def encode_url_safe(data: bytes) -> str:
    return encode_standard(data).replace("+", "-").replace("/", "_").replace("=", "")


def encode_url_safe_padded(data: bytes) -> str:
    """URL-safe base64 **with** ``=`` padding. Ente's auth token is exactly this form, and the
    server looks the token up as a verbatim string (no decoding), so the padding must be
    preserved — dropping it makes every authenticated request 401."""
    return encode_standard(data).replace("+", "-").replace("/", "_")
