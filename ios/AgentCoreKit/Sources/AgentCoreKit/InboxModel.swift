import Foundation
import Observation
import os

private let logger = Logger(subsystem: "AgentCoreKit", category: "inbox")

extension SignedInAPI {
    func inbox(limit: Int, after cursor: String?) async throws(AgentCoreClientError) -> InboxPage {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.inbox(session: sealed, limit: limit, after: cursor)
        }
    }

    func markRead(_ id: String) async throws(AgentCoreClientError) {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.markRead(session: sealed, notificationId: id)
        }
    }

    func markAllRead() async throws(AgentCoreClientError) {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.markAllRead(session: sealed)
        }
    }

    func prompts(for id: String) async throws(AgentCoreClientError) -> [InboxPrompt] {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.inboxPrompts(session: sealed, notificationId: id)
        }
    }

    func respond(
        to id: String,
        answers: [(interruptId: String, response: JSONValue)]
    ) async throws(AgentCoreClientError) -> String {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.respond(session: sealed, notificationId: id, answers: answers)
        }
    }

    func run(_ runId: String) async throws(AgentCoreClientError) -> RunStatus {
        try await call { client, sealed throws(AgentCoreClientError) in
            try await client.run(session: sealed, id: runId)
        }
    }
}

/// The inbox: newest first, a page at a time, with the unread count for the badge.
@MainActor
@Observable
public final class InboxModel {
    public static let pageSize = 30

    public private(set) var items: [InboxItem] = []
    public private(set) var unreadCount = 0
    public private(set) var hasLoaded = false
    public private(set) var isRefreshing = false
    public private(set) var problem: LoadProblem?
    public private(set) var isLoadingMore = false
    public private(set) var loadMoreFailed = false
    public var canLoadMore: Bool { nextCursor != nil }

    public let api: SignedInAPI
    private var nextCursor: String?
    private var generation = 0

    public init(api: SignedInAPI) {
        self.api = api
    }

    public func refresh() async {
        generation += 1
        let current = generation
        isRefreshing = true
        defer { if current == generation { isRefreshing = false } }

        do {
            let page = try await api.inbox(limit: Self.pageSize, after: nil)
            guard current == generation else { return }
            items = Self.unique(page.items)
            unreadCount = page.unreadCount
            nextCursor = page.nextCursor
            problem = nil
            loadMoreFailed = false
        } catch {
            guard current == generation else { return }
            problem = ConversationListModel.problem(for: error)
        }
        hasLoaded = true
    }

    public func loadMore(after item: InboxItem) async {
        guard item.id == items.last?.id else { return }
        await loadMore()
    }

    public func loadMore() async {
        guard let cursor = nextCursor, !isLoadingMore else { return }
        let current = generation
        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            let page = try await api.inbox(limit: Self.pageSize, after: cursor)
            guard current == generation else { return }
            items = Self.unique(items + page.items)
            unreadCount = page.unreadCount
            nextCursor = page.nextCursor
            loadMoreFailed = false
        } catch {
            guard current == generation else { return }
            loadMoreFailed = true
        }
    }

    /// Mark one row read, here at once and then on the server. A failure is
    /// only logged: the next refresh shows the server's state.
    public func markRead(_ item: InboxItem) async {
        guard !item.isRead, let index = items.firstIndex(where: { $0.id == item.id }) else { return }
        items[index] = item.markedRead()
        unreadCount = max(0, unreadCount - 1)
        do {
            try await api.markRead(item.id)
        } catch {
            logger.error("Couldn't mark a notification read: \(String(describing: error), privacy: .public)")
        }
    }

    public func markAllRead() async {
        items = items.map { $0.markedRead() }
        unreadCount = 0
        do {
            try await api.markAllRead()
        } catch {
            logger.error("Couldn't mark all read: \(String(describing: error), privacy: .public)")
            await refresh()
        }
    }

    /// The review sheet for a waiting run's row.
    public func review(_ item: InboxItem) -> InboxReviewModel {
        InboxReviewModel(notificationId: item.id, label: item.needsUser?.label, api: api)
    }

    /// The read-only transcript of the conversation a row names.
    public func transcript(for conversationId: String) -> TranscriptModel {
        TranscriptModel(conversationId: conversationId, source: api)
    }

    private static func unique(_ rows: [InboxItem]) -> [InboxItem] {
        var seen = Set<String>()
        return rows.filter { seen.insert($0.id).inserted }
    }
}

extension InboxItem {
    func markedRead() -> InboxItem {
        InboxItem(
            id: id, kind: kind, createdAt: createdAt, isRead: true, projectName: projectName,
            actorName: actorName, conversationId: conversationId)
    }
}

/// Answering one waiting run: read its live prompts, collect an answer to
/// each, send them together, then follow the run until it says how it ended.
@MainActor
@Observable
public final class InboxReviewModel {
    public enum Phase: Equatable, Sendable {
        case loading
        /// The prompts are on screen, waiting for answers.
        case answering
        case sending
        /// Sent; the run is continuing.
        case following
        /// How the resumed run ended.
        case ended(RunStatus.Outcome)
        /// The run didn't report back within the time a run can take.
        case lost
        /// It was answered somewhere else, or expired, before this answer got there.
        case alreadyAnswered
        case expired
        /// Some prompt can only be answered in the conversation.
        case openConversationInstead
        case failed(LoadProblem)
    }

    public let notificationId: String
    public let label: String?
    public private(set) var phase: Phase = .loading
    public private(set) var prompts: [InboxPrompt] = []
    public var drafts: [String: PromptDraft] = [:]
    /// The answer couldn't be sent and the prompts are still open.
    public private(set) var sendProblem: LoadProblem?

    private let api: SignedInAPI
    /// How long to wait between asks while following a run.
    let pollInterval: Duration
    /// A run with no outcome after this long died with its server task (`RUN_LOST_AFTER_MS`).
    let giveUpAfter: Duration

    public init(
        notificationId: String,
        label: String?,
        api: SignedInAPI,
        pollInterval: Duration = .seconds(3),
        giveUpAfter: Duration = .seconds(16 * 60)
    ) {
        self.notificationId = notificationId
        self.label = label
        self.api = api
        self.pollInterval = pollInterval
        self.giveUpAfter = giveUpAfter
    }

    public func load() async {
        phase = .loading
        do {
            let prompts = try await api.prompts(for: notificationId)
            self.prompts = prompts
            drafts = Dictionary(uniqueKeysWithValues: prompts.map { ($0.id, PromptDraft(for: $0)) })
            phase = prompts.allSatisfy(\.isAnswerableHere) && !prompts.isEmpty ? .answering : .openConversationInstead
        } catch {
            phase = Self.phase(for: error)
        }
    }

    /// Every prompt has an answer that can be sent.
    public var canSend: Bool {
        phase == .answering && !prompts.isEmpty && answers() != nil
    }

    /// Send every answer at once, then follow the run.
    public func send() async {
        guard phase == .answering, let answers = answers() else { return }
        phase = .sending
        sendProblem = nil
        let runId: String
        do {
            runId = try await api.respond(to: notificationId, answers: answers)
        } catch {
            switch Self.phase(for: error) {
            case .failed(let problem):
                // Nothing was claimed, or the claim was given back: the prompts stay.
                sendProblem = problem
                phase = .answering
            case let settled:
                phase = settled
            }
            return
        }
        phase = .following
        await follow(runId)
    }

    private func follow(_ runId: String) async {
        let clock = ContinuousClock()
        let started = clock.now
        while clock.now - started < giveUpAfter {
            do {
                try await Task.sleep(for: pollInterval)
            } catch {
                return
            }
            // A failed ask is skipped: the run goes on whether or not this one answered.
            guard let status = try? await api.run(runId), status.outcome != .running else { continue }
            phase = .ended(status.outcome)
            return
        }
        phase = .lost
    }

    private func answers() -> [(interruptId: String, response: JSONValue)]? {
        var answers: [(interruptId: String, response: JSONValue)] = []
        for prompt in prompts {
            guard let draft = drafts[prompt.id], let response = prompt.response(from: draft) else { return nil }
            answers.append((prompt.id, response))
        }
        return answers
    }

    private static func phase(for error: AgentCoreClientError) -> Phase {
        switch error {
        case .serverError(status: 409):
            return .alreadyAnswered
        case .serverError(status: 410):
            return .expired
        case .rejected, .serverError(status: 422):
            // Answers the server won't take from here, e.g. a row it marks not answerable.
            return .openConversationInstead
        case .notSupported:
            // The row is gone, or this deployment doesn't answer from the inbox.
            return .openConversationInstead
        default:
            return .failed(ConversationListModel.problem(for: error))
        }
    }
}
