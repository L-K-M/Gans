import SwiftUI

/// A small colored avatar showing an issuer's initial(s), tinted by a hue derived
/// deterministically from the issuer name. Makes rows scannable at a glance without
/// bundling any icon assets — GitHub is always the same green-ish chip, AWS the same
/// orange-ish one, run after run.
struct IssuerChip: View {
    let name: String
    var size: CGFloat = 26

    /// Up to two initials: first letters of the first two words, else the first two
    /// characters, uppercased. Falls back to "•" for an empty/symbol-only name.
    private var initials: String {
        let words = name.split(whereSeparator: { " -_.".contains($0) })
        let letters: String
        if words.count >= 2, let a = words[0].first, let b = words[1].first {
            letters = String([a, b])
        } else {
            letters = String(name.prefix(2))
        }
        let trimmed = letters.trimmingCharacters(in: .whitespaces).uppercased()
        return trimmed.isEmpty ? "•" : trimmed
    }

    var body: some View {
        let hue = Self.hue(for: name)
        let base = Color(hue: hue, saturation: 0.55, brightness: 0.85)
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(base.gradient)
            .frame(width: size, height: size)
            .overlay(
                Text(initials)
                    .font(.system(size: size * 0.42, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
                    .shadow(color: .black.opacity(0.25), radius: 0.5, y: 0.5)
            )
            .accessibilityHidden(true)
    }

    /// A stable hue in 0...1 from the name. Uses a small FNV-style fold so the same
    /// issuer always lands on the same color across launches.
    static func hue(for name: String) -> Double {
        let folded = SearchFilter.fold(name)
        guard !folded.isEmpty else { return 0 }
        var hash: UInt64 = 1_469_598_103_934_665_603
        for byte in folded.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 1_099_511_628_211
        }
        return Double(hash % 360) / 360.0
    }
}
