import Foundation
// swift-sodium vends the C library via the product "Clibsodium", whose underlying
// importable module/target is "_Clibsodium". We use the C API directly for exactness.
import _Clibsodium

/// Thin, precise wrappers over the exact libsodium primitives Ente's E2EE uses. We call
/// the C API (via the `Clibsodium` module) directly rather than the `Sodium` Swift
/// wrapper so the byte-level contract — algorithm ids, key/nonce lengths, memlimit
/// *units* — is unambiguous and matches the reference (Ente CLI) implementation.
///
/// All inputs/outputs are raw `[UInt8]`. Callers base64-decode Ente's fields first.
enum EnteCrypto {

    enum CryptoError: LocalizedError {
        case notInitialized
        case badLength(String)
        case operationFailed(String)

        var errorDescription: String? {
            switch self {
            case .notInitialized: return "libsodium failed to initialize."
            case .badLength(let what): return "Unexpected length for \(what)."
            case .operationFailed(let what): return "\(what) failed (wrong password or corrupt data)."
            }
        }
    }

    /// Must be called once before any other primitive. `sodium_init()` is idempotent and
    /// returns 0 (first init) or 1 (already initialized); negative means failure.
    @discardableResult
    static func initialize() -> Bool {
        sodium_init() >= 0
    }

    /// Best-effort scrub of sensitive bytes once they're no longer needed. Uses libsodium's
    /// `sodium_memzero`, which (unlike a plain loop) the optimizer won't elide. Effective
    /// only for the uniquely-referenced buffer passed in — Swift value semantics mean any
    /// earlier copies are out of reach.
    static func wipe(_ bytes: inout [UInt8]) {
        guard !bytes.isEmpty else { return }
        bytes.withUnsafeMutableBufferPointer { buffer in
            sodium_memzero(buffer.baseAddress!, buffer.count)
        }
    }

    // MARK: Argon2id (KEK derivation)

    /// `crypto_pwhash` with Argon2id v1.3. `memLimit` is in **bytes** (libsodium's unit —
    /// not KiB), exactly as Ente returns it in the key/SRP attributes.
    static func deriveKeyEncryptionKey(password: String,
                                       salt: [UInt8],
                                       memLimit: Int,
                                       opsLimit: Int,
                                       outputLength: Int = 32) throws -> [UInt8] {
        guard salt.count == Int(crypto_pwhash_saltbytes()) else {
            throw CryptoError.badLength("Argon2 salt (need \(crypto_pwhash_saltbytes()) bytes)")
        }
        var passwordBytes = Array(password.utf8)
        defer { wipe(&passwordBytes) }
        var out = [UInt8](repeating: 0, count: outputLength)

        let rc = passwordBytes.withUnsafeBytes { pwRaw in
            salt.withUnsafeBufferPointer { saltPtr in
                out.withUnsafeMutableBufferPointer { outPtr in
                    crypto_pwhash(
                        outPtr.baseAddress!, UInt64(outputLength),
                        pwRaw.bindMemory(to: CChar.self).baseAddress!, UInt64(passwordBytes.count),
                        saltPtr.baseAddress!,
                        UInt64(opsLimit), memLimit,
                        crypto_pwhash_alg_argon2id13()
                    )
                }
            }
        }
        guard rc == 0 else { throw CryptoError.operationFailed("Key derivation (Argon2id)") }
        return out
    }

    // MARK: KDF (login key)

    /// `crypto_kdf_derive_from_key`. Ente derives the login key with context `"loginctx"`,
    /// subkey id 1, length 32, then **truncates to 16 bytes**.
    static func deriveLoginKey(keyEncryptionKey kek: [UInt8]) throws -> [UInt8] {
        guard kek.count == Int(crypto_kdf_keybytes()) else {
            throw CryptoError.badLength("KEK for KDF")
        }
        let context = Array("loginctx".utf8) // 8 bytes == crypto_kdf_CONTEXTBYTES
        var subKey = [UInt8](repeating: 0, count: 32)
        defer { wipe(&subKey) } // the returned 16-byte prefix is a separate copy

        let rc = context.withUnsafeBufferPointer { ctx in
            kek.withUnsafeBufferPointer { keyPtr in
                subKey.withUnsafeMutableBufferPointer { outPtr in
                    ctx.baseAddress!.withMemoryRebound(to: CChar.self, capacity: 8) { ctxChar in
                        crypto_kdf_derive_from_key(
                            outPtr.baseAddress!, 32,
                            1,
                            ctxChar,
                            keyPtr.baseAddress!
                        )
                    }
                }
            }
        }
        guard rc == 0 else { throw CryptoError.operationFailed("Login-key derivation") }
        return Array(subKey.prefix(16))
    }

    // MARK: secretbox (symmetric key unwrap)

    /// `crypto_secretbox_open_easy`. 24-byte nonce, 32-byte key.
    static func secretBoxOpen(cipherText: [UInt8], nonce: [UInt8], key: [UInt8]) throws -> [UInt8] {
        guard nonce.count == Int(crypto_secretbox_noncebytes()) else {
            throw CryptoError.badLength("secretbox nonce")
        }
        guard key.count == Int(crypto_secretbox_keybytes()) else {
            throw CryptoError.badLength("secretbox key")
        }
        let macBytes = Int(crypto_secretbox_macbytes())
        guard cipherText.count >= macBytes else { throw CryptoError.badLength("secretbox ciphertext") }
        var out = [UInt8](repeating: 0, count: cipherText.count - macBytes)

        let rc = cipherText.withUnsafeBufferPointer { c in
            nonce.withUnsafeBufferPointer { n in
                key.withUnsafeBufferPointer { k in
                    out.withUnsafeMutableBufferPointer { m in
                        crypto_secretbox_open_easy(
                            m.baseAddress!, c.baseAddress!, UInt64(cipherText.count),
                            n.baseAddress!, k.baseAddress!
                        )
                    }
                }
            }
        }
        guard rc == 0 else { throw CryptoError.operationFailed("Decryption (secretbox)") }
        return out
    }

    // MARK: sealed box (token unwrap)

    /// `crypto_box_seal_open` — anonymous box. The recipient's X25519 public + secret keys
    /// are required; sealed-box overhead is 48 bytes.
    static func sealedBoxOpen(cipherText: [UInt8], publicKey: [UInt8], secretKey: [UInt8]) throws -> [UInt8] {
        guard publicKey.count == Int(crypto_box_publickeybytes()),
              secretKey.count == Int(crypto_box_secretkeybytes()) else {
            throw CryptoError.badLength("box key")
        }
        let sealBytes = Int(crypto_box_sealbytes())
        guard cipherText.count >= sealBytes else { throw CryptoError.badLength("sealed-box ciphertext") }
        var out = [UInt8](repeating: 0, count: cipherText.count - sealBytes)

        let rc = cipherText.withUnsafeBufferPointer { c in
            publicKey.withUnsafeBufferPointer { pk in
                secretKey.withUnsafeBufferPointer { sk in
                    out.withUnsafeMutableBufferPointer { m in
                        crypto_box_seal_open(
                            m.baseAddress!, c.baseAddress!, UInt64(cipherText.count),
                            pk.baseAddress!, sk.baseAddress!
                        )
                    }
                }
            }
        }
        guard rc == 0 else { throw CryptoError.operationFailed("Token decryption (sealed box)") }
        return out
    }

    // MARK: secretstream (authenticator entity decryption)

    /// `crypto_secretstream_xchacha20poly1305_*`, single-chunk pull. Ente Auth entities are
    /// one block and may end on `TAG_MESSAGE (0)` rather than `TAG_FINAL (3)`, so we do not
    /// require the final tag — any successful pull is accepted.
    static func secretStreamOpenSingleChunk(cipherText: [UInt8], header: [UInt8], key: [UInt8]) throws -> [UInt8] {
        guard header.count == Int(crypto_secretstream_xchacha20poly1305_headerbytes()) else {
            throw CryptoError.badLength("secretstream header")
        }
        guard key.count == Int(crypto_secretstream_xchacha20poly1305_keybytes()) else {
            throw CryptoError.badLength("secretstream key")
        }
        let aBytes = Int(crypto_secretstream_xchacha20poly1305_abytes())
        guard cipherText.count >= aBytes else { throw CryptoError.badLength("secretstream ciphertext") }

        var state = crypto_secretstream_xchacha20poly1305_state()
        let initRC = header.withUnsafeBufferPointer { h in
            key.withUnsafeBufferPointer { k in
                crypto_secretstream_xchacha20poly1305_init_pull(&state, h.baseAddress!, k.baseAddress!)
            }
        }
        guard initRC == 0 else { throw CryptoError.operationFailed("Secretstream init") }

        var out = [UInt8](repeating: 0, count: cipherText.count - aBytes)
        var outLen: UInt64 = 0
        var tag: UInt8 = 0
        let rc = cipherText.withUnsafeBufferPointer { c in
            out.withUnsafeMutableBufferPointer { m in
                crypto_secretstream_xchacha20poly1305_pull(
                    &state,
                    m.baseAddress!, &outLen, &tag,
                    c.baseAddress!, UInt64(cipherText.count),
                    nil, 0
                )
            }
        }
        guard rc == 0 else { throw CryptoError.operationFailed("Decryption (secretstream)") }
        return Array(out.prefix(Int(outLen)))
    }
}
