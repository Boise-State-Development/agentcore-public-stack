import Foundation

/// One live prompt a paused run waits on, from `GET /notifications/{id}/prompts`
/// (`RelayedPrompt` in apis/shared/handoff/models.py). `questions` and
/// `elicitation` are JSON-encoded on the wire and decoded here.
public struct InboxPrompt: Sendable, Equatable, Identifiable {
    /// The kinds the inbox answers. A browser sign-in and an account
    /// connection can only be done in the conversation.
    static let answerableKinds: Set<String> = ["tool_approval", "user_question", "mcp_elicitation"]

    public enum Kind: Sendable, Equatable {
        /// `toolInput` is pretty-printed JSON, for the user to inspect before deciding.
        case toolApproval(toolName: String, message: String?, toolInput: String?)
        case questions([Question])
        case form(ElicitationForm)
        /// A kind the inbox can't answer.
        case notAnswerableHere(String)
    }

    public let id: String
    public let kind: Kind

    public init(id: String, kind: Kind) {
        self.id = id
        self.kind = kind
    }

    public var isAnswerableHere: Bool {
        if case .notAnswerableHere = kind { return false }
        return true
    }
}

public struct Question: Sendable, Equatable, Decodable {
    public struct Option: Sendable, Equatable, Decodable {
        public let label: String
        public let description: String?

        public init(label: String, description: String? = nil) {
            self.label = label
            self.description = description
        }

        public init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.label = try container.decode(String.self, forKey: .label)
            self.description = container.lenient(String.self, forKey: .description)
        }

        enum CodingKeys: String, CodingKey {
            case label
            case description
        }
    }

    /// The short label answers are keyed by.
    public let header: String
    public let question: String
    public let multiSelect: Bool
    public let options: [Option]

    public init(header: String, question: String, multiSelect: Bool = false, options: [Option]) {
        self.header = header
        self.question = question
        self.multiSelect = multiSelect
        self.options = options
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.question = try container.decode(String.self, forKey: .question)
        self.header = container.lenient(String.self, forKey: .header) ?? ""
        self.multiSelect = container.lenient(Bool.self, forKey: .multiSelect) ?? false
        self.options = container.lenientList(Option.self, forKey: .options).elements.compactMap(\.self)
    }

    enum CodingKeys: String, CodingKey {
        case header
        case question
        case multiSelect
        case options
    }
}

/// A connected service's form (`mcp_elicitation`), as the server normalized it
/// (`FormField.to_dict` in apis/shared/mcp_elicitation/forms.py).
public struct ElicitationForm: Sendable, Equatable, Decodable {
    public let toolName: String?
    public let serverName: String?
    public let message: String?
    public let fields: [FormField]
    /// A field this build doesn't know how to fill. Such a form can only be declined here.
    public let hasUnsupportedField: Bool

    public init(toolName: String? = nil, serverName: String?, message: String?, fields: [FormField]) {
        self.toolName = toolName
        self.serverName = serverName
        self.message = message
        self.fields = fields
        self.hasUnsupportedField = false
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.toolName = container.lenient(String.self, forKey: .toolName)
        self.serverName = container.lenient(String.self, forKey: .serverName)
        self.message = container.lenient(String.self, forKey: .message)
        let fields = container.lenientList(FormField.self, forKey: .fields).elements
        self.fields = fields.compactMap(\.self)
        self.hasUnsupportedField = fields.contains { $0 == nil }
    }

    enum CodingKeys: String, CodingKey {
        case toolName
        case serverName
        case message
        case fields
    }
}

public struct FormField: Sendable, Equatable, Decodable {
    public enum Kind: Sendable, Equatable {
        case text(format: String?, minLength: Int?, maxLength: Int?)
        case number(minimum: Double?, maximum: Double?)
        case integer(minimum: Double?, maximum: Double?)
        case boolean
        case choice(options: [ChoiceOption], multiSelect: Bool, minItems: Int?, maxItems: Int?)
    }

    public struct ChoiceOption: Sendable, Equatable, Decodable {
        public let value: String
        public let label: String

        public init(value: String, label: String) {
            self.value = value
            self.label = label
        }
    }

    public let name: String
    public let title: String
    public let description: String?
    public let required: Bool
    public let defaultValue: JSONValue?
    public let kind: Kind

    public init(
        name: String,
        title: String,
        description: String? = nil,
        required: Bool = false,
        defaultValue: JSONValue? = nil,
        kind: Kind
    ) {
        self.name = name
        self.title = title
        self.description = description
        self.required = required
        self.defaultValue = defaultValue
        self.kind = kind
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.name = try container.decode(String.self, forKey: .name)
        self.title = container.lenient(String.self, forKey: .title) ?? name
        self.description = container.lenient(String.self, forKey: .description)
        self.required = container.lenient(Bool.self, forKey: .required) ?? false
        let defaultValue = container.lenient(JSONValue.self, forKey: .default)
        self.defaultValue = defaultValue == .null ? nil : defaultValue
        let minimum = container.lenient(Double.self, forKey: .minimum)
        let maximum = container.lenient(Double.self, forKey: .maximum)
        switch try container.decode(String.self, forKey: .kind) {
        case "text":
            self.kind = .text(
                format: container.lenient(String.self, forKey: .format),
                minLength: container.lenient(Int.self, forKey: .minLength),
                maxLength: container.lenient(Int.self, forKey: .maxLength))
        case "number":
            self.kind = .number(minimum: minimum, maximum: maximum)
        case "integer":
            self.kind = .integer(minimum: minimum, maximum: maximum)
        case "boolean":
            self.kind = .boolean
        case "choice":
            self.kind = .choice(
                options: container.lenientList(ChoiceOption.self, forKey: .options).elements.compactMap(\.self),
                multiSelect: container.lenient(Bool.self, forKey: .multiSelect) ?? false,
                minItems: container.lenient(Int.self, forKey: .minItems),
                maxItems: container.lenient(Int.self, forKey: .maxItems))
        default:
            // An unknown kind fails this field, which marks the form unsupported.
            throw DecodingError.dataCorruptedError(
                forKey: .kind, in: container, debugDescription: "Unknown form field kind")
        }
    }

    enum CodingKeys: String, CodingKey {
        case name
        case title
        case description
        case required
        case `default`
        case kind
        case format
        case minLength
        case maxLength
        case minimum
        case maximum
        case options
        case multiSelect
        case minItems
        case maxItems
    }
}

// MARK: - Decoding the wire shape

/// `InboxPromptsResponse`.
struct InboxPromptsPage: Decodable {
    let sessionId: String?
    let prompts: [InboxPrompt]

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.sessionId = container.lenient(String.self, forKey: .sessionId)
        self.prompts = try container.decode(LenientList<WirePrompt>.self, forKey: .prompts)
            .elements.compactMap { $0?.prompt }
    }

    enum CodingKeys: String, CodingKey {
        case sessionId
        case prompts
    }
}

private struct WirePrompt: Decodable {
    let prompt: InboxPrompt

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let id = try container.decode(String.self, forKey: .interruptId)
        let kind = container.lenient(String.self, forKey: .kind) ?? ""
        switch kind {
        case "tool_approval":
            let input = container.lenient(String.self, forKey: .toolInput)
            prompt = InboxPrompt(
                id: id,
                kind: .toolApproval(
                    toolName: container.lenient(String.self, forKey: .toolName) ?? "",
                    message: container.lenient(String.self, forKey: .message),
                    toolInput: input.map(Self.prettyJSON)))
        case "user_question":
            let questions = Self.embedded(
                LenientList<Question>.self, container.lenient(String.self, forKey: .questions))
            prompt = InboxPrompt(id: id, kind: .questions(questions?.elements.compactMap(\.self) ?? []))
        case "mcp_elicitation":
            guard let form = Self.embedded(ElicitationForm.self, container.lenient(String.self, forKey: .elicitation))
            else {
                prompt = InboxPrompt(id: id, kind: .notAnswerableHere(kind))
                return
            }
            prompt = InboxPrompt(id: id, kind: .form(form))
        default:
            prompt = InboxPrompt(id: id, kind: .notAnswerableHere(kind))
        }
    }

    enum CodingKeys: String, CodingKey {
        case interruptId
        case kind
        case toolName
        case toolInput
        case message
        case questions
        case elicitation
    }

    private static func embedded<T: Decodable>(_ type: T.Type, _ json: String?) -> T? {
        guard let json else { return nil }
        return try? JSONDecoder().decode(type, from: Data(json.utf8))
    }

    /// The tool input, indented, or as sent when it isn't JSON.
    private static func prettyJSON(_ raw: String) -> String {
        guard let object = try? JSONSerialization.jsonObject(with: Data(raw.utf8), options: [.fragmentsAllowed]),
            let data = try? JSONSerialization.data(
                withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .fragmentsAllowed])
        else { return raw }
        return String(decoding: data, as: UTF8.self)
    }
}
