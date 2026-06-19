import XCTest
@testable import Gans

final class OtpAuthURITests: XCTestCase {

    func testParsesStandardTOTP() {
        let uri = "otpauth://totp/GitHub:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=GitHub&algorithm=SHA1&digits=6&period=30"
        let entry = AuthEntry.parse(uri: uri, id: "1")
        XCTAssertNotNil(entry)
        XCTAssertEqual(entry?.issuer, "GitHub")
        XCTAssertEqual(entry?.account, "alice@example.com")
        XCTAssertEqual(entry?.digits, 6)
        XCTAssertEqual(entry?.period, 30)
        XCTAssertEqual(entry?.algorithm, .sha1)
        if case .totp = entry?.kind {} else { XCTFail("expected totp") }
    }

    func testIssuerFromLabelPrefixWhenQueryMissing() {
        let uri = "otpauth://totp/AWS:root?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "2")
        XCTAssertEqual(entry?.issuer, "AWS")
        XCTAssertEqual(entry?.account, "root")
    }

    func testParsesHOTPWithCounter() {
        let uri = "otpauth://hotp/Acme:bob?secret=JBSWY3DPEHPK3PXP&counter=42"
        let entry = AuthEntry.parse(uri: uri, id: "3")
        if case .hotp(let counter) = entry?.kind {
            XCTAssertEqual(counter, 42)
        } else {
            XCTFail("expected hotp")
        }
    }

    func testParsesSteamDefaults() {
        let uri = "otpauth://steam/Steam:gamer?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "4")
        XCTAssertEqual(entry?.digits, 5)
        if case .steam = entry?.kind {} else { XCTFail("expected steam") }
    }

    func testAlgorithmFallsBackToSHA1() {
        let uri = "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&algorithm=BOGUS"
        XCTAssertEqual(AuthEntry.parse(uri: uri, id: "5")?.algorithm, .sha1)
    }

    func testRejectsMissingSecretAndWrongScheme() {
        XCTAssertNil(AuthEntry.parse(uri: "otpauth://totp/x?issuer=Y", id: "6"))
        XCTAssertNil(AuthEntry.parse(uri: "https://example.com", id: "7"))
    }

    func testDisplayName() {
        let withBoth = AuthEntry.parse(uri: "otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP", id: "8")
        XCTAssertEqual(withBoth?.displayName, "Iss (acct)")
        let accountOnly = AuthEntry.parse(uri: "otpauth://totp/justme?secret=JBSWY3DPEHPK3PXP", id: "9")
        XCTAssertEqual(accountOnly?.issuer, "")
        XCTAssertEqual(accountOnly?.account, "justme")
        XCTAssertEqual(accountOnly?.displayName, "justme")
    }
}
