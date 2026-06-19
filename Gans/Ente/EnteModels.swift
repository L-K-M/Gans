import Foundation

/// Codable DTOs mirroring the Ente API JSON shapes (read from the official CLI source).

/// `GET /users/srp/attributes?email=` → `{ "attributes": { ... } }`
struct SRPAttributesResponse: Decodable {
    let attributes: SRPAttributes
}

struct SRPAttributes: Decodable {
    let srpUserID: String
    let srpSalt: String
    let memLimit: Int
    let opsLimit: Int
    let kekSalt: String
    let isEmailMFAEnabled: Bool
}

/// The account key hierarchy needed to unwrap the master key + token.
struct KeyAttributes: Decodable, Equatable {
    let kekSalt: String
    let encryptedKey: String
    let keyDecryptionNonce: String
    let publicKey: String
    let encryptedSecretKey: String
    let secretKeyDecryptionNonce: String
    let memLimit: Int
    let opsLimit: Int
}

/// Returned by `verify-session`, `verify-email`, and `two-factor/verify`.
struct AuthorizationResponse: Decodable, Equatable {
    let id: Int64?
    let keyAttributes: KeyAttributes?
    let encryptedToken: String?
    let token: String?
    let twoFactorSessionID: String?
    let passkeySessionID: String?
    let accountsUrl: String?
    let srpM2: String?

    var requiresTwoFactor: Bool { (twoFactorSessionID?.isEmpty == false) }
    var requiresPasskey: Bool { (passkeySessionID?.isEmpty == false) }
}

/// `POST /users/srp/create-session` → `{ sessionID, srpB }`
struct CreateSRPSessionResponse: Decodable {
    let sessionID: String
    let srpB: String
}

/// `GET /authenticator/key`
struct AuthenticatorKey: Decodable {
    let userID: Int64?
    let encryptedKey: String
    let header: String
}

/// One encrypted authenticator entity from `/authenticator/entity/diff`.
struct AuthEntity: Decodable {
    let id: String
    let encryptedData: String?
    let header: String?
    let isDeleted: Bool
    let createdAt: Int64?
    let updatedAt: Int64?
}

/// `GET /authenticator/entity/diff` → `{ "diff": [ ... ] }`
struct AuthEntityDiff: Decodable {
    let diff: [AuthEntity]
}
