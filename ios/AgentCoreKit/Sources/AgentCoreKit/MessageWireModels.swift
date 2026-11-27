import Foundation

// The wire shape of `GET /sessions/{id}/messages` (`MessagesListResponse` in
// `apis/shared/sessions/models.py`), reduced to what the transcript shows.
// Everything else on the response (toolSummaries, handoffs, deliveries,
// extractions, sourceProvenance, uiResources, …) is ignored by not being
// declared. `Transcript.init(pages:)` turns these into what the screen draws.

/// One page of messages. The server pages oldest first: `nextToken` continues
/// forward, and replay fields and pending interrupts ride on the first page.
struct MessagesPage: Sendable, Decodable {
    let messages: [WireMessage?]
    let nextToken: String?
    let pendingInterrupts: [WireInterrupt]
    /// Memory's events expired and this is the archive's text-only copy.
    let fromArchive: Bool

    init(
        messages: [WireMessage?],
        nextToken: String? = nil,
        pendingInterrupts: [WireInterrupt] = [],
        fromArchive: Bool = false
    ) {
        self.messages = messages
        self.nextToken = nextToken
        self.pendingInterrupts = pendingInterrupts
        self.fromArchive = fromArchive
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.messages = try container.decode(LenientList<WireMessage>.self, forKey: .messages).elements
        let token = container.lenient(String.self, forKey: .nextToken)
        self.nextToken = token?.isEmpty == true ? nil : token
        self.pendingInterrupts = container.lenientList(WireInterrupt.self, forKey: .pendingInterrupts)
            .elements.compactMap(\.self)
        self.fromArchive = container.lenient(Bool.self, forKey: .fromArchive) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case messages
        case nextToken
        case pendingInterrupts
        case fromArchive
    }
}

/// `MessageResponse`.
struct WireMessage: Sendable, Decodable {
    let id: String
    let role: String
    let content: [WireBlock]
    let createdAt: String?
    /// `metadata.displayText`: what the user typed, before the server added
    /// retrieved context or other framing to the stored message.
    let displayText: String?
    /// The user turn a Continue sent. Not typed by the user.
    let continuation: Bool

    init(
        id: String,
        role: String,
        content: [WireBlock],
        createdAt: String? = nil,
        displayText: String? = nil,
        continuation: Bool = false
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.createdAt = createdAt
        self.displayText = displayText
        self.continuation = continuation
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try container.decode(String.self, forKey: .id)
        self.role = container.lenient(String.self, forKey: .role) ?? ""
        self.content = container.lenientList(WireBlock.self, forKey: .content)
            .elements.map { $0 ?? .unknown(type: "") }
        self.createdAt = container.lenient(String.self, forKey: .createdAt)
        self.displayText = container.lenient(Metadata.self, forKey: .metadata)?.displayText
        self.continuation = container.lenient(Bool.self, forKey: .continuation) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case id
        case role
        case content
        case createdAt
        case metadata
        case continuation
    }

    private struct Metadata: Decodable {
        let displayText: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.displayText = container.lenient(String.self, forKey: .displayText)
        }

        enum CodingKeys: String, CodingKey {
            case displayText
        }
    }
}

/// `MessageContent`: one Converse content block, discriminated by `type`.
enum WireBlock: Sendable, Equatable, Decodable {
    case text(String)
    case toolUse(id: String?, name: String)
    case toolResult(toolUseId: String?, failed: Bool)
    case image
    case document(name: String?)
    case reasoning
    /// A block type this build doesn't know, or one that didn't decode.
    case unknown(type: String)

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = container.lenient(String.self, forKey: .type) ?? ""
        switch type {
        case "text":
            self = .text(container.lenient(String.self, forKey: .text) ?? "")
        case "toolUse":
            guard let use = container.lenient(ToolUse.self, forKey: .toolUse) else {
                self = .unknown(type: type)
                return
            }
            self = .toolUse(id: use.toolUseId, name: use.name ?? "")
        case "toolResult":
            guard let result = container.lenient(ToolResult.self, forKey: .toolResult) else {
                self = .unknown(type: type)
                return
            }
            self = .toolResult(toolUseId: result.toolUseId, failed: result.status == "error")
        case "image":
            self = .image
        case "document":
            self = .document(name: container.lenient(Document.self, forKey: .document)?.name)
        case "reasoningContent":
            self = .reasoning
        default:
            self = .unknown(type: type)
        }
    }

    enum CodingKeys: String, CodingKey {
        case type
        case text
        case toolUse
        case toolResult
        case document
    }

    private struct ToolUse: Decodable {
        let toolUseId: String?
        let name: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.toolUseId = container.lenient(String.self, forKey: .toolUseId)
            self.name = container.lenient(String.self, forKey: .name)
        }

        enum CodingKeys: String, CodingKey {
            case toolUseId
            case name
        }
    }

    private struct ToolResult: Decodable {
        let toolUseId: String?
        let status: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.toolUseId = container.lenient(String.self, forKey: .toolUseId)
            self.status = container.lenient(String.self, forKey: .status)
        }

        enum CodingKeys: String, CodingKey {
            case toolUseId
            case status
        }
    }

    private struct Document: Decodable {
        let name: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.name = container.lenient(String.self, forKey: .name)
        }

        enum CodingKeys: String, CodingKey {
            case name
        }
    }
}

/// `PendingInterrupt`: a paused turn's breadcrumb. Several fields are
/// JSON-encoded strings on the wire (`questions`, `elicitation`), decoded here.
struct WireInterrupt: Sendable, Equatable, Decodable {
    let interruptId: String
    let kind: String
    let triggeringMessageId: String?
    let toolUseId: String?
    let toolName: String?
    let message: String?
    let providerId: String?
    let reason: String?
    /// The question text of each question, from the encoded `questions`.
    let questions: [String]
    /// From the encoded `elicitation`.
    let formServerName: String?
    let formMessage: String?

    init(
        interruptId: String,
        kind: String,
        triggeringMessageId: String? = nil,
        toolUseId: String? = nil,
        toolName: String? = nil,
        message: String? = nil,
        providerId: String? = nil,
        reason: String? = nil,
        questions: [String] = [],
        formServerName: String? = nil,
        formMessage: String? = nil
    ) {
        self.interruptId = interruptId
        self.kind = kind
        self.triggeringMessageId = triggeringMessageId
        self.toolUseId = toolUseId
        self.toolName = toolName
        self.message = message
        self.providerId = providerId
        self.reason = reason
        self.questions = questions
        self.formServerName = formServerName
        self.formMessage = formMessage
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.interruptId = try container.decode(String.self, forKey: .interruptId)
        // A row with no kind predates the discriminator and is OAuth.
        self.kind = container.lenient(String.self, forKey: .kind) ?? "oauth"
        self.triggeringMessageId = container.lenient(String.self, forKey: .triggeringMessageId)
        self.toolUseId = container.lenient(String.self, forKey: .toolUseId)
        self.toolName = container.lenient(String.self, forKey: .toolName)
        self.message = container.lenient(String.self, forKey: .message)
        self.providerId = container.lenient(String.self, forKey: .providerId)
        self.reason = container.lenient(String.self, forKey: .reason)

        let questions = Self.decodeEmbedded(
            LenientList<Question>.self, from: container.lenient(String.self, forKey: .questions))
        self.questions = (questions?.elements ?? []).compactMap { $0?.question }

        let form = Self.decodeEmbedded(Elicitation.self, from: container.lenient(String.self, forKey: .elicitation))
        self.formServerName = form?.serverName
        self.formMessage = form?.message
    }

    enum CodingKeys: String, CodingKey {
        case interruptId
        case kind
        case triggeringMessageId
        case toolUseId
        case toolName
        case message
        case providerId
        case reason
        case questions
        case elicitation
    }

    private static func decodeEmbedded<T: Decodable>(_ type: T.Type, from json: String?) -> T? {
        guard let json else { return nil }
        return try? JSONDecoder().decode(type, from: Data(json.utf8))
    }

    private struct Question: Decodable {
        let question: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.question = container.lenient(String.self, forKey: .question)
        }

        enum CodingKeys: String, CodingKey {
            case question
        }
    }

    private struct Elicitation: Decodable {
        let serverName: String?
        let message: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.serverName = container.lenient(String.self, forKey: .serverName)
            self.message = container.lenient(String.self, forKey: .message)
        }

        enum CodingKeys: String, CodingKey {
            case serverName
            case message
        }
    }
}
