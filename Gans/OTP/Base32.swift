import Foundation

/// RFC 4648 base32 decoder, used for the `secret` in `otpauth://` URIs. Tolerant of
/// lowercase, spaces, and missing `=` padding (authenticator secrets are routinely
/// shared without it).
enum Base32 {
    private static let alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    private static let lookup: [Character: UInt8] = {
        var map: [Character: UInt8] = [:]
        for (index, character) in alphabet.enumerated() { map[character] = UInt8(index) }
        return map
    }()

    static func decode(_ input: String) -> [UInt8]? {
        let cleaned = input
            .uppercased()
            .replacingOccurrences(of: " ", with: "")
            .replacingOccurrences(of: "-", with: "")
            .trimmingCharacters(in: CharacterSet(charactersIn: "="))
        guard !cleaned.isEmpty else { return nil }

        var bits = 0
        var accumulator = 0
        var output = [UInt8]()
        output.reserveCapacity(cleaned.count * 5 / 8)

        for character in cleaned {
            guard let value = lookup[character] else { return nil }
            accumulator = (accumulator << 5) | Int(value)
            bits += 5
            if bits >= 8 {
                bits -= 8
                output.append(UInt8((accumulator >> bits) & 0xFF))
            }
        }
        return output
    }
}
