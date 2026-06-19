import SwiftUI
import AppKit

/// The Settings window content: account, Quick Search behavior, permissions, startup, and
/// updates.
struct SettingsView: View {
    @ObservedObject var preferences: Preferences
    @ObservedObject var vault: EnteVault
    @ObservedObject var updateChecker: UpdateChecker

    var onSignIn: () -> Void
    var onSignOut: () -> Void
    var onHotkeyChanged: () -> Void

    @State private var launchAtLogin = LaunchAtLogin.isEnabled
    @State private var hasAccessibility = CodeInjector.hasAccessibilityPermission(prompt: false)

    var body: some View {
        Form {
            accountSection
            quickSearchSection
            permissionsSection
            startupSection
            updatesSection
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .frame(minHeight: 520)
    }

    private var accountSection: some View {
        Section("Account") {
            if vault.isSignedIn {
                LabeledContent("Signed in", value: vault.accountEmail ?? "Ente")
                if let lastSync = vault.lastSync {
                    LabeledContent("Last sync", value: lastSync.formatted(date: .abbreviated, time: .shortened))
                }
                LabeledContent("Entries", value: "\(vault.entries.count)")
                Button("Sign Out", role: .destructive, action: onSignOut)
            } else {
                Button("Sign in to Ente…", action: onSignIn)
            }
        }
    }

    private var quickSearchSection: some View {
        Section("Quick Search") {
            HStack {
                Text("Hotkey")
                Spacer()
                HotkeyRecorderView(spec: Binding(
                    get: { preferences.hotkey },
                    set: { preferences.hotkey = $0; onHotkeyChanged() }
                ))
                .frame(width: 160, height: 24)
            }
            Picker("On select", selection: $preferences.deliveryMode) {
                ForEach(DeliveryMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            if preferences.deliveryMode == .type {
                Toggle("Also copy to clipboard", isOn: $preferences.alsoCopyWhenTyping)
            }
        }
    }

    private var permissionsSection: some View {
        Section("Permissions") {
            HStack {
                Label("Accessibility", systemImage: hasAccessibility ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(hasAccessibility ? .green : .orange)
                Spacer()
                if !hasAccessibility {
                    Button("Grant…") {
                        _ = CodeInjector.hasAccessibilityPermission(prompt: true)
                        openAccessibilitySettings()
                    }
                }
            }
            Text("Required only to type or paste a code into another app. Copying from the menu works without it.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var startupSection: some View {
        Section("Startup") {
            Toggle("Launch Gans at login", isOn: $launchAtLogin)
                .onChange(of: launchAtLogin) { LaunchAtLogin.set($0) }
        }
    }

    private var updatesSection: some View {
        Section("Updates") {
            Toggle("Check for updates automatically", isOn: $updateChecker.automaticChecksEnabled)
            if let last = updateChecker.lastCheckDate {
                LabeledContent("Last checked", value: last.formatted(date: .abbreviated, time: .shortened))
            }
            Button("Check Now") { updateChecker.checkNow() }
                .disabled(updateChecker.isChecking)
        }
    }

    private func openAccessibilitySettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") else { return }
        NSWorkspace.shared.open(url)
    }
}
