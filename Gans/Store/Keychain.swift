import Foundation
import Security

/// A tiny generic-password Keychain wrapper. Gans stores only the Ente auth token, the
/// 32-byte authenticator key, and a little session metadata here — never the password,
/// and never plaintext TOTP secrets.
enum Keychain {
    private static let service = "ch.lkmc.Gans"

    @discardableResult
    static func set(_ data: Data, for account: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)

        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
    }

    static func data(for account: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }

    @discardableResult
    static func remove(_ account: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    // MARK: Typed convenience

    static func setString(_ value: String, for account: String) {
        set(Data(value.utf8), for: account)
    }

    static func string(for account: String) -> String? {
        data(for: account).flatMap { String(data: $0, encoding: .utf8) }
    }
}
