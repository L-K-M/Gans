"""Interop checks against vectors produced by libsodium's C API (tests/tools/gen_vectors.c),
plus round-trips through PyNaCl's high-level API."""

import json
import unittest
from pathlib import Path

import nacl.bindings as sodium
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox

from gans import crypto

VECTORS = json.loads((Path(__file__).parent / "vectors" / "libsodium.json").read_text())


def h(name):
    return bytes.fromhex(VECTORS[name])


class LibsodiumVectorTests(unittest.TestCase):
    def test_initialize(self):
        self.assertTrue(crypto.initialize())

    def test_login_key_matches_crypto_kdf_derive_from_key(self):
        # The wrapper rebuilds crypto_kdf_derive_from_key from BLAKE2b salt/personal; this
        # pins it to the real thing (subkey id 1, context "loginctx").
        kek = h("kdf_key")
        self.assertEqual(crypto.derive_login_key(kek), h("kdf_subkey_id1_loginctx_32")[:16])
        # ...and would differ for another subkey id (guards the salt encoding).
        self.assertNotEqual(crypto.derive_login_key(kek), h("kdf_subkey_id2_loginctx_32")[:16])

    def test_login_key_rejects_bad_kek_length(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.derive_login_key(b"short")

    def test_argon2id_matches_crypto_pwhash(self):
        out = crypto.derive_key_encryption_key(VECTORS["pwhash_password"], h("pwhash_salt"),
                                               VECTORS["pwhash_memlimit"], VECTORS["pwhash_opslimit"])
        self.assertEqual(out, h("pwhash_out"))

    def test_argon2id_rejects_bad_salt(self):
        with self.assertRaises(crypto.CryptoError) as context:
            crypto.derive_key_encryption_key("pw", b"short", 8192, 1)
        self.assertEqual(context.exception.kind, "bad_length")

    def test_secretbox_open_matches(self):
        self.assertEqual(crypto.secret_box_open(h("secretbox_ciphertext"), h("secretbox_nonce"), h("secretbox_key")),
                         h("secretbox_plaintext"))

    def test_secretbox_rejects_tampering_and_lengths(self):
        cipher = bytearray(h("secretbox_ciphertext"))
        cipher[-1] ^= 1
        with self.assertRaises(crypto.CryptoError) as context:
            crypto.secret_box_open(bytes(cipher), h("secretbox_nonce"), h("secretbox_key"))
        self.assertEqual(context.exception.kind, "operation_failed")
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_box_open(h("secretbox_ciphertext"), b"\0" * 23, h("secretbox_key"))
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_box_open(h("secretbox_ciphertext"), h("secretbox_nonce"), b"\0" * 31)
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_box_open(b"\0" * 15, h("secretbox_nonce"), h("secretbox_key"))

    def test_sealed_box_open_matches(self):
        self.assertEqual(crypto.sealed_box_open(h("sealedbox_ciphertext"), h("sealedbox_pk"), h("sealedbox_sk")),
                         h("sealedbox_plaintext"))
        with self.assertRaises(crypto.CryptoError):
            crypto.sealed_box_open(h("sealedbox_ciphertext"), h("sealedbox_pk"), bytes(32))
        with self.assertRaises(crypto.CryptoError):
            crypto.sealed_box_open(b"\0" * 47, h("sealedbox_pk"), h("sealedbox_sk"))

    def test_secretstream_single_chunk_accepts_message_and_final_tags(self):
        expected = VECTORS["secretstream_plaintext"].encode("utf-8")
        for variant in ("message_tag", "final_tag"):
            plaintext = crypto.secret_stream_open_single_chunk(
                h(f"secretstream_{variant}_ciphertext"), h(f"secretstream_{variant}_header"), h("secretstream_key"))
            self.assertEqual(plaintext, expected, variant)

    def test_secretstream_rejects_tampering_and_lengths(self):
        cipher = bytearray(h("secretstream_message_tag_ciphertext"))
        cipher[5] ^= 0x80
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_stream_open_single_chunk(bytes(cipher), h("secretstream_message_tag_header"), h("secretstream_key"))
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_stream_open_single_chunk(h("secretstream_message_tag_ciphertext"), b"\0" * 23, h("secretstream_key"))
        with self.assertRaises(crypto.CryptoError):
            crypto.secret_stream_open_single_chunk(b"\0" * 16, h("secretstream_message_tag_header"), h("secretstream_key"))


class RoundTripTests(unittest.TestCase):
    def test_secretbox_round_trip_with_high_level_api(self):
        key = bytes(range(32))
        nonce = bytes(range(24))
        boxed = SecretBox(key).encrypt(b"hello", nonce).ciphertext
        self.assertEqual(crypto.secret_box_open(boxed, nonce, key), b"hello")

    def test_sealed_box_round_trip_with_high_level_api(self):
        private = PrivateKey.generate()
        sealed = SealedBox(private.public_key).encrypt(b"token-bytes")
        self.assertEqual(crypto.sealed_box_open(sealed, bytes(private.public_key), bytes(private)), b"token-bytes")

    def test_secretstream_round_trip_and_empty_message(self):
        key = bytes(range(32))
        state = sodium.crypto_secretstream_xchacha20poly1305_state()
        header = sodium.crypto_secretstream_xchacha20poly1305_init_push(state, key)
        cipher = sodium.crypto_secretstream_xchacha20poly1305_push(state, b"", None, 0)
        self.assertEqual(crypto.secret_stream_open_single_chunk(cipher, header, key), b"")

    def test_argon2id_small_parameters_round_trip(self):
        salt = bytes(16)
        first = crypto.derive_key_encryption_key("pw", salt, 8192, 1)
        self.assertEqual(len(first), 32)
        self.assertEqual(first, crypto.derive_key_encryption_key("pw", salt, 8192, 1))
        self.assertNotEqual(first, crypto.derive_key_encryption_key("pw2", salt, 8192, 1))


if __name__ == "__main__":
    unittest.main()
