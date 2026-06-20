import Foundation

/// The decrypted material from a successful login.
struct UnwrappedKeys {
    var masterKey: [UInt8]
    var secretKey: [UInt8]
    /// URL-safe base64 auth token for the `X-Auth-Token` header.
    let token: String

    /// Scrubs the key bytes once the caller has finished unwrapping with them.
    mutating func wipe() {
        EnteCrypto.wipe(&masterKey)
        EnteCrypto.wipe(&secretKey)
    }
}

/// Turns an `AuthorizationResponse` + the account password into usable keys + token.
/// This is shared by every login path (SRP, email-OTP, 2FA) since they all converge on
/// the same response shape.
enum KeyUnwrap {

    enum UnwrapError: LocalizedError {
        case missingKeyAttributes
        case missingToken
        case badField(String)

        var errorDescription: String? {
            switch self {
            case .missingKeyAttributes: return "Login response had no key attributes."
            case .missingToken: return "Login response had no token."
            case .badField(let field): return "Login response field '\(field)' was malformed."
            }
        }
    }

    static func unwrap(authorization: AuthorizationResponse, password: String) throws -> UnwrappedKeys {
        guard let keyAttributes = authorization.keyAttributes else { throw UnwrapError.missingKeyAttributes }

        guard let kekSalt = Base64.decodeStandard(keyAttributes.kekSalt) else { throw UnwrapError.badField("kekSalt") }
        guard let encryptedKey = Base64.decodeStandard(keyAttributes.encryptedKey) else { throw UnwrapError.badField("encryptedKey") }
        guard let keyNonce = Base64.decodeStandard(keyAttributes.keyDecryptionNonce) else { throw UnwrapError.badField("keyDecryptionNonce") }
        guard let encryptedSecretKey = Base64.decodeStandard(keyAttributes.encryptedSecretKey) else { throw UnwrapError.badField("encryptedSecretKey") }
        guard let secretKeyNonce = Base64.decodeStandard(keyAttributes.secretKeyDecryptionNonce) else { throw UnwrapError.badField("secretKeyDecryptionNonce") }
        guard let publicKey = Base64.decodeStandard(keyAttributes.publicKey) else { throw UnwrapError.badField("publicKey") }

        // KEK from password via Argon2id (parameters come from the key attributes).
        var kek = try EnteCrypto.deriveKeyEncryptionKey(
            password: password, salt: kekSalt,
            memLimit: keyAttributes.memLimit, opsLimit: keyAttributes.opsLimit
        )
        defer { EnteCrypto.wipe(&kek) }

        let masterKey = try EnteCrypto.secretBoxOpen(cipherText: encryptedKey, nonce: keyNonce, key: kek)
        let secretKey = try EnteCrypto.secretBoxOpen(cipherText: encryptedSecretKey, nonce: secretKeyNonce, key: masterKey)

        // Prefer the sealed `encryptedToken` (the normal case); fall back to a plaintext
        // `token` if that's all the server returned.
        let token: String
        if let encryptedToken = authorization.encryptedToken,
           let encryptedTokenBytes = Base64.decodeStandard(encryptedToken) {
            let tokenBytes = try EnteCrypto.sealedBoxOpen(cipherText: encryptedTokenBytes, publicKey: publicKey, secretKey: secretKey)
            // Ente issues the auth token as URL-safe base64 *with padding* for the
            // X-Auth-Token header; the server matches it as a literal string.
            token = Base64.encodeURLSafePadded(tokenBytes)
        } else if let plain = authorization.token, !plain.isEmpty {
            token = plain
        } else {
            throw UnwrapError.missingToken
        }

        return UnwrappedKeys(masterKey: masterKey, secretKey: secretKey, token: token)
    }
}
