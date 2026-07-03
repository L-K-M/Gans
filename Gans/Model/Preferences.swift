import Foundation
import Combine

/// How a selected code is delivered to the focused field from Quick Search.
enum DeliveryMode: String, CaseIterable, Identifiable {
    /// Synthesize the code's characters directly (no clipboard change). Default.
    case type
    /// Copy to the clipboard, then synthesize ⌘V.
    case paste

    var id: String { rawValue }
    var label: String { self == .type ? "Type the code" : "Paste the code (⌘V)" }
}

/// App-wide settings, persisted to `UserDefaults` and observable by SwiftUI.
final class Preferences: ObservableObject {
    static let shared = Preferences()

    private let defaults: UserDefaults

    private enum Key {
        static let hotkey = "quickSearchHotkey"
        static let deliveryMode = "deliveryMode"
        static let alsoCopyWhenTyping = "alsoCopyWhenTyping"
        static let clearClipboardEnabled = "clearClipboardEnabled"
        static let clearClipboardSeconds = "clearClipboardSeconds"
        static let recentlyUsedIDs = "recentlyUsedIDs"
        static let usageCounts = "usageCounts"
        static let lastUsedAt = "lastUsedAt"
        static let requireUnlock = "requireUnlock"
        static let showCodesInQuickSearch = "showCodesInQuickSearch"
        static let honkOnCopy = "honkOnCopy"
        static let hasCompletedOnboarding = "hasCompletedOnboarding"
    }

    /// How many recently-used entry ids to remember (for Quick Search ordering).
    private static let recentLimit = 50

    @Published var hotkey: HotkeySpec {
        didSet { persist(hotkey, forKey: Key.hotkey) }
    }

    /// How Quick Search delivers a code to the focused app.
    @Published var deliveryMode: DeliveryMode {
        didSet { defaults.set(deliveryMode.rawValue, forKey: Key.deliveryMode) }
    }

    /// When typing the code, also place it on the clipboard as a convenience.
    @Published var alsoCopyWhenTyping: Bool {
        didSet { defaults.set(alsoCopyWhenTyping, forKey: Key.alsoCopyWhenTyping) }
    }

    /// Clear a copied code from the clipboard after a delay (only if it's still there).
    @Published var clearClipboardEnabled: Bool {
        didSet { defaults.set(clearClipboardEnabled, forKey: Key.clearClipboardEnabled) }
    }

    /// How long to wait before clearing a copied code (seconds).
    @Published var clearClipboardSeconds: Int {
        didSet { defaults.set(clearClipboardSeconds, forKey: Key.clearClipboardSeconds) }
    }

    /// Require Touch ID / device password to unlock Gans on launch (and via Lock Now).
    @Published var requireUnlock: Bool {
        didSet { defaults.set(requireUnlock, forKey: Key.requireUnlock) }
    }

    /// Reveal the live code in each Quick Search row. Off by default — Quick Search just
    /// types the selected code without ever displaying it.
    @Published var showCodesInQuickSearch: Bool {
        didSet { defaults.set(showCodesInQuickSearch, forKey: Key.showCodesInQuickSearch) }
    }

    /// 🪿 Play a little honk (and blink the menu-bar key into a goose) when a code is
    /// copied or typed. Off by default, obviously.
    @Published var honkOnCopy: Bool {
        didSet { defaults.set(honkOnCopy, forKey: Key.honkOnCopy) }
    }

    /// Set once the first-run welcome has been shown, so it never repeats.
    @Published var hasCompletedOnboarding: Bool {
        didSet { defaults.set(hasCompletedOnboarding, forKey: Key.hasCompletedOnboarding) }
    }

    /// Most-recently-used entry ids, most recent first. Drives Quick Search ordering.
    @Published private(set) var recentlyUsedIDs: [String]

    /// Per-entry lifetime use counts, for frecency ordering + the Settings "most used".
    @Published private(set) var usageCounts: [String: Int]
    /// Per-entry last-used timestamps (epoch seconds), for the recency half of frecency.
    private var lastUsedAt: [String: Double]

    /// The clipboard-clear delay, or `nil` when the feature is off — what callers pass to
    /// `CodeInjector`.
    var clipboardClearDelay: TimeInterval? {
        clearClipboardEnabled ? TimeInterval(clearClipboardSeconds) : nil
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.hotkey = Self.decode(HotkeySpec.self, from: defaults, key: Key.hotkey) ?? .default
        self.deliveryMode = DeliveryMode(rawValue: defaults.string(forKey: Key.deliveryMode) ?? "") ?? .type
        self.alsoCopyWhenTyping = defaults.object(forKey: Key.alsoCopyWhenTyping) as? Bool ?? true
        self.clearClipboardEnabled = defaults.object(forKey: Key.clearClipboardEnabled) as? Bool ?? true
        self.clearClipboardSeconds = (defaults.object(forKey: Key.clearClipboardSeconds) as? Int) ?? 30
        self.requireUnlock = defaults.object(forKey: Key.requireUnlock) as? Bool ?? false
        self.showCodesInQuickSearch = defaults.object(forKey: Key.showCodesInQuickSearch) as? Bool ?? false
        self.honkOnCopy = defaults.object(forKey: Key.honkOnCopy) as? Bool ?? false
        self.hasCompletedOnboarding = defaults.object(forKey: Key.hasCompletedOnboarding) as? Bool ?? false
        self.recentlyUsedIDs = defaults.stringArray(forKey: Key.recentlyUsedIDs) ?? []
        self.usageCounts = (defaults.dictionary(forKey: Key.usageCounts) as? [String: Int]) ?? [:]
        self.lastUsedAt = (defaults.dictionary(forKey: Key.lastUsedAt) as? [String: Double]) ?? [:]
    }

    // MARK: Recently used

    /// Records that `id` was just used: moves it to the front of the recency list, bumps
    /// its lifetime count, and stamps the time — feeding both recency and frecency.
    func recordUsage(_ id: String) {
        var ids = recentlyUsedIDs
        ids.removeAll { $0 == id }
        ids.insert(id, at: 0)
        if ids.count > Self.recentLimit { ids = Array(ids.prefix(Self.recentLimit)) }
        recentlyUsedIDs = ids
        defaults.set(ids, forKey: Key.recentlyUsedIDs)

        var counts = usageCounts
        counts[id, default: 0] += 1
        usageCounts = counts
        defaults.set(counts, forKey: Key.usageCounts)

        var stamps = lastUsedAt
        stamps[id] = Date().timeIntervalSince1970
        lastUsedAt = stamps
        defaults.set(stamps, forKey: Key.lastUsedAt)
    }

    /// Entry ids ranked by **frecency** — frequency tempered by recency, so a code you
    /// use often *and* lately floats highest. Drives Quick Search's ordering and tie-
    /// breaks. Entries never used aren't listed (they fall back to name order downstream).
    var frecencyRankedIDs: [String] {
        let now = Date().timeIntervalSince1970
        func score(_ id: String) -> Double {
            let count = Double(usageCounts[id] ?? 0)
            let ageDays = max(0, (now - (lastUsedAt[id] ?? 0)) / 86_400)
            return count / (1.0 + ageDays)   // frequency, decayed by age
        }
        return usageCounts.keys.sorted { lhs, rhs in
            let sl = score(lhs), sr = score(rhs)
            if sl != sr { return sl > sr }
            return (lastUsedAt[lhs] ?? 0) > (lastUsedAt[rhs] ?? 0)
        }
    }

    /// The most-used entry ids with their counts, highest first — for a Settings summary.
    func mostUsed(limit: Int = 5) -> [(id: String, count: Int)] {
        usageCounts
            .sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
            .prefix(limit)
            .map { (id: $0.key, count: $0.value) }
    }

    // MARK: Codable helpers

    private func persist<T: Encodable>(_ value: T, forKey key: String) {
        if let data = try? JSONEncoder().encode(value) {
            defaults.set(data, forKey: key)
        }
    }

    private static func decode<T: Decodable>(_ type: T.Type, from defaults: UserDefaults, key: String) -> T? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }
}
