import SwiftUI
import Combine

/// View model for the Quick Search panel: holds the query, the filtered results, and the
/// current selection. The view binds to it; the panel controller feeds it the live entry
/// list and handles commit.
final class QuickSearchModel: ObservableObject {
    @Published var query: String = "" {
        didSet { recomputeResults() }
    }
    @Published private(set) var results: [AuthEntry] = []
    @Published var selectedIndex: Int = 0

    /// Bumped on every `code(at:)`-relevant tick so rows refresh their displayed code.
    @Published var tick: Date = Date()

    private var allEntries: [AuthEntry] = []

    func setEntries(_ entries: [AuthEntry]) {
        allEntries = entries
        recomputeResults()
    }

    func reset() {
        query = ""
        selectedIndex = 0
        recomputeResults()
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

    private func recomputeResults() {
        results = SearchFilter.filter(allEntries, query: query)
        if !results.indices.contains(selectedIndex) { selectedIndex = 0 }
    }
}
