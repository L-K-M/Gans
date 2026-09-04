"""One authenticator entry, parsed from an ``otpauth://`` URI. ``id`` is Ente's entity id so
entries stay stable across syncs."""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from typing import Optional, Tuple
from urllib.parse import unquote, unquote_plus, urlsplit

from . import base32, otp
from .otp import OTPAlgorithm

__all__ = ["AuthEntry", "Kind"]


@dataclass(frozen=True)
class Kind:
    """``totp`` / ``hotp(counter)`` / ``steam``."""

    name: str
    counter: int = 0

    @classmethod
    def totp(cls) -> "Kind":
        return cls("totp")

    @classmethod
    def hotp(cls, counter: int) -> "Kind":
        return cls("hotp", counter)

    @classmethod
    def steam(cls) -> "Kind":
        return cls("steam")

    @property
    def is_totp(self) -> bool:
        return self.name == "totp"

    @property
    def is_hotp(self) -> bool:
        return self.name == "hotp"

    @property
    def is_steam(self) -> bool:
        return self.name == "steam"


def _now() -> float:
    return _time.time()


@dataclass
class AuthEntry:
    id: str
    kind: Kind
    #: e.g. "GitHub" — the service.
    issuer: str
    #: e.g. "alice@example.com" — the specific account.
    account: str
    secret: bytes
    algorithm: OTPAlgorithm
    digits: int
    period: int
    #: Ente ``codeDisplay`` metadata: pinned entries float to the top of Quick Search.
    pinned: bool = False
    #: The entry sits in Ente's trash and must not be shown or typed.
    is_trashed: bool = False
    #: Free-form user note from Ente (kept for display/search; empty when absent).
    note: str = ""
    #: User tags from Ente's ``codeDisplay`` (filterable as ``#tag`` in Quick Search).
    tags: Tuple[str, ...] = field(default_factory=tuple)

    Kind = Kind

    @property
    def display_name(self) -> str:
        """A human label: "Issuer (account)" or just one when the other is empty."""
        if self.issuer and self.account:
            return f"{self.issuer} ({self.account})"
        if self.issuer:
            return self.issuer
        if self.account:
            return self.account
        return "Unknown"

    def code(self, at: Optional[float] = None) -> str:
        """The current code at ``at`` (epoch seconds; now when omitted)."""
        if self.kind.is_hotp:
            return otp.code(self.secret, self.kind.counter, self.digits, self.algorithm)
        if self.kind.is_steam:
            return otp.steam(self.secret, at, self.period)
        return otp.totp(self.secret, at, self.period, self.digits, self.algorithm)

    def formatted_code(self, at: Optional[float] = None) -> str:
        """A nicely spaced form for display. Numeric codes are grouped (3+3 / 4+4); Steam
        codes are shown verbatim."""
        raw = self.code(at)
        if self.kind.is_steam:
            return raw
        if len(raw) == 6:
            return f"{raw[:3]} {raw[3:]}"
        if len(raw) == 8:
            return f"{raw[:4]} {raw[4:]}"
        return raw

    def seconds_remaining(self, at: Optional[float] = None) -> int:
        return otp.seconds_remaining(at, self.period)

    @property
    def is_time_based(self) -> bool:
        """Whether the code rotates on a clock (TOTP/Steam). HOTP advances by counter, so it
        has no time-based countdown."""
        return not self.kind.is_hotp

    def fraction_remaining(self, at: Optional[float] = None) -> float:
        """Fraction of the current period still remaining (1 → just refreshed, 0 → expiring),
        for a countdown indicator. Always 1 for non-time-based kinds."""
        if not self.is_time_based or self.period <= 0:
            return 1.0
        return self.seconds_remaining(at) / float(self.period)

    def precise_fraction_remaining(self, at: Optional[float] = None) -> float:
        """Sub-second variant for a smoothly sweeping ring."""
        if not self.is_time_based or self.period <= 0:
            return 1.0
        instant = _now() if at is None else at
        elapsed = instant % float(self.period)
        return (self.period - elapsed) / float(self.period)

    # MARK: Parsing

    @staticmethod
    def parse(uri: str, id: str) -> Optional["AuthEntry"]:
        """Parses an ``otpauth://{totp|hotp|steam}/[issuer:]account?secret=...&...`` URI.
        Returns ``None`` if the scheme/secret are missing or unusable.

        Escaping rules, chosen to match what Ente's own clients do:

        - The label is split on ``:`` **before** percent-decoding, so an encoded colon
          (``%3A``) inside a name can't confuse the issuer/account split, and each side is
          decoded exactly once.
        - Query values are form-decoded the way ``URLSearchParams`` does on Ente's web
          client: ``+`` means space, then percent-decode once. Duplicate keys keep the
          first value, and keys compare case-insensitively.
        - Raw spaces and other unencoded characters (common in real-world exports) are
          tolerated rather than rejected.
        """
        try:
            components = urlsplit(uri.strip())
        except ValueError:
            return None
        if components.scheme.lower() != "otpauth":
            return None

        type_string = components.netloc.lower()
        query = _form_decoded_query(components.query)

        secret_string = query.get("secret")
        secret = base32.decode(secret_string) if secret_string else None
        if not secret:
            return None

        # Label is the raw path minus the leading slash; "Issuer:Account" or "Account".
        raw_label = components.path
        if raw_label.startswith("/"):
            raw_label = raw_label[1:]
        issuer = query.get("issuer", "")
        account = _decode_label_component(raw_label)
        if ":" in raw_label:
            prefix, suffix = raw_label.split(":", 1)
            prefix = _decode_label_component(prefix)
            if not issuer:
                issuer = prefix
            account = _decode_label_component(suffix)

        algorithm = OTPAlgorithm.lenient(query.get("algorithm"))
        # Clamp digits (1...9) so an oversized value can't overflow code generation, and
        # keep the period positive so the TOTP window math stays sane.
        parsed_digits = _int(query.get("digits"), 5 if type_string == "steam" else 6)
        digits = min(max(parsed_digits, 1), 9)
        period = max(_int(query.get("period"), 30), 1)

        if type_string == "hotp":
            kind = Kind.hotp(max(_int(query.get("counter"), 0), 0))
        elif type_string == "steam":
            kind = Kind.steam()
        else:
            kind = Kind.totp()

        # Ente appends a `codeDisplay` query param: JSON with pinned/trashed/note/tags.
        pinned = False
        trashed = False
        note = ""
        tags: Tuple[str, ...] = ()
        raw_display = query.get("codedisplay")
        if raw_display:
            try:
                display = json.loads(raw_display)
            except ValueError:
                display = None
            if isinstance(display, dict):
                pinned = display.get("pinned") is True
                trashed = display.get("trashed") is True
                note_value = display.get("note")
                note = note_value if isinstance(note_value, str) else ""
                raw_tags = display.get("tags")
                if isinstance(raw_tags, list):
                    tags = tuple(tag for tag in raw_tags if isinstance(tag, str) and tag)

        return AuthEntry(id=id, kind=kind, issuer=issuer, account=account, secret=secret,
                         algorithm=algorithm, digits=5 if type_string == "steam" else digits,
                         period=period, pinned=pinned, is_trashed=trashed, note=note, tags=tags)


# MARK: Escaping helpers

def _int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _decode_label_component(raw: str) -> str:
    """Percent-decodes one label component exactly once and trims surrounding spaces."""
    return unquote(raw).strip(" ")


def _form_decoded_query(raw_query: str) -> dict:
    """Form-decodes the raw (still percent-encoded) query string: ``+`` means space, then
    percent-decode once. Keys are lowercased; on duplicates the first value wins."""
    result: dict = {}
    if not raw_query:
        return result
    for pair in raw_query.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        if not name:
            continue
        key = unquote_plus(name).lower()
        if key not in result:
            result[key] = unquote_plus(value)
    return result
