import Foundation
import Carbon.HIToolbox
import KeycapKit

/// A global hotkey: a virtual key code plus Carbon modifier flags. Codable so it can live
/// in `UserDefaults`. This is Gans's persisted shape; Keycap's `KeyCombo` is the model the
/// registration and recorder use.
struct HotkeySpec: Codable, Equatable {
    /// Virtual key code (e.g. `kVK_Space`).
    var keyCode: UInt32
    /// Carbon modifier mask (`controlKey | optionKey | cmdKey | shiftKey`).
    var modifiers: UInt32

    /// Default trigger: ⌃⌥Space — unlikely to collide with system shortcuts.
    static let `default` = HotkeySpec(keyCode: UInt32(kVK_Space),
                                      modifiers: UInt32(controlKey | optionKey))

    init(keyCode: UInt32, modifiers: UInt32) {
        self.keyCode = keyCode
        self.modifiers = modifiers
    }

    /// Builds the persisted spec from Keycap's combo model.
    init(combo: KeyCombo) {
        self.init(keyCode: combo.keyCode, modifiers: combo.carbonModifiers)
    }

    /// The Keycap model for this persisted combo.
    var combo: KeyCombo {
        KeyCombo(keyCode: keyCode, modifiers: HotkeyModifiers(carbonFlags: modifiers))
    }

    /// A human-readable form like "⌃⌥Space" for Settings.
    var displayString: String { combo.displayString }
}
