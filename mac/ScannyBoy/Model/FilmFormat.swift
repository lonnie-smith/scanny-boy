import Foundation

/// The roll's film format, saved via `roll set-setup`. A stored choice
/// only — nothing reads it yet.
enum FilmFormat: String, Sendable, Hashable, CaseIterable, Identifiable {
    case halfFrame = "half-frame"
    case f35mm = "35mm"
    case sixByThree = "6x3"
    case f645 = "645"
    case sixBySix = "6x6"
    case sixBySeven = "6x7"
    case xpan = "xpan"
    case sixByNine = "6x9"
    case sixByTwelve = "6x12"
    case sixBySeventeen = "6x17"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .halfFrame: "Half Frame"
        case .f35mm: "35mm"
        case .sixByThree: "6×3"
        case .f645: "645"
        case .sixBySix: "6×6"
        case .sixBySeven: "6×7"
        case .xpan: "XPan"
        case .sixByNine: "6×9"
        case .sixByTwelve: "6×12"
        case .sixBySeventeen: "6×17"
        }
    }
}
