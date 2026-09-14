import CoreGraphics
import Foundation
import ImageIO

/// Mean squared 4-neighbour Laplacian over the centre half of an 8-bit grey
/// frame (docs/FOCUS_ASSIST_PLAN.md §3.1). Pure — no camera dependency.
enum FocusScore {
    static func score(jpegData: Data) -> Double? {
        guard let image = decodeJPEG(jpegData) else { return nil }
        return score(image: image)
    }

    static func score(image: CGImage) -> Double {
        let width = image.width
        let height = image.height
        guard width > 4, height > 4 else { return 0 }
        var grey = [UInt8](repeating: 0, count: width * height)
        let drawn = grey.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return 0 }
        var sum = 0.0
        var count = 0
        for y in (height / 4)..<(height * 3 / 4) {
            for x in (width / 4)..<(width * 3 / 4) {
                let index = y * width + x
                let laplacian = 4 * Int(grey[index])
                    - Int(grey[index - 1])
                    - Int(grey[index + 1])
                    - Int(grey[index - width])
                    - Int(grey[index + width])
                sum += Double(laplacian * laplacian)
                count += 1
            }
        }
        return count > 0 ? sum / Double(count) : 0
    }

    private static func decodeJPEG(_ jpeg: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }
}
