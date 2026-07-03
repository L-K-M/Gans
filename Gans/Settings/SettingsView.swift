import SwiftUI
import AppKit
import Combine

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
            mostUsedSection
            quickSearchSection
            securitySection
            permissionsSection
            startupSection
            updatesSection
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .frame(minHeight: 520)
        // The user may grant Accessibility in System Settings and switch back; re-check
        // when we reactivate so the status row reflects reality without a reopen.
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            hasAccessibility = CodeInjector.hasAccessibilityPermission(prompt: false)
            launchAtLogin = LaunchAtLogin.isEnabled
        }
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
            Toggle("Show codes in Quick Search", isOn: $preferences.showCodesInQuickSearch)
            if preferences.deliveryMode == .type {
                Toggle("Also copy to clipboard", isOn: $preferences.alsoCopyWhenTyping)
            }
            Toggle("Clear copied codes from the clipboard", isOn: $preferences.clearClipboardEnabled)
            if preferences.clearClipboardEnabled {
                Picker("Clear after", selection: $preferences.clearClipboardSeconds) {
                    Text("15 seconds").tag(15)
                    Text("30 seconds").tag(30)
                    Text("1 minute").tag(60)
                    Text("2 minutes").tag(120)
                }
            }
            Toggle("Honk on copy 🪿", isOn: $preferences.honkOnCopy)
        }
    }

    private struct UsageRow: Identifiable {
        let entry: AuthEntry
        let count: Int
        var id: String { entry.id }
    }

    /// Top entries by frecency-tracked use count — a small "you reach for these" summary.
    private var mostUsed: [UsageRow] {
        let byID = Dictionary(vault.entries.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return preferences.mostUsed(limit: 5).compactMap { item in
            byID[item.id].map { UsageRow(entry: $0, count: item.count) }
        }
    }

    @ViewBuilder
    private var mostUsedSection: some View {
        if !mostUsed.isEmpty {
            Section("Most used") {
                ForEach(mostUsed) { row in
                    LabeledContent(row.entry.displayName, value: "\(row.count)×")
                }
            }
        }
    }

    private var securitySection: some View {
        Section("Security") {
            Toggle("Require Touch ID or password to unlock", isOn: $preferences.requireUnlock)
            Text("When on, Gans locks on launch (and via “Lock Now”) and asks for Touch ID or your Mac password before showing or typing any codes. The Ente token and key stay in the device-only Keychain.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var permissionsSection: some View {
        Section("Permissions") {
            HStack {
                Label("Accessibility", systemImage: hasAccessibility ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(hasAccessibility ? .green : .orange)
                Spacer()
                if !hasAccessibility {
                    Button("Grant…") { CodeInjector.openAccessibilitySettings() }
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

}
