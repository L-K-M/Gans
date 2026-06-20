import SwiftUI
import AppKit

/// The Spotlight-style content: a large search field over a results list. Text entry is
/// handled here; navigation/commit keys are handled by the hosting panel (so they work
/// regardless of SwiftUI focus quirks on macOS 13).
struct QuickSearchView: View {
    @ObservedObject var model: QuickSearchModel
    @FocusState private var searchFocused: Bool

    /// Invoked when a row is clicked or Return is pressed.
    var onCommit: (AuthEntry) -> Void

    var body: some View {
        VStack(spacing: 0) {
            searchField
            if !model.results.isEmpty {
                Divider()
                resultsList
            } else {
                emptyState
            }
        }
        .frame(width: 560)
        .background(VisualEffectBackground())
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .onAppear { searchFocused = true }
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("Search Ente Auth…", text: $model.query)
                .textFieldStyle(.plain)
                .font(.system(size: 22, weight: .regular))
                .focused($searchFocused)
        }
        .padding(.horizontal, 18)
        .frame(height: 60)
    }

    private var resultsList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(model.results) { entry in
                        QuickSearchRow(entry: entry, tick: model.tick,
                                       isSelected: entry.id == model.selectedID,
                                       showCode: model.showCodes)
                            .id(entry.id)
                            .contentShape(Rectangle())
                            .onTapGesture { onCommit(entry) }
                    }
                }
                .padding(8)
            }
            .frame(maxHeight: 320)
            .onChange(of: model.selectedID) { newValue in
                guard let id = newValue else { return }
                withAnimation(.easeOut(duration: 0.1)) { proxy.scrollTo(id, anchor: .center) }
            }
        }
    }

    private var emptyState: some View {
        VStack {
            if model.query.isEmpty {
                Text("Type to search your codes")
            } else {
                Text("No matches for “\(model.query)”")
            }
        }
        .font(.system(size: 14))
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
    }
}

/// A single result row: name on the left, live code + countdown on the right.
private struct QuickSearchRow: View {
    let entry: AuthEntry
    let tick: Date
    let isSelected: Bool
    /// When false, the code is masked (Quick Search just types it on commit).
    let showCode: Bool

    /// A dot mask sized to the code, so a hidden row still reads as "a code lives here".
    private var maskedCode: String {
        String(repeating: "•", count: max(entry.digits, 4))
    }

    /// Foreground that stays legible on the accent-filled selection — black or white,
    /// whichever genuinely contrasts with the accent (the accent can be light, e.g. lime).
    private var primaryForeground: Color {
        isSelected ? Color.accentColor.contrastingForeground : .primary
    }

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.issuer.isEmpty ? entry.displayName : entry.issuer)
                    .font(.system(size: 15, weight: .medium))
                    .lineLimit(1)
                if !entry.account.isEmpty && !entry.issuer.isEmpty {
                    Text(entry.account)
                        .font(.system(size: 12))
                        .foregroundStyle(isSelected ? primaryForeground.opacity(0.85) : Color.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            Text(showCode ? entry.formattedCode(at: tick) : maskedCode)
                .font(.system(size: 18, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(primaryForeground)
            if entry.isTimeBased {
                CountdownRing(fraction: entry.fractionRemaining(at: tick),
                              seconds: entry.secondsRemaining(at: tick),
                              tint: isSelected ? primaryForeground : .accentColor)
            }
        }
        .padding(.horizontal, 12)
        .frame(height: 44)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isSelected ? Color.accentColor : Color.clear)
        )
        .foregroundStyle(primaryForeground)
    }
}

/// A small circular countdown that depletes over the code's period and warms to amber/red
/// as expiry nears, so you can tell at a glance whether to wait for the next code.
private struct CountdownRing: View {
    /// 1 = full period remaining, 0 = expiring now.
    let fraction: Double
    let seconds: Int
    /// Base ring color, already chosen to contrast with whatever it's drawn on.
    let tint: Color

    var body: some View {
        ZStack {
            Circle()
                .stroke(tint.opacity(0.25), lineWidth: 2)
            Circle()
                .trim(from: 0, to: max(0.001, min(fraction, 1)))
                .stroke(ringColor, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: 16, height: 16)
        .animation(.linear(duration: 0.25), value: fraction)
        .accessibilityLabel("\(seconds) seconds remaining")
    }

    private var ringColor: Color {
        if seconds <= 5 { return .red }
        if seconds <= 10 { return .orange }
        return tint
    }
}

private extension Color {
    /// Black or white — whichever has the higher WCAG contrast ratio against this color
    /// used as a fill. (A luminance threshold alone gets mid-tone accents wrong; comparing
    /// the actual ratios picks correctly, e.g. black on a lime/green accent.)
    var contrastingForeground: Color {
        let resolved = NSColor(self).usingColorSpace(.sRGB)
            ?? NSColor.controlAccentColor.usingColorSpace(.sRGB)
        guard let c = resolved else { return .white }
        func lin(_ v: CGFloat) -> CGFloat { v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
        let luminance = 0.2126 * lin(c.redComponent) + 0.7152 * lin(c.greenComponent) + 0.0722 * lin(c.blueComponent)
        let contrastWithWhite = 1.05 / (luminance + 0.05)
        let contrastWithBlack = (luminance + 0.05) / 0.05
        return contrastWithWhite >= contrastWithBlack ? .white : .black
    }
}

/// `NSVisualEffectView` bridge for the frosted panel background.
private struct VisualEffectBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}
