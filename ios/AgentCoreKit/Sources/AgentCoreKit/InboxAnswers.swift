import Foundation

/// What the user has chosen for one prompt so far. Turned into the exact JSON
/// the conversation's own card sends (`_decision_for` in
/// apis/shared/scheduled_prompts/inbox_answer.py checks the shape).
public enum PromptDraft: Sendable, Equatable {
    case approval(Bool?)
    case questions(QuestionsDraft)
    case form(FormDraft)
    case notAnswerable

    /// A blank draft for `prompt`, with a form's defaults filled in.
    public init(for prompt: InboxPrompt) {
        switch prompt.kind {
        case .toolApproval: self = .approval(nil)
        case .questions: self = .questions(QuestionsDraft())
        case .form(let form): self = .form(FormDraft(form: form))
        case .notAnswerableHere: self = .notAnswerable
        }
    }
}

/// Answers to one prompt's questions, keyed by each question's `header`.
public struct QuestionsDraft: Sendable, Equatable {
    /// The user chose to skip every question.
    public var skipped = false
    /// Picked option labels per header.
    public var selected: [String: [String]] = [:]
    /// "Other" text per header.
    public var otherText: [String: String] = [:]

    public init() {}

    public func isPicked(_ label: String, for question: Question) -> Bool {
        selected[question.header]?.contains(label) == true
    }

    /// Pick or unpick an option: one at a time, unless the question allows several.
    public mutating func toggle(_ label: String, for question: Question) {
        skipped = false
        var picked = selected[question.header] ?? []
        if let index = picked.firstIndex(of: label) {
            picked.remove(at: index)
        } else if question.multiSelect {
            picked.append(label)
        } else {
            picked = [label]
        }
        selected[question.header] = picked
    }

    func hasAnswer(for question: Question) -> Bool {
        !(selected[question.header] ?? []).isEmpty
            || !(otherText[question.header] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// `{skipped: true}`, or `{answers: {header: {selected, text?}}}` for the questions answered.
    /// A question left blank is skipped by the server.
    func response(for questions: [Question]) -> JSONValue {
        if skipped {
            return .object(["skipped": .bool(true)])
        }
        var answers: [String: JSONValue] = [:]
        for question in questions where hasAnswer(for: question) {
            var answer: [String: JSONValue] = [
                "selected": .array((selected[question.header] ?? []).map(JSONValue.string))
            ]
            let text = (otherText[question.header] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                answer["text"] = .string(text)
            }
            answers[question.header] = .object(answer)
        }
        return .object(["answers": .object(answers)])
    }
}

/// A form being filled in, or declined.
public struct FormDraft: Sendable, Equatable {
    public enum Value: Sendable, Equatable {
        case text(String)
        case bool(Bool)
        /// Picked option values.
        case choices([String])
    }

    public var declined = false
    public var values: [String: Value]

    public init(form: ElicitationForm) {
        var values: [String: Value] = [:]
        for field in form.fields {
            values[field.name] = Self.initialValue(for: field)
        }
        self.values = values
    }

    private static func initialValue(for field: FormField) -> Value {
        switch field.kind {
        case .text:
            return .text(field.defaultValue?.stringValue ?? "")
        case .number, .integer:
            if case .number(let number) = field.defaultValue {
                return .text(number.rounded() == number ? String(Int64(number)) : String(number))
            }
            return .text("")
        case .boolean:
            if case .bool(let value) = field.defaultValue { return .bool(value) }
            return .bool(false)
        case .choice:
            switch field.defaultValue {
            case .string(let value): return .choices([value])
            case .array(let values): return .choices(values.compactMap(\.stringValue))
            default: return .choices([])
            }
        }
    }

    public func text(_ field: FormField) -> String {
        if case .text(let value) = values[field.name] { return value }
        return ""
    }

    public func bool(_ field: FormField) -> Bool {
        if case .bool(let value) = values[field.name] { return value }
        return false
    }

    public func choices(_ field: FormField) -> [String] {
        if case .choices(let value) = values[field.name] { return value }
        return []
    }

    public mutating func toggle(_ value: String, in field: FormField) {
        guard case .choice(_, let multiSelect, _, _) = field.kind else { return }
        var picked = choices(field)
        if let index = picked.firstIndex(of: value) {
            picked.remove(at: index)
        } else if multiSelect {
            picked.append(value)
        } else {
            picked = [value]
        }
        values[field.name] = .choices(picked)
    }

    /// Why each field doesn't fit its rules, by field name. Empty when the form can be sent.
    /// The same rules the server applies (`validate_answer`), so it never refuses what this sends.
    public func problems(in form: ElicitationForm) -> [String: FieldProblem] {
        var problems: [String: FieldProblem] = [:]
        for field in form.fields {
            if case .failure(let problem) = check(field) {
                problems[field.name] = problem
            }
        }
        return problems
    }

    /// `{action: "decline"}`, or `{action: "accept", content}` once every field fits.
    func response(for form: ElicitationForm) -> JSONValue? {
        if declined {
            return .object(["action": .string("decline")])
        }
        var content: [String: JSONValue] = [:]
        for field in form.fields {
            switch check(field) {
            case .failure:
                return nil
            case .success(let value?):
                content[field.name] = value
            case .success(nil):
                continue
            }
        }
        return .object(["action": .string("accept"), "content": .object(content)])
    }

    /// The field's value as it would be sent (`nil` for an optional field left blank), or why it can't be.
    private func check(_ field: FormField) -> Result<JSONValue?, FieldProblem> {
        switch field.kind {
        case .text(_, let minLength, let maxLength):
            let value = text(field)
            if value.isEmpty {
                return field.required ? .failure(.required) : .success(nil)
            }
            if let minLength, value.count < minLength { return .failure(.tooShort(minLength)) }
            if let maxLength, value.count > maxLength { return .failure(.tooLong(maxLength)) }
            return .success(.string(value))
        case .number(let minimum, let maximum), .integer(let minimum, let maximum):
            let raw = text(field).trimmingCharacters(in: .whitespaces)
            if raw.isEmpty {
                return field.required ? .failure(.required) : .success(nil)
            }
            guard let number = Double(raw), number.isFinite else { return .failure(.notANumber) }
            if case .integer = field.kind, number.rounded() != number { return .failure(.notAWholeNumber) }
            if let minimum, number < minimum { return .failure(.belowMinimum(minimum)) }
            if let maximum, number > maximum { return .failure(.aboveMaximum(maximum)) }
            return .success(.number(number))
        case .boolean:
            return .success(.bool(bool(field)))
        case .choice(let options, let multiSelect, let minItems, let maxItems):
            let allowed = Set(options.map(\.value))
            let picked = choices(field).filter(allowed.contains)
            if !multiSelect {
                guard let first = picked.first else {
                    return field.required ? .failure(.required) : .success(nil)
                }
                return .success(.string(first))
            }
            if picked.isEmpty && field.required { return .failure(.required) }
            if let minItems, !picked.isEmpty || field.required, picked.count < minItems {
                return .failure(.tooFewChoices(minItems))
            }
            if let maxItems, picked.count > maxItems { return .failure(.tooManyChoices(maxItems)) }
            return .success(picked.isEmpty ? nil : .array(picked.map(JSONValue.string)))
        }
    }
}

/// Why a form field can't be sent yet. Worded by the app.
public enum FieldProblem: Error, Sendable, Equatable {
    case required
    case tooShort(Int)
    case tooLong(Int)
    case notANumber
    case notAWholeNumber
    case belowMinimum(Double)
    case aboveMaximum(Double)
    case tooFewChoices(Int)
    case tooManyChoices(Int)
}

extension InboxPrompt {
    /// The JSON this prompt's draft sends, or `nil` while it can't be sent.
    public func response(from draft: PromptDraft) -> JSONValue? {
        switch (kind, draft) {
        case (.toolApproval, .approval(let approved?)):
            return .string(approved ? "approved" : "declined")
        case (.questions(let questions), .questions(let answers)):
            guard answers.skipped || questions.contains(where: answers.hasAnswer) else { return nil }
            return answers.response(for: questions)
        case (.form(let form), .form(let values)):
            if form.hasUnsupportedField && !values.declined { return nil }
            return values.response(for: form)
        default:
            return nil
        }
    }
}
