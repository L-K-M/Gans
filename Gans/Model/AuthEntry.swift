import Foundation

/// One authenticator entry, parsed from an `otpauth://` URI. `id` is Ente's entity id so
/// entries stay stable across syncs.
struct AuthEntry: Identifiable, Equatable {
    enum Kind: Equatable {
        case totp
        case hotp(counter: UInt64)
        case steam
    }

    let id: String
    let kind: Kind
    /// e.g. "GitHub" — the service.
    let issuer: String
    /// e.g. "alice@example.com" — the specific account.
    let account: String
    let secret: [UInt8]
    let algorithm: OTPAlgorithm
    let digits: Int
    let period: Int

    /// A human label: "Issuer (account)" or just one when the other is empty.
    var displayName: String {
        switch (issuer.isEmpty, account.isEmpty) {
        case (false, false): return "\(issuer) (\(account))"
        case (false, true): return issuer
        case (true, false): return account
        case (true, true): return "Unknown"
        }
    }

    /// The current code at `time`.
    func code(at time: Date = Date()) -> String {
        switch kind {
        case .totp:
            return TOTPGenerator.totp(secret: secret, time: time, period: period, digits: digits, algorithm: algorithm)
        case .hotp(let counter):
            return TOTPGenerator.code(secret: secret, counter: counter, digits: digits, algorithm: algorithm)
        case .steam:
            return TOTPGenerator.steam(secret: secret, time: time, period: period)
        }
    }

    /// A nicely spaced form for display. Numeric codes are grouped (3+3 / 4+4); Steam
    /// codes are shown verbatim.
    func formattedCode(at time: Date = Date()) -> String {
        let raw = code(at: time)
        if case .steam = kind { return raw }
        switch raw.count {
        case 6: return "\(raw.prefix(3)) \(raw.suffix(3))"
        case 8: return "\(raw.prefix(4)) \(raw.suffix(4))"
        default: return raw
        }
    }

    func secondsRemaining(at time: Date = Date()) -> Int {
        TOTPGenerator.secondsRemaining(time: time, period: period)
    }
}

extension AuthEntry {
    /// Parses an `otpauth://{totp|hotp|steam}/[issuer:]account?secret=...&...` URI.
    /// Returns `nil` if the scheme/secret are missing or unusable.
    static func parse(uri: String, id: String) -> AuthEntry? {
        guard let components = URLComponents(string: uri.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme?.lowercased() == "otpauth" else { return nil }

        // Some exports percent-encode the label oddly; URLComponents handles the common case.
        let typeString = (components.host ?? "").lowercased()
        let query = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name.lowercased(), $0.value ?? "") })

        guard let secretString = query["secret"], let secret = Base32.decode(secretString), !secret.isEmpty else {
            return nil
        }

        // Label is the path minus the leading slash; "Issuer:Account" or "Account".
        let label = components.path.hasPrefix("/") ? String(components.path.dropFirst()) : components.path
        let decodedLabel = label.removingPercentEncoding ?? label
        var issuer = query["issuer"] ?? ""
        var account = decodedLabel
        if let colon = decodedLabel.firstIndex(of: ":") {
            let prefix = String(decodedLabel[..<colon]).trimmingCharacters(in: .whitespaces)
            let suffix = String(decodedLabel[decodedLabel.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            if issuer.isEmpty { issuer = prefix }
            account = suffix
        }

        let algorithm = OTPAlgorithm(rawValueLenient: query["algorithm"])
        let digits = Int(query["digits"] ?? "") ?? (typeString == "steam" ? 5 : 6)
        let period = Int(query["period"] ?? "") ?? 30

        let kind: Kind
        switch typeString {
        case "hotp":
            kind = .hotp(counter: UInt64(query["counter"] ?? "0") ?? 0)
        case "steam":
            kind = .steam
        default:
            kind = .totp
        }

        return AuthEntry(id: id, kind: kind, issuer: issuer, account: account,
                         secret: secret, algorithm: algorithm,
                         digits: typeString == "steam" ? 5 : digits, period: period)
    }
}
