"""SRP-6a self-consistency against a server simulated with the go-srp formulas
(``ente-io/go-srp``): the server derives B from the verifier and computes S/M1/M2 its own
way; the client must agree byte-for-byte."""

import hashlib
import secrets
import unittest

from gans import b64, crypto
from gans.ente import srp
from gans.ente.srp import EnteSRP, SRPError


def H(*parts):
    return hashlib.sha256(b"".join(parts)).digest()


class _Server:
    """go-srp server side: v = g^x, B = k·v + g^b, u = H(PAD(A)|PAD(B)), S = (A·v^u)^b."""

    def __init__(self, identity, salt, login_key):
        inner = H(identity.encode(), b":", login_key)
        self.x = int.from_bytes(H(salt, inner), "big")
        self.v = pow(srp.G, self.x, srp.N)
        self.k = int.from_bytes(H(srp._pad(srp.N), srp._pad(srp.G)), "big")
        self.b = int.from_bytes(secrets.token_bytes(32), "big") % srp.N
        self.B = (self.k * self.v + pow(srp.G, self.b, srp.N)) % srp.N

    def verify(self, A_b64, m1_b64):
        A = int.from_bytes(b64.decode_standard(A_b64), "big")
        if A % srp.N == 0:  # RFC 5054 §2.5.4: the server must abort on A ≡ 0 (mod N)
            return None
        u = int.from_bytes(H(srp._pad(A), srp._pad(self.B)), "big")
        S = pow(A * pow(self.v, u, srp.N), self.b, srp.N)
        expected_m1 = H(srp.serialize(A), srp.serialize(self.B), srp.serialize(S))
        if expected_m1 != b64.decode_standard(m1_b64):
            return None
        K = H(srp._pad(S))
        return b64.encode_standard(H(srp.serialize(A), expected_m1, K))


class SRPTests(unittest.TestCase):
    def test_handshake_agrees_with_go_srp_server(self):
        identity = "5e9a1a6e-2c9b-4a49-9c9c-000000000001"
        salt = secrets.token_bytes(16)
        login_key = crypto.derive_login_key(bytes(range(32)))
        self.assertEqual(len(login_key), 16)

        server = _Server(identity, salt, login_key)
        session = EnteSRP.begin(identity, salt, login_key)
        m1 = session.compute_m1(b64.encode_standard(srp.serialize(server.B)))
        m2 = server.verify(session.srp_a_base64, m1)
        self.assertIsNotNone(m2, "server rejected the client proof")
        self.assertTrue(session.verify_server_proof(m2, m1))
        self.assertFalse(session.verify_server_proof(b64.encode_standard(bytes(32)), m1))

    def test_wrong_password_fails(self):
        identity = "user"
        salt = secrets.token_bytes(16)
        server = _Server(identity, salt, b"\x01" * 16)
        session = EnteSRP.begin(identity, salt, b"\x02" * 16)
        m1 = session.compute_m1(b64.encode_standard(srp.serialize(server.B)))
        self.assertIsNone(server.verify(session.srp_a_base64, m1))

    def test_rejects_bad_server_values(self):
        session = EnteSRP.begin("user", bytes(16), bytes(16))
        with self.assertRaises(SRPError):
            session.compute_m1("not base64!")
        with self.assertRaises(SRPError):
            session.compute_m1(b64.encode_standard(srp.serialize(srp.N)))  # B ≡ 0 mod N
        with self.assertRaises(SRPError):
            session.compute_m1(b64.encode_standard(b""))
        with self.assertRaises(SRPError):
            session.compute_m1(b64.encode_standard(srp.serialize(srp.N + 5)))  # B >= N
        self.assertFalse(session.verify_server_proof("AAAA", "AAAA"))  # nothing computed yet

    def test_a_is_512_bytes_or_less_and_minimal(self):
        session = EnteSRP.begin("user", bytes(16), bytes(16))
        raw = b64.decode_standard(session.srp_a_base64)
        self.assertLessEqual(len(raw), 512)
        self.assertTrue(raw and raw[0] != 0)

    def test_serialize_is_minimal_big_endian(self):
        self.assertEqual(srp.serialize(0), b"")
        self.assertEqual(srp.serialize(1), b"\x01")
        self.assertEqual(srp.serialize(256), b"\x01\x00")
        self.assertEqual(len(srp._pad(1)), 512)


if __name__ == "__main__":
    unittest.main()
