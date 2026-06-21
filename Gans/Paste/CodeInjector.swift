import AppKit
import Carbon.HIToolbox
import CoreGraphics
import ApplicationServices

/// Delivers a one-time code into whatever field had focus before Quick Search appeared.
///
/// The flow: Quick Search records the previously-frontmost app, then on commit we order
/// our panel out, re-activate that app, and synthesize key events into it. Posting events
/// to *another* app requires the Accessibility (TCC) permission — if it's not granted we
/// fall back to leaving the code on the clipboard and tell the caller.
enum CodeInjector {

    /// Whether the app may post synthetic events to other apps. Pass `prompt: true` to
    /// show the system Accessibility prompt the first time.
    static func hasAccessibilityPermission(prompt: Bool) -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        return AXIsProcessTrustedWithOptions([key: prompt] as CFDictionary)
    }

    enum Result {
        case delivered
        case copiedOnly // no Accessibility permission; code is on the clipboard
    }

    /// Reactivates `targetApp` and delivers `code` per `mode`. Returns whether it could
    /// inject keystrokes or only copy. Runs its keystroke work after a short delay so the
    /// target app is frontmost first. When the code lands on the clipboard,
    /// `clearClipboardAfter` (if set) schedules it to be wiped later.
    @discardableResult
    static func deliver(code: String, to targetApp: NSRunningApplication?, mode: DeliveryMode,
                        alsoCopy: Bool, clearClipboardAfter: TimeInterval? = nil,
                        completion: ((Result) -> Void)? = nil) -> Result {
        let canInject = hasAccessibilityPermission(prompt: false)

        if mode == .paste || alsoCopy || !canInject {
            copyToClipboard(code, clearAfter: clearClipboardAfter)
        }

        guard canInject else {
            completion?(.copiedOnly)
            return .copiedOnly
        }

        targetApp?.activate(options: [])

        // Give the target a beat to come frontmost before posting events.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            switch mode {
            case .type: typeString(code)
            case .paste: sendCommandV()
            }
            completion?(.delivered)
        }
        return .delivered
    }

    // MARK: Clipboard

    /// Copies `string` to the general pasteboard. If `clearAfter` is set, the code is
    /// wiped after that delay — but only if the pasteboard hasn't changed since (so we
    /// never clobber something the user copied afterwards).
    static func copyToClipboard(_ string: String, clearAfter: TimeInterval? = nil) {
        let pasteboard = NSPasteboard.general
        // Mark the code as concealed + transient so clipboard-history managers and Universal
        // Clipboard don't persist or sync the one-time code (https://nspasteboard.org).
        let concealed = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
        let transient = NSPasteboard.PasteboardType("org.nspasteboard.TransientType")
        pasteboard.declareTypes([.string, concealed, transient], owner: nil)
        pasteboard.setString(string, forType: .string)
        pasteboard.setString("", forType: concealed)
        pasteboard.setString("", forType: transient)

        guard let delay = clearAfter, delay > 0 else { return }
        let changeCountAtCopy = pasteboard.changeCount
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            let pasteboard = NSPasteboard.general
            // Only clear if our code is still the most recent thing on the clipboard.
            guard pasteboard.changeCount == changeCountAtCopy else { return }
            pasteboard.clearContents()
        }
    }

    // MARK: Synthetic events

    /// Types `string` by posting one key event carrying the full Unicode payload — clean
    /// (no clipboard change) and layout-independent.
    private static func typeString(_ string: String) {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
        let utf16 = Array(string.utf16)

        guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else { return }
        utf16.withUnsafeBufferPointer { buffer in
            keyDown.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
            keyUp.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
        }
        keyDown.post(tap: .cghidEventTap)
        keyUp.post(tap: .cghidEventTap)
    }

    /// Synthesizes ⌘V to paste the current clipboard contents.
    private static func sendCommandV() {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
        let vKey = CGKeyCode(kVK_ANSI_V)

        let vDown = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: true)
        vDown?.flags = .maskCommand
        let vUp = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: false)
        vUp?.flags = .maskCommand

        vDown?.post(tap: .cghidEventTap)
        vUp?.post(tap: .cghidEventTap)
    }
}
