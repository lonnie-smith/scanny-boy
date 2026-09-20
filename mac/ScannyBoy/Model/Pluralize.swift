import Foundation

/// User-visible counts: "1 negative", "14 negatives" — never "negative(s)".
enum Pluralize {
    static func count(_ count: Int, _ noun: String) -> String {
        "\(count) \(count == 1 ? noun : noun + "s")"
    }
}
