// TetherProbe — a throwaway spike that settled whether tethered capture on the
// Nikon Z f can rest on macOS's ImageCaptureCore. It can, through raw PTP; the
// findings are written up in docs/TETHER_PLAN.md. Nothing here is production
// code, and nothing here belongs in the app target.
//
// Modes:
//   --info             Fires nothing. Decodes GetDeviceInfo; reads the exposure
//                      settings, the recording-media choices, and Nikon's event
//                      queue; fetches anything waiting in the camera's buffer
//                      (which clears it).
//   --release METHODS  Fires once per method over raw PTP, proving each shot by
//                      card object counts, events, and buffer handle scans:
//                        standard           InitiateCapture (0x100E), to card
//                        nikon-media        InitiateCaptureRecInMedia (0x9207), to card
//                        nikon-sdram        InitiateCaptureRecInSdram (0x90C0), buffer only
//                        nikon-media-sdram  0x9207 with media 1 (not run on the Z f)
//   --liveview         Starts Nikon live view (0x9201), reads and logs the zoom
//                      property's allowed range (GetDevicePropDesc 0xD1A3) and the
//                      live view status/prohibit-condition properties (0xD1A2,
//                      0xD1A4), then for each allowed zoom value: sets it
//                      (SetDevicePropValue), waits ~500ms, and pulls --lv-frames
//                      frames back-to-back over GetLiveViewImage (0x9203). Writes
//                      the first frame's JPEG and raw payload per zoom level into
//                      --out, decodes JPEG dimensions with ImageIO, and logs
//                      latency/fps and a per-zoom summary table. Always sends
//                      EndLiveView (0x9202) on exit, including on error.
//   --shots N, or interactive (Return)
//                      requestTakePicture(), which does nothing on the Z f. Kept
//                      because that negative result is part of the record.
//                      Interactively, p fires 0x100E and i sends GetDeviceInfo.
//
// Build and run:
//   swiftc -O -o /tmp/tether-probe mac/Tools/TetherProbe.swift \
//       -framework Foundation -framework ImageCaptureCore -framework ImageIO
//   /tmp/tether-probe --info
//   /tmp/tether-probe --liveview --lv-frames 30
//
// Before running: the camera on and connected in its PTP mode (in mass storage
// mode it mounts under /Volumes and cannot be driven), Photos and Image Capture
// quit so nothing else holds the device, and the lens in manual focus.

import Foundation
import ImageCaptureCore
import ImageIO

// MARK: - Logging

let processStart = Date()

func log(_ message: String) {
    print(String(format: "[%7.3fs] %@", Date().timeIntervalSince(processStart), message))
    fflush(stdout)
}

func note(_ message: String) {
    print("           \(message)")
    fflush(stdout)
}

// MARK: - PTP framing
//
// ImageCaptureCore's requestSendPTPCommand takes a PTP command container
// and hands back the data and response containers. The framing below is
// the standard PTP/USB container layout; whether ImageCaptureCore wants
// exactly this is itself part of what the spike tests — a GetDeviceInfo
// answering 0x2001 (OK) proves it.
//
//   uint32 length | uint16 type (1 = command) | uint16 opcode
//   uint32 transaction id | uint32 params...

enum PTP {
    static let getDeviceInfo: UInt16 = 0x1001
    static let initiateCapture: UInt16 = 0x100E
    static let responseOK: UInt16 = 0x2001

    static func command(_ opcode: UInt16, params: [UInt32] = [], transaction: UInt32 = 0) -> Data {
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

    /// Frames a data-OUT phase the same way `command` frames the command
    /// phase: length | type 2 (data) | opcode | transaction | payload.
    /// Unverified against the camera — SetDevicePropValue is the first
    /// opcode in this file that needs an outData phase at all.
    static func dataOut(_ opcode: UInt16, transaction: UInt32 = 0, payload: [UInt8]) -> Data {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        append(UInt32(12 + payload.count))
        append(UInt16(2))
        append(opcode)
        append(transaction)
        data.append(contentsOf: payload)
        return data
    }

    /// The response code sits at offset 6 of a response container.
    static func responseCode(_ data: Data?) -> UInt16? {
        guard let data, data.count >= 8 else { return nil }
        return UInt16(data[data.startIndex + 6]) | (UInt16(data[data.startIndex + 7]) << 8)
    }

    static let getDevicePropValue: UInt16 = 0x1015
    static let setDevicePropValue: UInt16 = 0x1016
    static let getNumObjects: UInt16 = 0x1006
    static let getObjectInfo: UInt16 = 0x1008
    static let getObject: UInt16 = 0x1009
    static let getObjectHandles: UInt16 = 0x1007
    static let getStorageIDs: UInt16 = 0x1004
    static let getStorageInfo: UInt16 = 0x1005

    // Nikon live view (libgphoto2 ptp.h naming; unverified against this body
    // until the mode actually runs).
    static let nikonStartLiveView: UInt16 = 0x9201
    static let nikonEndLiveView: UInt16 = 0x9202
    static let nikonGetLiveViewImage: UInt16 = 0x9203
    static let nikonChangeAfArea: UInt16 = 0x9205
    static let responseNotInLiveView: UInt16 = 0xA00B
    static let responseDeviceBusy: UInt16 = 0x2019

    static let propLiveViewStatus: UInt16 = 0xD1A2
    static let propLiveViewImageZoomRatio: UInt16 = 0xD1A3
    static let propLiveViewProhibitCondition: UInt16 = 0xD1A4

    /// The classic Nikon buffer handle, used only when ObjectAddedInSdram carries
    /// none. The Z f names its own (0x0B000001 observed) — never assume this one.
    static let sdramHandle: UInt32 = 0xFFFF_0001
    static let nikonCheckEvent: UInt16 = 0x90C7
    static let getDevicePropDesc: UInt16 = 0x1014

    /// An event container: length, type 4, code, transaction id, params.
    static func decodeEvent(_ data: Data) -> (code: UInt16, params: [UInt32])? {
        var reader = Reader(bytes: [UInt8](data))
        guard let length = reader.u32(), reader.u16() == 4, let code = reader.u16(),
              reader.u32() != nil
        else { return nil }
        var params: [UInt32] = []
        while reader.offset + 4 <= min(Int(length), reader.bytes.count), let param = reader.u32() {
            params.append(param)
        }
        return (code, params)
    }

    static func eventName(_ code: UInt16) -> String {
        interestingEvents.first { $0.0 == code }?.1 ?? "unlisted"
    }
    static let nikonDeviceReady: UInt16 = 0x90C8

    /// Response parameters follow the 12-byte response container header.
    static func responseParameter(_ data: Data, _ index: Int = 0) -> UInt32? {
        let bytes = [UInt8](data)
        let start = 12 + 4 * index
        guard bytes.count >= start + 4 else { return nil }
        return (0..<4).reduce(UInt32(0)) { $0 | UInt32(bytes[start + $1]) << (8 * $1) }
    }

    /// The data phase may or may not arrive wrapped in a 12-byte data
    /// container header; strip one when it is there.
    static func payload(_ data: Data) -> [UInt8] {
        let bytes = [UInt8](data)
        guard bytes.count >= 12 else { return bytes }
        let length = UInt32(bytes[0]) | UInt32(bytes[1]) << 8
            | UInt32(bytes[2]) << 16 | UInt32(bytes[3]) << 24
        let type = UInt16(bytes[4]) | UInt16(bytes[5]) << 8
        return type == 2 && Int(length) == bytes.count ? Array(bytes[12...]) : bytes
    }

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

        /// A PTP string: a UInt8 character count (terminator included), then UTF-16LE.
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

    // The standard codes are from the PTP spec (ISO 15740); the Nikon vendor
    // codes follow libgphoto2's ptp.h naming.
    static let interestingOperations: [(UInt16, String)] = [
        (0x100E, "InitiateCapture"),
        (0x1015, "GetDevicePropValue"),
        (0x1016, "SetDevicePropValue"),
        (0x90C0, "Nikon InitiateCaptureRecInSdram"),
        (0x9207, "Nikon InitiateCaptureRecInMedia"),
        (0x90C7, "Nikon CheckEvent (polled events)"),
        (0x90C8, "Nikon DeviceReady"),
        (0x9201, "Nikon StartLiveView"),
        (0x9202, "Nikon EndLiveView"),
        (0x9203, "Nikon GetLiveViewImage"),
        (0x9205, "Nikon ChangeAfArea"),
    ]

    static let interestingEvents: [(UInt16, String)] = [
        (0x4002, "ObjectAdded"),
        (0x4006, "DevicePropChanged"),
        (0x400D, "CaptureComplete"),
        (0xC101, "Nikon ObjectAddedInSdram"),
        (0xC102, "Nikon CaptureCompleteRecInSdram"),
    ]

    static let interestingProperties: [(UInt16, String)] = [
        (0x500E, "exposure program"),
        (0x500D, "shutter speed"),
        (0x5007, "aperture"),
        (0x500F, "ISO"),
        (0x5005, "white balance"),
        (0x500A, "focus mode"),
        (0xD10B, "Nikon recording media"),
    ]

    static func describeProperty(_ code: UInt16, _ raw: UInt32) -> String {
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
                    0x8012: "AF-A"][raw] ?? String(format: "0x%04x", raw)
        case 0xD10B:
            return [0: "card", 1: "host SDRAM"][raw] ?? String(format: "0x%02x", raw)
        default:
            return String(format: "0x%x", raw)
        }
    }

    static func describe(_ code: UInt16?) -> String {
        guard let code else { return "no response container" }
        let name =
            switch code {
            case 0x2001: "OK"
            case 0x2002: "General error"
            case 0x2003: "Session not open"
            case 0x2005: "Operation not supported"
            case 0x2006: "Parameter not supported"
            case 0x2009: "Invalid object handle"
            case 0x2019: "Device busy"
            case 0x200A: "Device prop not supported"
            case 0x201E: "Invalid parameter"
            case 0xA004: "Nikon Invalid status"
            case 0xA00B: "Nikon Not in live view"
            default: "unknown"
            }
        return String(format: "0x%04x (%@)", code, name)
    }
}

// MARK: - Release methods

enum ReleaseMethod: String, CaseIterable {
    case standard
    case nikonMedia = "nikon-media"
    case nikonSdram = "nikon-sdram"
    case nikonMediaSdram = "nikon-media-sdram"

    var opcode: UInt16 {
        switch self {
        case .standard: 0x100E
        case .nikonMedia: 0x9207
        case .nikonSdram: 0x90C0
        case .nikonMediaSdram: 0x9207
        }
    }

    // 0x100E: storage 0, format 0 = the camera's own defaults.
    // 0x9207: 0xFFFFFFFF = release without autofocus, 0 = record to card
    //         (libgphoto2's ptp_nikon_capture2 argument layout).
    // 0x90C0: 0xFFFFFFFF = release without autofocus; the frame stays in the
    //         camera's buffer and never touches a card.
    // 0x9207 with media 1 asks for the buffer through the newer release call.
    var params: [UInt32] {
        switch self {
        case .standard: [0, 0]
        case .nikonMedia: [0xFFFF_FFFF, 0]
        case .nikonSdram: [0xFFFF_FFFF]
        case .nikonMediaSdram: [0xFFFF_FFFF, 1]
        }
    }

    var label: String {
        switch self {
        case .standard: "PTP InitiateCapture (0x100E)"
        case .nikonMedia: "Nikon InitiateCaptureRecInMedia (0x9207)"
        case .nikonSdram: "Nikon InitiateCaptureRecInSdram (0x90C0)"
        case .nikonMediaSdram: "Nikon InitiateCaptureRecInMedia to buffer (0x9207, media 1)"
        }
    }

    var recordsToCard: Bool { self == .standard || self == .nikonMedia }
}

// MARK: - Options

struct Options {
    var outputDirectory = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("tether-shots", isDirectory: true)
    var discoveryTimeout: TimeInterval = 10
    var arrivalTimeout: TimeInterval = 30
    var autoShots = 0
    var nameFilter: String?
    var infoOnly = false
    var releaseMethods: [ReleaseMethod] = []
    var liveView = false
    var lvFrames = 30
    var lvPoint: (x: UInt32, y: UInt32)?

    static func parse(_ arguments: [String]) -> Options {
        var options = Options()
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            let value: String? = index + 1 < arguments.count ? arguments[index + 1] : nil
            switch argument {
            case "--out":
                guard let value else { fail("--out needs a directory") }
                options.outputDirectory = URL(fileURLWithPath: value, isDirectory: true)
                index += 1
            case "--shots":
                guard let value, let count = Int(value) else { fail("--shots needs a number") }
                options.autoShots = count
                index += 1
            case "--name":
                guard let value else { fail("--name needs a substring") }
                options.nameFilter = value
                index += 1
            case "--discovery-timeout":
                guard let value, let seconds = TimeInterval(value) else {
                    fail("--discovery-timeout needs seconds")
                }
                options.discoveryTimeout = seconds
                index += 1
            case "--arrival-timeout":
                guard let value, let seconds = TimeInterval(value) else {
                    fail("--arrival-timeout needs seconds")
                }
                options.arrivalTimeout = seconds
                index += 1
            case "--info":
                options.infoOnly = true
            case "--release":
                guard let value else { fail("--release needs a comma-separated method list") }
                options.releaseMethods = value.split(separator: ",").map { name in
                    guard let method = ReleaseMethod(rawValue: String(name)) else {
                        fail("unknown release method '\(name)'; use "
                            + ReleaseMethod.allCases.map(\.rawValue).joined(separator: ", "))
                    }
                    return method
                }
                index += 1
            case "--liveview":
                options.liveView = true
            case "--lv-frames":
                guard let value, let count = Int(value), count > 0 else {
                    fail("--lv-frames needs a positive number")
                }
                options.lvFrames = count
                index += 1
            case "--lv-point":
                guard let value else { fail("--lv-point needs X,Y") }
                let parts = value.split(separator: ",")
                guard parts.count == 2, let x = UInt32(parts[0]), let y = UInt32(parts[1]) else {
                    fail("--lv-point needs X,Y as two integers, e.g. --lv-point 3024,2016")
                }
                options.lvPoint = (x, y)
                index += 1
            case "--help", "-h":
                usage()
                exit(0)
            default:
                fail("unknown argument: \(argument)")
            }
            index += 1
        }
        return options
    }

    static func usage() {
        print("""
        tether-probe [--out DIR] [--shots N] [--name SUBSTRING]
                     [--discovery-timeout SECS] [--arrival-timeout SECS]
                     [--liveview [--lv-frames N] [--lv-point X,Y]]

          --out                Where downloaded files land.
                               Default: $TMPDIR/tether-shots
          --shots N            Fire N shots automatically, then quit.
                               Default: interactive.
          --name SUBSTRING     Pick the camera whose name contains this.
                               Default: the first camera found.
          --discovery-timeout  Give up looking for a camera. Default: 10s
          --arrival-timeout    Give up waiting for a shot to arrive. Default: 30s
          --info               Fire nothing: decode GetDeviceInfo, read the current
                               exposure settings over PTP, and quit.
          --release METHODS    Fire once per listed method over raw PTP (standard,
                               nikon-media, nikon-sdram,
                               nikon-media-sdram), proving each shot by the card's object
                               count and DeviceReady rather than by eye, then quit.
          --liveview           Start Nikon live view, sweep every zoom ratio the
                               camera allows, and measure frame size, header
                               length, and fps at each — for judging a manual-
                               focus loupe. Always ends live view on exit.
          --lv-frames N        Frames to pull per zoom level in --liveview.
                               Default: 30
          --lv-point X,Y       Move the live view zoom point (ChangeAfArea,
                               sensor coordinates) before sweeping zoom.
                               Default: leave it centred.

        Interactive keys (press then Return):
          <Return>  fire via requestTakePicture()
          p         fire via a raw PTP InitiateCapture
          i         send a raw PTP GetDeviceInfo
          q         quit
        """)
    }

    static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data("tether-probe: \(message)\n".utf8))
        usage()
        exit(2)
    }
}

// MARK: - Probe

final class Probe: NSObject {
    private let options: Options
    private let browser = ICDeviceBrowser()

    private var camera: ICCameraDevice?
    private var sessionRequestedAt: Date?
    private var shutterFiredAt: Date?
    private var downloadStartedAt: [String: Date] = [:]
    private var itemsThisShot = 0
    private var shotsFired = 0
    private var shotsDownloaded = 0
    private var inFlightDownloads = 0
    private var arrivalTimer: Timer?
    private var discoveryTimer: Timer?
    private var catalogComplete = false
    /// Recording media (0xD10B) as found, when --liveview changed it to retry
    /// StartLiveView; `endLiveView` puts it back.
    private var liveViewRestoreRecordingMedia: UInt8?
    private var catalogItemsSeen = 0
    private var advertisesTakePicture = false
    private var advertisesPTP = false
    private var ptpRepliesReceived = 0
    private var releaseSequenceStarted = false
    private var addedHandles: [UInt32] = []
    private var captureCompleted = false
    private var objectsFetched = 0
    private var iccArrivalsThisShot = 0
    private var storageIDs: [UInt32] = []
    private var ignoredHandles: Set<UInt32> = []
    private var shuttingDown = false

    init(options: Options) {
        self.options = options
        super.init()
    }

    // MARK: Lifecycle

    func start() {
        do {
            try FileManager.default.createDirectory(
                at: options.outputDirectory, withIntermediateDirectories: true)
        } catch {
            log("FAIL  could not create \(options.outputDirectory.path): \(error)")
            exit(1)
        }

        log("probe starting; downloads go to \(options.outputDirectory.path)")

        browser.delegate = self
        browser.browsedDeviceTypeMask = ICDeviceTypeMask(
            rawValue: ICDeviceTypeMask.camera.rawValue
                | ICDeviceLocationTypeMask.local.rawValue)!
        browser.start()
        log("browsing for local cameras…")

        discoveryTimer = Timer.scheduledTimer(
            withTimeInterval: options.discoveryTimeout, repeats: false
        ) { [weak self] _ in
            self?.discoveryTimedOut()
        }
    }

    private func discoveryTimedOut() {
        guard camera == nil else { return }
        log("FAIL  no camera appeared within \(options.discoveryTimeout)s")
        note("Check, in this order:")
        note("  1. The camera's USB mode. If it mounts as a volume under /Volumes,")
        note("     it is in mass storage mode and ImageCaptureCore cannot see it.")
        note("     Switch it to MTP/PTP in the camera's setup menu.")
        note("  2. Photos.app and Image Capture.app are quit — either will hold")
        note("     the device and starve this process.")
        note("  3. The camera is awake, and its power-off timer has not fired.")
        note("  4. `system_profiler SPUSBDataType | grep -i nikon` sees it at all.")
        finish(code: 1)
    }

    // MARK: Adoption

    fileprivate func adopt(_ device: ICDevice) {
        guard camera == nil, let found = device as? ICCameraDevice else { return }
        if let filter = options.nameFilter,
           !(found.name ?? "").localizedCaseInsensitiveContains(filter) {
            log("skipping '\(found.name ?? "?")' (does not match --name \(filter))")
            return
        }

        camera = found
        discoveryTimer?.invalidate()

        log("found camera: \(found.name ?? "(unnamed)")")
        note("transport:    \(found.transportType ?? "unknown")")

        if found.transportType == ICDeviceTransport.transportTypeMassStorage.rawValue {
            log("FAIL  the camera is in MASS STORAGE mode, not MTP/PTP")
            note("ImageCaptureCore can see it and can copy files off the card, but it")
            note("cannot drive the shutter and PTP commands go unanswered. Everything")
            note("this probe would report from here is about a card reader, not a")
            note("tether.")
            note("")
            note("Switch the camera's USB connection mode to MTP/PTP and run again.")
            note("It currently mounts at /Volumes — that is the giveaway.")
            camera = found
            finish(code: 1)
            return
        }
        note("usb location: \(String(format: "0x%08x", found.usbLocationID))")

        // PTP events are the layer under all of this. When
        // requestTakePicture() appears to do nothing, these say whether the
        // camera is talking back at all. The block wins over the delegate
        // when both are set.
        found.ptpEventHandler = { [weak self] data in
            DispatchQueue.main.async { self?.received(event: data) }
        }

        found.delegate = self
        sessionRequestedAt = Date()
        found.requestOpenSession()
        log("opening session…")
    }

    fileprivate func received(event data: Data) {
        guard let event = PTP.decodeEvent(data) else {
            log("PTP event (undecodable): "
                + data.map { String(format: "%02x", $0) }.joined(separator: " "))
            return
        }
        handleEvent(code: event.code, params: event.params, source: "pushed")
    }

    fileprivate func handleEvent(code: UInt16, params: [UInt32], source: String) {
        let described = params.map { String(format: "0x%08x", $0) }.joined(separator: ", ")
        log(String(format: "PTP event (%@) 0x%04x %@ [%@]", source, code, PTP.eventName(code), described))
        switch code {
        case 0x4002:
            if let handle = params.first, !ignoredHandles.contains(handle), !addedHandles.contains(handle) {
                addedHandles.append(handle)
            }
        case 0xC101:
            // ObjectAddedInSdram may carry no handle, or zero; the buffer's is fixed.
            let handle = params.first.flatMap { $0 == 0 ? nil : $0 } ?? PTP.sdramHandle
            if !ignoredHandles.contains(handle), !addedHandles.contains(handle) {
                addedHandles.append(handle)
            }
        case 0x4006:
            if params.first == 0xD10B {
                note("recording media toggles around every buffer capture — not a settings change")
            } else if let property = params.first,
                      let name = PTP.interestingProperties.first(where: { UInt32($0.0) == property })?.1 {
                note("the camera reports its \(name) changed — exposure drift is observable live")
            }
        case 0x400D, 0xC102:
            captureCompleted = true
        default:
            break
        }
    }

    // MARK: Capability reporting

    private func reportCapabilities(_ camera: ICCameraDevice) {
        let capabilities = camera.capabilities
        advertisesTakePicture = capabilities.contains(ICDeviceCapability.cameraDeviceCanTakePicture.rawValue)
        advertisesPTP = capabilities.contains(ICDeviceCapability.cameraDeviceCanAcceptPTPCommands.rawValue)
        let advertisesBodyRelease =
            capabilities.contains(
                ICDeviceCapability.cameraDeviceCanTakePictureUsingShutterReleaseOnCamera.rawValue)

        note("capabilities: \(capabilities.isEmpty ? "(none)" : capabilities.joined(separator: ", "))")
        note("tetheredCaptureEnabled: \(camera.tetheredCaptureEnabled)")
        if camera.batteryLevelAvailable {
            note("battery:      \(camera.batteryLevel)%")
        }

        if advertisesTakePicture {
            log("PASS  canTakePicture advertised — remote release should work")
        } else {
            log("WARN  canTakePicture NOT advertised")
            note("requestTakePicture() is tried anyway: the capability list is")
            note("advisory and some PTP bodies under-report it.")
        }

        if advertisesBodyRelease {
            log("PASS  canTakePictureUsingShutterReleaseOnCamera advertised")
            note("This is the fallback route: fire with a wired remote release and")
            note("let the app catch the arrival. It survives even if remote release")
            note("turns out to be unsupported.")
        } else {
            log("WARN  canTakePictureUsingShutterReleaseOnCamera NOT advertised")
            note("Press the camera's own shutter button at the prompt to test it")
            note("regardless — arrivals are timed however they were triggered.")
        }

        if advertisesPTP {
            log("PASS  canAcceptPTPCommands advertised — raw PTP passthrough available")
            note("This matters: it means exposure, aperture, ISO and white balance")
            note("can be read and locked without libgphoto2. Testing it now.")
        } else {
            log("WARN  canAcceptPTPCommands NOT advertised (unusual for a PTP body)")
        }
    }

    // MARK: Capture

    fileprivate func sessionOpened() {
        guard let camera else { return }
        let elapsed = sessionRequestedAt.map { Date().timeIntervalSince($0) } ?? 0
        log(String(format: "session open (%.3fs)", elapsed))
        reportCapabilities(camera)

        if options.infoOnly {
            Task { await self.runInfo() }
            return
        }

        if options.liveView {
            Task { await self.runLiveView() }
            return
        }

        if advertisesPTP {
            sendPTP(PTP.getDeviceInfo, label: "GetDeviceInfo")
        }

        if catalogComplete {
            prompt()
        } else {
            log("waiting for the card's content catalog before firing…")
            note("Firing during the catalog scan makes new files indistinguishable")
            note("from old ones in the arrival stream.")
        }
    }

    fileprivate func fire() {
        guard let camera, !shuttingDown else { return }
        armArrivalTimer(label: "requestTakePicture()")
        camera.requestTakePicture()
    }

    fileprivate func firePTP() {
        guard advertisesPTP else {
            log("skipping PTP capture: the camera does not advertise PTP commands")
            return
        }
        armArrivalTimer(label: "PTP InitiateCapture")
        // storageID 0 and objectFormatCode 0 mean "camera's own defaults".
        sendPTP(PTP.initiateCapture, params: [0, 0], label: "InitiateCapture")
    }

    private func armArrivalTimer(label: String) {
        itemsThisShot = 0
        shutterFiredAt = Date()
        shotsFired += 1
        log("shot \(shotsFired): \(label)")

        arrivalTimer?.invalidate()
        arrivalTimer = Timer.scheduledTimer(
            withTimeInterval: options.arrivalTimeout, repeats: false
        ) { [weak self] _ in
            self?.arrivalTimedOut()
        }
    }

    private func arrivalTimedOut() {
        guard itemsThisShot == 0 else { return }
        log("FAIL  nothing arrived within \(options.arrivalTimeout)s of the shutter request")
        note("If the shutter did not audibly trip, this release path is unsupported")
        note("for this body — try 'p' for the raw PTP route before concluding that")
        note("ImageCaptureCore is a dead end.")
        note("If it DID trip but no file arrived, the camera is set to save to card")
        note("only; check its USB/tether destination setting.")
        prompt()
    }

    // MARK: PTP passthrough

    private func sendPTP(_ opcode: UInt16, params: [UInt32] = [], label: String) {
        guard let camera else { return }
        let sentAt = Date()
        log(String(format: "PTP → %@ (opcode 0x%04x)", label, opcode))

        camera.requestSendPTPCommand(
            PTP.command(opcode, params: params),
            outData: nil
        ) { [weak self] responseData, ptpResponseData, error in
            DispatchQueue.main.async {
                guard let self else { return }
                let elapsed = Date().timeIntervalSince(sentAt)
                self.ptpRepliesReceived += 1
                if let error {
                    log(String(format: "PTP ← %@ failed after %.3fs: %@",
                               label, elapsed, error.localizedDescription))
                    return
                }
                // The two buffers are the data phase and the response
                // container; which is which is worth confirming here rather
                // than assuming, so both are reported.
                let code = PTP.responseCode(ptpResponseData) ?? PTP.responseCode(responseData)
                log(String(format: "PTP ← %@ in %.3fs: %@", label, elapsed, PTP.describe(code)))
                note("data phase: \(responseData.count) bytes, "
                    + "response container: \(ptpResponseData.count) bytes")
                if code == PTP.responseOK, opcode == PTP.getDeviceInfo {
                    log("PASS  raw PTP passthrough works — property read/write is on")
                    note("the table without libgphoto2, and so is a PTP shutter release")
                    note("if requestTakePicture() disappoints.")
                }
            }
        }
    }

    // MARK: Info mode

    private func ptp(
        _ opcode: UInt16, params: [UInt32] = [], outData: Data? = nil
    ) async -> (Data, Data, Error?) {
        guard let camera else { return (Data(), Data(), nil) }
        return await withCheckedContinuation { continuation in
            camera.requestSendPTPCommand(PTP.command(opcode, params: params), outData: outData) {
                data, response, error in
                DispatchQueue.main.async { self.ptpRepliesReceived += 1 }
                continuation.resume(returning: (data, response, error))
            }
        }
    }

    private func runInfo() async {
        let sentAt = Date()
        log("PTP → GetDeviceInfo")
        let (data, response, error) = await ptp(PTP.getDeviceInfo)
        if let error {
            log("FAIL  GetDeviceInfo: \(error.localizedDescription)")
            DispatchQueue.main.async { self.finish(code: 1) }
            return
        }
        log(String(format: "PTP ← GetDeviceInfo in %.3fs: %@", Date().timeIntervalSince(sentAt),
                   PTP.describe(PTP.responseCode(response))))

        var reader = PTP.Reader(bytes: PTP.payload(data))
        guard let standard = reader.u16(), let vendor = reader.u32(),
              let vendorVersion = reader.u16(), let vendorDescription = reader.string(),
              let functionalMode = reader.u16(), let operations = reader.u16Array(),
              let events = reader.u16Array(), let properties = reader.u16Array(),
              let captureFormats = reader.u16Array(), let imageFormats = reader.u16Array()
        else {
            log("FAIL  could not decode the \(data.count)-byte DeviceInfo dataset")
            note("first bytes: " + data.prefix(24).map { String(format: "%02x", $0) }
                .joined(separator: " "))
            DispatchQueue.main.async { self.finish(code: 1) }
            return
        }
        let manufacturer = reader.string() ?? "?"
        let model = reader.string() ?? "?"
        let firmware = reader.string() ?? "?"

        log("device: \(manufacturer) \(model), firmware \(firmware)")
        note(String(format: "PTP %d.%02d, vendor extension 0x%08x v%d.%02d, functional mode %d",
                    standard / 100, standard % 100, vendor, vendorVersion / 100,
                    vendorVersion % 100, functionalMode))
        note("vendor extension: \(vendorDescription.prefix(80))")
        note("\(operations.count) operations, \(events.count) events, "
            + "\(properties.count) properties, \(captureFormats.count) capture formats, "
            + "\(imageFormats.count) image formats")

        log("operations that matter for tethering:")
        for (code, name) in PTP.interestingOperations {
            note(String(format: "  %@ 0x%04x %@", operations.contains(code) ? "yes" : " no",
                        code, name))
        }
        log("events that matter for tethering:")
        for (code, name) in PTP.interestingEvents {
            note(String(format: "  %@ 0x%04x %@", events.contains(code) ? "yes" : " no",
                        code, name))
        }

        log("current settings (GetDevicePropValue):")
        for (code, name) in PTP.interestingProperties {
            let (value, reply, error) = await ptp(PTP.getDevicePropValue, params: [UInt32(code)])
            let status = PTP.responseCode(reply)
            let listed = properties.contains(code) ? "" : " (not in the advertised list)"
            if let error {
                note(String(format: "  0x%04x %@: error %@%@", code, name,
                            error.localizedDescription, listed))
            } else if status != PTP.responseOK {
                note(String(format: "  0x%04x %@: %@%@", code, name, PTP.describe(status), listed))
            } else {
                let bytes = PTP.payload(value).prefix(4)
                let raw = bytes.enumerated().reduce(UInt32(0)) { $0 | UInt32($1.element) << (8 * $1.offset) }
                note(String(format: "  0x%04x %@: %@%@", code, name,
                            PTP.describeProperty(code, raw), listed))
            }
        }

        log("recording media choices (GetDevicePropDesc 0xD10B):")
        let (desc, descResponse, descError) = await ptp(PTP.getDevicePropDesc, params: [0xD10B])
        let descCode = descError == nil ? PTP.responseCode(descResponse) : nil
        if descCode == PTP.responseOK {
            var r = PTP.Reader(bytes: PTP.payload(desc))
            let widths: [UInt16: Int] = [1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4]
            if r.u16() != nil, let type = r.u16(), let width = widths[type], let getSet = r.u8() {
                func value() -> UInt32? {
                    switch width {
                    case 1: r.u8().map(UInt32.init)
                    case 2: r.u16().map(UInt32.init)
                    default: r.u32()
                    }
                }
                let factory = value(), current = value()
                var choices = "no form"
                switch r.u8() {
                case 1:
                    if let low = value(), let high = value(), let step = value() {
                        choices = "range \(low)...\(high) step \(step)"
                    }
                case 2:
                    if let count = r.u16() {
                        choices = "allowed values ["
                            + (0..<count).compactMap { _ in value().map(String.init) }
                                .joined(separator: ", ") + "]"
                    }
                default:
                    break
                }
                note("\(getSet == 1 ? "settable" : "read-only"), factory "
                    + "\(factory.map(String.init) ?? "?"), current \(current.map(String.init) ?? "?"), \(choices)")
                note("(libgphoto2 names 0 = card, 1 = camera buffer)")
            } else {
                note("could not decode the property description")
            }
        } else {
            note("GetDevicePropDesc: " + (descError?.localizedDescription ?? PTP.describe(descCode)))
        }

        log("queued Nikon events (CheckEvent 0x90C7):")
        await pollNikonEvents(reportEmpty: true)

        log("object handles by top byte (GetObjectHandles):")
        var candidates: [UInt32] = [PTP.sdramHandle, 0x0B00_0001, 0x0B00_0002]
        let (list, listResponse, listError) = await ptp(PTP.getObjectHandles, params: [0xFFFF_FFFF, 0, 0])
        let listCode = listError == nil ? PTP.responseCode(listResponse) : nil
        if listCode == PTP.responseOK {
            var r = PTP.Reader(bytes: PTP.payload(list))
            var byTopByte: [UInt32: Int] = [:]
            if let count = r.u32() {
                for _ in 0..<count {
                    guard let handle = r.u32() else { break }
                    byTopByte[handle >> 24, default: 0] += 1
                    // Card files on this body live under 0x09 (slot 1) and 0x0A (slot 2).
                    if handle >> 24 != 0x09, handle >> 24 != 0x0A,
                       !candidates.contains(handle), candidates.count < 20 {
                        candidates.append(handle)
                    }
                }
            }
            note(byTopByte.keys.sorted()
                .map { String(format: "0x%02x", $0) + ": \(byTopByte[$0] ?? 0)" }
                .joined(separator: ", "))
        } else {
            note("GetObjectHandles: " + (listError?.localizedDescription ?? PTP.describe(listCode)))
        }

        log("looking for a frame in the camera buffer:")
        for handle in candidates {
            let (_, info, error) = await ptp(PTP.getObjectInfo, params: [handle])
            let code = error == nil ? PTP.responseCode(info) : nil
            note(String(format: "GetObjectInfo 0x%08x → %@", handle,
                        error?.localizedDescription ?? PTP.describe(code)))
            if code == PTP.responseOK {
                await fetch(handle, releasedAt: nil)
            }
        }

        DispatchQueue.main.async { self.finish(code: 0) }
    }

    // MARK: Live view mode

    private struct ZoomResult {
        let zoom: UInt32
        let ok: Bool
        let width: Int
        let height: Int
        let jpegBytes: Int
        let headerLength: Int
        let minMs: Double
        let medianMs: Double
        let maxMs: Double
        let fps: Double
        let note: String
    }

    /// The Z f's full sensor, for judging how close max zoom gets to 1:1.
    private static let sensorWidth = 6048
    private static let sensorHeight = 4032
    /// How long the manual-zoom watch pulls frames on bodies without 0xD1A3.
    private static let liveViewWatchSeconds: TimeInterval = 40

    /// Lists the live view operations and 0xD1A0–0xD1FF properties the body
    /// advertises, with each property's current value.
    private func logLiveViewSupport() async {
        let (data, response, error) = await ptp(PTP.getDeviceInfo)
        guard error == nil, PTP.responseCode(response) == PTP.responseOK else {
            note("GetDeviceInfo: " + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
            return
        }
        var reader = PTP.Reader(bytes: PTP.payload(data))
        guard reader.u16() != nil, reader.u32() != nil, reader.u16() != nil, reader.string() != nil,
              reader.u16() != nil, let operations = reader.u16Array(), reader.u16Array() != nil,
              let properties = reader.u16Array()
        else {
            note("could not decode DeviceInfo")
            return
        }
        func hex(_ codes: [UInt16]) -> String {
            codes.isEmpty ? "none" : codes.map { String(format: "0x%04x", $0) }.joined(separator: " ")
        }
        let liveViewOperations = operations
            .filter { (0x9200...0x92FF).contains($0) || (0x9400...0x94FF).contains($0) }.sorted()
        log("advertised operations 0x92xx/0x94xx: " + hex(liveViewOperations))
        let liveViewProperties = properties.filter { (0xD1A0...0xD1FF).contains($0) }.sorted()
        log("advertised properties 0xD1A0–0xD1FF: " + hex(liveViewProperties))
        for code in liveViewProperties { await logPropValue(code, "advertised") }
    }

    /// Tries Nikon GetLiveViewImageEx (0x9428), which the Z f advertises and
    /// libgphoto2 uses on newer bodies, to see whether it returns bigger frames.
    private func probeLiveViewImageEx() async {
        log("PTP → GetLiveViewImageEx (0x9428), 3 tries:")
        for attempt in 1...3 {
            let started = Date()
            let (data, response, error) = await ptp(0x9428)
            let elapsedMs = Date().timeIntervalSince(started) * 1000
            let code = error == nil ? PTP.responseCode(response) : nil
            guard code == PTP.responseOK else {
                note("try \(attempt): " + (error?.localizedDescription ?? PTP.describe(code)))
                continue
            }
            let bytes = PTP.payload(data)
            let headerHex = bytes.prefix(min(64, findJPEGStart(bytes) ?? 64))
                .map { String(format: "%02x", $0) }.joined(separator: " ")
            guard let soi = findJPEGStart(bytes), let image = decodeJPEG(Data(bytes[soi...])) else {
                note("try \(attempt): \(bytes.count) bytes, no decodable JPEG; first bytes: \(headerHex)")
                continue
            }
            note(String(format: "try %d: %.1fms, %d bytes, header %d bytes, JPEG %dx%d; header starts: %@",
                        attempt, elapsedMs, bytes.count, soi, image.width, image.height, headerHex))
            if attempt == 1 {
                try? Data(bytes[soi...]).write(to: options.outputDirectory.appendingPathComponent("lv-ex.jpg"))
                try? Data(bytes).write(to: options.outputDirectory.appendingPathComponent("lv-ex.bin"))
            }
        }
    }

    private func decodeJPEG(_ jpeg: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }

    /// Mean squared Laplacian over the centre half of the frame in 8-bit grey.
    /// Grain drives it, which is what focus is judged on. Only comparable
    /// between frames at the same zoom.
    private func sharpness(_ image: CGImage) -> Double {
        let width = image.width, height = image.height
        guard width > 4, height > 4 else { return 0 }
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
        guard drawn else { return 0 }
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

    /// The focus test. Pulls frames for `liveViewWatchSeconds` while the user
    /// zooms and focuses on the body; counts frames whose JPEG differs from the
    /// last (the real refresh rate), reads the zoom from the header (bytes
    /// 16–17: sensor pixels across), and scores sharpness with a running peak
    /// that resets whenever the zoom changes.
    private func watchManualZoom() async {
        await logLiveViewSupport()
        await probeLiveViewImageEx()
        let seconds = Probe.liveViewWatchSeconds
        log("focus test starts in 5s and runs \(Int(seconds))s. Before it starts:")
        note("magnify 3 steps on the body (512 sensor px across) and throw focus clearly off.")
        note("Then turn slowly through sharp focus to clearly off the other side, and back to sharp.")
        try? await Task.sleep(nanoseconds: 5_000_000_000)

        let start = Date()
        let deadline = start.addingTimeInterval(seconds)
        var latenciesMs: [Double] = []
        var failures = 0
        var distinct = 0
        var lastJPEG = Data()
        var areaWidth = 0
        var peak = 0.0
        var peakAt = 0.0
        var nextReport = 0.0
        var pending: [Double] = []
        while Date() < deadline {
            let started = Date()
            let (data, response, error) = await ptp(PTP.nikonGetLiveViewImage)
            let code = error == nil ? PTP.responseCode(response) : nil
            guard code == PTP.responseOK else {
                failures += 1
                if failures <= 5 {
                    note("GetLiveViewImage: " + (error?.localizedDescription ?? PTP.describe(code)))
                }
                try? await Task.sleep(nanoseconds: 50_000_000)
                continue
            }
            latenciesMs.append(Date().timeIntervalSince(started) * 1000)

            let bytes = PTP.payload(data)
            guard let soi = findJPEGStart(bytes), soi >= 20 else { continue }
            let jpeg = Data(bytes[soi...])
            guard jpeg != lastJPEG else { continue }
            lastJPEG = jpeg
            distinct += 1

            let elapsed = Date().timeIntervalSince(start)
            let width = Int(bytes[16]) << 8 | Int(bytes[17])
            if width != areaWidth {
                areaWidth = width
                peak = 0
                log(String(format: "t%.1fs zoom shows %d sensor px across; peak reset", elapsed, width))
            }
            guard let image = decodeJPEG(jpeg) else { continue }
            let score = sharpness(image)
            pending.append(score)
            if score > peak {
                peak = score
                peakAt = elapsed
                try? jpeg.write(to: options.outputDirectory.appendingPathComponent("lv-peak-\(width).jpg"))
            }
            if elapsed >= nextReport {
                let mean = pending.reduce(0, +) / Double(pending.count)
                let fraction = peak > 0 ? mean / peak : 0
                note(String(format: "t%5.1fs  sharp %8.1f  peak %8.1f @%5.1fs  %3.0f%%  ",
                            elapsed, mean, peak, peakAt, fraction * 100)
                    + String(repeating: "█", count: Int(fraction * 30)))
                pending.removeAll()
                nextReport = elapsed + 0.5
            }
        }

        let sorted = latenciesMs.sorted()
        let medianMs = sorted.isEmpty ? 0 : sorted[sorted.count / 2]
        log(String(format: "focus test done: %d fetches, %d distinct frames in %.0fs (real refresh ~%.1f fps), "
                       + "median fetch %.1fms, %d failed fetches",
                   latenciesMs.count, distinct, seconds, Double(distinct) / seconds, medianMs, failures))
        note("sharpest frame per zoom saved as lv-peak-<sensor px across>.jpg in \(options.outputDirectory.path)")
    }

    private func findJPEGStart(_ bytes: [UInt8]) -> Int? {
        guard bytes.count >= 3 else { return nil }
        for index in 0...(bytes.count - 3)
        where bytes[index] == 0xFF && bytes[index + 1] == 0xD8 && bytes[index + 2] == 0xFF {
            return index
        }
        return nil
    }

    private func logPropValue(_ code: UInt16, _ name: String) async {
        let (value, response, error) = await ptp(PTP.getDevicePropValue, params: [UInt32(code)])
        if let error {
            note(String(format: "0x%04x %@: error %@", code, name, error.localizedDescription))
            return
        }
        guard PTP.responseCode(response) == PTP.responseOK else {
            note(String(format: "0x%04x %@: %@", code, name, PTP.describe(PTP.responseCode(response))))
            return
        }
        let bytes = PTP.payload(value)
        let raw = bytes.prefix(4).enumerated()
            .reduce(UInt32(0)) { $0 | UInt32($1.element) << (8 * $1.offset) }
        note(String(format: "0x%04x %@: raw 0x%08x (%u), bytes [%@]", code, name, raw, raw,
                    bytes.prefix(8).map { String(format: "%02x", $0) }.joined(separator: " ")))
    }

    /// Mirrors the 0xD10B decode in `runInfo`, generalised to hand back the
    /// allowed values instead of just logging them — the zoom sweep needs the
    /// list, not just a description of it.
    private func readPropDesc(_ code: UInt16) async -> (allowed: [UInt32], description: String) {
        let (desc, response, error) = await ptp(PTP.getDevicePropDesc, params: [UInt32(code)])
        guard error == nil, PTP.responseCode(response) == PTP.responseOK else {
            return ([], "GetDevicePropDesc 0x\(String(format: "%04x", code)): "
                + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
        }
        var reader = PTP.Reader(bytes: PTP.payload(desc))
        let widths: [UInt16: Int] = [1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4]
        guard reader.u16() != nil, let type = reader.u16(), let width = widths[type],
              let getSet = reader.u8()
        else {
            return ([], "could not decode the property description")
        }
        func value() -> UInt32? {
            switch width {
            case 1: reader.u8().map(UInt32.init)
            case 2: reader.u16().map(UInt32.init)
            default: reader.u32()
            }
        }
        let factory = value(), current = value()
        var allowed: [UInt32] = []
        var form = "no form"
        switch reader.u8() {
        case 1:
            if let low = value(), let high = value(), let step = value(), step > 0 {
                var next = low
                while next <= high {
                    allowed.append(next)
                    next += step
                }
                form = "range \(low)...\(high) step \(step)"
            }
        case 2:
            if let count = reader.u16() {
                for _ in 0..<count { if let entry = value() { allowed.append(entry) } }
                form = "allowed values \(allowed)"
            }
        default:
            break
        }
        let description = "\(getSet == 1 ? "settable" : "read-only"), factory "
            + "\(factory.map(String.init) ?? "?"), current \(current.map(String.init) ?? "?"), \(form)"
        return (allowed, description)
    }

    private func endLiveView() async {
        log("PTP → EndLiveView (0x9202)")
        let (_, response, error) = await ptp(PTP.nikonEndLiveView)
        log("PTP ← EndLiveView: "
            + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
        if let original = liveViewRestoreRecordingMedia {
            let (_, setResponse, setError) = await ptp(
                PTP.setDevicePropValue, params: [0xD10B],
                outData: PTP.dataOut(PTP.setDevicePropValue, payload: [original]))
            log("PTP ← SetDevicePropValue(RecordingMedia=\(original)) restore: "
                + (setError?.localizedDescription ?? PTP.describe(PTP.responseCode(setResponse))))
            liveViewRestoreRecordingMedia = nil
        }
    }

    private func startLiveView() async -> (code: UInt16?, error: Error?) {
        log("PTP → StartLiveView (0x9201)")
        let (_, response, error) = await ptp(PTP.nikonStartLiveView)
        let code = error == nil ? PTP.responseCode(response) : nil
        log("PTP ← StartLiveView: " + (error?.localizedDescription ?? PTP.describe(code)))
        return (code, error)
    }

    /// LiveViewProhibitCondition (0xD1A4) is a bitfield; the bit meanings are
    /// Nikon's and not decoded here, so list which bits are set.
    private func logProhibitBits() async {
        let (value, response, error) = await ptp(
            PTP.getDevicePropValue, params: [UInt32(PTP.propLiveViewProhibitCondition)])
        guard error == nil, PTP.responseCode(response) == PTP.responseOK else {
            note("LiveViewProhibitCondition: "
                + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
            return
        }
        let raw = PTP.payload(value).prefix(4).enumerated()
            .reduce(UInt32(0)) { $0 | UInt32($1.element) << (8 * $1.offset) }
        let bits = (0..<32).filter { raw & (1 << $0) != 0 }
        note(String(format: "LiveViewProhibitCondition: 0x%08x, bits set: ", raw)
            + (bits.isEmpty ? "none" : bits.map(String.init).joined(separator: ", ")))
    }

    private func runLiveView() async {
        log("before StartLiveView (why the camera might refuse):")
        await logPropValue(PTP.propLiveViewStatus, "LiveViewStatus")
        await logProhibitBits()
        await logPropValue(0xD10B, "RecordingMedia")

        var (startCode, startError) = await startLiveView()
        if startError == nil, startCode == 0xA004 {
            log("StartLiveView refused with Invalid status; prohibit condition now:")
            await logProhibitBits()
            // libgphoto2 points Nikon recording media at the buffer before live
            // view. Try that once, and put the card choice back on exit.
            let (media, mediaResponse, _) = await ptp(PTP.getDevicePropValue, params: [0xD10B])
            if PTP.responseCode(mediaResponse) == PTP.responseOK, let original = PTP.payload(media).first {
                log("retrying with RecordingMedia = 1 (buffer); was \(original)")
                let (_, setResponse, setError) = await ptp(
                    PTP.setDevicePropValue, params: [0xD10B],
                    outData: PTP.dataOut(PTP.setDevicePropValue, payload: [1]))
                let setCode = setError == nil ? PTP.responseCode(setResponse) : nil
                log("PTP ← SetDevicePropValue(RecordingMedia=1): "
                    + (setError?.localizedDescription ?? PTP.describe(setCode)))
                if setCode == PTP.responseOK {
                    if original != 1 { liveViewRestoreRecordingMedia = original }
                    (startCode, startError) = await startLiveView()
                }
            }
        }
        guard startError == nil, startCode == PTP.responseOK else {
            log("FAIL  StartLiveView did not return OK — aborting the sweep")
            await endLiveView()
            DispatchQueue.main.async { self.finish(code: 1) }
            return
        }

        log("polling DeviceReady until the post-StartLiveView busy period clears:")
        var lastReady: UInt16?
        let startupDeadline = Date().addingTimeInterval(10)
        while Date() < startupDeadline {
            let (_, ready, readyError) = await ptp(PTP.nikonDeviceReady)
            let code = readyError == nil ? PTP.responseCode(ready) : nil
            if code != lastReady || readyError != nil {
                note("DeviceReady: " + (readyError?.localizedDescription ?? PTP.describe(code)))
                lastReady = code
            }
            if code == PTP.responseOK { break }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }

        log("live view status properties (logged raw; a read failure does not abort):")
        await logPropValue(PTP.propLiveViewStatus, "LiveViewStatus")
        await logPropValue(PTP.propLiveViewProhibitCondition, "LiveViewProhibitCondition")

        log("zoom property description (GetDevicePropDesc 0xD1A3):")
        let (allowedFromCamera, zoomDescription) = await readPropDesc(PTP.propLiveViewImageZoomRatio)
        note(zoomDescription)
        var zoomLevels = allowedFromCamera.filter { $0 <= 255 }
        if zoomLevels.isEmpty {
            log("WARN  no usable zoom values from GetDevicePropDesc — falling back to 0...5")
            zoomLevels = Array(0...5)
        }

        if let point = options.lvPoint {
            log("PTP → ChangeAfArea(\(point.x), \(point.y))")
            let (_, response, error) = await ptp(PTP.nikonChangeAfArea, params: [point.x, point.y])
            log("PTP ← ChangeAfArea: "
                + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
        }

        // The Z f answers 0x200A for 0xD1A3, so zoom can't be driven over PTP.
        // Watch instead while the user zooms on the body.
        let (_, zoomRead, _) = await ptp(
            PTP.getDevicePropValue, params: [UInt32(PTP.propLiveViewImageZoomRatio)])
        if PTP.responseCode(zoomRead) == 0x200A {
            await watchManualZoom()
            await endLiveView()
            DispatchQueue.main.async { self.finish(code: 0) }
            return
        }

        var results: [ZoomResult] = []
        for zoom in zoomLevels {
            log("— zoom \(zoom) —")
            let outData = PTP.dataOut(PTP.setDevicePropValue, payload: [UInt8(zoom)])
            let (_, setResponse, setError) = await ptp(
                PTP.setDevicePropValue, params: [UInt32(PTP.propLiveViewImageZoomRatio)],
                outData: outData)
            let setCode = setError == nil ? PTP.responseCode(setResponse) : nil
            log("PTP ← SetDevicePropValue(zoom=\(zoom)): "
                + (setError?.localizedDescription ?? PTP.describe(setCode)))
            guard setError == nil, setCode == PTP.responseOK else {
                results.append(ZoomResult(
                    zoom: zoom, ok: false, width: 0, height: 0, jpegBytes: 0, headerLength: 0,
                    minMs: 0, medianMs: 0, maxMs: 0, fps: 0, note: "SetDevicePropValue failed"))
                continue
            }
            try? await Task.sleep(nanoseconds: 500_000_000)

            var latenciesMs: [Double] = []
            var firstJPEG: Data?
            var firstRaw: Data?
            var headerLength = 0
            var width = 0, height = 0
            var frameNote = ""

            for frameIndex in 0..<options.lvFrames {
                var frameData: Data?
                var attempt = 0
                while attempt < 5 {
                    attempt += 1
                    let started = Date()
                    let (data, response, error) = await ptp(PTP.nikonGetLiveViewImage)
                    let elapsedMs = Date().timeIntervalSince(started) * 1000
                    let code = error == nil ? PTP.responseCode(response) : nil
                    if let error {
                        frameNote = "frame \(frameIndex) failed: \(error.localizedDescription)"
                        break
                    }
                    if code == PTP.responseOK {
                        latenciesMs.append(elapsedMs)
                        frameData = data
                        break
                    }
                    if code == PTP.responseDeviceBusy || code == PTP.responseNotInLiveView {
                        try? await Task.sleep(nanoseconds: 50_000_000)
                        continue
                    }
                    frameNote = "frame \(frameIndex): \(PTP.describe(code))"
                    break
                }
                guard let frameData, frameIndex == 0 else { continue }

                let bytes = PTP.payload(frameData)
                firstRaw = Data(bytes)
                guard let soi = findJPEGStart(bytes) else {
                    frameNote = "no JPEG SOI (FF D8 FF) found in \(bytes.count)-byte payload"
                    continue
                }
                headerLength = soi
                let jpegData = Data(bytes[soi...])
                firstJPEG = jpegData
                let headerHexLength = min(64, soi)
                note("header length \(headerLength) bytes, first \(headerHexLength) hex: "
                    + bytes.prefix(headerHexLength).map { String(format: "%02x", $0) }
                        .joined(separator: " "))
                if let source = CGImageSourceCreateWithData(jpegData as CFData, nil),
                   let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
                       as? [CFString: Any],
                   let decodedWidth = properties[kCGImagePropertyPixelWidth] as? Int,
                   let decodedHeight = properties[kCGImagePropertyPixelHeight] as? Int {
                    width = decodedWidth
                    height = decodedHeight
                } else {
                    frameNote = "could not decode JPEG dimensions via ImageIO"
                }
            }

            if let firstJPEG {
                let jpegPath = options.outputDirectory
                    .appendingPathComponent("lv-zoom\(zoom)-frame0.jpg")
                do {
                    try firstJPEG.write(to: jpegPath)
                    note("wrote \(jpegPath.lastPathComponent) (\(firstJPEG.count) bytes)")
                } catch {
                    note("writing \(jpegPath.path) failed: \(error.localizedDescription)")
                }
            }
            if let firstRaw {
                let rawPath = options.outputDirectory
                    .appendingPathComponent("lv-zoom\(zoom)-frame0.bin")
                try? firstRaw.write(to: rawPath)
            }

            let sorted = latenciesMs.sorted()
            let minMs = sorted.first ?? 0
            let maxMs = sorted.last ?? 0
            let medianMs = sorted.isEmpty ? 0 : sorted[sorted.count / 2]
            let totalSeconds = latenciesMs.reduce(0, +) / 1000
            let fps = totalSeconds > 0 ? Double(latenciesMs.count) / totalSeconds : 0
            let widthRatio = width > 0 ? Double(width) / Double(Probe.sensorWidth) : 0

            log(String(
                format: "%d/%d frames; %dx%d, %@ JPEG; latency min/median/max %.1f/%.1f/%.1fms; "
                    + "~%.1f fps; frame width vs full sensor width %d: %.3f (the header's "
                    + "displayed-area fraction is unknown — decode the header hex to confirm)",
                latenciesMs.count, options.lvFrames, width, height,
                byteCount(off_t(firstJPEG?.count ?? 0)), minMs, medianMs, maxMs, fps,
                Probe.sensorWidth, widthRatio))
            if !frameNote.isEmpty { note(frameNote) }

            results.append(ZoomResult(
                zoom: zoom, ok: !latenciesMs.isEmpty, width: width, height: height,
                jpegBytes: firstJPEG?.count ?? 0, headerLength: headerLength, minMs: minMs,
                medianMs: medianMs, maxMs: maxMs, fps: fps, note: frameNote))
        }

        log(String(format: "summary (sensor is %d×%d):",
                   Probe.sensorWidth, Probe.sensorHeight))
        note("  zoom   size          jpegKB  hdrLen  minMs  medMs  maxMs   fps")
        for result in results {
            let size = result.ok ? "\(result.width)x\(result.height)" : "-"
            let line = String(
                format: "  %4d   %-12@ %7.1f %7d %6.1f %6.1f %6.1f %6.1f",
                result.zoom, size, Double(result.jpegBytes) / 1000, result.headerLength,
                result.minMs, result.medianMs, result.maxMs, result.fps)
            note(line + (result.ok ? "" : "  FAILED: \(result.note)"))
        }

        await endLiveView()
        DispatchQueue.main.async { self.finish(code: 0) }
    }

    // MARK: Release mode

    private func objectCount() async -> UInt32? {
        let (_, response, error) = await ptp(PTP.getNumObjects, params: [0xFFFF_FFFF, 0, 0])
        let code = PTP.responseCode(response)
        guard error == nil, code == PTP.responseOK else {
            log("  GetNumObjects: \(error?.localizedDescription ?? PTP.describe(code))")
            return nil
        }
        return PTP.responseParameter(response)
    }

    private func fetch(_ handle: UInt32, releasedAt: Date?) async {
        let (infoData, infoResponse, infoError) = await ptp(PTP.getObjectInfo, params: [handle])
        guard infoError == nil, PTP.responseCode(infoResponse) == PTP.responseOK else {
            log(String(format: "  GetObjectInfo 0x%08x: %@", handle,
                       infoError?.localizedDescription ?? PTP.describe(PTP.responseCode(infoResponse))))
            return
        }

        // ObjectInfo dataset (ISO 15740 §5.5.2), read up to the capture date.
        var reader = PTP.Reader(bytes: PTP.payload(infoData))
        guard let storage = reader.u32(), let format = reader.u16(), reader.u16() != nil,
              let size = reader.u32()
        else {
            log("  could not decode ObjectInfo for handle \(handle)")
            return
        }
        _ = (reader.u16(), reader.u32(), reader.u32(), reader.u32())   // thumbnail
        let width = reader.u32() ?? 0, height = reader.u32() ?? 0
        _ = reader.u32()   // bit depth: 0 for a NEF
        _ = (reader.u32(), reader.u16(), reader.u32(), reader.u32())   // parent, association, sequence
        let filename = reader.string() ?? String(format: "object-%08x", handle)
        let captured = reader.string() ?? "?"
        log(String(format: "  object 0x%08x on storage 0x%08x: %@ — format 0x%04x, %@, %u×%u, captured %@",
                   handle, storage, filename, format, byteCount(off_t(size)), width, height, captured))

        let inCatalog = await MainActor.run {
            (self.camera?.mediaFiles ?? []).contains { $0.name == filename }
        }
        note(inCatalog
            ? "ImageCaptureCore's catalog has it — requestDownloadFile would also reach it"
            : "not in ImageCaptureCore's catalog — GetObject is the only way to it")

        let started = Date()
        let (objectData, objectResponse, objectError) = await ptp(PTP.getObject, params: [handle])
        let elapsed = Date().timeIntervalSince(started)
        guard objectError == nil, PTP.responseCode(objectResponse) == PTP.responseOK else {
            log("  GetObject \(filename) failed: "
                + (objectError?.localizedDescription ?? PTP.describe(PTP.responseCode(objectResponse))))
            return
        }

        let bytes = PTP.payload(objectData)
        // Prefixed by handle, the only thing unique here: a backup slot writes the
        // same filename twice, and every buffer frame is DSC_0000.NEF on storage 0.
        let destination = options.outputDirectory
            .appendingPathComponent(String(format: "%08x-%@", handle, filename))
        do {
            try Data(bytes).write(to: destination)
        } catch {
            log("  writing \(destination.path) failed: \(error.localizedDescription)")
            return
        }
        await MainActor.run { self.objectsFetched += 1 }

        let megabytes = Double(bytes.count) / 1_000_000
        log(String(format: "  GetObject %@: %@ in %.3fs (%.1f MB/s)%@", filename,
                   byteCount(off_t(bytes.count)), elapsed, elapsed > 0 ? megabytes / elapsed : 0,
                   bytes.count == Int(size) ? "" : " — SIZE MISMATCH against ObjectInfo"))
        if let releasedAt {
            log(String(format: "  release → file on disk: %.3fs", Date().timeIntervalSince(releasedAt)))
        }
    }

    private func listStorages() async {
        let (data, response, error) = await ptp(PTP.getStorageIDs)
        guard error == nil, PTP.responseCode(response) == PTP.responseOK else {
            log("GetStorageIDs: "
                + (error?.localizedDescription ?? PTP.describe(PTP.responseCode(response))))
            return
        }
        var reader = PTP.Reader(bytes: PTP.payload(data))
        guard let count = reader.u32() else { return }
        log("storages: \(count)")
        for _ in 0..<count {
            guard let id = reader.u32() else { break }
            await MainActor.run { self.storageIDs.append(id) }
            let (info, infoResponse, infoError) = await ptp(PTP.getStorageInfo, params: [id])
            guard infoError == nil, PTP.responseCode(infoResponse) == PTP.responseOK else {
                note(String(format: "  0x%08x: %@", id, infoError?.localizedDescription
                    ?? PTP.describe(PTP.responseCode(infoResponse))))
                continue
            }
            var infoReader = PTP.Reader(bytes: PTP.payload(info))
            _ = (infoReader.u16(), infoReader.u16(), infoReader.u16())   // type, filesystem, access
            let capacity = infoReader.u64() ?? 0, free = infoReader.u64() ?? 0
            _ = infoReader.u32()   // free space in images
            let description = infoReader.string() ?? "", label = infoReader.string() ?? ""
            note(String(format: "  0x%08x: %@ %@ — %@ free of %@", id, description, label,
                        byteCount(off_t(free)), byteCount(off_t(capacity))))
        }
    }

    /// Nikon's queue holds every event since the camera was switched on —
    /// ObjectAdded for frames shot in earlier sessions included — until it is
    /// read. Drain it before firing, or that history is taken for the new frame.
    private func drainNikonEvents() async -> [UInt32] {
        var drained = 0
        var bufferHandles: [UInt32] = []
        var seenHandles: Set<UInt32> = []
        for _ in 0..<10 {
            let (data, response, error) = await ptp(PTP.nikonCheckEvent)
            guard error == nil, PTP.responseCode(response) == PTP.responseOK else { break }
            var reader = PTP.Reader(bytes: PTP.payload(data))
            guard let count = reader.u16(), count > 0 else { break }
            for _ in 0..<count {
                guard let code = reader.u16(), let param = reader.u32() else { break }
                drained += 1
                if code == 0x4002 || code == 0xC101 { seenHandles.insert(param) }
                if code == 0xC101 { bufferHandles.append(param) }
            }
        }
        let toIgnore = seenHandles
        await MainActor.run { self.ignoredHandles.formUnion(toIgnore) }
        log("drained \(drained) queued event(s)" + (bufferHandles.isEmpty ? ""
            : "; frames waiting in the buffer: "
                + bufferHandles.map { String(format: "0x%08x", $0) }.joined(separator: ", ")))
        return bufferHandles
    }

    /// Nikon's own event queue. Some bodies hold buffer-capture events here
    /// instead of pushing them through the interrupt pipe.
    @discardableResult
    private func pollNikonEvents(reportEmpty: Bool = false) async -> Int {
        let (data, response, error) = await ptp(PTP.nikonCheckEvent)
        let code = PTP.responseCode(response)
        guard error == nil, code == PTP.responseOK else {
            if reportEmpty { note("CheckEvent: " + (error?.localizedDescription ?? PTP.describe(code))) }
            return 0
        }
        var reader = PTP.Reader(bytes: PTP.payload(data))
        guard let count = reader.u16(), count > 0 else {
            if reportEmpty { note("CheckEvent: no events queued") }
            return 0
        }
        var seen = 0
        for _ in 0..<count {
            guard let eventCode = reader.u16(), let param = reader.u32() else { break }
            seen += 1
            await MainActor.run {
                self.handleEvent(code: eventCode, params: [param], source: "CheckEvent")
            }
        }
        return seen
    }

    /// The Z f numbers buffer frames 0x0B000001, 0x0B000002, … and hides them from
    /// GetObjectHandles, so the only way to see what the buffer holds is to ask
    /// for each handle in turn.
    private func bufferedHandles() async -> [UInt32] {
        var found: [UInt32] = []
        for handle in UInt32(0x0B00_0001)...0x0B00_0010 {
            let (_, info, error) = await ptp(PTP.getObjectInfo, params: [handle])
            if error == nil, PTP.responseCode(info) == PTP.responseOK { found.append(handle) }
        }
        return found
    }

    private func slotCounts() async -> [UInt32: UInt32] {
        var counts: [UInt32: UInt32] = [:]
        for id in await MainActor.run(body: { self.storageIDs }) {
            let (_, response, error) = await ptp(PTP.getNumObjects, params: [id, 0, 0])
            if error == nil, PTP.responseCode(response) == PTP.responseOK,
               let count = PTP.responseParameter(response) {
                counts[id] = count
            }
        }
        return counts
    }

    private func reportBufferOutcome(
        handles: [UInt32], slotsBefore: [UInt32: UInt32], fetchedBefore: Int
    ) async {
        // Leave room for a card write that was going to happen anyway.
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        let slotsAfter = await slotCounts()
        for id in slotsBefore.keys.sorted() {
            note(String(format: "slot 0x%08x: %@ → %@ objects", id, String(slotsBefore[id] ?? 0),
                        slotsAfter[id].map(String.init) ?? "?"))
        }

        let fetched = await MainActor.run { self.objectsFetched } - fetchedBefore
        let cardsUnchanged = !slotsBefore.isEmpty && slotsBefore == slotsAfter
        switch (fetched > 0, cardsUnchanged) {
        case (true, true):
            log("PASS  straight to disk: \(fetched) file(s) fetched from the camera's buffer, "
                + "nothing written to either card")
        case (true, false):
            log("WARN  fetched from the buffer, but the cards changed too — it wrote there anyway")
        case (false, false):
            log("FAIL  the frame went to the cards, not the buffer")
        case (false, true):
            log("FAIL  nothing fetched and nothing written — the release produced no frame")
        }

        // Does the buffer let go once the frame is downloaded? If it still
        // answers, the capture module has to clear it (Nikon DelImageSDRAM,
        // 0x90C3) before the next shot.
        guard fetched > 0 else {
            note("buffer release not checked: nothing was fetched, so an empty buffer proves nothing")
            return
        }
        for handle in handles {
            let (_, info, error) = await ptp(PTP.getObjectInfo, params: [handle])
            let code = error == nil ? PTP.responseCode(info) : nil
            if code == PTP.responseOK {
                log(String(format: "WARN  the buffer still holds 0x%08x after download — it must be cleared",
                           handle))
            } else {
                log(String(format: "PASS  the buffer let go of 0x%08x after download (%@)", handle,
                           error?.localizedDescription ?? PTP.describe(code)))
            }
        }
    }

    private func runReleaseSequence() async {
        await listStorages()

        _ = await drainNikonEvents()

        // Anything earlier shots left in the buffer: fetch it, then ask again —
        // whether a download clears the buffer decides if the capture module has
        // to delete each frame itself.
        let leftovers = await bufferedHandles()
        if leftovers.isEmpty {
            log("camera buffer is empty")
        } else {
            log("camera buffer holds: "
                + leftovers.map { String(format: "0x%08x", $0) }.joined(separator: ", "))
            for handle in leftovers {
                await fetch(handle, releasedAt: nil)
                let (_, info, error) = await ptp(PTP.getObjectInfo, params: [handle])
                let code = error == nil ? PTP.responseCode(info) : nil
                log(String(format: "  after download, 0x%08x is ", handle)
                    + (code == PTP.responseOK ? "STILL in the buffer" : "gone (\(PTP.describe(code)))"))
            }
        }
        for method in options.releaseMethods {
            _ = await drainNikonEvents()
            let bufferBefore = Set(await bufferedHandles())
            let before = await objectCount()
            let slotsBefore = await slotCounts()
            let fetchedBefore = await MainActor.run { self.objectsFetched }
            log("next: \(method.label) — firing in 5s, watch the camera")
            note("objects on card before: \(before.map(String.init) ?? "unknown")")
            try? await Task.sleep(nanoseconds: 5_000_000_000)

            await MainActor.run {
                self.itemsThisShot = 0
                self.shutterFiredAt = Date()
                self.shotsFired += 1
                self.addedHandles = []
                self.captureCompleted = false
                self.iccArrivalsThisShot = 0
            }
            let sentAt = Date()
            log("shot \(shotsFired): \(method.label)")
            let (_, response, error) = await ptp(method.opcode, params: method.params)
            if let error {
                log("  release failed: \(error.localizedDescription)")
            } else {
                log(String(format: "  release answered in %.3fs: %@",
                           Date().timeIntervalSince(sentAt), PTP.describe(PTP.responseCode(response))))
            }

            // Nikon bodies answer DeviceReady with busy (0x2019) while a capture
            // is in progress. Watching it flip back to OK is evidence that
            // consumes none of the events ImageCaptureCore might be waiting on.
            var lastReady: UInt16?
            let readyDeadline = Date().addingTimeInterval(10)
            while Date() < readyDeadline {
                let (_, ready, readyError) = await ptp(PTP.nikonDeviceReady)
                let code = readyError == nil ? PTP.responseCode(ready) : nil
                if code != lastReady || readyError != nil {
                    log(String(format: "  DeviceReady: %@ (%.3fs after release)",
                               readyError?.localizedDescription ?? PTP.describe(code),
                               Date().timeIntervalSince(sentAt)))
                    lastReady = code
                }
                if code == PTP.responseOK, Date().timeIntervalSince(sentAt) > 1 { break }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }

            // Nikon pushes one ObjectAdded per file written, then CaptureComplete.
            // ImageCaptureCore does not turn these into didAdd callbacks, so
            // the handles are the capture module's own to follow.
            let completeDeadline = Date().addingTimeInterval(method.recordsToCard ? 8 : 30)
            while Date() < completeDeadline {
                if method.recordsToCard {
                    if await MainActor.run(body: { self.captureCompleted }) { break }
                } else {
                    // The Z f queues ObjectAddedInSdram rather than pushing it, sends
                    // no CaptureComplete, and on one of two test shots sent no
                    // ObjectAddedInSdram at all. A new handle in the buffer is the
                    // signal that doesn't depend on the event arriving.
                    await pollNikonEvents()
                    let fresh = Set(await bufferedHandles()).subtracting(bufferBefore).sorted()
                    if !fresh.isEmpty {
                        log(String(format: "  handle scan found %@ (%.3fs after release)",
                                   fresh.map { String(format: "0x%08x", $0) }.joined(separator: ", "),
                                   Date().timeIntervalSince(sentAt)))
                        await MainActor.run {
                            for handle in fresh where !self.addedHandles.contains(handle) {
                                self.addedHandles.append(handle)
                            }
                        }
                    }
                    if await MainActor.run(body: { !self.addedHandles.isEmpty }) { break }
                }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }

            if method.recordsToCard {
                let after = await objectCount()
                note("objects on card after:  \(after.map(String.init) ?? "unknown")")
                if let before, let after, after > before {
                    log("PASS  \(method.label) FIRED — the card gained \(after - before) object(s)")
                } else if before != nil, after != nil {
                    log("FAIL  \(method.label) did not fire — the card's object count is unchanged")
                } else {
                    log("????  object count unreadable; rely on what you saw")
                }
            }
            let handles = await MainActor.run { self.addedHandles }
            log("ObjectAdded handles: " + (handles.isEmpty
                ? "none" : handles.map { String(format: "0x%08x", $0) }.joined(separator: ", ")))
            for handle in handles {
                await fetch(handle, releasedAt: sentAt)
            }
            if !method.recordsToCard {
                await reportBufferOutcome(
                    handles: handles, slotsBefore: slotsBefore, fetchedBefore: fetchedBefore)
            }

            // Whether didAdd fires at all decides if the capture module can take
            // arrivals from ImageCaptureCore or must follow the PTP event stream.
            let iccDeadline = Date().addingTimeInterval(5)
            while Date() < iccDeadline {
                if await MainActor.run(body: { self.iccArrivalsThisShot > 0 }) { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            let iccArrivals = await MainActor.run { self.iccArrivalsThisShot }
            log(iccArrivals > 0
                ? "PASS  ImageCaptureCore's didAdd reported \(iccArrivals) item(s) for this shot"
                : "WARN  ImageCaptureCore's didAdd reported nothing for this shot, unfiltered")
        }
        await MainActor.run { self.finish(code: 0) }
    }

    // MARK: Arrival and download

    fileprivate func arrived(_ items: [ICCameraItem]) {
        let now = Date()
        arrivalTimer?.invalidate()

        // An unfiltered record of every post-catalog didAdd. The filter below
        // used to drop these silently, which made "ImageCaptureCore delivered
        // nothing" unprovable.
        if catalogComplete {
            iccArrivalsThisShot += items.count
            for item in items {
                log("ICC didAdd: \(item.name ?? "?") "
                    + "(wasAddedAfterContentCatalogCompleted = \(item.wasAddedAfterContentCatalogCompleted))")
            }
        }
        // Release mode fetches over PTP; an ImageCaptureCore download of the
        // same file would race it to the same path.
        guard options.releaseMethods.isEmpty else { return }

        // ICCameraDevice reports every pre-existing file on the card through
        // this same delegate method while it builds its content catalog, so an
        // arrival only means a capture once the catalog is complete.
        // `wasAddedAfterContentCatalogCompleted` looks like the discriminator for
        // this and is not one: on the Z f it reads false for a frame shot a
        // second earlier, long after the catalog finished. Filtering on it drops
        // every real capture.
        guard catalogComplete else {
            catalogItemsSeen += items.count
            if catalogItemsSeen % 200 == 0 || catalogItemsSeen < 2 {
                log("cataloguing existing card contents… \(catalogItemsSeen) files so far")
            }
            return
        }

        for item in items {
            itemsThisShot += 1
            guard let file = item as? ICCameraFile else {
                log("  item \(itemsThisShot): '\(item.name ?? "?")' is a folder, ignoring")
                continue
            }

            let latencyText = shutterFiredAt
                .map { String(format: "%.3fs after shutter", now.timeIntervalSince($0)) }
                ?? "no shutter request outstanding — fired from the camera body?"
            log("  item \(itemsThisShot): \(file.name ?? "?") "
                + "(\(byteCount(file.fileSize)), \(file.uti ?? "unknown UTI")) — \(latencyText)")

            if itemsThisShot > 1 {
                note("more than one item for this shot — RAW+JPEG, or the camera is")
                note("writing to card AND host. Worth knowing before the capture plan")
                note("assumes one file per cell of the grid.")
            }

            download(file)
        }
    }

    private func download(_ file: ICCameraFile) {
        guard let camera, let name = file.name else { return }
        downloadStartedAt[name] = Date()
        inFlightDownloads += 1

        camera.requestDownloadFile(
            file,
            options: [
                .downloadsDirectoryURL: options.outputDirectory,
                .overwrite: true,
            ],
            downloadDelegate: self,
            didDownloadSelector: #selector(didDownloadFile(_:error:options:contextInfo:)),
            contextInfo: nil)
    }

    // MARK: Driving

    private func prompt() {
        guard !shuttingDown, !options.infoOnly, !options.liveView else { return }

        if !options.releaseMethods.isEmpty {
            guard !releaseSequenceStarted else { return }
            releaseSequenceStarted = true
            Task { await self.runReleaseSequence() }
            return
        }

        if options.autoShots > 0 {
            if shotsFired < options.autoShots {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                    self?.fire()
                }
            } else {
                log("\(shotsFired) shots fired, \(shotsDownloaded) complete — done")
                finish(code: 0)
            }
            return
        }

        note("")
        note("Return = requestTakePicture · p = PTP capture · i = PTP info · q = quit")
        note("(You can also just press the camera's own shutter button — arrivals")
        note(" are timed however they were triggered.)")
        fflush(stdout)
    }

    func readStandardInput() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine(strippingNewline: true) {
                let command = line.trimmingCharacters(in: .whitespaces).lowercased()
                DispatchQueue.main.async {
                    guard let self else { return }
                    switch command {
                    case "q", "quit": self.finish(code: 0)
                    case "p": self.firePTP()
                    case "i": self.sendPTP(PTP.getDeviceInfo, label: "GetDeviceInfo")
                    default: self.fire()
                    }
                }
            }
            DispatchQueue.main.async { self?.finish(code: 0) }
        }
    }

    func finish(code: Int32) {
        guard !shuttingDown else { return }
        shuttingDown = true
        arrivalTimer?.invalidate()
        discoveryTimer?.invalidate()
        summarise()

        if let camera {
            camera.ptpEventHandler = { _ in }
            camera.requestCloseSession()
            // Give the close a moment to land before tearing the process down.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exit(code) }
        } else {
            exit(code)
        }
    }

    private func summarise() {
        note("")
        log("summary")
        note("camera seen:            \(camera != nil ? "yes" : "no")")
        note("transport:              \(camera?.transportType ?? "n/a")")
        note("content catalog:        \(catalogComplete ? "complete" : "INCOMPLETE")")
        note("PTP replies received:   \(ptpRepliesReceived)")
        note("canTakePicture:         \(advertisesTakePicture ? "advertised" : "not advertised")")
        note("canAcceptPTPCommands:   \(advertisesPTP ? "advertised" : "not advertised")")
        note("shots requested:        \(shotsFired)")
        note("shots fully downloaded: \(shotsDownloaded)")
        note("objects fetched by PTP: \(objectsFetched)")
        note("downloads in:           \(options.outputDirectory.path)")
    }

    private func byteCount(_ bytes: off_t) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

// MARK: - ICDeviceBrowserDelegate

extension Probe: ICDeviceBrowserDelegate {
    func deviceBrowser(_: ICDeviceBrowser, didAdd device: ICDevice, moreComing _: Bool) {
        adopt(device)
    }

    func deviceBrowser(_: ICDeviceBrowser, didRemove device: ICDevice, moreGoing _: Bool) {
        guard device === camera else { return }
        log("FAIL  the camera disappeared mid-session")
        note("This is the failure mode to design for. Shooting to card AND host")
        note("means a dropped tether never costs an exposure.")
        finish(code: 1)
    }
}

// MARK: - ICCameraDeviceDelegate

extension Probe: ICCameraDeviceDelegate {
    func didRemove(_: ICDevice) {}

    func device(_: ICDevice, didOpenSessionWithError error: Error?) {
        if let error {
            log("FAIL  could not open a session: \(error.localizedDescription)")
            note("A session that will not open usually means another process owns")
            note("the device (Photos, Image Capture, Nikon's own tether app), or")
            note("macOS has not granted this binary access to it.")
            finish(code: 1)
            return
        }
        sessionOpened()
    }

    func device(_: ICDevice, didCloseSessionWithError error: Error?) {
        log(error.map { "session closed with error: \($0.localizedDescription)" }
            ?? "session closed")
    }

    func deviceDidBecomeReady(withCompleteContentCatalog device: ICCameraDevice) {
        guard !catalogComplete else { return }
        catalogComplete = true
        log("content catalog complete: \(device.mediaFiles?.count ?? 0) files on the card")
        note("Note the time above. Until now a new shot is indistinguishable from")
        note("the card's history in didAdd. PTP ObjectAdded events never replay")
        note("history, so following those needs no wait at all.")
        prompt()
    }

    func cameraDevice(_: ICCameraDevice, didAdd items: [ICCameraItem]) {
        arrived(items)
    }

    func cameraDevice(_: ICCameraDevice, didRemove _: [ICCameraItem]) {}

    func cameraDevice(_: ICCameraDevice, didRenameItems _: [ICCameraItem]) {}

    func cameraDevice(_: ICCameraDevice, didCompleteDeleteFilesWithError _: Error?) {}

    func cameraDeviceDidChangeCapability(_ device: ICCameraDevice) {
        let nowAdvertised = device.capabilities.contains(ICDeviceCapability.cameraDeviceCanTakePicture.rawValue)
        guard nowAdvertised != advertisesTakePicture else { return }
        advertisesTakePicture = nowAdvertised
        log("capability change: canTakePicture is now "
            + (nowAdvertised ? "advertised" : "withdrawn"))
    }

    func cameraDevice(
        _: ICCameraDevice, didReceiveThumbnail _: CGImage?,
        for _: ICCameraItem, error _: Error?
    ) {}

    func cameraDevice(
        _: ICCameraDevice, didReceiveMetadata _: [AnyHashable: Any]?,
        for _: ICCameraItem, error _: Error?
    ) {}

    func cameraDevice(_: ICCameraDevice, didReceivePTPEvent _: Data) {
        // Never called while `ptpEventHandler` is set — the block wins — but
        // the protocol requires the method to exist.
    }

    func cameraDeviceDidRemoveAccessRestriction(_: ICDevice) {}

    func cameraDeviceDidEnableAccessRestriction(_: ICDevice) {}
}

// MARK: - ICCameraDeviceDownloadDelegate

extension Probe: ICCameraDeviceDownloadDelegate {
    @objc func didDownloadFile(
        _ file: ICCameraFile,
        error: Error?,
        options _: [String: Any],
        contextInfo _: UnsafeMutableRawPointer?
    ) {
        inFlightDownloads -= 1
        let name = file.name ?? "?"
        let elapsed = downloadStartedAt.removeValue(forKey: name)
            .map { Date().timeIntervalSince($0) } ?? 0

        if let error {
            log("FAIL  download of \(name) failed: \(error.localizedDescription)")
        } else {
            let megabytes = Double(file.fileSize) / 1_000_000
            log(String(format: "  downloaded %@ in %.3fs (%.1f MB/s)",
                       name, elapsed, elapsed > 0 ? megabytes / elapsed : 0))
            if let shutterFiredAt {
                log(String(format: "  shutter → file on disk: %.3fs",
                           Date().timeIntervalSince(shutterFiredAt)))
            }
        }

        guard inFlightDownloads == 0 else { return }
        shotsDownloaded += 1
        prompt()
    }

    func didReceiveDownloadProgress(
        for _: ICCameraFile, downloadedBytes _: off_t, maxBytes _: off_t
    ) {}
}

// MARK: - Main

let options = Options.parse(Array(CommandLine.arguments.dropFirst()))
let probe = Probe(options: options)

let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
interrupt.setEventHandler { probe.finish(code: 0) }
interrupt.resume()
signal(SIGINT, SIG_IGN)

probe.start()
if options.autoShots == 0, !options.infoOnly, !options.liveView, options.releaseMethods.isEmpty {
    probe.readStandardInput()
}
RunLoop.main.run()
