import Foundation
import ServiceManagement

/// Thin wrapper over `SMAppService.mainApp` for the "launch at login" toggle (macOS 13+).
enum LaunchAtLogin {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func set(_ enabled: Bool) {
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
            } else {
                if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            }
        } catch {
            Log.app.error("Launch-at-login toggle failed: \(error.localizedDescription, privacy: .public)")
        }
    }
}
