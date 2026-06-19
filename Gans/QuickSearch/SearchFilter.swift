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
    /// `recentIDs` is the most-recently-used entry ids (most recent first); it biases
    /// ordering so codes you actually use float up. An empty query returns everything,
    /// recently-used first and then alphabetically. For a non-empty query, entries are
    /// ranked prefix → substring → subsequence (fuzzy), with recency and then name
    /// breaking ties *within* a rank — so a real prefix hit always beats a fuzzy one.
    static func filter(_ entries: [AuthEntry], query: String, recentIDs: [String] = []) -> [AuthEntry] {
        let recencyRank: (AuthEntry) -> Int = { entry in
            recentIDs.firstIndex(of: entry.id) ?? Int.max
        }
        let byRecencyThenName: (AuthEntry, AuthEntry) -> Bool = { lhs, rhs in
            let lr = recencyRank(lhs), rr = recencyRank(rhs)
            if lr != rr { return lr < rr }
            return lhs.displayName.localizedCaseInsensitiveCompare(rhs.displayName) == .orderedAscending
        }

        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return entries.sorted(by: byRecencyThenName)
        }

        let needle = fold(trimmed)
        let scored: [(entry: AuthEntry, rank: Int)] = entries.compactMap { entry in
            let issuer = fold(entry.issuer)
            let account = fold(entry.account)
            let display = fold(entry.displayName)

            if issuer.hasPrefix(needle) || display.hasPrefix(needle) { return (entry, 0) }
            if account.hasPrefix(needle) { return (entry, 1) }
            if issuer.contains(needle) || account.contains(needle) || display.contains(needle) { return (entry, 2) }
            // Loose fuzzy: every needle character appears in order somewhere in the name
            // ("ghb" → "GitHub"). Ranked last so it never outranks a literal match.
            if isSubsequence(needle, of: display) || isSubsequence(needle, of: account) { return (entry, 3) }
            return nil
        }

        return scored.sorted { lhs, rhs in
            if lhs.rank != rhs.rank { return lhs.rank < rhs.rank }
            return byRecencyThenName(lhs.entry, rhs.entry)
        }.map(\.entry)
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
