import Foundation
import Combine

/// Drives the login UI through Ente's steps and, on success, hands the authorization to
/// the vault to unwrap keys and persist the session.
@MainActor
final class LoginViewModel: ObservableObject {

    enum Stage: Equatable {
        case credentials
        case emailCode
        case twoFactor(sessionID: String)
        case passkey(passkeySessionID: String, accountsURL: String)
    }

    @Published var email = ""
    @Published var password = ""
    @Published var code = ""

    @Published private(set) var stage: Stage = .credentials
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?

    private let login: EnteLogin
    private let vault: EnteVault
    /// Called once a session is fully established.
    var onSignedIn: () -> Void = {}

    init(vault: EnteVault, api: EnteAPI = EnteAPI()) {
        self.vault = vault
        self.login = EnteLogin(api: api)
    }

    var canSubmitCredentials: Bool {
        !email.trimmingCharacters(in: .whitespaces).isEmpty && !password.isEmpty && !isBusy
    }

    // MARK: Actions

    /// Tries SRP first; on accounts that require email verification it transparently falls
    /// back to the email-code flow.
    func signInWithPassword() {
        run {
            do {
                let step = try await self.login.startSRP(email: self.trimmedEmail, password: self.password)
                try await self.handle(step)
            } catch EnteLogin.LoginError.srpUnavailable {
                let step = try await self.login.sendEmailOTP(email: self.trimmedEmail)
                try await self.handle(step)
            }
        }
    }

    /// Explicitly requests an email code instead of using SRP.
    func sendEmailCode() {
        run {
            let step = try await self.login.sendEmailOTP(email: self.trimmedEmail)
            try await self.handle(step)
        }
    }

    func submitCode() {
        run {
            let step: EnteLogin.Step
            switch self.stage {
            case .emailCode:
                step = try await self.login.verifyEmailOTP(email: self.trimmedEmail, code: self.code)
            case .twoFactor(let sessionID):
                step = try await self.login.verifyTwoFactor(sessionID: sessionID, code: self.code)
            default:
                return
            }
            try await self.handle(step)
        }
    }

    func restart() {
        currentTask?.cancel()
        currentTask = nil
        isBusy = false
        stage = .credentials
        code = ""
        errorMessage = nil
    }

    // MARK: Passkey

    /// The browser URL that runs the passkey ceremony for the current passkey stage.
    var passkeyVerificationURL: URL? {
        guard case .passkey(let sessionID, let accountsURL) = stage else { return nil }
        return EnteLogin.passkeyVerificationURL(accountsURL: accountsURL,
                                                passkeySessionID: sessionID,
                                                clientPackage: EnteAPI.clientPackage)
    }

    /// Begins waiting for the browser passkey ceremony to finish (polls for the token).
    /// The view opens `passkeyVerificationURL` and then calls this.
    func waitForPasskey() {
        guard case .passkey(let sessionID, _) = stage else { return }
        run {
            let step = try await self.login.waitForPasskeyToken(passkeySessionID: sessionID)
            try await self.handle(step)
        }
    }

    // MARK: Step handling

    private func handle(_ step: EnteLogin.Step) async throws {
        switch step {
        case .authorized(let authorization):
            try await vault.completeLogin(authorization: authorization, password: password, email: trimmedEmail)
            onSignedIn()
        case .needsEmailCode:
            code = ""
            stage = .emailCode
        case .needsTwoFactor(let sessionID):
            code = ""
            stage = .twoFactor(sessionID: sessionID)
        case .needsPasskey(let passkeySessionID, let accountsURL):
            stage = .passkey(passkeySessionID: passkeySessionID, accountsURL: accountsURL)
        }
    }

    private var trimmedEmail: String { email.trimmingCharacters(in: .whitespaces) }

    /// The in-flight login task, tracked so `restart()` can cancel a long passkey wait.
    private var currentTask: Task<Void, Never>?

    /// Runs an async task with busy/error bookkeeping.
    private func run(_ work: @escaping () async throws -> Void) {
        guard !isBusy else { return }
        isBusy = true
        errorMessage = nil
        currentTask = Task { @MainActor in
            defer { isBusy = false; currentTask = nil }
            do {
                try await work()
            } catch is CancellationError {
                // Deliberately aborted (e.g. the user pressed Back) — not an error.
            } catch {
                errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            }
        }
    }
}
