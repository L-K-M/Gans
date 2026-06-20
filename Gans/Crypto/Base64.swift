import Foundation

/// Base64 helpers. Ente uses **standard** base64 (with padding) for every crypto field
/// (salts, nonces, encrypted blobs, SRP A/B/M values) and **URL-safe** base64 for the
/// auth token it issues. Mixing the two silently corrupts bytes, so they're kept
/// distinct here.
enum Base64 {
    /// Decodes a standard-base64 string, tolerating missing padding.
    static func decodeStandard(_ string: String) -> [UInt8]? {
        let padded = pad(string)
        guard let data = Data(base64Encoded: padded) else { return nil }
        return [UInt8](data)
    }

    static func encodeStandard(_ bytes: [UInt8]) -> String {
        Data(bytes).base64EncodedString()
    }

    /// Decodes a URL-safe base64 string (`-`/`_`, optional padding).
    static func decodeURLSafe(_ string: String) -> [UInt8]? {
        let std = string
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        return decodeStandard(std)
    }

    static func encodeURLSafe(_ bytes: [UInt8]) -> String {
        Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// URL-safe base64 **with** `=` padding. Ente's auth token is exactly this form, and
    /// the server looks the token up as a verbatim string (no decoding), so the padding
    /// must be preserved — dropping it makes every authenticated request 401.
    static func encodeURLSafePadded(_ bytes: [UInt8]) -> String {
        Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
    }

    /// Appends `=` until the length is a multiple of 4 so Foundation will accept input
    /// that arrived without padding.
    private static func pad(_ string: String) -> String {
        let remainder = string.count % 4
        guard remainder != 0 else { return string }
        return string + String(repeating: "=", count: 4 - remainder)
    }
}
