import SwiftUI
import Combine

/// View model for the Quick Search panel: holds the query, the filtered results, and the
/// current selection. The view binds to it; the panel controller feeds it the live entry
/// list and handles commit.
final class QuickSearchModel: ObservableObject {
    @Published var query: String = "" {
        didSet { recompute(resetSelection: true) }
    }
    @Published private(set) var results: [AuthEntry] = []
    @Published var selectedIndex: Int = 0

    /// Bumped on every `code(at:)`-relevant tick so rows refresh their displayed code.
    @Published var tick: Date = Date()

    private var allEntries: [AuthEntry] = []

    /// Most-recently-used entry ids (most recent first), used to bias result ordering.
    var recentIDs: [String] = [] {
        didSet { recompute(resetSelection: false) }
    }

    func setEntries(_ entries: [AuthEntry]) {
        allEntries = entries
        recompute(resetSelection: false)
    }

    func reset() {
        query = ""
        recompute(resetSelection: true)
    }

    var selectedEntry: AuthEntry? {
        guard results.indices.contains(selectedIndex) else { return nil }
        return results[selectedIndex]
    }

    func moveSelection(down: Bool) {
        if let next = SearchFilter.nextIndex(count: results.count, current: selectedIndex, down: down) {
            selectedIndex = next
        }
    }

    /// Recomputes the filtered results. On a query change we always re-anchor the selection
    /// to the top match (so the best result is highlighted and Enter picks it); on a
    /// background refresh we keep the current selection if it's still in range.
    private func recompute(resetSelection: Bool) {
        results = SearchFilter.filter(allEntries, query: query, recentIDs: recentIDs)
        if resetSelection || !results.indices.contains(selectedIndex) {
            selectedIndex = 0
        }
    }
}
