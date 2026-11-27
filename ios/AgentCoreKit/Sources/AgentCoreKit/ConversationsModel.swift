import Foundation
import Observation
import os

private let logger = Logger(subsystem: "AgentCoreKit", category: "conversations")

/// Why a list or a transcript couldn't load. Mapped to words by the app.
public enum LoadProblem: Equatable, Sendable {
    /// No answer: offline, or the server is down.
    case unreachable
    /// The conversation doesn't exist, or isn't this user's.
    case notFound
    /// The server answered with something the app couldn't use.
    case serverError
}

/// The signed-in user's conversations, as both screens reach them.
///
/// Holds the sealed session for the life of one sign-in. A `401` from any
/// call means the session is gone, and is handed to `onSessionEnded` so the
/// app goes back to sign-in exactly as it does at launch.
@MainActor
public struct ConversationSource {
    let client: AgentCoreClient
    let sealed: String
    let onSessionEnded: @MainActor () -> Void

    public init(client: AgentCoreClient, sealed: String, onSessionEnded: @escaping @MainActor () -> Void) {
        self.client = client
        self.sealed = sealed
        self.onSessionEnded = onSessionEnded
    }

    func page(limit: Int, after nextToken: String?) async throws(AgentCoreClientError) -> ConversationPage {
        do {
            return try await client.conversations(session: sealed, limit: limit, after: nextToken)
        } catch {
            if error == .unauthorized { onSessionEnded() }
            throw error
        }
    }

    func transcript(of conversationId: String) async throws(AgentCoreClientError) -> Transcript {
        do {
            return try await client.transcript(session: sealed, conversationId: conversationId)
        } catch {
            if error == .unauthorized { onSessionEnded() }
            throw error
        }
    }
}

/// The conversation list: first page, pull to refresh, and the next page as
/// the user scrolls. Newest first, as the server sorts it.
@MainActor
@Observable
public final class ConversationListModel {
    public static let pageSize = 30

    public private(set) var conversations: [ConversationSummary] = []
    /// False until the first page has answered, successfully or not.
    public private(set) var hasLoaded = false
    public private(set) var isRefreshing = false
    /// The last refresh failed. Rows already shown stay.
    public private(set) var problem: LoadProblem?
    public private(set) var isLoadingMore = false
    public private(set) var loadMoreFailed = false
    public var canLoadMore: Bool { nextToken != nil }

    public let source: ConversationSource
    private var nextToken: String?
    /// Bumped by each refresh, so a page that was in flight when the list was
    /// replaced is dropped instead of appended to the new one.
    private var generation = 0

    public init(source: ConversationSource) {
        self.source = source
    }

    /// Load the first page, replacing whatever is shown.
    public func refresh() async {
        generation += 1
        let current = generation
        isRefreshing = true
        defer { if current == generation { isRefreshing = false } }

        do {
            let page = try await source.page(limit: Self.pageSize, after: nil)
            guard current == generation else { return }
            conversations = Self.unique(page.conversations)
            nextToken = page.nextToken
            problem = nil
            loadMoreFailed = false
        } catch {
            guard current == generation else { return }
            problem = Self.problem(for: error)
        }
        hasLoaded = true
    }

    /// Load the next page when `conversation` is the last one shown.
    public func loadMore(after conversation: ConversationSummary) async {
        guard conversation.id == conversations.last?.id else { return }
        await loadMore()
    }

    /// Load the next page, if there is one.
    public func loadMore() async {
        guard let token = nextToken, !isLoadingMore else { return }
        let current = generation
        isLoadingMore = true
        defer { isLoadingMore = false }

        do {
            let page = try await source.page(limit: Self.pageSize, after: token)
            guard current == generation else { return }
            conversations = Self.unique(conversations + page.conversations)
            nextToken = page.nextToken
            loadMoreFailed = false
        } catch {
            guard current == generation else { return }
            loadMoreFailed = true
        }
    }

    /// A transcript screen sharing this sign-in.
    public func transcript(for conversationId: String) -> TranscriptModel {
        TranscriptModel(conversationId: conversationId, source: source)
    }

    /// A row that moved between pages while the user scrolled would show twice.
    private static func unique(_ rows: [ConversationSummary]) -> [ConversationSummary] {
        var seen = Set<String>()
        return rows.filter { seen.insert($0.id).inserted }
    }

    static func problem(for error: AgentCoreClientError) -> LoadProblem {
        switch error {
        case .unreachable:
            return .unreachable
        case .notSupported:
            return .notFound
        case .unauthorized, .rejected, .serverError, .unexpectedResponse:
            logger.error("Conversations didn't load: \(String(describing: error), privacy: .public)")
            return .serverError
        }
    }
}

/// One conversation's transcript, read-only.
@MainActor
@Observable
public final class TranscriptModel {
    public let conversationId: String
    public private(set) var transcript: Transcript?
    public private(set) var problem: LoadProblem?
    public private(set) var isLoading = false

    private let source: ConversationSource

    public init(conversationId: String, source: ConversationSource) {
        self.conversationId = conversationId
        self.source = source
    }

    public func load() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            transcript = try await source.transcript(of: conversationId)
            problem = nil
        } catch {
            problem = ConversationListModel.problem(for: error)
        }
    }
}

extension SessionModel {
    /// The conversation list for whoever is signed in, or `nil` when nobody is.
    public func makeConversationList() -> ConversationListModel? {
        guard case .signedIn = phase, let source = conversationSource() else { return nil }
        return ConversationListModel(source: source)
    }
}
