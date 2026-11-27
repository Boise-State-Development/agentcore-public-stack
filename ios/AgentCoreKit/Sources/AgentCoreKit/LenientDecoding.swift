import Foundation

/// A JSON array decoded one element at a time, where an element that does not
/// decode becomes `nil` instead of failing the whole array.
///
/// A deployment newer than the shipped app sends shapes the app has never
/// seen, and a shipped build can't be updated in place. One odd element must
/// cost that element, never the screen.
struct LenientList<Element: Decodable>: Decodable {
    let elements: [Element?]

    init(_ elements: [Element?] = []) {
        self.elements = elements
    }

    init(from decoder: any Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var elements: [Element?] = []
        while !container.isAtEnd {
            if let element = try? container.decode(Element.self) {
                elements.append(element)
            } else if (try? container.decode(SkippedValue.self)) != nil {
                // A failed decode does not move the container on; this does.
                elements.append(nil)
            } else {
                break
            }
        }
        self.elements = elements
    }
}

/// Decodes any JSON value and keeps nothing.
private struct SkippedValue: Decodable {
    init(from decoder: any Decoder) throws {}
}

extension KeyedDecodingContainer {
    /// The value at `key` if it is there and has the expected type, else `nil`.
    /// Never throws: a field the app reads is optional to the app.
    func lenient<T: Decodable>(_ type: T.Type, forKey key: Key) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }

    /// A lenient list at `key`, empty when it is missing or not an array.
    func lenientList<T: Decodable>(_ type: T.Type, forKey key: Key) -> LenientList<T> {
        lenient(LenientList<T>.self, forKey: key) ?? LenientList()
    }
}

/// Server timestamps, which come in more than one ISO 8601 spelling.
///
/// app-api writes `2026-11-27T05:09:55.853557+00:00`, older rows carry the
/// invalid `+00:00Z` (see `apis/shared/timestamps.py`), and some values are
/// plain `Z`. The SPA normalizes the same set on read (`normalizeIsoTimestamp`).
enum ServerTimestamp {
    static func parse(_ raw: String?) -> Date? {
        guard var value = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        if value.hasSuffix("+00:00Z") {
            value.removeLast()
        }
        // A naive timestamp is UTC: the server never writes local time.
        if let time = value.split(separator: "T", maxSplits: 1).last,
            !time.contains("Z"), !time.contains("+"), !time.contains("-")
        {
            value += "Z"
        }
        return (try? Date(value, strategy: Date.ISO8601FormatStyle(includingFractionalSeconds: true)))
            ?? (try? Date(value, strategy: Date.ISO8601FormatStyle()))
    }
}
