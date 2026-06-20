import XCTest
@testable import Gans

final class Base64Tests: XCTestCase {

    func testStandardRoundTrip() {
        let bytes: [UInt8] = [0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x10]
        let encoded = Base64.encodeStandard(bytes)
        XCTAssertEqual(Base64.decodeStandard(encoded), bytes)
    }

    func testStandardToleratesMissingPadding() {
        // "foob" -> "Zm9vYg==" ; without padding it should still decode.
        XCTAssertEqual(Base64.decodeStandard("Zm9vYg"), Array("foob".utf8))
        XCTAssertEqual(Base64.decodeStandard("Zm9vYg=="), Array("foob".utf8))
    }

    func testURLSafeRoundTrip() {
        let bytes: [UInt8] = (0...40).map { UInt8($0) }
        let encoded = Base64.encodeURLSafe(bytes)
        XCTAssertFalse(encoded.contains("+"))
        XCTAssertFalse(encoded.contains("/"))
        XCTAssertFalse(encoded.contains("="))
        XCTAssertEqual(Base64.decodeURLSafe(encoded), bytes)
    }

    func testURLSafePaddedKeepsPaddingButStaysURLSafe() {
        // "foob" -> standard "Zm9vYg==" ; padded url-safe must keep the "==".
        let bytes = Array("foob".utf8)
        let padded = Base64.encodeURLSafePadded(bytes)
        XCTAssertEqual(padded, "Zm9vYg==")
        XCTAssertFalse(padded.contains("+"))
        XCTAssertFalse(padded.contains("/"))
        // A pattern that yields '+'/'/' in standard base64 must use '-'/'_' instead.
        let tricky = Base64.encodeURLSafePadded([0xFB, 0xFF, 0xBF])
        XCTAssertFalse(tricky.contains("+"))
        XCTAssertFalse(tricky.contains("/"))
        XCTAssertEqual(Base64.decodeURLSafe(tricky), [0xFB, 0xFF, 0xBF])
    }

    func testURLSafeDecodesStandardDistinctly() {
        // A byte pattern that produces '+' and '/' in standard base64.
        let bytes: [UInt8] = [0xFB, 0xFF, 0xBF]
        let std = Base64.encodeStandard(bytes)
        XCTAssertTrue(std.contains("+") || std.contains("/"))
        let url = Base64.encodeURLSafe(bytes)
        XCTAssertEqual(Base64.decodeURLSafe(url), bytes)
    }
}
