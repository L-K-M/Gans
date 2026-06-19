import SwiftUI
import AppKit

/// The login window content. Walks through credentials → optional email code / 2FA →
/// done. Email + password are always collected because the password unwraps the key
/// hierarchy regardless of how the session is authenticated.
struct LoginView: View {
    @ObservedObject var model: LoginViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header
            content
            if let error = model.errorMessage {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(24)
        .frame(width: 380)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "key.fill")
                .font(.system(size: 22))
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 1) {
                Text("Sign in to Ente").font(.headline)
                Text("Your codes stay end-to-end encrypted.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.stage {
        case .credentials: credentials
        case .emailCode: codeEntry(title: "Enter the code we emailed you", help: "Check \(model.email).")
        case .twoFactor: codeEntry(title: "Two-factor code", help: "Enter the 6-digit code from your authenticator.")
        case .passkeyRequired(let url): passkey(url: url)
        }
    }

    private var credentials: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("Email", text: $model.email)
                .textFieldStyle(.roundedBorder)
                .disableAutocorrection(true)
            SecureField("Password", text: $model.password)
                .textFieldStyle(.roundedBorder)
                .onSubmit { if model.canSubmitCredentials { model.signInWithPassword() } }

            HStack {
                Button(action: { model.signInWithPassword() }) {
                    if model.isBusy { ProgressView().controlSize(.small) } else { Text("Sign In") }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!model.canSubmitCredentials)

                Button("Email me a code") { model.sendEmailCode() }
                    .disabled(!model.canSubmitCredentials)
            }
        }
    }

    private func codeEntry(title: String, help: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title).font(.subheadline.weight(.medium))
            Text(help).font(.caption).foregroundStyle(.secondary)
            TextField("Code", text: $model.code)
                .textFieldStyle(.roundedBorder)
                .onSubmit { model.submitCode() }
            HStack {
                Button(action: { model.submitCode() }) {
                    if model.isBusy { ProgressView().controlSize(.small) } else { Text("Continue") }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(model.code.isEmpty || model.isBusy)
                Button("Back") { model.restart() }
            }
        }
    }

    private func passkey(url: String?) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("This account uses a passkey")
                .font(.subheadline.weight(.medium))
            Text("Passkey sign-in needs a browser. Open Ente to authenticate, then sign in here with your password or an email code.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                if let url, let link = URL(string: url) {
                    Button("Open Ente") { NSWorkspace.shared.open(link) }
                }
                Button("Back") { model.restart() }
            }
        }
    }
}
