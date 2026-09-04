"""DTOs mirroring the Ente API JSON shapes (read from the official CLI source)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

__all__ = ["SRPAttributes", "KeyAttributes", "AuthorizationResponse", "CreateSRPSessionResponse",
           "AuthenticatorKey", "AuthEntity", "AuthEntityDiff", "DecodingError"]


class DecodingError(ValueError):
    """A required field was missing or of the wrong type."""


def _require(data: dict, key: str, kind) -> Any:
    value = data.get(key)
    if kind is int and isinstance(value, bool):
        raise DecodingError(f"'{key}' is malformed")
    if kind is int and isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, kind):
        raise DecodingError(f"'{key}' is missing or malformed")
    return value


def _optional(data: dict, key: str, kind) -> Any:
    value = data.get(key)
    if value is None:
        return None
    if kind is int and isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, kind) and not (kind is int and isinstance(value, bool)) else None


def _dict(data: Any, what: str) -> dict:
    if not isinstance(data, dict):
        raise DecodingError(f"{what}: expected an object")
    return data


@dataclass
class SRPAttributes:
    """``GET /users/srp/attributes?email=`` → ``{ "attributes": { ... } }``"""

    srp_user_id: str
    srp_salt: str
    mem_limit: int
    ops_limit: int
    kek_salt: str
    is_email_mfa_enabled: bool

    @classmethod
    def from_json(cls, data: Any) -> "SRPAttributes":
        root = _dict(data, "SRP attributes")
        attributes = _dict(root.get("attributes"), "SRP attributes")
        return cls(
            srp_user_id=_require(attributes, "srpUserID", str),
            srp_salt=_require(attributes, "srpSalt", str),
            mem_limit=_require(attributes, "memLimit", int),
            ops_limit=_require(attributes, "opsLimit", int),
            kek_salt=_require(attributes, "kekSalt", str),
            is_email_mfa_enabled=bool(attributes.get("isEmailMFAEnabled", False)),
        )


@dataclass
class KeyAttributes:
    """The account key hierarchy needed to unwrap the master key + token."""

    kek_salt: str
    encrypted_key: str
    key_decryption_nonce: str
    public_key: str
    encrypted_secret_key: str
    secret_key_decryption_nonce: str
    mem_limit: int
    ops_limit: int

    @classmethod
    def from_json(cls, data: Any) -> "KeyAttributes":
        attributes = _dict(data, "key attributes")
        return cls(
            kek_salt=_require(attributes, "kekSalt", str),
            encrypted_key=_require(attributes, "encryptedKey", str),
            key_decryption_nonce=_require(attributes, "keyDecryptionNonce", str),
            public_key=_require(attributes, "publicKey", str),
            encrypted_secret_key=_require(attributes, "encryptedSecretKey", str),
            secret_key_decryption_nonce=_require(attributes, "secretKeyDecryptionNonce", str),
            mem_limit=_require(attributes, "memLimit", int),
            ops_limit=_require(attributes, "opsLimit", int),
        )


@dataclass
class AuthorizationResponse:
    """Returned by ``verify-session``, ``verify-email``, and ``two-factor/verify``."""

    id: Optional[int] = None
    key_attributes: Optional[KeyAttributes] = None
    encrypted_token: Optional[str] = None
    token: Optional[str] = None
    two_factor_session_id: Optional[str] = None
    passkey_session_id: Optional[str] = None
    accounts_url: Optional[str] = None
    srp_m2: Optional[str] = None

    @property
    def requires_two_factor(self) -> bool:
        return bool(self.two_factor_session_id)

    @property
    def requires_passkey(self) -> bool:
        return bool(self.passkey_session_id)

    @classmethod
    def from_json(cls, data: Any) -> "AuthorizationResponse":
        root = _dict(data, "authorization response")
        raw_key_attributes = root.get("keyAttributes")
        return cls(
            id=_optional(root, "id", int),
            key_attributes=KeyAttributes.from_json(raw_key_attributes) if isinstance(raw_key_attributes, dict) else None,
            encrypted_token=_optional(root, "encryptedToken", str),
            token=_optional(root, "token", str),
            two_factor_session_id=_optional(root, "twoFactorSessionID", str),
            passkey_session_id=_optional(root, "passkeySessionID", str),
            accounts_url=_optional(root, "accountsUrl", str),
            srp_m2=_optional(root, "srpM2", str),
        )


@dataclass
class CreateSRPSessionResponse:
    """``POST /users/srp/create-session`` → ``{ sessionID, srpB }``"""

    session_id: str
    srp_b: str

    @classmethod
    def from_json(cls, data: Any) -> "CreateSRPSessionResponse":
        root = _dict(data, "SRP session")
        return cls(session_id=_require(root, "sessionID", str), srp_b=_require(root, "srpB", str))


@dataclass
class AuthenticatorKey:
    """``GET /authenticator/key``"""

    user_id: Optional[int]
    encrypted_key: str
    header: str

    @classmethod
    def from_json(cls, data: Any) -> "AuthenticatorKey":
        root = _dict(data, "authenticator key")
        return cls(user_id=_optional(root, "userID", int),
                   encrypted_key=_require(root, "encryptedKey", str),
                   header=_require(root, "header", str))


@dataclass
class AuthEntity:
    """One encrypted authenticator entity from ``/authenticator/entity/diff``."""

    id: str
    encrypted_data: Optional[str]
    header: Optional[str]
    is_deleted: bool
    created_at: Optional[int]
    updated_at: Optional[int]

    @classmethod
    def from_json(cls, data: Any) -> "AuthEntity":
        root = _dict(data, "authenticator entity")
        return cls(
            id=_require(root, "id", str),
            encrypted_data=_optional(root, "encryptedData", str),
            header=_optional(root, "header", str),
            is_deleted=bool(root.get("isDeleted", False)),
            created_at=_optional(root, "createdAt", int),
            updated_at=_optional(root, "updatedAt", int),
        )


@dataclass
class AuthEntityDiff:
    """``GET /authenticator/entity/diff`` → ``{ "diff": [ ... ] }``"""

    diff: List[AuthEntity]

    @classmethod
    def from_json(cls, data: Any) -> "AuthEntityDiff":
        root = _dict(data, "entity diff")
        raw = root.get("diff")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise DecodingError("'diff' is malformed")
        return cls(diff=[AuthEntity.from_json(item) for item in raw])
