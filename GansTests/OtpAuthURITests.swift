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

    func testDigitsAndPeriodClampedToSafeRange() {
        // An oversized digit count must be clamped (10^digits would overflow UInt32), and
        // a zero/negative period must be coerced to a sane value.
        let uri = "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&digits=12&period=0"
        let entry = AuthEntry.parse(uri: uri, id: "10")
        XCTAssertEqual(entry?.digits, 9)
        XCTAssertEqual(entry?.period, 1)
        // Generating a code must not crash and must respect the clamped width.
        XCTAssertEqual(entry?.code(at: Date(timeIntervalSince1970: 59)).count, 9)
    }

    func testDisplayName() {
        let withBoth = AuthEntry.parse(uri: "otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP", id: "8")
        XCTAssertEqual(withBoth?.displayName, "Iss (acct)")
        let accountOnly = AuthEntry.parse(uri: "otpauth://totp/justme?secret=JBSWY3DPEHPK3PXP", id: "9")
        XCTAssertEqual(accountOnly?.issuer, "")
        XCTAssertEqual(accountOnly?.account, "justme")
        XCTAssertEqual(accountOnly?.displayName, "justme")
    }

    // MARK: Escaping / robustness

    func testDuplicateQueryKeysDoNotCrashAndFirstWins() {
        // Real-world exports repeat parameters; Dictionary(uniqueKeysWithValues:) would trap.
        let uri = "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&secret=AAAA&digits=6&DIGITS=8"
        let entry = AuthEntry.parse(uri: uri, id: "20")
        XCTAssertNotNil(entry)
        XCTAssertEqual(entry?.digits, 6)
        XCTAssertEqual(entry?.secret, Base32.decode("JBSWY3DPEHPK3PXP"))
    }

    func testLabelIsDecodedExactlyOnce() {
        // "Rate %20 Club" is stored with its literal %-sequence encoded as %2520; a
        // double decode would turn it into "Rate   Club".
        let uri = "otpauth://totp/Rate%20%2520%20Club:bob?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "21")
        XCTAssertEqual(entry?.issuer, "Rate %20 Club")
        XCTAssertEqual(entry?.account, "bob")
    }

    func testEncodedColonInsideIssuerDoesNotSplitLabel() {
        let uri = "otpauth://totp/we%3Aird:alice?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "22")
        XCTAssertEqual(entry?.issuer, "we:ird")
        XCTAssertEqual(entry?.account, "alice")
    }

    func testPlusInQueryValueMeansSpaceLikeEntesWebClient() {
        let uri = "otpauth://totp/acct?secret=JBSWY3DPEHPK3PXP&issuer=My+Bank"
        XCTAssertEqual(AuthEntry.parse(uri: uri, id: "23")?.issuer, "My Bank")
        // …while an encoded plus stays a plus.
        let encoded = "otpauth://totp/acct?secret=JBSWY3DPEHPK3PXP&issuer=Disney%2B"
        XCTAssertEqual(AuthEntry.parse(uri: encoded, id: "24")?.issuer, "Disney+")
    }

    func testRawSpacesInLabelParseInsteadOfBeingDropped() {
        // URLComponents(string:) rejects unencoded spaces; the lenient path must save it.
        let uri = "otpauth://totp/My Bank:alice bob?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "25")
        XCTAssertEqual(entry?.issuer, "My Bank")
        XCTAssertEqual(entry?.account, "alice bob")
    }

    func testUnicodeNamesSurvive() {
        let uri = "otpauth://totp/B%C3%A4ckerei%20Z%C3%BCrich:fr%C3%A9d%C3%A9ric?secret=JBSWY3DPEHPK3PXP"
        let entry = AuthEntry.parse(uri: uri, id: "26")
        XCTAssertEqual(entry?.issuer, "Bäckerei Zürich")
        XCTAssertEqual(entry?.account, "frédéric")
    }

    // MARK: codeDisplay metadata

    func testCodeDisplayTrashedAndPinnedAndNote() {
        let display = "%7B%22pinned%22%3Atrue%2C%22trashed%22%3Afalse%2C%22note%22%3A%22hello%22%7D"
        let uri = "otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay=\(display)"
        let entry = AuthEntry.parse(uri: uri, id: "27")
        XCTAssertEqual(entry?.pinned, true)
        XCTAssertEqual(entry?.isTrashed, false)
        XCTAssertEqual(entry?.note, "hello")

        let trashed = "%7B%22trashed%22%3Atrue%7D"
        let trashedURI = "otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay=\(trashed)"
        XCTAssertEqual(AuthEntry.parse(uri: trashedURI, id: "28")?.isTrashed, true)
    }

    func testMalformedCodeDisplayIsIgnored() {
        let uri = "otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay=notjson"
        let entry = AuthEntry.parse(uri: uri, id: "29")
        XCTAssertNotNil(entry)
        XCTAssertEqual(entry?.pinned, false)
        XCTAssertEqual(entry?.isTrashed, false)
    }
}
