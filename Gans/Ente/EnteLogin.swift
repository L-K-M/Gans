import Foundation

/// Drives Ente's interactive login. The password is always required (it unwraps the key
/// hierarchy regardless of how the session is authenticated), so the UI collects
/// email + password up front, then this coordinator picks the best path:
///
///  1. **SRP-6a** when the account has it and email-MFA is off — no email round-trip.
///  2. **Email-OTP** otherwise (or if SRP fails) — Ente emails a code to verify.
///  3. **Account 2FA** (TOTP) when the chosen path returns a `twoFactorSessionID`.
///
/// All branches end in an `AuthorizationResponse` carrying `keyAttributes` +
/// `encryptedToken`, which `KeyUnwrap` turns into keys + token.
actor EnteLogin {
    private let api: EnteAPI

    init(api: EnteAPI) { self.api = api }

    /// The next thing the UI must collect before a session can be produced.
    enum Step: Equatable {
        /// Authentication is complete; unwrap keys with the password.
        case authorized(AuthorizationResponse)
        /// Ente emailed a code; call `verifyEmailOTP`.
        case needsEmailCode
        /// Account-level 2FA; call `verifyTwoFactor` with `sessionID`.
        case needsTwoFactor(sessionID: String)
        /// Passkey-only accounts require the browser flow.
        case needsPasskey(url: String?)
    }

    enum LoginError: LocalizedError {
        case srpUnavailable
        var errorDescription: String? { "SRP login isn't available for this account." }
    }

    // MARK: SRP path

    /// Attempts SRP login. Throws if SRP isn't usable (caller should fall back to email).
    func startSRP(email: String, password: String) async throws -> Step {
        let attributes = try await api.get(SRPAttributesResponse.self,
                                            path: "users/srp/attributes",
                                            query: [URLQueryItem(name: "email", value: email)],
                                            authenticated: false).attributes
        if attributes.isEmailMFAEnabled {
            throw LoginError.srpUnavailable // account prefers email verification
        }
        guard let kekSalt = Base64.decodeStandard(attributes.kekSalt),
              let srpSalt = Base64.decodeStandard(attributes.srpSalt) else {
            throw LoginError.srpUnavailable
        }

        // loginKey = first 16 bytes of KDF(Argon2id(password, kekSalt)).
        let kek = try EnteCrypto.deriveKeyEncryptionKey(password: password, salt: kekSalt,
                                                         memLimit: attributes.memLimit, opsLimit: attributes.opsLimit)
        let loginKey = try EnteCrypto.deriveLoginKey(keyEncryptionKey: kek)

        let handshake = try EnteSRP.begin(identity: attributes.srpUserID, salt: srpSalt, loginKey: loginKey)
        let createResponse = try await api.post(CreateSRPSessionResponse.self,
                                                path: "users/srp/create-session",
                                                body: ["srpUserID": attributes.srpUserID, "srpA": handshake.srpABase64],
                                                authenticated: false)
        let m1 = try handshake.computeM1(serverBBase64: createResponse.srpB)

        struct VerifyBody: Encodable { let srpUserID: String; let sessionID: String; let srpM1: String }
        let auth = try await api.post(AuthorizationResponse.self,
                                      path: "users/srp/verify-session",
                                      body: VerifyBody(srpUserID: attributes.srpUserID,
                                                       sessionID: createResponse.sessionID,
                                                       srpM1: m1),
                                      authenticated: false)
        return step(for: auth)
    }

    // MARK: Email-OTP path

    /// Requests an email login code (`/users/ott`).
    func sendEmailOTP(email: String) async throws -> Step {
        struct Body: Encodable { let email: String; let purpose: String }
        _ = try await api.post(EmptyResponse.self, path: "users/ott",
                               body: Body(email: email, purpose: "login"), authenticated: false)
        return .needsEmailCode
    }

    /// Verifies the emailed code (`/users/verify-email`).
    func verifyEmailOTP(email: String, code: String) async throws -> Step {
        struct Body: Encodable { let email: String; let ott: String }
        let auth = try await api.post(AuthorizationResponse.self, path: "users/verify-email",
                                      body: Body(email: email, ott: code.trimmingCharacters(in: .whitespaces)),
                                      authenticated: false)
        return step(for: auth)
    }

    // MARK: 2FA

    /// Verifies an account-level TOTP code (`/users/two-factor/verify`).
    func verifyTwoFactor(sessionID: String, code: String) async throws -> Step {
        struct Body: Encodable { let sessionID: String; let code: String }
        let auth = try await api.post(AuthorizationResponse.self, path: "users/two-factor/verify",
                                      body: Body(sessionID: sessionID, code: code.trimmingCharacters(in: .whitespaces)),
                                      authenticated: false)
        return step(for: auth)
    }

    // MARK: Helpers

    private func step(for auth: AuthorizationResponse) -> Step {
        if auth.requiresPasskey { return .needsPasskey(url: auth.accountsUrl) }
        if auth.requiresTwoFactor, let sessionID = auth.twoFactorSessionID { return .needsTwoFactor(sessionID: sessionID) }
        return .authorized(auth)
    }
}
