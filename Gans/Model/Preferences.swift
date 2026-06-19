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
        static let copyOnMenuSelect = "copyOnMenuSelect"
        static let alsoCopyWhenTyping = "alsoCopyWhenTyping"
    }

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

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.hotkey = Self.decode(HotkeySpec.self, from: defaults, key: Key.hotkey) ?? .default
        self.deliveryMode = DeliveryMode(rawValue: defaults.string(forKey: Key.deliveryMode) ?? "") ?? .type
        self.alsoCopyWhenTyping = defaults.object(forKey: Key.alsoCopyWhenTyping) as? Bool ?? true
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
