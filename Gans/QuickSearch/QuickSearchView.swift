import SwiftUI
import AppKit

/// Fixed layout metrics shared by the SwiftUI content and the panel controller, so the
/// panel height is computed deterministically (no `fittingSize` round-trips) and the
/// window can be anchored by its top edge while the list grows and shrinks.
enum QuickSearchMetrics {
    static let width: CGFloat = 560
    static let searchFieldHeight: CGFloat = 60
    static let dividerHeight: CGFloat = 1
    static let rowHeight: CGFloat = 44
    static let rowSpacing: CGFloat = 2
    static let listPadding: CGFloat = 8
    static let maxListHeight: CGFloat = 320
    static let emptyStateHeight: CGFloat = 64
    static let footerHeight: CGFloat = 26

    /// The results list hugs its content up to `maxListHeight` — previously the
    /// ScrollView greedily filled the maximum, leaving dead space under short lists.
    static func listHeight(forRows rows: Int) -> CGFloat {
        guard rows > 0 else { return 0 }
        let content = CGFloat(rows) * rowHeight + CGFloat(rows - 1) * rowSpacing + listPadding * 2
        return min(content, maxListHeight)
    }

    static func panelHeight(forRows rows: Int) -> CGFloat {
        guard rows > 0 else { return searchFieldHeight + dividerHeight + emptyStateHeight }
        return searchFieldHeight + dividerHeight + listHeight(forRows: rows) + footerHeight
    }
}

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
            Divider()
            if model.results.isEmpty {
                emptyState
            } else {
                resultsList
                footer
            }
        }
        .frame(width: QuickSearchMetrics.width)
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
        .frame(height: QuickSearchMetrics.searchFieldHeight)
    }

    private var resultsList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: QuickSearchMetrics.rowSpacing) {
                    ForEach(Array(model.results.enumerated()), id: \.element.id) { index, entry in
                        QuickSearchRow(entry: entry, tick: model.tick,
                                       isSelected: entry.id == model.selectedID,
                                       showCode: model.showCodes,
                                       shortcutHint: model.showIndices && index < 9 ? "⌘\(index + 1)" : nil)
                            .id(entry.id)
                            .contentShape(Rectangle())
                            .onTapGesture { onCommit(entry) }
                    }
                }
                .padding(QuickSearchMetrics.listPadding)
            }
            .frame(height: QuickSearchMetrics.listHeight(forRows: model.results.count))
            .onChange(of: model.selectedID) { newValue in
                guard let id = newValue else { return }
                withAnimation(.easeOut(duration: 0.1)) { proxy.scrollTo(id, anchor: .center) }
            }
        }
    }

    private var emptyState: some View {
        Group {
            if !model.query.isEmpty {
                Text("No matches for “\(model.query)”")
            } else if model.hasEntries {
                Text("Type to search your codes")
            } else {
                Text("No codes yet — add them in the Ente Auth app")
            }
        }
        .font(.system(size: 14))
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
        .frame(height: QuickSearchMetrics.emptyStateHeight)
    }

    /// A quiet key-hint bar, so ⌥↩ and ⌘1–9 are discoverable without documentation.
    private var footer: some View {
        HStack(spacing: 14) {
            keyHint("↩", "Insert")
            keyHint("⌥↩", "Copy")
            keyHint("⌘1–9", "Quick pick")
            Spacer()
            keyHint("esc", "Dismiss")
        }
        .padding(.horizontal, 14)
        .frame(height: QuickSearchMetrics.footerHeight)
    }

    private func keyHint(_ key: String, _ label: String) -> some View {
        HStack(spacing: 4) {
            Text(key)
                .font(.system(size: 10, weight: .semibold, design: .rounded))
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: 3).fill(Color.primary.opacity(0.08)))
            Text(label)
                .font(.system(size: 10))
        }
        .foregroundStyle(.secondary)
    }
}

/// A single result row: name on the left, live code + countdown on the right.
private struct QuickSearchRow: View {
    let entry: AuthEntry
    let tick: Date
    let isSelected: Bool
    /// When false, the code is masked (Quick Search just types it on commit).
    let showCode: Bool
    /// "⌘3" while the command key is held; nil otherwise.
    let shortcutHint: String?

    @State private var isHovered = false

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
            if let shortcutHint {
                Text(shortcutHint)
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(isSelected ? primaryForeground.opacity(0.8) : Color.secondary)
                    .frame(width: 30, alignment: .leading)
            }
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
                CountdownRing(fraction: preciseFraction,
                              seconds: entry.secondsRemaining(at: tick),
                              tint: isSelected ? primaryForeground : .accentColor)
            }
        }
        .padding(.horizontal, 12)
        .frame(height: QuickSearchMetrics.rowHeight)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isSelected ? Color.accentColor
                                 : (isHovered ? Color.primary.opacity(0.06) : Color.clear))
        )
        .foregroundStyle(primaryForeground)
        .onHover { isHovered = $0 }
        .help(entry.displayName)
        .accessibilityElement(children: .combine)
    }

    /// Sub-second fraction of the period remaining. `secondsRemaining` is quantized to
    /// whole seconds; pairing this precise value with a 1s linear animation makes the
    /// ring sweep continuously instead of stepping once per tick.
    private var preciseFraction: Double {
        guard entry.period > 0 else { return 1 }
        let period = Double(entry.period)
        let elapsed = tick.timeIntervalSince1970.truncatingRemainder(dividingBy: period)
        return (period - elapsed) / period
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
        // The tick arrives once per second; a 1s linear animation bridges the gaps so
        // the ring sweeps smoothly (and refills with a quick forward sweep on rollover).
        .animation(.linear(duration: 1.0), value: fraction)
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
