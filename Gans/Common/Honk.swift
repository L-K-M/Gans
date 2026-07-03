import AppKit

/// 🪿 A synthesized goose honk, built once in memory and played on demand — no bundled
/// audio asset. A buzzy, nasal tone with a rise-and-fall pitch contour, deliberately
/// short and a little silly. Opt-in (see `Preferences.honkOnCopy`).
enum Honk {
    /// Lazily-built sound; nil only if the platform refuses the generated WAV.
    private static let sound: NSSound? = makeSound()

    /// Plays the honk (restarting it if one is already sounding). No-op if unavailable.
    static func play() {
        guard let sound else { return }
        sound.stop()
        sound.play()
    }

    private static func makeSound() -> NSSound? {
        let sampleRate = 22_050.0
        let duration = 0.34
        let count = Int(sampleRate * duration)
        var samples = [Int16]()
        samples.reserveCapacity(count)

        for i in 0..<count {
            let t = Double(i) / sampleRate
            let frac = t / duration
            // Nasal pitch contour: a quick rise then settle, ~300–460 Hz.
            let f0 = 300.0 + 160.0 * sin(.pi * min(frac * 1.2, 1.0))
            // A few harmonics → buzzy, goose-ish timbre.
            var s = sin(2 * .pi * f0 * t)
            s += 0.5 * sin(2 * .pi * 2 * f0 * t)
            s += 0.33 * sin(2 * .pi * 3 * f0 * t)
            s += 0.2 * sin(2 * .pi * 4 * f0 * t)
            // Envelope: fast attack, gentle release, slight two-syllable waver.
            let attack = min(frac / 0.05, 1.0)
            let release = frac > 0.7 ? max(0.0, 1.0 - (frac - 0.7) / 0.3) : 1.0
            let waver = 0.85 + 0.15 * cos(2 * .pi * 3 * frac)
            let amp = attack * release * waver * 0.33
            let value = max(-1.0, min(1.0, s / 2.0 * amp))
            samples.append(Int16(value * 32_767))
        }

        return NSSound(data: wav(samples: samples, sampleRate: Int(sampleRate)))
    }

    /// Wraps 16-bit mono PCM samples in a minimal RIFF/WAVE container `NSSound` can play.
    private static func wav(samples: [Int16], sampleRate: Int) -> Data {
        var data = Data()
        let dataSize = samples.count * 2
        func ascii(_ s: String) { data.append(contentsOf: s.utf8) }
        func u32(_ v: UInt32) { var x = v.littleEndian; withUnsafeBytes(of: &x) { data.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { var x = v.littleEndian; withUnsafeBytes(of: &x) { data.append(contentsOf: $0) } }

        ascii("RIFF"); u32(UInt32(36 + dataSize)); ascii("WAVE")
        ascii("fmt "); u32(16); u16(1); u16(1)                       // PCM, mono
        u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        ascii("data"); u32(UInt32(dataSize))
        for sample in samples { u16(UInt16(bitPattern: sample)) }
        return data
    }
}
