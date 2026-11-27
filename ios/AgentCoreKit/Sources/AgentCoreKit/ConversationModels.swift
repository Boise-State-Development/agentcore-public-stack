import Foundation

/// What a paused turn in a conversation is waiting on, from the list's
/// `awaitingInput` (`AwaitingInputKind` in `apis/shared/sessions/models.py`).
/// Set only while the pause can still be answered.
public enum AwaitingInput: Sendable, Equatable {
    case connectAccount
    case toolApproval
    case question
    case browserSignIn
    case form
    /// A kind this build doesn't know. Still a conversation that needs the user.
    case other(String)

    init(wireValue: String) {
        switch wireValue {
        case "oauth": self = .connectAccount
        case "tool_approval": self = .toolApproval
        case "user_question": self = .question
        case "browser_login": self = .browserSignIn
        case "mcp_elicitation": self = .form
        default: self = .other(wireValue)
        }
    }
}

/// One row of `GET /sessions` (`SessionMetadataResponse`), the fields the list shows.
public struct ConversationSummary: Sendable, Equatable, Identifiable, Decodable {
    public let id: String
    public let title: String
    public let lastMessageAt: Date?
    public let messageCount: Int
    /// A scheduled run left an answer the user hasn't opened yet.
    public let unread: Bool
    public let awaitingInput: AwaitingInput?

    public init(
        id: String,
        title: String,
        lastMessageAt: Date?,
        messageCount: Int = 0,
        unread: Bool = false,
        awaitingInput: AwaitingInput? = nil
    ) {
        self.id = id
        self.title = title
        self.lastMessageAt = lastMessageAt
        self.messageCount = messageCount
        self.unread = unread
        self.awaitingInput = awaitingInput
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // The id is the one field without which a row is no use.
        self.id = try container.decode(String.self, forKey: .sessionId)
        self.title = container.lenient(String.self, forKey: .title) ?? ""
        self.lastMessageAt =
            ServerTimestamp.parse(container.lenient(String.self, forKey: .lastMessageAt))
            ?? ServerTimestamp.parse(container.lenient(String.self, forKey: .createdAt))
        self.messageCount = container.lenient(Int.self, forKey: .messageCount) ?? 0
        self.unread = container.lenient(Bool.self, forKey: .unread) ?? false
        self.awaitingInput = container.lenient(String.self, forKey: .awaitingInput).map(AwaitingInput.init)
    }

    enum CodingKeys: String, CodingKey {
        case sessionId
        case title
        case createdAt
        case lastMessageAt
        case messageCount
        case unread
        case awaitingInput
    }
}

/// One page of `GET /sessions` (`SessionsListResponse`), newest first.
public struct ConversationPage: Sendable, Equatable, Decodable {
    public let conversations: [ConversationSummary]
    /// Pass back as `next_token` for the next page; `nil` on the last one.
    public let nextToken: String?

    public init(conversations: [ConversationSummary], nextToken: String?) {
        self.conversations = conversations
        self.nextToken = nextToken
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // `sessions` is required by the server; a page without it is not a page.
        let sessions = try container.decode(LenientList<ConversationSummary>.self, forKey: .sessions)
        self.conversations = sessions.elements.compactMap(\.self)
        let token = container.lenient(String.self, forKey: .nextToken)
        self.nextToken = token?.isEmpty == true ? nil : token
    }

    enum CodingKeys: String, CodingKey {
        case sessions
        case nextToken
    }
}
