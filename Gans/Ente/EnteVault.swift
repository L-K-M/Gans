import Foundation
import Combine

/// The app's single source of truth for the Ente session and the decrypted entries.
///
/// Security model: the Keychain holds the auth token + the 32-byte authenticator key;
/// the disk cache holds only Ente's encrypted entity blobs; the password and plaintext
/// secrets live solely in memory. On launch we decrypt the cache (instant menu), then
/// refresh from the network.
@MainActor
final class EnteVault: ObservableObject {

    enum State: Equatable {
        case signedOut
        case loading
        case ready
        case error(String)
    }

    @Published private(set) var entries: [AuthEntry] = []
    @Published private(set) var state: State = .signedOut
    @Published private(set) var accountEmail: String?
    @Published private(set) var lastSync: Date?

    private let api: EnteAPI
    private let cache = EntityCache()

    /// The unwrapped 32-byte authenticator key (in memory while signed in).
    private var authKey: [UInt8]?

    // Keychain account keys.
    private enum K {
        static let token = "ente.token"
        static let authKey = "ente.authKey"
        static let email = "ente.email"
    }

    init(api: EnteAPI = EnteAPI()) {
        self.api = api
    }

    var isSignedIn: Bool { Keychain.data(for: K.token) != nil && Keychain.data(for: K.authKey) != nil }

    // MARK: Session restore

    /// Restores a persisted session: load token + authKey, decrypt cached entities, then
    /// kick off a network refresh. Call once at launch.
    func restore() async {
        guard let token = Keychain.string(for: K.token),
              let authKeyData = Keychain.data(for: K.authKey) else {
            state = .signedOut
            return
        }
        await api.setAuthToken(token)
        authKey = [UInt8](authKeyData)
        accountEmail = Keychain.string(for: K.email)

        // Instant, offline-friendly: decrypt whatever we cached last time.
        let snapshot = cache.load()
        entries = decrypt(entities: snapshot.entities)
        state = entries.isEmpty ? .loading : .ready

        await refresh()
    }

    // MARK: Login

    /// Completes login from an authorized response: unwrap keys, fetch + unwrap the
    /// authenticator key, persist the session, and do a first sync.
    func completeLogin(authorization: AuthorizationResponse, password: String, email: String) async throws {
        state = .loading
        var keys = try KeyUnwrap.unwrap(authorization: authorization, password: password)
        defer { keys.wipe() } // master/secret keys are only needed to unwrap the authKey below
        await api.setAuthToken(keys.token)

        let wrappedAuthKey = try await api.get(AuthenticatorKey.self, path: "authenticator/key", authenticated: true)
        guard let encrypted = Base64.decodeStandard(wrappedAuthKey.encryptedKey),
              let nonce = Base64.decodeStandard(wrappedAuthKey.header) else {
            throw EnteCrypto.CryptoError.badLength("authenticator key")
        }
        let unwrappedAuthKey = try EnteCrypto.secretBoxOpen(cipherText: encrypted, nonce: nonce, key: keys.masterKey)

        // Persist (token + authKey + email). Password is intentionally never stored.
        Keychain.setString(keys.token, for: K.token)
        Keychain.set(Data(unwrappedAuthKey), for: K.authKey)
        Keychain.setString(email, for: K.email)

        authKey = unwrappedAuthKey
        accountEmail = email
        cache.clear()
        await refresh()
    }

    // MARK: Sync

    /// True when the persisted token was rejected (HTTP 401) — the user must sign in
    /// again before anything will sync. Cached entries keep working offline meanwhile.
    @Published private(set) var sessionExpired = false

    /// The in-flight refresh, so concurrent callers (menu + launch + timer) coalesce
    /// into one network pass instead of racing the cache.
    private var refreshTask: Task<Void, Never>?

    /// Fetches new/changed entities since the cached cursor, updates the cache, and
    /// republishes the decrypted entries.
    func refresh() async {
        if let refreshTask {
            await refreshTask.value
            return
        }
        let task = Task { await performRefresh() }
        refreshTask = task
        await task.value
        refreshTask = nil
    }

    private func performRefresh() async {
        guard authKey != nil else { return }
        if entries.isEmpty { state = .loading }
        do {
            var snapshot = cache.load()
            var byID = Dictionary(snapshot.entities.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
            var cursor = snapshot.sinceTime
            let limit = 500

            while true {
                let cursorAtPageStart = cursor
                let diff = try await api.get(AuthEntityDiff.self, path: "authenticator/entity/diff",
                                             query: [URLQueryItem(name: "sinceTime", value: String(cursor)),
                                                     URLQueryItem(name: "limit", value: String(limit))],
                                             authenticated: true).diff
                for entity in diff {
                    if let updatedAt = entity.updatedAt { cursor = max(cursor, updatedAt) }
                    if entity.isDeleted {
                        byID[entity.id] = nil
                    } else if let data = entity.encryptedData, let header = entity.header {
                        byID[entity.id] = EntityCache.CachedEntity(id: entity.id, encryptedData: data, header: header)
                    }
                }
                // Stop on a short page, or if a full page failed to advance the cursor
                // (otherwise we'd request the same window forever).
                if diff.count < limit || cursor == cursorAtPageStart { break }
            }

            snapshot.entities = Array(byID.values)
            snapshot.sinceTime = cursor
            cache.save(snapshot)

            entries = decrypt(entities: snapshot.entities)
            lastSync = Date()
            state = .ready
            sessionExpired = false
        } catch {
            // A 401 means the token is dead: quietly showing (aging) cached codes forever
            // would hide that nothing syncs anymore — flag it so the UI can offer to
            // re-authenticate. Other failures are transient; keep the cached entries.
            if case EnteAPI.APIError.http(let status, _) = error, status == 401 {
                sessionExpired = true
                state = .error("Session expired — please sign in again.")
            } else if entries.isEmpty {
                state = .error((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
            }
            Log.ente.error("Refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    // MARK: Sign out

    func signOut() {
        Keychain.remove(K.token)
        Keychain.remove(K.authKey)
        Keychain.remove(K.email)
        cache.clear()
        authKey = nil
        accountEmail = nil
        entries = []
        state = .signedOut
        sessionExpired = false
        Task { await api.setAuthToken(nil) }
    }

    // MARK: Decryption

    /// Decrypts cached entities into displayable entries, sorted by display name. Entries
    /// that fail to decrypt or parse are skipped (logged), so one bad row can't break the
    /// list.
    private func decrypt(entities: [EntityCache.CachedEntity]) -> [AuthEntry] {
        guard let authKey else { return [] }
        var result: [AuthEntry] = []
        for entity in entities {
            guard let cipher = Base64.decodeStandard(entity.encryptedData),
                  let header = Base64.decodeStandard(entity.header) else { continue }
            do {
                let plaintext = try EnteCrypto.secretStreamOpenSingleChunk(cipherText: cipher, header: header, key: authKey)
                let uri = Self.unwrapURI(from: plaintext)
                if let entry = AuthEntry.parse(uri: uri, id: entity.id) {
                    result.append(entry)
                }
            } catch {
                Log.ente.error("Skipped an entity that failed to decrypt: \(error.localizedDescription, privacy: .public)")
            }
        }
        return result.sorted {
            $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending
        }
    }

    /// The decrypted entity payload is a JSON string literal wrapping the `otpauth://`
    /// URI (e.g. `"otpauth://totp/…"`). Unwrap it, tolerating a bare (unquoted) URI too.
    private static func unwrapURI(from plaintext: [UInt8]) -> String {
        let data = Data(plaintext)
        if let decoded = try? JSONDecoder().decode(String.self, from: data) {
            return decoded
        }
        if let fragment = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) as? String {
            return fragment
        }
        return String(decoding: plaintext, as: UTF8.self)
    }
}
