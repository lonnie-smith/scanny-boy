import AppKit
import Foundation

/// The gentle countdown beeps before each release (docs/TETHER_PLAN.md §3.3).
///
/// Synthesised as short sine tones, so nothing is bundled and the two
/// pitches can be chosen exactly: the last beep, on the release itself, is a
/// little higher than the 3-2-1 ticks before it.
enum CaptureCues {
    /// Beeps at these seconds-before-release, once each, when the interval
    /// is long enough to contain them.
    static let countdownSeconds = [3, 2, 1]

    static let tickFrequency = 660.0
    /// About a minor third above the tick.
    static let captureFrequency = 784.0

    private static let sampleRate = 44_100
    private static let duration = 0.07
    private static let amplitude = 0.25

    private static let tickData = toneWAV(frequency: tickFrequency)
    private static let captureData = toneWAV(frequency: captureFrequency)

    @MainActor static func playTick() { play(tickData) }
    @MainActor static func playCapture() { play(captureData) }

    @MainActor
    private static func play(_ data: Data) {
        NSSound(data: data)?.play()
    }

    /// A mono 16-bit PCM WAV of a sine tone with a raised-cosine envelope,
    /// so it starts and ends without a click.
    static func toneWAV(frequency: Double) -> Data {
        let frameCount = Int(Double(sampleRate) * duration)
        var samples = Data(capacity: frameCount * 2)
        for index in 0..<frameCount {
            let position = Double(index) / Double(frameCount - 1)
            let envelope = 0.5 * (1 - cos(2 * .pi * position))
            let value = sin(2 * .pi * frequency * Double(index) / Double(sampleRate))
            let sample = Int16(value * envelope * amplitude * Double(Int16.max))
            withUnsafeBytes(of: sample.littleEndian) { samples.append(contentsOf: $0) }
        }

        func littleEndian<T: FixedWidthInteger>(_ value: T) -> Data {
            withUnsafeBytes(of: value.littleEndian) { Data($0) }
        }
        var wav = Data()
        wav.append(contentsOf: Array("RIFF".utf8))
        wav.append(littleEndian(UInt32(36 + samples.count)))
        wav.append(contentsOf: Array("WAVEfmt ".utf8))
        wav.append(littleEndian(UInt32(16)))
        wav.append(littleEndian(UInt16(1)))
        wav.append(littleEndian(UInt16(1)))
        wav.append(littleEndian(UInt32(sampleRate)))
        wav.append(littleEndian(UInt32(sampleRate * 2)))
        wav.append(littleEndian(UInt16(2)))
        wav.append(littleEndian(UInt16(16)))
        wav.append(contentsOf: Array("data".utf8))
        wav.append(littleEndian(UInt32(samples.count)))
        wav.append(samples)
        return wav
    }
}
