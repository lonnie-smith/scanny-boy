import Foundation

/// PTP container framing and dataset decoders for ImageCaptureCore's raw
/// passthrough (docs/TETHER_PLAN.md §2.2). Extracted from `TetherProbe.swift`.
enum PTP {
    // MARK: - Opcodes

    static let getDeviceInfo: UInt16 = 0x1001
    static let getStorageIDs: UInt16 = 0x1004
    static let getStorageInfo: UInt16 = 0x1005
    static let getObjectInfo: UInt16 = 0x1008
    static let getObject: UInt16 = 0x1009
    static let getDevicePropValue: UInt16 = 0x1015
    static let initiateCapture: UInt16 = 0x100E
    static let nikonCheckEvent: UInt16 = 0x90C7
    static let nikonDeviceReady: UInt16 = 0x90C8
    static let nikonInitiateCaptureRecInSdram: UInt16 = 0x90C0
    static let nikonInitiateCaptureRecInMedia: UInt16 = 0x9207
    static let nikonDelImageSDRAM: UInt16 = 0x90C3
    static let nikonStartLiveView: UInt16 = 0x9201
    static let nikonEndLiveView: UInt16 = 0x9202
    static let nikonGetLiveViewImage: UInt16 = 0x9203

    static let responseOK: UInt16 = 0x2001
    static let responseDeviceBusy: UInt16 = 0x2019
    static let responseInvalidObjectHandle: UInt16 = 0x2009

    static let sdramHandle: UInt32 = 0xFFFF_0001

    /// Operations required for tethered capture on the Z f.
    static let requiredOperations: Set<UInt16> = [
        getObjectInfo, getObject, getDevicePropValue,
        nikonCheckEvent, nikonDeviceReady, nikonInitiateCaptureRecInSdram,
    ]

    static let requiredCardOperations: Set<UInt16> = [
        getStorageIDs, getStorageInfo, nikonInitiateCaptureRecInMedia,
    ]

    // MARK: - Framing

    static func command(
        _ opcode: UInt16, params: [UInt32] = [], transaction: UInt32 = 0
    ) -> Data {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        append(UInt32(12 + 4 * params.count))
        append(UInt16(1))
        append(opcode)
        append(transaction)
        params.forEach(append)
        return data
    }

    static let containerTypeCommand: UInt16 = 1
    static let containerTypeData: UInt16 = 2
    static let containerTypeResponse: UInt16 = 3
    static let containerTypeEvent: UInt16 = 4

    static func responseCode(_ data: Data?) -> UInt16? {
        guard let data, data.count >= 8 else { return nil }
        return UInt16(data[data.startIndex + 6]) | (UInt16(data[data.startIndex + 7]) << 8)
    }

    static func containerType(_ data: Data?) -> UInt16? {
        guard let data, data.count >= 6 else { return nil }
        return UInt16(data[data.startIndex + 4]) | (UInt16(data[data.startIndex + 5]) << 8)
    }

    /// A PTP response container is short and carries a response code in the
    /// 0x2000 range. A multi-megabyte GetObject buffer can have bytes 4–5 that
    /// look like type 3; never treat that as a swapped response.
    static func isPlausibleResponseContainer(_ data: Data) -> Bool {
        guard data.count >= 8, data.count <= 64 else { return false }
        guard containerType(data) == containerTypeResponse else { return false }
        guard let code = responseCode(data) else { return false }
        return (0x2000...0x2FFF).contains(code)
    }

    /// ImageCaptureCore's completion takes `(inData, response, error)`. For
    /// commands with no data-in phase it sometimes puts the response
    /// container in the first `Data` and leaves the second empty — the probe
    /// confirmed this rather than assuming the labels. Callers always get
    /// `(dataPhase, responseContainer)`.
    static func orderedCommandResult(data: Data, response: Data) -> (Data, Data) {
        if isPlausibleResponseContainer(response) {
            return (data, response)
        }
        if isPlausibleResponseContainer(data) {
            return (response, data)
        }
        return (data, response)
    }

    /// Picks the GetObject payload whose byte count matches ObjectInfo.size,
    /// or the larger non-response side when ImageCaptureCore's labels lie.
    static func objectPayload(dataPhase: Data, response: Data, expectedSize: UInt32) -> [UInt8] {
        let ordered = orderedCommandResult(data: dataPhase, response: response)
        let stripped = [payload(ordered.0), payload(ordered.1)]
        let expected = Int(expectedSize)
        if let exact = stripped.first(where: { $0.count == expected }) {
            return exact
        }
        return stripped.max(by: { $0.count < $1.count }) ?? []
    }

    static func response(
        _ code: UInt16, transaction: UInt32 = 0, params: [UInt32] = []
    ) -> Data {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        append(UInt32(12 + 4 * params.count))
        append(containerTypeResponse)
        append(code)
        append(transaction)
        params.forEach(append)
        return data
    }

    static func responseParameter(_ data: Data, _ index: Int = 0) -> UInt32? {
        let bytes = [UInt8](data)
        let start = 12 + 4 * index
        guard bytes.count >= start + 4 else { return nil }
        return (0..<4).reduce(UInt32(0)) { $0 | UInt32(bytes[start + $1]) << (8 * $1) }
    }

    static func payload(_ data: Data) -> [UInt8] {
        let bytes = [UInt8](data)
        guard bytes.count >= 12 else { return bytes }
        let length = UInt32(bytes[0]) | UInt32(bytes[1]) << 8
            | UInt32(bytes[2]) << 16 | UInt32(bytes[3]) << 24
        let type = UInt16(bytes[4]) | UInt16(bytes[5]) << 8
        return type == 2 && Int(length) == bytes.count ? Array(bytes[12...]) : bytes
    }

    // MARK: - Events

    static func decodeEvent(_ data: Data) -> PTPEvent? {
        guard let decoded = decodeEventContainer(data) else { return nil }
        return PTPEvent(code: decoded.code, params: decoded.params)
    }

    static func decodeEventContainer(_ data: Data) -> (code: UInt16, params: [UInt32])? {
        var reader = Reader(bytes: [UInt8](data))
        guard let length = reader.u32(), reader.u16() == 4, let code = reader.u16(),
              reader.u32() != nil
        else { return nil }
        var params: [UInt32] = []
        while reader.offset + 4 <= min(Int(length), reader.bytes.count),
              let param = reader.u32()
        {
            params.append(param)
        }
        return (code, params)
    }

    struct PTPEvent: Sendable, Hashable {
        let code: UInt16
        let params: [UInt32]
    }

    /// One entry from Nikon's `CheckEvent` queue.
    struct QueuedEvent: Sendable, Hashable {
        let code: UInt16
        let param: UInt32
    }

    static func decodeCheckEvents(_ data: Data) -> [QueuedEvent] {
        var reader = Reader(bytes: payload(data))
        guard let count = reader.u16(), count > 0 else { return [] }
        var events: [QueuedEvent] = []
        for _ in 0..<count {
            guard let code = reader.u16(), let param = reader.u32() else { break }
            events.append(QueuedEvent(code: code, param: param))
        }
        return events
    }

    // MARK: - Datasets

    struct DeviceInfo: Sendable, Hashable {
        let standardVersion: UInt16
        let vendorExtensionID: UInt32
        let operations: Set<UInt16>
        let events: Set<UInt16>
        let manufacturer: String
        let model: String
        let firmware: String

        init?(payload data: Data) {
            var reader = Reader(bytes: PTP.payload(data))
            guard let standard = reader.u16(), let vendor = reader.u32(),
                  reader.u16() != nil, reader.string() != nil,
                  reader.u16() != nil, let operations = reader.u16Array(),
                  let events = reader.u16Array(), reader.u16Array() != nil,
                  reader.u16Array() != nil, reader.u16Array() != nil
            else { return nil }
            manufacturer = reader.string() ?? "?"
            model = reader.string() ?? "?"
            firmware = reader.string() ?? "?"
            standardVersion = standard
            vendorExtensionID = vendor
            self.operations = Set(operations)
            self.events = Set(events)
        }

        func supportsBufferCapture() -> Bool {
            operations.isSuperset(of: PTP.requiredOperations)
        }

        func supportsCardCapture() -> Bool {
            operations.isSuperset(of: PTP.requiredOperations.union(PTP.requiredCardOperations))
        }
    }

    struct ObjectInfo: Sendable, Hashable {
        let handle: UInt32
        let storageID: UInt32
        let objectFormat: UInt16
        let size: UInt32
        let width: UInt32
        let height: UInt32
        let filename: String
        let captureDate: String

        init?(handle: UInt32, payload data: Data) {
            var reader = Reader(bytes: PTP.payload(data))
            guard let storage = reader.u32(), let format = reader.u16(), reader.u16() != nil,
                  let size = reader.u32()
            else { return nil }
            _ = (reader.u16(), reader.u32(), reader.u32(), reader.u32())
            let width = reader.u32() ?? 0
            let height = reader.u32() ?? 0
            _ = reader.u32()
            _ = (reader.u32(), reader.u16(), reader.u32(), reader.u32())
            let filename = reader.string() ?? String(format: "object-%08x", handle)
            let captureDate = reader.string() ?? "?"
            self.handle = handle
            storageID = storage
            objectFormat = format
            self.size = size
            self.width = width
            self.height = height
            self.filename = filename
            self.captureDate = captureDate
        }

        /// `captureDate` (PTP DateTime, `yyyyMMddThhmmss[.s]`) in local time.
        var capturedAt: Date? {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = .current
            formatter.dateFormat = "yyyyMMdd'T'HHmmss"
            return formatter.date(from: String(captureDate.prefix(15)))
        }
    }

    struct StorageInfo: Sendable, Hashable {
        let storageID: UInt32
        let description: String
        let label: String

        init?(storageID: UInt32, payload data: Data) {
            var reader = Reader(bytes: PTP.payload(data))
            _ = (reader.u16(), reader.u16(), reader.u16())
            _ = (reader.u64(), reader.u64(), reader.u32())
            description = reader.string() ?? ""
            label = reader.string() ?? ""
            self.storageID = storageID
        }
    }

    // MARK: - Property decoding

    static func decodePropertyValue(_ code: UInt16, raw: UInt32) -> String {
        switch code {
        case 0x500E:
            return [1: "Manual", 2: "Program", 3: "Aperture priority",
                    4: "Shutter priority"][raw] ?? String(format: "0x%04x", raw)
        case 0x500D:
            if raw == 0xFFFF_FFFF { return "bulb" }
            let seconds = Double(raw) / 10_000
            return seconds >= 1 || seconds == 0
                ? String(format: "%.1fs", seconds)
                : String(format: "1/%.0fs", 1 / seconds)
        case 0x5007:
            return String(format: "f/%.1f", Double(raw) / 100)
        case 0x500F:
            return "ISO \(raw)"
        case 0x5005:
            return [1: "Manual", 2: "Auto", 4: "Daylight", 5: "Fluorescent", 6: "Incandescent",
                    7: "Flash", 0x8010: "Cloudy", 0x8011: "Shade", 0x8012: "Colour temperature",
                    0x8013: "Preset manual"][raw] ?? String(format: "0x%04x", raw)
        case 0x500A:
            return [1: "Manual", 2: "Automatic", 3: "Macro", 0x8010: "AF-S", 0x8011: "AF-C",
                    0x8012: "AF-A"][raw] ?? String(format: "0x%02x", raw)
        default:
            return String(format: "0x%x", raw)
        }
    }

    static func describeResponse(_ code: UInt16?) -> String {
        guard let code else { return "no response" }
        let name =
            switch code {
            case 0x2001: "OK"
            case 0x2009: "Invalid object handle"
            case 0x2019: "Device busy"
            default: "unknown"
            }
        return String(format: "0x%04x (%@)", code, name)
    }

    // MARK: - Live view

    /// Decodes the 384-byte header before a live view JPEG (TETHER_PLAN §0.9).
    struct LiveViewHeader: Sendable, Hashable {
        let jpegLength: UInt32
        let frameSize: SizePair
        let sensorSize: SizePair
        let areaSize: SizePair
        let areaCentre: PointPair

        struct SizePair: Sendable, Hashable {
            let width: UInt16
            let height: UInt16
        }

        struct PointPair: Sendable, Hashable {
            let x: UInt16
            let y: UInt16
        }

        /// Parses `payload` and returns the JPEG bytes when the header length
        /// and `jpegLength` agree with the payload size.
        static func decode(_ payload: [UInt8]) -> (header: LiveViewHeader, jpeg: Data)? {
            guard payload.count >= FocusAssistTuning.liveViewHeaderLength else { return nil }
            let jpegLength = readUInt32BE(payload, offset: 4)
            let expectedSize = FocusAssistTuning.liveViewHeaderLength + Int(jpegLength)
            guard expectedSize == payload.count else { return nil }
            let header = LiveViewHeader(
                jpegLength: jpegLength,
                frameSize: readSizePair(payload, offset: 8),
                sensorSize: readSizePair(payload, offset: 12),
                areaSize: readSizePair(payload, offset: 16),
                areaCentre: readPointPair(payload, offset: 20)
            )
            let jpegStart = FocusAssistTuning.liveViewHeaderLength
            let jpegEnd = jpegStart + Int(jpegLength)
            return (header, Data(payload[jpegStart..<jpegEnd]))
        }

        private static func readUInt32BE(_ bytes: [UInt8], offset: Int) -> UInt32 {
            guard offset + 4 <= bytes.count else { return 0 }
            return (0..<4).reduce(UInt32(0)) {
                $0 | UInt32(bytes[offset + $1]) << (24 - 8 * $1)
            }
        }

        private static func readUInt16BE(_ bytes: [UInt8], offset: Int) -> UInt16 {
            guard offset + 2 <= bytes.count else { return 0 }
            return UInt16(bytes[offset]) << 8 | UInt16(bytes[offset + 1])
        }

        private static func readSizePair(_ bytes: [UInt8], offset: Int) -> SizePair {
            SizePair(
                width: readUInt16BE(bytes, offset: offset),
                height: readUInt16BE(bytes, offset: offset + 2)
            )
        }

        private static func readPointPair(_ bytes: [UInt8], offset: Int) -> PointPair {
            PointPair(
                x: readUInt16BE(bytes, offset: offset),
                y: readUInt16BE(bytes, offset: offset + 2)
            )
        }
    }

    // MARK: - Reader

    struct Reader {
        let bytes: [UInt8]
        var offset = 0

        mutating func u8() -> UInt8? {
            guard offset + 1 <= bytes.count else { return nil }
            defer { offset += 1 }
            return bytes[offset]
        }

        mutating func u16() -> UInt16? {
            guard offset + 2 <= bytes.count else { return nil }
            defer { offset += 2 }
            return UInt16(bytes[offset]) | UInt16(bytes[offset + 1]) << 8
        }

        mutating func u32() -> UInt32? {
            guard offset + 4 <= bytes.count else { return nil }
            defer { offset += 4 }
            return (0..<4).reduce(UInt32(0)) { $0 | UInt32(bytes[offset + $1]) << (8 * $1) }
        }

        mutating func u64() -> UInt64? {
            guard let low = u32(), let high = u32() else { return nil }
            return UInt64(high) << 32 | UInt64(low)
        }

        mutating func string() -> String? {
            guard let count = u8() else { return nil }
            var units: [UInt16] = []
            for _ in 0..<count {
                guard let unit = u16() else { return nil }
                if unit != 0 { units.append(unit) }
            }
            return String(decoding: units, as: UTF16.self)
        }

        mutating func u16Array() -> [UInt16]? {
            guard let count = u32() else { return nil }
            var values: [UInt16] = []
            for _ in 0..<count {
                guard let value = u16() else { return nil }
                values.append(value)
            }
            return values
        }
    }
}
