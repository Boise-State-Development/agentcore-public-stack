import Foundation
import Testing

@testable import AgentCoreKit

@Suite("MarkdownBlocks")
struct MarkdownBlocksTests {
    @Test("splits headings, paragraphs, lists, quotes, rules and code")
    func blocks() {
        let markdown = """
            # Summary

            First line
            second line

            - one
              - nested
            2. two

            > quoted

            ---

            ```swift
            let x = 1
            ```
            """
        #expect(
            MarkdownBlock.parse(markdown) == [
                .heading(level: 1, AttributedString("Summary")),
                .paragraph(AttributedString("First line\nsecond line")),
                .listItem(marker: "•", depth: 0, AttributedString("one")),
                .listItem(marker: "•", depth: 1, AttributedString("nested")),
                .listItem(marker: "2.", depth: 0, AttributedString("two")),
                .quote(AttributedString("quoted")),
                .rule,
                .code("let x = 1"),
            ])
    }

    @Test("inline markdown is parsed, so bold text isn't shown with its asterisks")
    func inline() throws {
        guard case .paragraph(let text) = try #require(MarkdownBlock.parse("**Two** things").first) else {
            Issue.record("expected a paragraph")
            return
        }
        #expect(String(text.characters) == "Two things")
        let bold = text.runs.first { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true }
        #expect(bold.map { String(text[$0.range].characters) } == "Two")
    }

    @Test("an unclosed fence still shows its code")
    func unclosedFence() {
        #expect(MarkdownBlock.parse("```\nprint(1)") == [.code("print(1)")])
    }

    @Test("a hash with no space is text, not a heading")
    func hashtag() {
        #expect(MarkdownBlock.parse("#general") == [.paragraph(AttributedString("#general"))])
    }

    @Test("empty text is no blocks")
    func empty() {
        #expect(MarkdownBlock.parse("  \n\n").isEmpty)
    }
}
