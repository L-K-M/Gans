import AppKit

/// Entry point. We drive `NSApplication` directly (rather than a SwiftUI `App` scene) so
/// Gans is a proper menu-bar agent: `.accessory` activation policy, no Dock icon. SwiftUI
/// is used only for view content hosted inside AppKit windows/panels.
@main
enum GansMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
