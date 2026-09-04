"""Thin, precise wrappers over the exact libsodium primitives Ente's E2EE uses.

We call libsodium through PyNaCl's *low-level* ``nacl.bindings`` (not the ``nacl.secret`` /
``nacl.public`` conveniences) so the byte-level contract — algorithm ids, key/nonce lengths,
memlimit *units* — is unambiguous and matches the reference (Ente CLI) implementation and
the macOS build (``Gans/Crypto/EnteCrypto.swift``), which calls the same C API.

All inputs/outputs are raw ``bytes``. Callers base64-decode Ente's fields first.
"""

from __future__ import annotations

import struct

import nacl.bindings as sodium
from nacl import exceptions as nacl_exceptions
from nacl.pwhash import argon2id

__all__ = [
    "CryptoError",
    "initialize",
    "derive_key_encryption_key",
    "derive_login_key",
    "secret_box_open",
    "sealed_box_open",
    "secret_stream_open_single_chunk",
]


class CryptoError(Exception):
    """``kind`` is one of ``not_initialized`` / ``bad_length`` / ``operation_failed``."""

    def __init__(self, kind: str, what: str = ""):
        self.kind = kind
        self.what = what
        super().__init__(self._message())

    def _message(self) -> str:
        if self.kind == "not_initialized":
            return "libsodium failed to initialize."
        if self.kind == "bad_length":
            return f"Unexpected length for {self.what}."
        return f"{self.what} failed (wrong password or corrupt data)."


def initialize() -> bool:
    """PyNaCl initializes libsodium on import; ``sodium_init`` is idempotent."""
    try:
        sodium.sodium_init()
        return True
    except Exception:  # pragma: no cover - defensive
        return False


# MARK: Argon2id (KEK derivation)

def derive_key_encryption_key(password: str, salt: bytes, mem_limit: int, ops_limit: int,
                              out_len: int = 32) -> bytes:
    """``crypto_pwhash`` with Argon2id v1.3. ``mem_limit`` is in **bytes** (libsodium's unit —
    not KiB), exactly as Ente returns it in the key/SRP attributes."""
    if len(salt) != argon2id.SALTBYTES:
        raise CryptoError("bad_length", f"Argon2 salt (need {argon2id.SALTBYTES} bytes)")
    try:
        return argon2id.kdf(out_len, password.encode("utf-8"), bytes(salt),
                            opslimit=int(ops_limit), memlimit=int(mem_limit))
    except (nacl_exceptions.CryptoError, nacl_exceptions.ValueError, MemoryError, RuntimeError) as error:
        raise CryptoError("operation_failed", "Key derivation (Argon2id)") from error


# MARK: KDF (login key)

_KDF_CONTEXT = b"loginctx"   # 8 bytes == crypto_kdf_CONTEXTBYTES
_KDF_SUBKEY_ID = 1


def derive_login_key(kek: bytes) -> bytes:
    """``crypto_kdf_derive_from_key(out, 32, id=1, ctx="loginctx", key=kek)`` truncated to
    16 bytes, as Ente derives its SRP password.

    libsodium defines that KDF as BLAKE2b with an empty message, the KEK as the key, the
    little-endian subkey id (zero-padded to 16 bytes) as the *salt*, and the context
    (zero-padded to 16 bytes) as the *personalization* — exactly what
    ``crypto_generichash_blake2b_salt_personal`` computes. Verified against libsodium's own
    ``crypto_kdf_derive_from_key`` in ``tests/test_crypto.py``.
    """
    if len(kek) != 32:  # crypto_kdf_KEYBYTES
        raise CryptoError("bad_length", "KEK for KDF")
    salt = struct.pack("<Q", _KDF_SUBKEY_ID) + bytes(8)
    person = _KDF_CONTEXT + bytes(8)
    try:
        sub_key = sodium.crypto_generichash_blake2b_salt_personal(
            b"", digest_size=32, key=bytes(kek), salt=salt, person=person)
    except (nacl_exceptions.CryptoError, nacl_exceptions.ValueError, RuntimeError) as error:
        raise CryptoError("operation_failed", "Login-key derivation") from error
    return sub_key[:16]


# MARK: secretbox (symmetric key unwrap)

def secret_box_open(cipher_text: bytes, nonce: bytes, key: bytes) -> bytes:
    """``crypto_secretbox_open_easy``. 24-byte nonce, 32-byte key."""
    if len(nonce) != sodium.crypto_secretbox_NONCEBYTES:
        raise CryptoError("bad_length", "secretbox nonce")
    if len(key) != sodium.crypto_secretbox_KEYBYTES:
        raise CryptoError("bad_length", "secretbox key")
    if len(cipher_text) < sodium.crypto_secretbox_MACBYTES:
        raise CryptoError("bad_length", "secretbox ciphertext")
    try:
        return sodium.crypto_secretbox_open(bytes(cipher_text), bytes(nonce), bytes(key))
    except nacl_exceptions.CryptoError as error:
        raise CryptoError("operation_failed", "Decryption (secretbox)") from error


# MARK: sealed box (token unwrap)

def sealed_box_open(cipher_text: bytes, public_key: bytes, secret_key: bytes) -> bytes:
    """``crypto_box_seal_open`` — anonymous box. The recipient's X25519 public + secret keys
    are required; sealed-box overhead is 48 bytes."""
    if len(public_key) != sodium.crypto_box_PUBLICKEYBYTES or len(secret_key) != sodium.crypto_box_SECRETKEYBYTES:
        raise CryptoError("bad_length", "box key")
    if len(cipher_text) < sodium.crypto_box_SEALBYTES:
        raise CryptoError("bad_length", "sealed-box ciphertext")
    try:
        return sodium.crypto_box_seal_open(bytes(cipher_text), bytes(public_key), bytes(secret_key))
    except nacl_exceptions.CryptoError as error:
        raise CryptoError("operation_failed", "Token decryption (sealed box)") from error


# MARK: secretstream (authenticator entity decryption)

def secret_stream_open_single_chunk(cipher_text: bytes, header: bytes, key: bytes) -> bytes:
    """``crypto_secretstream_xchacha20poly1305_*``, single-chunk pull. Ente Auth entities are
    one block and may end on ``TAG_MESSAGE (0)`` rather than ``TAG_FINAL (3)``, so we do not
    require the final tag — any successful pull is accepted."""
    if len(header) != sodium.crypto_secretstream_xchacha20poly1305_HEADERBYTES:
        raise CryptoError("bad_length", "secretstream header")
    if len(key) != sodium.crypto_secretstream_xchacha20poly1305_KEYBYTES:
        raise CryptoError("bad_length", "secretstream key")
    if len(cipher_text) < sodium.crypto_secretstream_xchacha20poly1305_ABYTES:
        raise CryptoError("bad_length", "secretstream ciphertext")
    state = sodium.crypto_secretstream_xchacha20poly1305_state()
    try:
        sodium.crypto_secretstream_xchacha20poly1305_init_pull(state, bytes(header), bytes(key))
    except (nacl_exceptions.CryptoError, nacl_exceptions.ValueError, RuntimeError) as error:
        raise CryptoError("operation_failed", "Secretstream init") from error
    try:
        message, _tag = sodium.crypto_secretstream_xchacha20poly1305_pull(state, bytes(cipher_text), None)
    except (nacl_exceptions.CryptoError, nacl_exceptions.ValueError, RuntimeError) as error:
        raise CryptoError("operation_failed", "Decryption (secretstream)") from error
    return message
