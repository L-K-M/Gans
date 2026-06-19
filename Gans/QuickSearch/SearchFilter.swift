import Foundation

/// Pure ranking/filter for Quick Search — no AppKit, so it's fully unit-testable.
/// Matches case- and diacritic-insensitively against the issuer, account, and combined
/// display name; ranks prefix matches above interior substring matches, then
/// alphabetically.
enum SearchFilter {

    static func fold(_ string: String) -> String {
        string.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
    }

    /// Returns the entries matching `query`, best matches first. An empty query returns
    /// everything sorted by display name.
    static func filter(_ entries: [AuthEntry], query: String) -> [AuthEntry] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return entries.sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }
        }

        let needle = fold(trimmed)
        let scored: [(entry: AuthEntry, rank: Int)] = entries.compactMap { entry in
            let issuer = fold(entry.issuer)
            let account = fold(entry.account)
            let display = fold(entry.displayName)

            if issuer.hasPrefix(needle) || display.hasPrefix(needle) { return (entry, 0) }
            if account.hasPrefix(needle) { return (entry, 1) }
            if issuer.contains(needle) || account.contains(needle) || display.contains(needle) { return (entry, 2) }
            return nil
        }

        return scored.sorted { lhs, rhs in
            if lhs.rank != rhs.rank { return lhs.rank < rhs.rank }
            return lhs.entry.displayName.localizedCaseInsensitiveCompare(rhs.entry.displayName) == .orderedAscending
        }.map(\.entry)
    }

    /// The next selection index when pressing up/down through `count` rows. Clamps at the
    /// ends; with no current selection, picks the first (down) or last (up).
    static func nextIndex(count: Int, current: Int?, down: Bool) -> Int? {
        guard count > 0 else { return nil }
        guard let current, current >= 0 else { return down ? 0 : count - 1 }
        return min(max(current + (down ? 1 : -1), 0), count - 1)
    }
}
