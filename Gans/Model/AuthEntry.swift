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
    /// Ente `codeDisplay` metadata: pinned entries float to the top of Quick Search.
    var pinned: Bool = false
    /// The entry sits in Ente's trash and must not be shown or typed.
    var isTrashed: Bool = false
    /// Free-form user note from Ente (kept for display/search; empty when absent).
    var note: String = ""

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

    /// Whether the code rotates on a clock (TOTP/Steam). HOTP advances by counter, so it
    /// has no time-based countdown.
    var isTimeBased: Bool {
        if case .hotp = kind { return false }
        return true
    }

    /// Fraction of the current period still remaining (1 → just refreshed, 0 → expiring),
    /// for a countdown indicator. Always 1 for non-time-based kinds.
    func fractionRemaining(at time: Date = Date()) -> Double {
        guard isTimeBased, period > 0 else { return 1 }
        return Double(secondsRemaining(at: time)) / Double(period)
    }
}

extension AuthEntry {
    /// Parses an `otpauth://{totp|hotp|steam}/[issuer:]account?secret=...&...` URI.
    /// Returns `nil` if the scheme/secret are missing or unusable.
    ///
    /// Escaping rules, chosen to match what Ente's own clients do:
    /// - The label is split on `:` **before** percent-decoding, so an encoded colon
    ///   (`%3A`) inside a name can't confuse the issuer/account split, and each side is
    ///   decoded exactly once (`URLComponents.path` would already be decoded — decoding
    ///   it again mangles names containing literal `%XX` sequences).
    /// - Query values are form-decoded the way `URLSearchParams` does on Ente's web
    ///   client: `+` means space, then percent-decode once. Duplicate keys keep the
    ///   first value (never trap), and keys compare case-insensitively.
    /// - URIs that `URLComponents` rejects (raw spaces and other unencoded characters —
    ///   common in real-world exports) are percent-escaped and retried rather than
    ///   silently dropped.
    static func parse(uri: String, id: String) -> AuthEntry? {
        guard let components = lenientComponents(from: uri.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme?.lowercased() == "otpauth" else { return nil }

        let typeString = (components.host ?? "").lowercased()
        let query = formDecodedQuery(components.percentEncodedQuery)

        guard let secretString = query["secret"], let secret = Base32.decode(secretString), !secret.isEmpty else {
            return nil
        }

        // Label is the raw path minus the leading slash; "Issuer:Account" or "Account".
        var rawLabel = components.percentEncodedPath
        if rawLabel.hasPrefix("/") { rawLabel.removeFirst() }
        var issuer = query["issuer"] ?? ""
        var account = decodeLabelComponent(rawLabel)
        if let colon = rawLabel.firstIndex(of: ":") {
            let prefix = decodeLabelComponent(String(rawLabel[..<colon]))
            let suffix = decodeLabelComponent(String(rawLabel[rawLabel.index(after: colon)...]))
            if issuer.isEmpty { issuer = prefix }
            account = suffix
        }

        let algorithm = OTPAlgorithm(rawValueLenient: query["algorithm"])
        // Clamp digits (1...9) so an oversized value can't overflow code generation, and
        // keep the period positive so the TOTP window math stays sane.
        let parsedDigits = Int(query["digits"] ?? "") ?? (typeString == "steam" ? 5 : 6)
        let digits = min(max(parsedDigits, 1), 9)
        let period = max(Int(query["period"] ?? "") ?? 30, 1)

        let kind: Kind
        switch typeString {
        case "hotp":
            kind = .hotp(counter: UInt64(query["counter"] ?? "0") ?? 0)
        case "steam":
            kind = .steam
        default:
            kind = .totp
        }

        // Ente appends a `codeDisplay` query param: JSON with pinned/trashed/note/tags.
        var pinned = false
        var trashed = false
        var note = ""
        if let raw = query["codedisplay"], let data = raw.data(using: .utf8),
           let display = try? JSONDecoder().decode(CodeDisplay.self, from: data) {
            pinned = display.pinned ?? false
            trashed = display.trashed ?? false
            note = display.note ?? ""
        }

        return AuthEntry(id: id, kind: kind, issuer: issuer, account: account,
                         secret: secret, algorithm: algorithm,
                         digits: typeString == "steam" ? 5 : digits, period: period,
                         pinned: pinned, isTrashed: trashed, note: note)
    }

    /// The subset of Ente's `codeDisplay` JSON that Gans understands. Unknown fields are
    /// ignored; every field is optional so a partial or evolving payload still parses.
    private struct CodeDisplay: Decodable {
        var pinned: Bool?
        var trashed: Bool?
        var note: String?
    }

    // MARK: Escaping helpers

    /// Percent-decodes one label component exactly once and trims surrounding spaces.
    private static func decodeLabelComponent(_ raw: String) -> String {
        (raw.removingPercentEncoding ?? raw).trimmingCharacters(in: .whitespaces)
    }

    /// Form-decodes the raw (still percent-encoded) query string: `+` means space, then
    /// percent-decode once. Keys are lowercased; on duplicates the first value wins —
    /// `Dictionary(uniqueKeysWithValues:)` would trap on real-world exports that repeat
    /// a parameter.
    private static func formDecodedQuery(_ rawQuery: String?) -> [String: String] {
        guard let rawQuery, !rawQuery.isEmpty else { return [:] }
        var result: [String: String] = [:]
        for pair in rawQuery.split(separator: "&") {
            let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard let first = parts.first, !first.isEmpty else { continue }
            let name = formDecode(String(first)).lowercased()
            let value = parts.count > 1 ? formDecode(String(parts[1])) : ""
            if result[name] == nil { result[name] = value }
        }
        return result
    }

    private static func formDecode(_ raw: String) -> String {
        let spaced = raw.replacingOccurrences(of: "+", with: " ")
        return spaced.removingPercentEncoding ?? spaced
    }

    /// `URLComponents(string:)` rejects URIs containing raw spaces or other unencoded
    /// characters, which real-world exports produce routinely. First try the string
    /// as-is; on failure percent-escape everything outside the URI-legal set (keeping
    /// existing `%XX` sequences); as a last resort escape `%` too (for strings with
    /// stray, malformed percents).
    private static func lenientComponents(from uri: String) -> URLComponents? {
        if let components = URLComponents(string: uri) { return components }
        if let components = URLComponents(string: percentEscapeDisallowed(uri, keepPercent: true)) {
            return components
        }
        return URLComponents(string: percentEscapeDisallowed(uri, keepPercent: false))
    }

    /// Every character RFC 3986 allows somewhere in a URI, plus `%` (handled separately).
    private static let uriAllowed: CharacterSet = {
        var set = CharacterSet.alphanumerics
        set.insert(charactersIn: "-._~:/?#[]@!$&'()*+,;=")
        return set
    }()

    private static func percentEscapeDisallowed(_ string: String, keepPercent: Bool) -> String {
        var out = String.UnicodeScalarView()
        for scalar in string.unicodeScalars {
            if uriAllowed.contains(scalar) || (keepPercent && scalar == "%") {
                out.append(scalar)
            } else {
                for byte in String(scalar).utf8 {
                    out.append(contentsOf: String(format: "%%%02X", byte).unicodeScalars)
                }
            }
        }
        return String(out)
    }
}
