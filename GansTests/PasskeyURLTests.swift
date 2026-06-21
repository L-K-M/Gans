import XCTest
@testable import Gans

/// The passkey verification URL must match what Ente's accounts page expects, since a
/// non-whitelisted `redirect` makes the page refuse to run the ceremony.
final class PasskeyURLTests: XCTestCase {

    func testBuildsVerificationURL() throws {
        let url = EnteLogin.passkeyVerificationURL(accountsURL: "https://accounts.ente.io",
                                                   passkeySessionID: "sess-123",
                                                   clientPackage: "io.ente.auth")
        let comps = try XCTUnwrap(url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) })
        XCTAssertEqual(comps.host, "accounts.ente.io")
        XCTAssertEqual(comps.path, "/passkeys/verify")

        let items = Dictionary(uniqueKeysWithValues: (comps.queryItems ?? []).map { ($0.name, $0.value) })
        XCTAssertEqual(items["passkeySessionID"], "sess-123")
        XCTAssertEqual(items["clientPackage"], "io.ente.auth")
        XCTAssertEqual(items["redirect"], "ente-cli://passkey")
    }

    func testFallsBackToDefaultAccountsHost() {
        let url = EnteLogin.passkeyVerificationURL(accountsURL: "",
                                                   passkeySessionID: "s",
                                                   clientPackage: "io.ente.auth")
        let host = url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.host }
        XCTAssertEqual(host, "accounts.ente.io")
    }

    func testRejectsNonEnteAccountsHost() {
        let url = EnteLogin.passkeyVerificationURL(accountsURL: "https://evil.example.com",
                                                   passkeySessionID: "s",
                                                   clientPackage: "io.ente.auth")
        let host = url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false)?.host }
        XCTAssertEqual(host, "accounts.ente.io", "a non-Ente host must not be opened in the browser")
    }

    func testRejectsLookalikeEnteHost() {
        // "ente.io.evil.com" and "evilente.io" must not be treated as Ente hosts.
        XCTAssertEqual(EnteLogin.sanitizedAccountsBase("https://accounts.ente.io.evil.com"),
                       EnteLogin.defaultAccountsURL)
        XCTAssertEqual(EnteLogin.sanitizedAccountsBase("https://evilente.io"),
                       EnteLogin.defaultAccountsURL)
    }

    func testRejectsNonHTTPSAccountsURL() {
        let comps = EnteLogin.passkeyVerificationURL(accountsURL: "http://accounts.ente.io",
                                                     passkeySessionID: "s",
                                                     clientPackage: "io.ente.auth")
            .flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }
        XCTAssertEqual(comps?.scheme, "https")
        XCTAssertEqual(comps?.host, "accounts.ente.io")
    }

    func testKeepsLegitimateEnteSubdomain() {
        XCTAssertEqual(EnteLogin.sanitizedAccountsBase("https://accounts.ente.io"),
                       "https://accounts.ente.io")
    }
}
