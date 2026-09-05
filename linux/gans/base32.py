"""RFC 4648 base32 decoder, used for the ``secret`` in ``otpauth://`` URIs. Tolerant of
lowercase, whitespace, dashes, and missing ``=`` padding (authenticator secrets are routinely
shared without it)."""

from __future__ import annotations

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_LOOKUP = {character: index for index, character in enumerate(_ALPHABET)}


def decode(text: str) -> bytes | None:
    cleaned = "".join(text.split()).upper().replace("-", "").rstrip("=")
    if not cleaned:
        return None

    bits = 0
    accumulator = 0
    output = bytearray()
    for character in cleaned:
        value = _LOOKUP.get(character)
        if value is None:
            return None
        accumulator = (accumulator << 5) | value
        bits += 5
        if bits >= 8:
            bits -= 8
            output.append((accumulator >> bits) & 0xFF)
            accumulator &= (1 << bits) - 1
    return bytes(output)
