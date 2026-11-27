import Foundation

// `GET /notifications` (`NotificationsResponse` in
// app_api/notifications/routes.py; rows are `Notification` in
// apis/shared/notifications/models.py), reduced to what the inbox shows.

/// One inbox row.
public struct InboxItem: Sendable, Equatable, Identifiable, Decodable {
    public enum Kind: Sendable, Equatable {
        /// A scheduled run paused on the user: `agent_needs_approval` / `agent_needs_input`.
        case needsUser(NeedsUser)
        /// A quiet watcher found something new.
        case watcherFound(label: String?, title: String?)
        /// A personal schedule stopped running on its own.
        case schedulePaused(label: String?)
        /// A handoff's conversation needs the user, finished, or stopped.
        case handoff(HandoffEvent, agentName: String?, sourceTitle: String?)
        /// Something about a shared project (`project_*`).
        case project(String)
        /// A quota request or extension (`quota_*`).
        case quota(QuotaEvent)
        /// A kind this build doesn't know.
        case other(String)
    }

    public enum QuotaEvent: Sendable, Equatable {
        /// Someone asked for more quota (the reader approves requests).
        case requested(by: String?)
        case approved
        case declined
        case moreInformationRequested
        case extensionGranted
        case extensionEnded
        case other(String)
    }

    public enum HandoffEvent: Sendable, Equatable {
        case needsInput
        case done
        case stopped
    }

    public let id: String
    public let kind: Kind
    public let createdAt: Date?
    public let isRead: Bool
    public let projectName: String?
    /// Who did it, named from the directory; `nil` when the directory has no name.
    public let actorName: String?
    /// The conversation this row is about, when it names one the user can open.
    public let conversationId: String?

    public init(
        id: String,
        kind: Kind,
        createdAt: Date? = nil,
        isRead: Bool = false,
        projectName: String? = nil,
        actorName: String? = nil,
        conversationId: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.createdAt = createdAt
        self.isRead = isRead
        self.projectName = projectName
        self.actorName = actorName
        self.conversationId = conversationId
    }

    public var needsUser: NeedsUser? {
        if case .needsUser(let needsUser) = kind { return needsUser }
        return nil
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try container.decode(String.self, forKey: .notificationId)
        let kind = container.lenient(String.self, forKey: .kind) ?? ""
        let payload = container.lenient(Payload.self, forKey: .payload) ?? Payload()
        let resolution = container.lenient(Resolution.self, forKey: .resolution)
        self.createdAt = ServerTimestamp.parse(container.lenient(String.self, forKey: .createdAt))
        self.isRead = container.lenient(String.self, forKey: .readAt) != nil
        self.projectName = container.lenient(String.self, forKey: .projectName)
        self.actorName = container.lenient(String.self, forKey: .actorName)

        switch kind {
        case "agent_needs_approval", "agent_needs_input":
            self.kind = .needsUser(NeedsUser(payload: payload, resolution: resolution))
            self.conversationId = payload.sessionId
        case "agent_item":
            self.kind = .watcherFound(label: payload.label, title: payload.title)
            self.conversationId = payload.sessionId
        case "schedule_paused":
            self.kind = .schedulePaused(label: payload.label)
            self.conversationId = nil
        case "handoff_needs_input", "handoff_done", "handoff_stopped":
            let event: HandoffEvent =
                switch kind {
                case "handoff_needs_input": .needsInput
                case "handoff_done": .done
                default: .stopped
                }
            self.kind = .handoff(event, agentName: payload.agentName, sourceTitle: payload.sourceTitle)
            self.conversationId = payload.targetSessionId
        case let project where project.hasPrefix("project_"):
            self.kind = .project(project)
            self.conversationId = nil
        case "quota_request_submitted":
            self.kind = .quota(.requested(by: payload.userEmail))
            self.conversationId = nil
        case "quota_request_decided":
            self.kind = .quota(payload.outcome == "approved" ? .approved : .declined)
            self.conversationId = nil
        case "quota_request_info_requested":
            self.kind = .quota(.moreInformationRequested)
            self.conversationId = nil
        case "quota_extension_granted":
            self.kind = .quota(.extensionGranted)
            self.conversationId = nil
        case "quota_extension_revoked":
            self.kind = .quota(.extensionEnded)
            self.conversationId = nil
        case let quota where quota.hasPrefix("quota_"):
            self.kind = .quota(.other(quota))
            self.conversationId = nil
        default:
            self.kind = .other(kind)
            self.conversationId = nil
        }
    }

    enum CodingKeys: String, CodingKey {
        case notificationId
        case kind
        case projectName
        case actorName
        case payload
        case createdAt
        case readAt
        case resolution
    }

    /// The payload fields any row kind uses. Every one is optional.
    struct Payload: Decodable {
        var sessionId: String?
        var targetSessionId: String?
        var label: String?
        var title: String?
        var agentName: String?
        var sourceTitle: String?
        var expiresAt: String?
        var outcome: String?
        var userEmail: String?
        var answerable = false
        var prompts: [NeedsUser.Prompt] = []

        init() {}

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            sessionId = container.lenient(String.self, forKey: .sessionId)
            targetSessionId = container.lenient(String.self, forKey: .targetSessionId)
            label = container.lenient(String.self, forKey: .label)
            title = container.lenient(String.self, forKey: .title)
            agentName = container.lenient(String.self, forKey: .agentName)
            sourceTitle = container.lenient(String.self, forKey: .sourceTitle)
            expiresAt = container.lenient(String.self, forKey: .expiresAt)
            outcome = container.lenient(String.self, forKey: .outcome)
            userEmail = container.lenient(String.self, forKey: .userEmail)
            answerable = container.lenient(Bool.self, forKey: .answerable) ?? false
            prompts = container.lenientList(NeedsUser.Prompt.self, forKey: .prompts).elements.compactMap(\.self)
        }

        enum CodingKeys: String, CodingKey {
            case sessionId
            case targetSessionId
            case label
            case title
            case agentName
            case sourceTitle
            case expiresAt
            case outcome
            case userEmail
            case answerable
            case prompts
        }
    }

    struct Resolution: Decodable {
        var state: String?
        var decision: String?
        var outcome: String?

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            state = container.lenient(String.self, forKey: .state)
            decision = container.lenient(String.self, forKey: .decision)
            outcome = container.lenient(String.self, forKey: .outcome)
        }

        enum CodingKeys: String, CodingKey {
            case state
            case decision
            case outcome
        }
    }
}

/// A scheduled run waiting on the user, as its inbox row describes it: ids and
/// display lines only. The prompts themselves are read live when the user reviews it.
public struct NeedsUser: Sendable, Equatable {
    /// One prompt as the row lists it.
    public struct Prompt: Sendable, Equatable, Decodable {
        public let interruptId: String
        /// `tool_approval`, `user_question`, `mcp_elicitation`, `browser_login` or `oauth`.
        public let kind: String
        public let toolName: String?
        public let serverName: String?
        /// One display line, at most 120 characters.
        public let headline: String?

        public init(
            interruptId: String, kind: String, toolName: String? = nil, serverName: String? = nil,
            headline: String? = nil
        ) {
            self.interruptId = interruptId
            self.kind = kind
            self.toolName = toolName
            self.serverName = serverName
            self.headline = headline
        }

        public init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            self.interruptId = try container.decode(String.self, forKey: .interruptId)
            self.kind = container.lenient(String.self, forKey: .kind) ?? ""
            self.toolName = container.lenient(String.self, forKey: .toolName)
            self.serverName = container.lenient(String.self, forKey: .serverName)
            self.headline = container.lenient(String.self, forKey: .headline)
        }

        enum CodingKeys: String, CodingKey {
            case interruptId
            case kind
            case toolName
            case serverName
            case headline
        }
    }

    /// Where the wait stands.
    public enum State: Sendable, Equatable {
        case waiting
        /// An answer was sent and the run is continuing.
        case applying
        /// Answered from the inbox; `decision` is approved, declined, answered or skipped.
        case answered(decision: String?)
        case answeredElsewhere
        case expired
        /// A sent answer couldn't be applied, so the row can be answered again.
        case retry
    }

    public let label: String?
    public let prompts: [Prompt]
    public let expiresAt: Date?
    /// The server can take this row's answers from the inbox. When false, it
    /// can only be answered in its conversation.
    public let answerable: Bool
    public let state: State

    public init(label: String?, prompts: [Prompt], expiresAt: Date?, answerable: Bool, state: State) {
        self.label = label
        self.prompts = prompts
        self.expiresAt = expiresAt
        self.answerable = answerable
        self.state = state
    }

    init(payload: InboxItem.Payload, resolution: InboxItem.Resolution?) {
        self.label = payload.label
        self.prompts = payload.prompts
        self.expiresAt = ServerTimestamp.parse(payload.expiresAt)
        self.answerable = payload.answerable
        self.state =
            switch resolution?.state {
            case nil: .waiting
            case "applying": .applying
            case "answered": .answered(decision: resolution?.decision)
            case "answered_elsewhere": .answeredElsewhere
            case "expired": .expired
            case "retry": .retry
            // A state this build doesn't know: not something to offer buttons for.
            default: .answeredElsewhere
            }
    }

    /// Still waiting, as of `now`: the server lists a lapsed one as expired
    /// only on the next read, so the app derives it too.
    public func isOpen(now: Date = .now) -> Bool {
        guard state == .waiting || state == .retry else { return false }
        return expiresAt.map { $0 > now } ?? true
    }

    /// Whether the inbox can answer this row now. Everything else opens the conversation.
    public func canAnswerHere(now: Date = .now) -> Bool {
        isOpen(now: now) && answerable
            && prompts.allSatisfy { InboxPrompt.answerableKinds.contains($0.kind) }
    }
}

/// One page of the inbox, newest first.
public struct InboxPage: Sendable, Equatable, Decodable {
    public let items: [InboxItem]
    public let unreadCount: Int
    public let nextCursor: String?

    public init(items: [InboxItem], unreadCount: Int, nextCursor: String?) {
        self.items = items
        self.unreadCount = unreadCount
        self.nextCursor = nextCursor
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.items = try container.decode(LenientList<InboxItem>.self, forKey: .notifications)
            .elements.compactMap(\.self)
        self.unreadCount = container.lenient(Int.self, forKey: .unreadCount) ?? 0
        let cursor = container.lenient(String.self, forKey: .nextCursor)
        self.nextCursor = cursor?.isEmpty == true ? nil : cursor
    }

    enum CodingKeys: String, CodingKey {
        case notifications
        case unreadCount
        case nextCursor
    }
}

/// `GET /runs/{runId}`: how a resumed run is getting on.
public struct RunStatus: Sendable, Equatable, Decodable {
    public enum Outcome: Sendable, Equatable {
        case running
        case finished
        /// It paused on the user again.
        case waitingAgain
        /// It needs an account connected, which only the conversation can do.
        case needsConnection
        case failed(String?)
    }

    public let runId: String
    public let outcome: Outcome

    public init(runId: String, outcome: Outcome) {
        self.runId = runId
        self.outcome = outcome
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.runId = try container.decode(String.self, forKey: .runId)
        let error = container.lenient(String.self, forKey: .error)
        self.outcome =
            switch container.lenient(String.self, forKey: .status) ?? "started" {
            case "started": .running
            case "completed": .finished
            case "needs_user": .waitingAgain
            case "oauth_required": .needsConnection
            default: .failed(error)
            }
    }

    enum CodingKeys: String, CodingKey {
        case runId
        case status
        case error
    }
}
