import Foundation
import CryptoKit

/// Hash algorithm for the HMAC step of an OTP.
enum OTPAlgorithm: String {
    case sha1 = "SHA1"
    case sha256 = "SHA256"
    case sha512 = "SHA512"

    init(rawValueLenient string: String?) {
        switch string?.uppercased() {
        case "SHA256": self = .sha256
        case "SHA512": self = .sha512
        default: self = .sha1
        }
    }
}

/// Generates one-time codes for TOTP (RFC 6238), HOTP (RFC 4226), and Steam Guard.
/// Pure and deterministic given a counter/time, so it's covered by RFC test vectors.
enum TOTPGenerator {

    /// The Steam Guard 5-character alphabet (base-26-ish over these symbols).
    private static let steamAlphabet = Array("23456789BCDFGHJKMNPQRTVWXY")

    /// HMAC of an 8-byte big-endian counter under `secret`, for the given algorithm.
    private static func hmac(counter: UInt64, secret: [UInt8], algorithm: OTPAlgorithm) -> [UInt8] {
        var bigEndian = counter.bigEndian
        let message = withUnsafeBytes(of: &bigEndian) { Array($0) }
        let key = SymmetricKey(data: Data(secret))
        switch algorithm {
        case .sha1:
            return Array(HMAC<Insecure.SHA1>.authenticationCode(for: Data(message), using: key))
        case .sha256:
            return Array(HMAC<SHA256>.authenticationCode(for: Data(message), using: key))
        case .sha512:
            return Array(HMAC<SHA512>.authenticationCode(for: Data(message), using: key))
        }
    }

    /// RFC 4226 dynamic truncation → a 31-bit integer.
    private static func truncate(_ digest: [UInt8]) -> UInt32 {
        let offset = Int(digest[digest.count - 1] & 0x0F)
        let value = (UInt32(digest[offset] & 0x7F) << 24)
            | (UInt32(digest[offset + 1]) << 16)
            | (UInt32(digest[offset + 2]) << 8)
            | UInt32(digest[offset + 3])
        return value
    }

    /// A counter-based code (HOTP, and the engine behind TOTP).
    static func code(secret: [UInt8], counter: UInt64, digits: Int, algorithm: OTPAlgorithm) -> String {
        let digest = hmac(counter: counter, secret: secret, algorithm: algorithm)
        let truncated = truncate(digest)
        let modulus = UInt32(pow(10.0, Double(digits)))
        let number = truncated % modulus
        return String(format: "%0\(digits)u", number)
    }

    /// A time-based code (TOTP) at a given instant.
    static func totp(secret: [UInt8], time: Date = Date(), period: Int = 30, digits: Int = 6,
                     algorithm: OTPAlgorithm = .sha1) -> String {
        let counter = UInt64(floor(time.timeIntervalSince1970 / Double(period)))
        return code(secret: secret, counter: counter, digits: digits, algorithm: algorithm)
    }

    /// A Steam Guard code: 5 chars over the Steam alphabet, SHA1, period 30.
    static func steam(secret: [UInt8], time: Date = Date(), period: Int = 30) -> String {
        let counter = UInt64(floor(time.timeIntervalSince1970 / Double(period)))
        let digest = hmac(counter: counter, secret: secret, algorithm: .sha1)
        var value = truncate(digest)
        var result = ""
        for _ in 0..<5 {
            let index = Int(value) % steamAlphabet.count
            result.append(steamAlphabet[index])
            value /= UInt32(steamAlphabet.count)
        }
        return result
    }

    /// Seconds remaining in the current TOTP window (for the countdown ring).
    static func secondsRemaining(time: Date = Date(), period: Int = 30) -> Int {
        let elapsed = Int(time.timeIntervalSince1970) % period
        return period - elapsed
    }
}
