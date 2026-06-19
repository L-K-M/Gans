import SwiftUI
import AppKit
import Carbon.HIToolbox

/// A focusable control that records the next key-with-modifiers chord and reports it as a
/// `HotkeySpec`. Click it, then press the desired combination.
struct HotkeyRecorderView: NSViewRepresentable {
    @Binding var spec: HotkeySpec

    func makeNSView(context: Context) -> RecorderNSView {
        let view = RecorderNSView()
        view.onCapture = { newSpec in spec = newSpec }
        view.spec = spec
        return view
    }

    func updateNSView(_ nsView: RecorderNSView, context: Context) {
        nsView.spec = spec
    }

    /// The underlying AppKit view: shows the current chord, turns into "Press keys…" while
    /// recording, and only accepts a chord that has at least one modifier.
    final class RecorderNSView: NSView {
        var onCapture: ((HotkeySpec) -> Void)?
        var spec: HotkeySpec = .default { didSet { needsDisplay = true } }
        private var recording = false { didSet { needsDisplay = true } }

        override var acceptsFirstResponder: Bool { true }
        override var intrinsicContentSize: NSSize { NSSize(width: 160, height: 24) }

        override func mouseDown(with event: NSEvent) {
            recording = true
            window?.makeFirstResponder(self)
        }

        override func resignFirstResponder() -> Bool {
            recording = false
            return super.resignFirstResponder()
        }

        override func keyDown(with event: NSEvent) {
            guard recording else { super.keyDown(with: event); return }
            let modifiers = KeyCodes.carbonModifiers(from: event.modifierFlags)
            // Require at least one modifier so the hotkey doesn't swallow ordinary typing.
            guard modifiers != 0 else { NSSound.beep(); return }
            let newSpec = HotkeySpec(keyCode: UInt32(event.keyCode), modifiers: modifiers)
            spec = newSpec
            onCapture?(newSpec)
            recording = false
            window?.makeFirstResponder(nil)
        }

        override func draw(_ dirtyRect: NSRect) {
            let radius: CGFloat = 5
            let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: radius, yRadius: radius)
            (recording ? NSColor.controlAccentColor.withAlphaComponent(0.15) : NSColor.controlBackgroundColor).setFill()
            path.fill()
            (recording ? NSColor.controlAccentColor : NSColor.separatorColor).setStroke()
            path.stroke()

            let text = recording ? "Press keys…" : spec.displayString
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 13),
                .foregroundColor: NSColor.labelColor,
            ]
            let size = text.size(withAttributes: attributes)
            let point = NSPoint(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2)
            text.draw(at: point, withAttributes: attributes)
        }
    }
}
