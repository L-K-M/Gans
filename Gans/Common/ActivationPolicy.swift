import AppKit

/// Helpers for the `.accessory` ↔ `.regular` dance a menu-bar agent does when it shows a
/// real window (so the window can take focus and appear in the Dock) and then hides it.
enum ActivationPolicy {
    /// Drops back to `.accessory` (no Dock icon) unless another ordinary window is still
    /// open. Call from a window's `windowWillClose`.
    static func revertToAccessoryIfNoOrdinaryWindows(excluding window: NSWindow?) {
        let stillOpen = NSApp.windows.contains { candidate in
            candidate != window
                && (candidate.isVisible || candidate.isMiniaturized)
                && candidate.canBecomeMain
        }
        if !stillOpen {
            NSApp.setActivationPolicy(.accessory)
        }
    }
}
