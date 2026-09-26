import Foundation
import Testing

@testable import ScannyBoy

@Suite("CaptureCues")
struct CaptureCuesTests {
    @Test("the capture beep is higher than the countdown tick")
    func captureIsHigherThanTick() {
        #expect(CaptureCues.captureFrequency > CaptureCues.tickFrequency)
        #expect(CaptureCues.captureFrequency < CaptureCues.tickFrequency * 1.5)
    }

    @Test("countdown ticks are 3, 2, 1")
    func countdownSeconds() {
        #expect(CaptureCues.countdownSeconds == [3, 2, 1])
    }

    @Test("a tone is a well-formed mono 16-bit WAV that fades in and out")
    func toneWAVIsWellFormed() throws {
        let wav = CaptureCues.toneWAV(frequency: CaptureCues.tickFrequency)
        #expect(String(decoding: wav.prefix(4), as: UTF8.self) == "RIFF")
        #expect(String(decoding: wav[8..<16], as: UTF8.self) == "WAVEfmt ")
        #expect(String(decoding: wav[36..<40], as: UTF8.self) == "data")
        let dataLength = wav[40..<44].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }
        #expect(Int(dataLength) == wav.count - 44)
        let riffLength = wav[4..<8].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }
        #expect(Int(riffLength) == wav.count - 8)

        let samples = wav[44...].withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        #expect(abs(Int(samples.first ?? 1)) <= 1)
        #expect(abs(Int(samples.last ?? 1)) <= 1)
        #expect((samples.map { abs(Int($0)) }.max() ?? 0) > 1000)
        // Gentle: well under full scale.
        #expect((samples.map { abs(Int($0)) }.max() ?? 0) < Int(Int16.max) / 2)
    }
}
