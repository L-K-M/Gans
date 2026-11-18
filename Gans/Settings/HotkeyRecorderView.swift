import SwiftUI
import KeycapKit

/// Adapts the persisted `HotkeySpec` binding to Keycap's shared `ShortcutRecorder`. Gans
/// has no "clear shortcut" action, so a bare ⌫/⌦ is ignored rather than clearing.
struct HotkeyRecorderView: View {
    @Binding var spec: HotkeySpec

    var body: some View {
        ShortcutRecorder(combo: Binding(
            get: { spec.combo },
            set: { newCombo in
                guard let newCombo else { return }
                spec = HotkeySpec(combo: newCombo)
            }
        ), deleteAction: .ignore)
    }
}
