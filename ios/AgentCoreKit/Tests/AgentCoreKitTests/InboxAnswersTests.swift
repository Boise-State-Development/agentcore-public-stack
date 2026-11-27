import Foundation
import Testing

@testable import AgentCoreKit

@Suite("InboxAnswers")
struct InboxAnswersTests {
    private static let term = Question(
        header: "Term", question: "Which term?", options: [.init(label: "Spring"), .init(label: "Fall")])
    private static let topics = Question(
        header: "Topics", question: "Which topics?", multiSelect: true,
        options: [.init(label: "Budget"), .init(label: "Travel")])
    private static let questions = InboxPrompt(id: "q", kind: .questions([term, topics]))

    private static let form = ElicitationForm(
        serverName: "Example Tracker", message: nil,
        fields: [
            FormField(
                name: "title", title: "Title", required: true, kind: .text(format: nil, minLength: 3, maxLength: 10)),
            FormField(
                name: "points", title: "Points", defaultValue: .number(3), kind: .integer(minimum: 1, maximum: 13)),
            FormField(name: "urgent", title: "Urgent", kind: .boolean),
            FormField(
                name: "labels", title: "Labels",
                kind: .choice(
                    options: [.init(value: "bug", label: "Bug"), .init(value: "docs", label: "Docs")],
                    multiSelect: true, minItems: nil, maxItems: 1)),
            FormField(
                name: "team", title: "Team", required: true, defaultValue: .string("web"),
                kind: .choice(
                    options: [.init(value: "web", label: "Web"), .init(value: "data", label: "Data")],
                    multiSelect: false, minItems: nil, maxItems: nil)),
        ])
    private static let formPrompt = InboxPrompt(id: "f", kind: .form(form))

    // MARK: - Approvals

    @Test("an approval sends approved or declined, and nothing until chosen")
    func approval() {
        let prompt = InboxPrompt(id: "a", kind: .toolApproval(toolName: "send_email", message: nil, toolInput: nil))
        #expect(prompt.response(from: .approval(nil)) == nil)
        #expect(prompt.response(from: .approval(true)) == .string("approved"))
        #expect(prompt.response(from: .approval(false)) == .string("declined"))
    }

    // MARK: - Questions

    @Test("answered questions go out keyed by header, picks and Other text together")
    func questionAnswers() {
        var draft = QuestionsDraft()
        #expect(Self.questions.response(from: .questions(draft)) == nil)

        draft.toggle("Fall", for: Self.term)
        draft.toggle("Spring", for: Self.term)  // one at a time: replaces Fall
        draft.toggle("Budget", for: Self.topics)
        draft.toggle("Travel", for: Self.topics)  // several allowed
        draft.otherText["Topics"] = "  Lodging  "

        #expect(
            Self.questions.response(from: .questions(draft))
                == .object([
                    "answers": .object([
                        "Term": .object(["selected": .array([.string("Spring")])]),
                        "Topics": .object([
                            "selected": .array([.string("Budget"), .string("Travel")]),
                            "text": .string("Lodging"),
                        ]),
                    ])
                ]))
    }

    @Test("a question left blank is left out, so the server skips it")
    func partialAnswers() {
        var draft = QuestionsDraft()
        draft.toggle("Fall", for: Self.term)
        #expect(
            Self.questions.response(from: .questions(draft))
                == .object(["answers": .object(["Term": .object(["selected": .array([.string("Fall")])])])]))
    }

    @Test("picking again unpicks")
    func unpick() {
        var draft = QuestionsDraft()
        draft.toggle("Fall", for: Self.term)
        draft.toggle("Fall", for: Self.term)
        #expect(draft.isPicked("Fall", for: Self.term) == false)
    }

    @Test("skipping sends {skipped: true}, never null")
    func skip() {
        var draft = QuestionsDraft()
        draft.skipped = true
        #expect(Self.questions.response(from: .questions(draft)) == .object(["skipped": .bool(true)]))
    }

    // MARK: - Forms

    @Test("a form starts from its defaults")
    func defaults() {
        let draft = FormDraft(form: Self.form)
        #expect(draft.text(Self.form.fields[1]) == "3")
        #expect(draft.choices(Self.form.fields[4]) == ["web"])
        #expect(draft.bool(Self.form.fields[2]) == false)
    }

    @Test("a filled form is sent with its values typed, and blank optional fields left out")
    func filledForm() {
        var draft = FormDraft(form: Self.form)
        draft.values["title"] = .text("New issue")
        draft.values["urgent"] = .bool(true)
        draft.toggle("docs", in: Self.form.fields[3])

        #expect(draft.problems(in: Self.form).isEmpty)
        #expect(
            Self.formPrompt.response(from: .form(draft))
                == .object([
                    "action": .string("accept"),
                    "content": .object([
                        "title": .string("New issue"),
                        "points": .number(3),
                        "urgent": .bool(true),
                        "labels": .array([.string("docs")]),
                        "team": .string("web"),
                    ]),
                ]))
    }

    @Test(
        "each rule the server checks is checked first",
        arguments: [
            ("title", FormDraft.Value.text(""), FieldProblem.required),
            ("title", .text("ab"), .tooShort(3)),
            ("title", .text("far too long a title"), .tooLong(10)),
            ("points", .text("three"), .notANumber),
            ("points", .text("2.5"), .notAWholeNumber),
            ("points", .text("0"), .belowMinimum(1)),
            ("points", .text("21"), .aboveMaximum(13)),
            ("labels", .choices(["bug", "docs"]), .tooManyChoices(1)),
            ("team", .choices([]), .required),
        ])
    func rules(field: String, value: FormDraft.Value, problem: FieldProblem) {
        var draft = FormDraft(form: Self.form)
        draft.values["title"] = .text("New issue")
        draft.values[field] = value
        #expect(draft.problems(in: Self.form) == [field: problem])
        #expect(Self.formPrompt.response(from: .form(draft)) == nil)
    }

    @Test("declining sends only the action, whatever the fields hold")
    func decline() {
        var draft = FormDraft(form: Self.form)
        draft.declined = true
        #expect(Self.formPrompt.response(from: .form(draft)) == .object(["action": .string("decline")]))
    }

    @Test("a form with a field this build can't show can only be declined")
    func unsupportedForm() throws {
        let json = #"{"fields": [{"name": "a", "kind": "text", "title": "A"}, {"name": "b", "kind": "signature"}]}"#
        let form = try JSONDecoder().decode(ElicitationForm.self, from: Data(json.utf8))
        let prompt = InboxPrompt(id: "f", kind: .form(form))
        var draft = FormDraft(form: form)
        draft.values["a"] = .text("x")
        #expect(prompt.response(from: .form(draft)) == nil)
        draft.declined = true
        #expect(prompt.response(from: .form(draft)) == .object(["action": .string("decline")]))
    }

    @Test("a whole number goes out as an integer, not 3.0")
    func integerEncoding() throws {
        let data = try JSONEncoder().encode(JSONValue.object(["points": .number(3), "ratio": .number(0.5)]))
        let text = String(decoding: data, as: UTF8.self)
        #expect(text.contains(#""points":3"#))
        #expect(text.contains(#""ratio":0.5"#))
    }
}
