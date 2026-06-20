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
        static let requireUnlock = "requireUnlock"
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

    /// Most-recently-used entry ids, most recent first. Drives Quick Search ordering.
    @Published private(set) var recentlyUsedIDs: [String]

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
        self.recentlyUsedIDs = defaults.stringArray(forKey: Key.recentlyUsedIDs) ?? []
    }

    // MARK: Recently used

    /// Records that `id` was just used: moves it to the front, de-duplicated and capped.
    func recordUsage(_ id: String) {
        var ids = recentlyUsedIDs
        ids.removeAll { $0 == id }
        ids.insert(id, at: 0)
        if ids.count > Self.recentLimit { ids = Array(ids.prefix(Self.recentLimit)) }
        recentlyUsedIDs = ids
        defaults.set(ids, forKey: Key.recentlyUsedIDs)
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
