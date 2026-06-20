import SwiftUI
import Combine

/// View model for the Quick Search panel: holds the query, the filtered results, and the
/// current selection. The view binds to it; the panel controller feeds it the live entry
/// list and handles commit.
///
/// Selection is tracked by **entry id**, not list offset: filtering reorders and shrinks
/// the results under stable row identities, so an offset-based selection produces stale
/// highlights (duplicate/none) and erratic arrow navigation. An id is stable across every
/// recompute.
final class QuickSearchModel: ObservableObject {
    @Published var query: String = "" {
        didSet { recompute(resetSelection: true) }
    }
    @Published private(set) var results: [AuthEntry] = []
    @Published var selectedID: AuthEntry.ID?

    /// Bumped on every `code(at:)`-relevant tick so rows refresh their displayed code.
    @Published var tick: Date = Date()

    /// Whether rows reveal the live code (off by default — codes are masked and typed).
    @Published var showCodes: Bool = false

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
        guard let id = selectedID else { return nil }
        return results.first { $0.id == id }
    }

    /// Move the highlight to the previous/next result, clamped at the ends. Anchors to the
    /// top if nothing valid is selected.
    func moveSelection(down: Bool) {
        guard !results.isEmpty else { selectedID = nil; return }
        let current = selectedID.flatMap { id in results.firstIndex { $0.id == id } }
        let next = SearchFilter.nextIndex(count: results.count, current: current, down: down) ?? 0
        selectedID = results[next].id
    }

    /// Recomputes the filtered results. On a query change we re-anchor to the top match (so
    /// the best result is highlighted and Enter picks it); on a background refresh we keep
    /// the current selection if that entry is still present.
    private func recompute(resetSelection: Bool) {
        results = SearchFilter.filter(allEntries, query: query, recentIDs: recentIDs)
        let stillSelectable = selectedID.map { id in results.contains { $0.id == id } } ?? false
        if resetSelection || !stillSelectable {
            selectedID = results.first?.id
        }
    }
}
