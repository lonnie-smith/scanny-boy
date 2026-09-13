import CoreGraphics
import Foundation
import ImageIO
import Testing

@testable import ScannyBoy

enum LiveViewFixtures {
    static let directory = TestSupport.repositoryRoot
        .appending(path: "tests/fixtures/liveview", directoryHint: .isDirectory)

    static func payload(named name: String) -> [UInt8] {
        let url = directory.appending(path: name, directoryHint: .notDirectory)
        guard let data = try? Data(contentsOf: url) else { return [] }
        return [UInt8](data)
    }

    static func makeFrame(areaWidth: UInt16, jpeg: Data) -> LiveViewFrame {
        var header = [UInt8](repeating: 0, count: FocusAssistTuning.liveViewHeaderLength)
        func writeBE16(_ offset: Int, _ value: UInt16) {
            header[offset] = UInt8(value >> 8)
            header[offset + 1] = UInt8(value & 0xFF)
        }
        func writeBE32(_ offset: Int, _ value: UInt32) {
            for shift in stride(from: 24, through: 0, by: -8) {
                header[offset + (24 - shift) / 8] = UInt8((value >> shift) & 0xFF)
            }
        }
        let areaHeight = UInt16(max(1, Int(areaWidth) * FocusAssistTuning.sensorHeight / FocusAssistTuning.sensorWidth))
        writeBE32(4, UInt32(jpeg.count))
        writeBE16(8, 640)
        writeBE16(10, 424)
        writeBE16(12, UInt16(FocusAssistTuning.sensorWidth))
        writeBE16(14, UInt16(FocusAssistTuning.sensorHeight))
        writeBE16(16, areaWidth)
        writeBE16(18, areaHeight)
        writeBE16(20, 3024)
        writeBE16(22, 2016)
        let payload = header + [UInt8](jpeg)
        let decoded = PTP.LiveViewHeader.decode(payload)!
        return LiveViewFrame(header: decoded.header, jpegData: decoded.jpeg)
    }

    static func texturedJPEG(seed: UInt8, size: Int = 64) -> Data {
        let colorSpace = CGColorSpaceCreateDeviceGray()
        guard let context = CGContext(
            data: nil,
            width: size,
            height: size,
            bitsPerComponent: 8,
            bytesPerRow: size,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.none.rawValue
        ), let buffer = context.data?.bindMemory(to: UInt8.self, capacity: size * size)
        else { return Data(LiveViewFixtures.payload(named: "lv-watch-t13.bin").suffix(384)) }
        for y in 0..<size {
            for x in 0..<size {
                buffer[y * size + x] = seed &+ UInt8((x * 17 + y * 31) & 0xFF)
            }
        }
        guard let image = context.makeImage() else { return Data() }
        return jpegData(from: image)
    }

    static func flatGreyJPEG(size: Int = 64, value: UInt8 = 128) -> Data {
        let colorSpace = CGColorSpaceCreateDeviceGray()
        guard let context = CGContext(
            data: nil,
            width: size,
            height: size,
            bitsPerComponent: 8,
            bytesPerRow: size,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.none.rawValue
        ), let buffer = context.data?.bindMemory(to: UInt8.self, capacity: size * size)
        else { return Data() }
        buffer.initialize(repeating: value, count: size * size)
        guard let image = context.makeImage() else { return Data() }
        return jpegData(from: image)
    }

    private static func jpegData(from image: CGImage) -> Data {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            data, "public.jpeg" as CFString, 1, nil
        ) else { return Data() }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return Data() }
        return data as Data
    }
}
