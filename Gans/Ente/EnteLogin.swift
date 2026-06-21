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
        /// The account's second factor is a passkey: open the verification URL in a
        /// browser, then poll for the token. Carries what both steps need.
        case needsPasskey(passkeySessionID: String, accountsURL: String)
    }

    enum LoginError: LocalizedError {
        case srpUnavailable
        case passkeyTimedOut
        var errorDescription: String? {
            switch self {
            case .srpUnavailable: return "SRP login isn't available for this account."
            case .passkeyTimedOut: return "Timed out waiting for passkey authentication. Please try again."
            }
        }
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

        // Verify the server's proof (M2) when present, completing SRP mutual auth. A
        // mismatch is logged rather than fatal: the key unwrap that follows is the
        // authoritative check (a server that didn't know the verifier can't produce a token
        // we can decrypt), and the exact M2 byte formula can't be validated against the live
        // server from CI — so we don't risk blocking a legitimate login on it.
        if let m2 = auth.srpM2, !m2.isEmpty, !handshake.verifyServerProof(m2Base64: m2, m1Base64: m1) {
            Log.ente.error("SRP server proof (M2) did not verify; relying on key-unwrap to reject an impostor server.")
        }
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
        if auth.requiresPasskey, let sessionID = auth.passkeySessionID {
            return .needsPasskey(passkeySessionID: sessionID,
                                 accountsURL: auth.accountsUrl ?? "https://accounts.ente.io")
        }
        if auth.requiresTwoFactor, let sessionID = auth.twoFactorSessionID { return .needsTwoFactor(sessionID: sessionID) }
        return .authorized(auth)
    }

    // MARK: Passkey (second factor)

    /// The accounts page that runs the WebAuthn passkey ceremony for this login session.
    /// Mirrors Ente's CLI: `…/passkeys/verify?passkeySessionID=…&redirect=…&clientPackage=…`.
    /// We don't handle the redirect ourselves — `redirect` only has to be a value the
    /// accounts page whitelists (`ente-cli://passkey` is), because we retrieve the token by
    /// polling `get-token` rather than via the browser redirect.
    static func passkeyVerificationURL(accountsURL: String, passkeySessionID: String,
                                       clientPackage: String) -> URL? {
        guard var components = URLComponents(string: sanitizedAccountsBase(accountsURL)) else { return nil }
        components.path = "/passkeys/verify"
        components.queryItems = [
            URLQueryItem(name: "passkeySessionID", value: passkeySessionID),
            URLQueryItem(name: "redirect", value: "ente-cli://passkey"),
            URLQueryItem(name: "clientPackage", value: clientPackage),
        ]
        return components.url
    }

    /// The canonical Ente accounts host, used when the server-supplied value can't be trusted.
    static let defaultAccountsURL = "https://accounts.ente.io"

    /// `accountsUrl` comes from the login response, so a hostile/MITM server could try to
    /// point the browser at an attacker-controlled page. Only honor it when it's an HTTPS
    /// Ente host; otherwise fall back to the canonical accounts host.
    static func sanitizedAccountsBase(_ raw: String) -> String {
        guard let components = URLComponents(string: raw),
              components.scheme?.lowercased() == "https",
              let host = components.host?.lowercased(),
              host == "ente.io" || host.hasSuffix(".ente.io") else {
            return defaultAccountsURL
        }
        return raw
    }

    /// Polls `get-token` until the browser passkey ceremony completes and the server has an
    /// authorization for this session, then returns the authorized step. Throws on timeout.
    /// Honors task cancellation so the UI can abort the wait.
    func waitForPasskeyToken(passkeySessionID: String,
                             timeout: TimeInterval = 180,
                             pollInterval: TimeInterval = 2) async throws -> Step {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            try Task.checkCancellation()
            // Until the ceremony finishes, this 404s / lacks key attributes; keep waiting.
            if let auth = try? await api.get(AuthorizationResponse.self,
                                             path: "users/two-factor/passkeys/get-token",
                                             query: [URLQueryItem(name: "sessionID", value: passkeySessionID)],
                                             authenticated: false),
               auth.keyAttributes != nil, auth.encryptedToken != nil || auth.token != nil {
                return .authorized(auth)
            }
            if Date() >= deadline { throw LoginError.passkeyTimedOut }
            try await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
    }
}
