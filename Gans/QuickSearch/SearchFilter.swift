import Foundation

/// Pure ranking/filter for Quick Search — no AppKit, so it's fully unit-testable.
/// Matches case- and diacritic-insensitively against the issuer, account, and combined
/// display name; ranks prefix matches above interior substring matches, then
/// alphabetically.
enum SearchFilter {

    static func fold(_ string: String) -> String {
        string.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
    }

    /// Returns the entries matching `query`, best matches first.
    ///
    /// The query is split on whitespace and every token must match (AND), so
    /// "github alice" and "alice github" both find the GitHub/alice entry. A single
    /// token ranks prefix → substring → subsequence (fuzzy); a multi-token query ranks
    /// by its *worst* token so a real prefix hit always beats a fuzzy one.
    ///
    /// A token beginning with `#` is a **tag filter**: `#work` keeps only entries carrying
    /// a matching Ente tag, combinable with text ("github #work"). Tag tokens filter but
    /// don't contribute to the text rank.
    ///
    /// Ordering within a rank: pinned entries first, then recently used (`recentIDs`,
    /// most recent first), then name. An empty query returns everything in that order.
    static func filter(_ entries: [AuthEntry], query: String, recentIDs: [String] = []) -> [AuthEntry] {
        let recencyRank: (AuthEntry) -> Int = { entry in
            recentIDs.firstIndex(of: entry.id) ?? Int.max
        }
        let byPinnedRecencyName: (AuthEntry, AuthEntry) -> Bool = { lhs, rhs in
            if lhs.pinned != rhs.pinned { return lhs.pinned }
            let lr = recencyRank(lhs), rr = recencyRank(rhs)
            if lr != rr { return lr < rr }
            return lhs.displayName.localizedCaseInsensitiveCompare(rhs.displayName) == .orderedAscending
        }

        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return entries.sorted(by: byPinnedRecencyName)
        }

        let allTokens = fold(trimmed).split(whereSeparator: { $0.isWhitespace }).map(String.init)
        let tagNeedles = allTokens.filter { $0.hasPrefix("#") && $0.count > 1 }.map { String($0.dropFirst()) }
        let textTokens = allTokens.filter { !$0.hasPrefix("#") }
        // A query of just "#" (or "#" plus whitespace) has nothing to match on.
        guard !tagNeedles.isEmpty || !textTokens.isEmpty else {
            return entries.sorted(by: byPinnedRecencyName)
        }

        let scored: [(entry: AuthEntry, rank: Int)] = entries.compactMap { entry in
            // Every tag needle must match one of the entry's tags (prefix or substring).
            if !tagNeedles.isEmpty {
                let entryTags = entry.tags.map(fold)
                for needle in tagNeedles {
                    guard entryTags.contains(where: { $0.hasPrefix(needle) || $0.contains(needle) }) else { return nil }
                }
            }

            let issuer = fold(entry.issuer)
            let account = fold(entry.account)
            let display = fold(entry.displayName)

            var worst = 0
            for token in textTokens {
                guard let rank = tokenRank(token, issuer: issuer, account: account, display: display) else {
                    return nil // every text token must match somewhere
                }
                worst = max(worst, rank)
            }
            return (entry, worst)
        }

        return scored.sorted { lhs, rhs in
            if lhs.rank != rhs.rank { return lhs.rank < rhs.rank }
            return byPinnedRecencyName(lhs.entry, rhs.entry)
        }.map(\.entry)
    }

    /// How well one (already-folded) token matches: 0 issuer/display prefix, 1 account
    /// prefix, 2 substring anywhere, 3 in-order subsequence ("ghb" → "GitHub"), nil none.
    private static func tokenRank(_ needle: String, issuer: String, account: String, display: String) -> Int? {
        if issuer.hasPrefix(needle) || display.hasPrefix(needle) { return 0 }
        if account.hasPrefix(needle) { return 1 }
        if issuer.contains(needle) || account.contains(needle) || display.contains(needle) { return 2 }
        if isSubsequence(needle, of: display) || isSubsequence(needle, of: account) { return 3 }
        return nil
    }

    /// Whether every character of `needle` appears in `haystack` in order (not necessarily
    /// contiguously). Both are expected to be already folded.
    static func isSubsequence(_ needle: String, of haystack: String) -> Bool {
        guard !needle.isEmpty else { return true }
        var iterator = haystack.makeIterator()
        for character in needle {
            var matched = false
            while let next = iterator.next() {
                if next == character { matched = true; break }
            }
            if !matched { return false }
        }
        return true
    }

    /// The next selection index when pressing up/down through `count` rows. Clamps at the
    /// ends; with no current selection, picks the first (down) or last (up).
    static func nextIndex(count: Int, current: Int?, down: Bool) -> Int? {
        guard count > 0 else { return nil }
        guard let current, current >= 0 else { return down ? 0 : count - 1 }
        return min(max(current + (down ? 1 : -1), 0), count - 1)
    }
}
