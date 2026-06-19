import Foundation
import CryptoKit
import Security
import BigInt

/// SRP-6a client for Ente, implemented directly because neither common Swift SRP library
/// matches Ente's variant (both use the RFC 2945 proof form; Ente uses the simple
/// `M1 = H(A | B | S)`). The math below is byte-verified against Ente's server
/// (`ente-io/go-srp`) and the CLI/web clients:
///
///   group = RFC 5054 4096-bit, g = 5, H = SHA-256
///   k  = H( PAD(N) | PAD(g) )
///   x  = H( salt | H( I | ":" | P ) )          I = srpUserID string, P = 16-byte loginKey
///   A  = g^a mod N
///   u  = H( PAD(A) | PAD(B) )
///   S  = (B - k·(g^x mod N))^(a + u·x) mod N
///   K  = H( S )
///   M1 = H( A | B | S )                          A, B, S as minimal big-endian bytes
///   M2 = H( A | M1 | K )
///
/// Padding to N's length (512 bytes) is applied only inside k (g), u (A, B), and K (S);
/// the values hashed into M1 are the minimal big-endian bytes that go on the wire, which
/// is what the server's `CheckM1` recomputes.
enum EnteSRP {

    /// RFC 5054 / RFC 3526 4096-bit MODP prime (big-endian hex).
    private static let primeHex = """
    FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74\
    020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437\
    4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED\
    EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05\
    98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB\
    9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B\
    E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718\
    3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33\
    A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7\
    ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864\
    D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2\
    08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A92108011A723C12A787E6D7\
    88719A10BDBA5B2699C327186AF4E23C1A946834B6150BDA2583E9CA2AD44CE8\
    DBBBC2DB04DE8EF92E8EFC141FBECAA6287C59474E6BC05D99B2964FA090C3A2\
    233BA186515BE7ED1F612970CEE2D7AFB81BDD762170481CD0069127D5B05AA9\
    93B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934063199FFFFFFFFFFFFFFFF
    """

    private static let N = BigUInt(primeHex.replacingOccurrences(of: "\n", with: ""), radix: 16)!
    private static let g = BigUInt(5)
    private static let byteLength = 512 // N is 4096 bits

    /// In-flight handshake state between `create-session` and `verify-session`.
    final class Session {
        let srpABase64: String

        private let a: BigUInt
        private let A: BigUInt
        private let x: BigUInt
        private var cachedK: BigUInt?

        init(a: BigUInt, A: BigUInt, x: BigUInt) {
            self.a = a
            self.A = A
            self.x = x
            self.srpABase64 = Base64.encodeStandard([UInt8](A.serialize()))
        }

        /// Computes the client proof M1 from the server's public value B (base64).
        func computeM1(serverBBase64: String) throws -> String {
            guard let bBytes = Base64.decodeStandard(serverBBase64) else { throw SRPError.badServerValue }
            let B = BigUInt(Data(bBytes))
            guard B % N != 0 else { throw SRPError.badServerValue }

            let k = EnteSRP.multiplier()
            let u = EnteSRP.hashInt(EnteSRP.pad(A) + EnteSRP.pad(B))
            guard u != 0 else { throw SRPError.badServerValue }

            // S = (B - k·g^x)^(a + u·x) mod N, with modular subtraction to stay positive.
            let gx = g.power(x, modulus: N)
            let kgx = (k * gx) % N
            let base = (B % N + N - kgx) % N
            let exponent = a + u * x
            let S = base.power(exponent, modulus: N)

            cachedK = EnteSRP.hashInt(EnteSRP.pad(S))

            // M1 hashes the minimal big-endian bytes of A, B, S (matches the server).
            let m1 = EnteSRP.hash(A.serialize() + B.serialize() + S.serialize())
            return Base64.encodeStandard([UInt8](m1))
        }

        /// Optionally verifies the server proof M2 = H(A | M1 | K) for mutual auth.
        func verifyServerProof(m2Base64: String, m1Base64: String) -> Bool {
            guard let k = cachedK,
                  let m1Bytes = Base64.decodeStandard(m1Base64),
                  let m2Bytes = Base64.decodeStandard(m2Base64) else { return false }
            let expected = EnteSRP.hash(A.serialize() + Data(m1Bytes) + k.serialize())
            return [UInt8](expected) == m2Bytes
        }
    }

    enum SRPError: LocalizedError {
        case badServerValue
        var errorDescription: String? { "The server's SRP response was invalid." }
    }

    /// Begins a handshake: derives x and the ephemeral A from the identity, salt, and the
    /// 16-byte login key.
    static func begin(identity: String, salt: [UInt8], loginKey: [UInt8]) throws -> Session {
        let identityBytes = Array(identity.utf8)
        let inner = hash(Data(identityBytes) + Data([0x3a]) + Data(loginKey)) // H(I | ":" | P)
        let x = hashInt(Data(salt) + inner)                                    // H(salt | inner)

        let a = randomExponent()
        let A = g.power(a, modulus: N)
        return Session(a: a, A: A, x: x)
    }

    // MARK: Primitives

    private static func multiplier() -> BigUInt {
        hashInt(pad(N) + pad(g)) // k = H(PAD(N) | PAD(g))
    }

    private static func hash(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }

    private static func hashInt(_ data: Data) -> BigUInt {
        BigUInt(hash(data))
    }

    /// Left-zero-pads a value's big-endian bytes to N's byte length.
    private static func pad(_ value: BigUInt) -> Data {
        let bytes = value.serialize()
        if bytes.count >= byteLength { return bytes }
        return Data(repeating: 0, count: byteLength - bytes.count) + bytes
    }

    private static func randomExponent() -> BigUInt {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        let value = BigUInt(Data(bytes)) % N
        return value == 0 ? BigUInt(1) : value
    }
}
