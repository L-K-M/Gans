import XCTest
@testable import Gans

final class SearchFilterTests: XCTestCase {

    private func entry(issuer: String, account: String) -> AuthEntry {
        AuthEntry(id: "\(issuer)-\(account)", kind: .totp, issuer: issuer, account: account,
                  secret: [1, 2, 3], algorithm: .sha1, digits: 6, period: 30)
    }

    private lazy var entries = [
        entry(issuer: "GitHub", account: "alice"),
        entry(issuer: "GitLab", account: "bob"),
        entry(issuer: "Google", account: "carol@github.io"),
        entry(issuer: "Amazon", account: "dave"),
    ]

    func testEmptyQueryReturnsAllSortedByName() {
        let result = SearchFilter.filter(entries, query: "  ")
        XCTAssertEqual(result.map(\.issuer), ["Amazon", "GitHub", "GitLab", "Google"])
    }

    func testPrefixMatchesRankAboveSubstring() {
        // "git" prefixes GitHub/GitLab (rank 0); it's also a substring of Google's account
        // "carol@github.io" (rank 2), so the two Git* entries must come first.
        let result = SearchFilter.filter(entries, query: "git")
        XCTAssertEqual(result.prefix(2).map(\.issuer).sorted(), ["GitHub", "GitLab"])
        XCTAssertEqual(result.last?.issuer, "Google")
    }

    func testCaseAndDiacriticInsensitive() {
        let withDiacritic = [entry(issuer: "Crédit", account: "x")]
        XCTAssertEqual(SearchFilter.filter(withDiacritic, query: "credit").count, 1)
        XCTAssertEqual(SearchFilter.filter(entries, query: "AMAZON").first?.issuer, "Amazon")
    }

    func testAccountMatch() {
        XCTAssertEqual(SearchFilter.filter(entries, query: "dave").first?.issuer, "Amazon")
    }

    func testNoMatch() {
        XCTAssertTrue(SearchFilter.filter(entries, query: "zzzzz").isEmpty)
    }

    func testNextIndexClampsAndWraps() {
        XCTAssertEqual(SearchFilter.nextIndex(count: 3, current: 0, down: true), 1)
        XCTAssertEqual(SearchFilter.nextIndex(count: 3, current: 2, down: true), 2) // clamp at end
        XCTAssertEqual(SearchFilter.nextIndex(count: 3, current: 0, down: false), 0) // clamp at start
        XCTAssertEqual(SearchFilter.nextIndex(count: 3, current: nil, down: true), 0)
        XCTAssertEqual(SearchFilter.nextIndex(count: 3, current: nil, down: false), 2)
        XCTAssertNil(SearchFilter.nextIndex(count: 0, current: nil, down: true))
    }
}
