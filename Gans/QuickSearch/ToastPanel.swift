import AppKit
import SwiftUI

/// A tiny transient HUD ("Code copied", permission hints, the first-run welcome) that
/// appears where Quick Search sits and fades out on its own. Fire-and-forget via
/// `ToastPanel.show(_:)`.
///
/// Without an action it's click-through and non-activating, so it never steals focus from
/// the app the user is returning to. With an action it shows a button and accepts clicks
/// (still without activating Gans).
@MainActor
enum ToastPanel {
    private static var current: NSPanel?
    private static var dismissWork: DispatchWorkItem?

    static func show(_ message: String,
                     duration: TimeInterval = 2.4,
                     actionTitle: String? = nil,
                     action: (() -> Void)? = nil) {
        dismissWork?.cancel()
        current?.orderOut(nil)
        current = nil

        let interactive = actionTitle != nil && action != nil
        let content = ToastContent(message: message, actionTitle: actionTitle, action: action)
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
        panel.ignoresMouseEvents = !interactive
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

        let work = DispatchWorkItem { Task { @MainActor in fadeOut(panel) } }
        dismissWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + duration, execute: work)
    }

    /// Dismisses the current toast now (used by an action button once tapped).
    static func dismiss() {
        dismissWork?.cancel()
        dismissWork = nil
        if let panel = current { fadeOut(panel) }
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

/// The toast's content: a message and an optional trailing action button.
private struct ToastContent: View {
    let message: String
    let actionTitle: String?
    let action: (() -> Void)?

    var body: some View {
        HStack(spacing: 12) {
            Text(message)
                .font(.system(size: 13, weight: .medium))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 340, alignment: .leading)
            if let actionTitle, let action {
                Button(actionTitle) {
                    action()
                    ToastPanel.dismiss()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}
