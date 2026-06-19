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
                    ForEach(Array(model.results.enumerated()), id: \.element.id) { index, entry in
                        QuickSearchRow(entry: entry, tick: model.tick, isSelected: index == model.selectedIndex)
                            .id(index)
                            .contentShape(Rectangle())
                            .onTapGesture { onCommit(entry) }
                    }
                }
                .padding(8)
            }
            .frame(maxHeight: 320)
            .onChange(of: model.selectedIndex) { newValue in
                withAnimation(.easeOut(duration: 0.1)) { proxy.scrollTo(newValue, anchor: .center) }
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

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.issuer.isEmpty ? entry.displayName : entry.issuer)
                    .font(.system(size: 15, weight: .medium))
                    .lineLimit(1)
                if !entry.account.isEmpty && !entry.issuer.isEmpty {
                    Text(entry.account)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            Text(entry.formattedCode(at: tick))
                .font(.system(size: 18, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(isSelected ? Color.white : Color.primary)
        }
        .padding(.horizontal, 12)
        .frame(height: 44)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isSelected ? Color.accentColor : Color.clear)
        )
        .foregroundStyle(isSelected ? Color.white : Color.primary)
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
