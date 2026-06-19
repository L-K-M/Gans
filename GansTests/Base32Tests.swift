import XCTest
@testable import Gans

/// RFC 4648 §10 base32 test vectors.
final class Base32Tests: XCTestCase {
    private func decodeString(_ s: String) -> String? {
        Base32.decode(s).map { String(decoding: $0, as: UTF8.self) }
    }

    func testRFC4648Vectors() {
        XCTAssertEqual(decodeString("MY======"), "f")
        XCTAssertEqual(decodeString("MZXQ===="), "fo")
        XCTAssertEqual(decodeString("MZXW6==="), "foo")
        XCTAssertEqual(decodeString("MZXW6YQ="), "foob")
        XCTAssertEqual(decodeString("MZXW6YTB"), "fooba")
        XCTAssertEqual(decodeString("MZXW6YTBOI======"), "foobar")
    }

    func testToleratesLowercaseSpacesAndMissingPadding() {
        XCTAssertEqual(decodeString("mzxw6ytb"), "fooba")
        XCTAssertEqual(decodeString("MZXW 6YTB"), "fooba")
        XCTAssertEqual(decodeString("MZXW6YTBOI"), "foobar") // no padding
    }

    func testRejectsInvalidCharacters() {
        XCTAssertNil(Base32.decode("0189")) // 0, 1, 8, 9 aren't in the alphabet
        XCTAssertNil(Base32.decode(""))
    }

    func testKnownAuthenticatorSeed() {
        // The RFC 6238 SHA1 seed "12345678901234567890".
        XCTAssertEqual(decodeString("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), "12345678901234567890")
    }
}
