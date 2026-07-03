import AppKit
import SwiftUI

/// A tiny transient HUD ("Code copied", permission hints) that appears where Quick
/// Search just was and fades out on its own. Fire-and-forget via `ToastPanel.show(_:)`.
/// Click-through and non-activating, so it never steals focus from the app the user is
/// returning to.
@MainActor
enum ToastPanel {
    private static var current: NSPanel?
    private static var dismissWork: DispatchWorkItem?

    static func show(_ message: String, duration: TimeInterval = 2.4) {
        current?.orderOut(nil)
        dismissWork?.cancel()

        let content = Text(message)
            .font(.system(size: 13, weight: .medium))
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .fixedSize()

        let hosting = NSHostingView(rootView: content)
        hosting.layoutSubtreeIfNeeded()
        let size = hosting.fittingSize

        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.contentView = hosting

        // Just above where the Quick Search panel sits, on the screen the user is on.
        let screen = screenWithMouse() ?? NSScreen.main
        if let frame = screen?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: frame.midX - size.width / 2,
                                         y: frame.midY + frame.height * 0.25 + 12))
        }

        panel.alphaValue = 0
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.15
            panel.animator().alphaValue = 1
        }
        current = panel

        let work = DispatchWorkItem {
            Task { @MainActor in fadeOut(panel) }
        }
        dismissWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + duration, execute: work)
    }

    private static func fadeOut(_ panel: NSPanel) {
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.35
            panel.animator().alphaValue = 0
        }, completionHandler: {
            panel.orderOut(nil)
            if current === panel { current = nil }
        })
    }

    private static func screenWithMouse() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
    }
}
