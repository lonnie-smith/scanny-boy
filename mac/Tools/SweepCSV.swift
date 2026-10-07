// SweepCSV — derives the committed fixture CSV from a `tether-probe --lv-record`
// recording (docs/FOCUS_LOCK_PLAN.md §0.4). The recordings themselves are large
// and stay in tests/fixtures/liveview/sweeps/ (ignored by Git); the lock tests
// read this CSV instead and need no JPEGs.
//
// For each row of the recording's frames.csv it reads frame-NNNNN.bin, decodes
// the JPEG (at offset 384, length from the header's bytes 4–7, big-endian) and
// writes one row of:
//
//   index,time_s,score,centre_diff[,pass]
//
//   score        the focus score, exactly as mac/ScannyBoy/Model/FocusScore.swift
//                computes it: mean squared 4-neighbour Laplacian over the centre
//                half of the frame drawn into an 8-bit DeviceGray context.
//   centre_diff  mean absolute difference between the centre half's grey pixels
//                and the previous frame's. Empty for index 0, and when the frame
//                size changed from the previous one (the zoom was moved).
//   pass         the §0.2 pass (A–E) whose [start_s, end_s) holds time_s, taken
//                from passes.csv. Present only when passes.csv is.
//
// time_s is copied from frames.csv as written. A frame that is missing or does
// not decode gets an empty score and centre_diff and a note on stderr; the
// row is kept so the index column stays aligned with the recording.
//
// Build and run:
//   swiftc -O -o /tmp/sweep-csv mac/Tools/SweepCSV.swift \
//       -framework Foundation -framework ImageIO -framework CoreGraphics
//   /tmp/sweep-csv tests/fixtures/liveview/sweeps/fine-dense \
//       tests/fixtures/liveview/sweep-fine-dense.csv
//
// The output path may also be an existing directory, in which case the file is
// written there as sweep-<recording directory name>.csv.

import CoreGraphics
import Foundation
import ImageIO

// MARK: - Arguments

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("sweep-csv: \(message)\n".utf8))
    exit(2)
}

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.contains("--help") || arguments.contains("-h") || arguments.count != 2 {
    print("""
    sweep-csv RECORDING_DIR OUTPUT_CSV

      RECORDING_DIR  A tether-probe --lv-record directory: frames.csv,
                     frame-NNNNN.bin, and optionally passes.csv.
      OUTPUT_CSV     Where to write sweep-<name>.csv (index,time_s,score,
                     centre_diff[,pass]). An existing directory gets
                     sweep-<recording directory name>.csv inside it.
    """)
    exit(arguments.count == 2 || arguments.isEmpty ? 0 : 2)
}

let recording = URL(fileURLWithPath: arguments[0], isDirectory: true)
var output = URL(fileURLWithPath: arguments[1])
var isDirectory: ObjCBool = false
if FileManager.default.fileExists(atPath: output.path, isDirectory: &isDirectory), isDirectory.boolValue {
    output = output.appendingPathComponent("sweep-\(recording.lastPathComponent).csv")
}

// MARK: - Inputs

/// A CSV with a header row, as rows of fields. No quoting: the probe writes none.
func readCSV(_ url: URL) -> (header: [String], rows: [[String]])? {
    guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
    let lines = text.split(whereSeparator: \.isNewline).map(String.init)
    guard let first = lines.first else { return nil }
    return (first.split(separator: ",", omittingEmptySubsequences: false).map(String.init),
            lines.dropFirst().map { $0.split(separator: ",", omittingEmptySubsequences: false).map(String.init) })
}

guard let frames = readCSV(recording.appendingPathComponent("frames.csv")) else {
    fail("no readable frames.csv in \(recording.path)")
}
guard let indexColumn = frames.header.firstIndex(of: "index"),
      let timeColumn = frames.header.firstIndex(of: "time_s")
else { fail("frames.csv has no index and time_s columns") }

struct Pass {
    let letter: String
    let start: Double
    let end: Double
}

var passes: [Pass]?
if let table = readCSV(recording.appendingPathComponent("passes.csv")),
   let letter = table.header.firstIndex(of: "pass"),
   let start = table.header.firstIndex(of: "start_s"),
   let end = table.header.firstIndex(of: "end_s") {
    passes = table.rows.compactMap { row in
        guard row.count > max(letter, start, end),
              let from = Double(row[start]), let to = Double(row[end]) else { return nil }
        return Pass(letter: row[letter], start: from, end: to)
    }
}

/// The pass holding `time`; the last pass also owns its end instant.
func passLabel(at time: Double) -> String {
    guard let passes else { return "" }
    for (position, pass) in passes.enumerated()
    where time >= pass.start && (time < pass.end || (position == passes.count - 1 && time <= pass.end)) {
        return pass.letter
    }
    return ""
}

// MARK: - Decoding and scoring

struct GreyFrame {
    let width: Int
    let height: Int
    let pixels: [UInt8]
}

/// The JPEG inside a probe payload: 384-byte header, then `jpegLength` bytes
/// (big-endian at bytes 4–7). nil when the file disagrees with its own header.
func jpegData(from payload: Data) -> Data? {
    let headerLength = 384
    let bytes = [UInt8](payload)
    guard bytes.count > headerLength else { return nil }
    let length = Int(bytes[4]) << 24 | Int(bytes[5]) << 16 | Int(bytes[6]) << 8 | Int(bytes[7])
    guard length > 0, headerLength + length <= bytes.count else { return nil }
    return Data(bytes[headerLength..<(headerLength + length)])
}

/// Drawn exactly as FocusScore.greyFrame does: 8-bit DeviceGray, no alpha.
func greyFrame(_ jpeg: Data) -> GreyFrame? {
    guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { return nil }
    let width = image.width
    let height = image.height
    guard width > 4, height > 4 else { return nil }
    var grey = [UInt8](repeating: 0, count: width * height)
    let drawn = grey.withUnsafeMutableBytes { buffer -> Bool in
        guard let context = CGContext(
            data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGImageAlphaInfo.none.rawValue)
        else { return false }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return true
    }
    return drawn ? GreyFrame(width: width, height: height, pixels: grey) : nil
}

/// Mean squared 4-neighbour Laplacian over the centre half (FocusScore.score).
func score(_ frame: GreyFrame) -> Double {
    let width = frame.width, height = frame.height, grey = frame.pixels
    var sum = 0.0
    var count = 0
    for y in (height / 4)..<(height * 3 / 4) {
        for x in (width / 4)..<(width * 3 / 4) {
            let i = y * width + x
            let laplacian = 4 * Int(grey[i]) - Int(grey[i - 1]) - Int(grey[i + 1])
                - Int(grey[i - width]) - Int(grey[i + width])
            sum += Double(laplacian * laplacian)
            count += 1
        }
    }
    return count > 0 ? sum / Double(count) : 0
}

/// Mean absolute grey difference over the centre half; nil if the sizes differ.
func centreDifference(_ frame: GreyFrame, from previous: GreyFrame) -> Double? {
    guard frame.width == previous.width, frame.height == previous.height else { return nil }
    let width = frame.width, height = frame.height
    var sum = 0
    var count = 0
    for y in (height / 4)..<(height * 3 / 4) {
        for x in (width / 4)..<(width * 3 / 4) {
            sum += abs(Int(frame.pixels[y * width + x]) - Int(previous.pixels[y * width + x]))
            count += 1
        }
    }
    return count > 0 ? Double(sum) / Double(count) : nil
}

// MARK: - Run

let hasPasses = passes != nil
var out = "index,time_s,score,centre_diff" + (hasPasses ? ",pass" : "") + "\n"
var previous: GreyFrame?
var undecoded = 0

for row in frames.rows {
    guard row.count > max(indexColumn, timeColumn), let index = Int(row[indexColumn]),
          let time = Double(row[timeColumn])
    else { fail("unreadable frames.csv row: \(row.joined(separator: ","))") }
    let name = String(format: "frame-%05d.bin", index)

    var scoreField = "", diffField = ""
    if let payload = try? Data(contentsOf: recording.appendingPathComponent(name)),
       let jpeg = jpegData(from: payload), let frame = greyFrame(jpeg) {
        scoreField = String(format: "%.4f", score(frame))
        if let previous, let diff = centreDifference(frame, from: previous) {
            diffField = String(format: "%.4f", diff)
        }
        previous = frame
    } else {
        undecoded += 1
        previous = nil
        FileHandle.standardError.write(Data("sweep-csv: \(name) missing or undecodable\n".utf8))
    }
    out += "\(index),\(row[timeColumn]),\(scoreField),\(diffField)"
        + (hasPasses ? ",\(passLabel(at: time))" : "") + "\n"
}

do {
    try Data(out.utf8).write(to: output)
} catch {
    fail("could not write \(output.path): \(error.localizedDescription)")
}
print("sweep-csv: \(frames.rows.count) frames"
    + (hasPasses ? ", \(passes?.count ?? 0) passes" : ", no passes.csv")
    + (undecoded > 0 ? ", \(undecoded) undecodable" : "")
    + " → \(output.path)")
