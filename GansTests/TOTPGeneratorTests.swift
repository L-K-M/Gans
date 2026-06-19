import XCTest
@testable import Gans

/// RFC 6238 (TOTP) and RFC 4226 (HOTP) published test vectors.
final class TOTPGeneratorTests: XCTestCase {

    private let sha1Seed = Array("12345678901234567890".utf8)
    private let sha256Seed = Array("12345678901234567890123456789012".utf8)
    private let sha512Seed = Array("1234567890123456789012345678901234567890123456789012345678901234".utf8)

    private func totp(_ seed: [UInt8], _ time: TimeInterval, _ algo: OTPAlgorithm) -> String {
        TOTPGenerator.totp(secret: seed, time: Date(timeIntervalSince1970: time),
                           period: 30, digits: 8, algorithm: algo)
    }

    func testRFC6238_SHA1() {
        XCTAssertEqual(totp(sha1Seed, 59, .sha1), "94287082")
        XCTAssertEqual(totp(sha1Seed, 1111111109, .sha1), "07081804")
        XCTAssertEqual(totp(sha1Seed, 1111111111, .sha1), "14050471")
        XCTAssertEqual(totp(sha1Seed, 1234567890, .sha1), "89005924")
        XCTAssertEqual(totp(sha1Seed, 2000000000, .sha1), "69279037")
        XCTAssertEqual(totp(sha1Seed, 20000000000, .sha1), "65353130")
    }

    func testDigitsClampedToSafeRange() {
        // 10^10 overflows UInt32; digits must be clamped so generation never traps.
        let huge = TOTPGenerator.code(secret: sha1Seed, counter: 1, digits: 99, algorithm: .sha1)
        XCTAssertEqual(huge.count, 9)
        XCTAssertEqual(huge, TOTPGenerator.code(secret: sha1Seed, counter: 1, digits: 9, algorithm: .sha1))
        // A zero/negative digit count clamps up to 1 (and stays non-empty).
        XCTAssertEqual(TOTPGenerator.code(secret: sha1Seed, counter: 1, digits: 0, algorithm: .sha1).count, 1)
    }

    func testRFC6238_SHA256() {
        XCTAssertEqual(totp(sha256Seed, 59, .sha256), "46119246")
        XCTAssertEqual(totp(sha256Seed, 1111111109, .sha256), "68084774")
        XCTAssertEqual(totp(sha256Seed, 2000000000, .sha256), "90698825")
    }

    func testRFC6238_SHA512() {
        XCTAssertEqual(totp(sha512Seed, 59, .sha512), "90693936")
        XCTAssertEqual(totp(sha512Seed, 1111111111, .sha512), "99943326")
        XCTAssertEqual(totp(sha512Seed, 20000000000, .sha512), "47863826")
    }

    func testRFC4226_HOTP() {
        let expected = ["755224", "287082", "359152", "969429", "338314",
                        "254676", "287922", "162583", "399871", "520489"]
        for (counter, code) in expected.enumerated() {
            XCTAssertEqual(TOTPGenerator.code(secret: sha1Seed, counter: UInt64(counter),
                                              digits: 6, algorithm: .sha1), code)
        }
    }

    func testSecondsRemaining() {
        XCTAssertEqual(TOTPGenerator.secondsRemaining(time: Date(timeIntervalSince1970: 0), period: 30), 30)
        XCTAssertEqual(TOTPGenerator.secondsRemaining(time: Date(timeIntervalSince1970: 10), period: 30), 20)
        XCTAssertEqual(TOTPGenerator.secondsRemaining(time: Date(timeIntervalSince1970: 29), period: 30), 1)
    }

    func testSteamIsDeterministicAndWellFormed() {
        let code = TOTPGenerator.steam(secret: sha1Seed, time: Date(timeIntervalSince1970: 59))
        XCTAssertEqual(code.count, 5)
        let alphabet = Set("23456789BCDFGHJKMNPQRTVWXY")
        XCTAssertTrue(code.allSatisfy { alphabet.contains($0) })
        // Stable within the same 30s window (t=30 and t=59 are both window 1).
        XCTAssertEqual(code, TOTPGenerator.steam(secret: sha1Seed, time: Date(timeIntervalSince1970: 30)))
    }
}
