import Foundation
import LocalAuthentication
import Combine

/// Optional app-level lock. When enabled (`Preferences.requireUnlock`), Gans starts
/// **locked**: the session isn't restored and codes aren't shown until the user passes a
/// Touch ID / device-password check. The Ente token + authenticator key live in the
/// Keychain (device-only, available only while the Mac is unlocked); this adds a second
/// gate so a copied/unlocked Mac left unattended doesn't expose the codes.
@MainActor
final class AppLock: ObservableObject {
    /// True when the app is locked and the vault must not be used yet.
    @Published private(set) var isLocked = false

    private let preferences: Preferences
    private var authenticating = false

    init(preferences: Preferences) {
        self.preferences = preferences
    }

    var isEnabled: Bool { preferences.requireUnlock }

    /// Engage the lock if the feature is on. Call at launch (when a session exists).
    func lockIfEnabled() {
        if preferences.requireUnlock { isLocked = true }
    }

    /// Lock immediately, regardless of the auto-lock setting — the manual "Lock Now"
    /// action. Unlocking still requires Touch ID / password.
    func lock() {
        isLocked = true
    }

    /// Prompt for Touch ID / device password and unlock on success. If the device has no
    /// authentication configured we unlock rather than risk a permanent lockout. Re-entrant
    /// calls while a prompt is showing are ignored.
    func authenticate(reason: String = "Unlock Gans to access your codes",
                      completion: ((Bool) -> Void)? = nil) {
        guard isLocked else { completion?(true); return }
        guard !authenticating else { completion?(false); return }
        authenticating = true

        let context = LAContext()
        context.localizedFallbackTitle = "Enter Password"

        var policyError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &policyError) else {
            Log.app.error("No device authentication available; unlocking. \(policyError?.localizedDescription ?? "", privacy: .public)")
            authenticating = false
            isLocked = false
            completion?(true)
            return
        }

        context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { [weak self] success, _ in
            Task { @MainActor in
                guard let self else { return }
                self.authenticating = false
                if success { self.isLocked = false }
                completion?(success)
            }
        }
    }
}
