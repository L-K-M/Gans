"""Turns an ``AuthorizationResponse`` + the account password into usable keys + token.
Shared by every login path (SRP, email-OTP, 2FA, passkey) since they all converge on the
same response shape."""

from __future__ import annotations

from dataclasses import dataclass

from .. import b64, crypto
from .models import AuthorizationResponse

__all__ = ["UnwrappedKeys", "UnwrapError", "unwrap"]


class UnwrapError(Exception):
    @classmethod
    def missing_key_attributes(cls) -> "UnwrapError":
        return cls("Login response had no key attributes.")

    @classmethod
    def missing_token(cls) -> "UnwrapError":
        return cls("Login response had no token.")

    @classmethod
    def bad_field(cls, field: str) -> "UnwrapError":
        return cls(f"Login response field '{field}' was malformed.")


@dataclass
class UnwrappedKeys:
    master_key: bytes
    secret_key: bytes
    #: URL-safe base64 auth token for the ``X-Auth-Token`` header.
    token: str


def _decode(value: str, field: str) -> bytes:
    decoded = b64.decode_standard(value)
    if decoded is None:
        raise UnwrapError.bad_field(field)
    return decoded


def unwrap(authorization: AuthorizationResponse, password: str) -> UnwrappedKeys:
    key_attributes = authorization.key_attributes
    if key_attributes is None:
        raise UnwrapError.missing_key_attributes()

    kek_salt = _decode(key_attributes.kek_salt, "kekSalt")
    encrypted_key = _decode(key_attributes.encrypted_key, "encryptedKey")
    key_nonce = _decode(key_attributes.key_decryption_nonce, "keyDecryptionNonce")
    encrypted_secret_key = _decode(key_attributes.encrypted_secret_key, "encryptedSecretKey")
    secret_key_nonce = _decode(key_attributes.secret_key_decryption_nonce, "secretKeyDecryptionNonce")
    public_key = _decode(key_attributes.public_key, "publicKey")

    # KEK from password via Argon2id (parameters come from the key attributes).
    kek = crypto.derive_key_encryption_key(password, kek_salt, key_attributes.mem_limit, key_attributes.ops_limit)
    master_key = crypto.secret_box_open(encrypted_key, key_nonce, kek)
    secret_key = crypto.secret_box_open(encrypted_secret_key, secret_key_nonce, master_key)

    # Prefer the sealed `encryptedToken` (the normal case); fall back to a plaintext
    # `token` if that's all the server returned.
    if authorization.encrypted_token:
        encrypted_token = b64.decode_standard(authorization.encrypted_token)
        if encrypted_token is None:
            raise UnwrapError.bad_field("encryptedToken")
        token_bytes = crypto.sealed_box_open(encrypted_token, public_key, secret_key)
        # Ente issues the auth token as URL-safe base64 *with padding* for the
        # X-Auth-Token header; the server matches it as a literal string.
        token = b64.encode_url_safe_padded(token_bytes)
    elif authorization.token:
        token = authorization.token
    else:
        raise UnwrapError.missing_token()

    return UnwrappedKeys(master_key=master_key, secret_key=secret_key, token=token)
