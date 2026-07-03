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

    func testSubsequenceFuzzyMatch() {
        // "ghb" isn't a prefix or substring of "GitHub", but it's a subsequence.
        let result = SearchFilter.filter(entries, query: "ghb")
        XCTAssertEqual(result.first?.issuer, "GitHub")
    }

    func testLiteralMatchOutranksFuzzy() {
        // "gitl" is a prefix of GitLab (rank 0) but only a subsequence of GitHub
        // ("git" + the "l" in alice, rank 3); the prefix hit must win.
        let result = SearchFilter.filter(entries, query: "gitl")
        XCTAssertEqual(result.first?.issuer, "GitLab")
    }

    func testIsSubsequence() {
        XCTAssertTrue(SearchFilter.isSubsequence("ghb", of: "github"))
        XCTAssertTrue(SearchFilter.isSubsequence("", of: "anything"))
        XCTAssertFalse(SearchFilter.isSubsequence("bhg", of: "github")) // order matters
        XCTAssertFalse(SearchFilter.isSubsequence("xyz", of: "github"))
    }

    func testRecentlyUsedFloatsToTopForEmptyQuery() {
        // Amazon would normally sort first alphabetically; recency overrides that.
        let result = SearchFilter.filter(entries, query: "", recentIDs: ["Google-carol@github.io", "GitLab-bob"])
        XCTAssertEqual(result.map(\.issuer), ["Google", "GitLab", "Amazon", "GitHub"])
    }

    func testRecencyBreaksTiesWithinSameRank() {
        // Both Git* are prefix matches for "git" (rank 0); the recent one wins the tie.
        let result = SearchFilter.filter(entries, query: "git", recentIDs: ["GitLab-bob"])
        XCTAssertEqual(result.first?.issuer, "GitLab")
    }

    func testMultiTokenQueryMatchesAcrossFieldsInAnyOrder() {
        XCTAssertEqual(SearchFilter.filter(entries, query: "github alice").map(\.issuer), ["GitHub"])
        XCTAssertEqual(SearchFilter.filter(entries, query: "alice github").map(\.issuer), ["GitHub"])
        XCTAssertTrue(SearchFilter.filter(entries, query: "github dave").isEmpty) // AND, not OR
    }

    func testMultiTokenRankUsesWorstToken() {
        // "git bob" → GitLab: "git" prefix (0) + "bob" account prefix (1) → worst 1.
        // Nothing else matches both tokens.
        XCTAssertEqual(SearchFilter.filter(entries, query: "git bob").map(\.issuer), ["GitLab"])
    }

    func testPinnedFloatsToTopForEmptyQueryAndWithinRank() {
        var pinnedGoogle = entry(issuer: "Google", account: "carol@github.io")
        pinnedGoogle.pinned = true
        let mixed = [entries[0], entries[1], pinnedGoogle, entries[3]]

        XCTAssertEqual(SearchFilter.filter(mixed, query: "").first?.issuer, "Google")
        // Within the same match rank, pinned beats recency.
        let result = SearchFilter.filter(mixed, query: "g", recentIDs: ["GitHub-alice"])
        XCTAssertEqual(result.first?.issuer, "Google")
    }

    func testTagFilterWithHashToken() {
        var work = entry(issuer: "GitHub", account: "alice")
        work.tags = ["Work", "dev"]
        var personal = entry(issuer: "GitLab", account: "bob")
        personal.tags = ["personal"]
        let set = [work, personal]

        XCTAssertEqual(SearchFilter.filter(set, query: "#work").map(\.issuer), ["GitHub"])
        XCTAssertEqual(SearchFilter.filter(set, query: "#personal").map(\.issuer), ["GitLab"])
        // Text token AND tag token together.
        XCTAssertEqual(SearchFilter.filter(set, query: "git #work").map(\.issuer), ["GitHub"])
        // A tag that matches nothing yields nothing.
        XCTAssertTrue(SearchFilter.filter(set, query: "#nope").isEmpty)
        // A lone '#' is not a tag filter — treat it as an empty query.
        XCTAssertEqual(SearchFilter.filter(set, query: "#").count, 2)
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
