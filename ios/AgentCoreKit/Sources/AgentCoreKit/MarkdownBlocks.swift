import Foundation

/// Message markdown split into the blocks SwiftUI's `Text` can't lay out by
/// itself: headings, list items, quotes, rules and fenced code. Inline syntax
/// inside each block (bold, italics, code, links) is parsed by Foundation's
/// `AttributedString(markdown:)`, so no package is needed.
public enum MarkdownBlock: Sendable, Equatable {
    case paragraph(AttributedString)
    case heading(level: Int, AttributedString)
    /// `marker` is `•` for an unordered item or the item's own number (`3.`).
    case listItem(marker: String, depth: Int, AttributedString)
    case quote(AttributedString)
    case code(String)
    case rule

    public static func parse(_ markdown: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var code: [String]?

        func flushParagraph() {
            guard !paragraph.isEmpty else { return }
            blocks.append(.paragraph(inline(paragraph.joined(separator: "\n"))))
            paragraph = []
        }

        for line in markdown.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if var open = code {
                if trimmed.hasPrefix("```") {
                    blocks.append(.code(open.joined(separator: "\n")))
                    code = nil
                } else {
                    open.append(line)
                    code = open
                }
                continue
            }
            if trimmed.hasPrefix("```") {
                flushParagraph()
                code = []
                continue
            }
            if trimmed.isEmpty {
                flushParagraph()
                continue
            }
            if let heading = heading(trimmed) {
                flushParagraph()
                blocks.append(heading)
            } else if isRule(trimmed) {
                flushParagraph()
                blocks.append(.rule)
            } else if let item = listItem(line) {
                flushParagraph()
                blocks.append(item)
            } else if trimmed.hasPrefix(">") {
                flushParagraph()
                blocks.append(.quote(inline(String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces))))
            } else {
                paragraph.append(line)
            }
        }
        flushParagraph()
        // An unclosed fence still shows what it held.
        if let open = code {
            blocks.append(.code(open.joined(separator: "\n")))
        }
        return blocks
    }

    /// Inline markdown, or the plain text when it doesn't parse.
    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible
        )
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }

    private static func heading(_ line: String) -> MarkdownBlock? {
        let hashes = line.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(hashes) else { return nil }
        let rest = line.dropFirst(hashes)
        guard rest.first == " " else { return nil }
        return .heading(level: hashes, inline(rest.trimmingCharacters(in: .whitespaces)))
    }

    private static func isRule(_ line: String) -> Bool {
        let compact = line.filter { !$0.isWhitespace }
        guard compact.count >= 3, let first = compact.first, "-*_".contains(first) else { return false }
        return compact.allSatisfy { $0 == first }
    }

    private static func listItem(_ line: String) -> MarkdownBlock? {
        let indent = line.prefix(while: { $0 == " " || $0 == "\t" })
        let depth = indent.reduce(0) { $0 + ($1 == "\t" ? 4 : 1) } / 2
        let body = line.dropFirst(indent.count)

        for bullet in ["- ", "* ", "+ "] where body.hasPrefix(bullet) {
            return .listItem(marker: "•", depth: depth, inline(String(body.dropFirst(2))))
        }
        let digits = body.prefix(while: \.isNumber)
        guard !digits.isEmpty, digits.count <= 3 else { return nil }
        let afterDigits = body.dropFirst(digits.count)
        guard afterDigits.hasPrefix(". ") || afterDigits.hasPrefix(") ") else { return nil }
        return .listItem(marker: "\(digits).", depth: depth, inline(String(afterDigits.dropFirst(2))))
    }
}
