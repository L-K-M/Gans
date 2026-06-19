import Foundation
import Carbon.HIToolbox

/// A global hotkey: a virtual key code plus Carbon modifier flags. Codable so it can live
/// in `UserDefaults`.
struct HotkeySpec: Codable, Equatable {
    /// Virtual key code (e.g. `kVK_Space`).
    var keyCode: UInt32
    /// Carbon modifier mask (`controlKey | optionKey | cmdKey | shiftKey`).
    var modifiers: UInt32

    /// Default trigger: ⌃⌥Space — unlikely to collide with system shortcuts.
    static let `default` = HotkeySpec(keyCode: UInt32(kVK_Space),
                                      modifiers: UInt32(controlKey | optionKey))

    /// A human-readable form like "⌃⌥Space" for Settings.
    var displayString: String {
        var parts = ""
        if modifiers & UInt32(controlKey) != 0 { parts += "⌃" }
        if modifiers & UInt32(optionKey) != 0 { parts += "⌥" }
        if modifiers & UInt32(shiftKey) != 0 { parts += "⇧" }
        if modifiers & UInt32(cmdKey) != 0 { parts += "⌘" }
        return parts + KeyCodes.name(for: keyCode)
    }
}
