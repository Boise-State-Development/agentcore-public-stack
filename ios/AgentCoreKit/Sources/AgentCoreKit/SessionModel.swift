import Foundation
import Observation
import os

private let logger = Logger(subsystem: "AgentCoreKit", category: "session")

/// The app's sign-in state machine: choose a server, sign in, stay signed in,
/// sign out. Views read `phase` and `problem` and call the methods; nothing
/// here imports a UI framework, so the whole flow is tested without one.
@MainActor
@Observable
public final class SessionModel {
    public enum Phase: Equatable, Sendable {
        /// Restoring the saved server and session.
        case starting
        /// No server chosen yet.
        case needsServer
        /// A server is chosen; nobody is signed in.
        case signedOut
        /// The sign-in sheet is open or the exchange is in flight.
        case signingIn
        case signedIn(SessionUser)
        /// A saved session exists but the server could not be reached to check it.
        case cannotReach
    }

    /// Something to tell the user. Mapped to words by the app.
    public enum Problem: Equatable, Sendable {
        case invalidServer(ServerConfigurationError)
        case unreachable
        /// The server does not offer native sign-in (yet, or for this app).
        case signInNotSupported
        /// The server ended the sign-in with a reason.
        case signInFailed(reason: String?)
        /// A saved session was no longer valid.
        case sessionExpired
        case couldNotSaveSession
    }

    public private(set) var phase: Phase = .starting
    public private(set) var server: ServerConfiguration?
    public private(set) var options: SignInOptions = .hostedOnly
    public private(set) var problem: Problem?
    public private(set) var isBusy = false

    private let serverStore: any ServerStore
    private let sessionStore: any SessionStore
    private let makeClient: @Sendable (ServerConfiguration) -> AgentCoreClient
    private let redirect: NativeRedirect
    private let device: DeviceDescription

    public init(
        serverStore: any ServerStore,
        sessionStore: any SessionStore,
        redirect: NativeRedirect,
        device: DeviceDescription,
        makeClient: @escaping @Sendable (ServerConfiguration) -> AgentCoreClient = { AgentCoreClient(server: $0) }
    ) {
        self.serverStore = serverStore
        self.sessionStore = sessionStore
        self.redirect = redirect
        self.device = device
        self.makeClient = makeClient
    }

    /// Restore the last server and, if there is one, its session.
    public func start() async {
        guard let saved = serverStore.load() else {
            phase = .needsServer
            return
        }
        server = saved
        await restoreSession(on: saved)
    }

    /// Validate an address the user typed, check the server answers, and keep it.
    public func connect(to rawValue: String) async {
        problem = nil
        let candidate: ServerConfiguration
        do {
            candidate = try ServerConfiguration(validating: rawValue)
        } catch {
            problem = .invalidServer(error)
            return
        }

        isBusy = true
        defer { isBusy = false }
        do {
            options = try await makeClient(candidate).signInOptions()
        } catch {
            problem = .unreachable
            return
        }
        server = candidate
        serverStore.save(candidate)
        await restoreSession(on: candidate)
    }

    /// Forget the server (and any session on it) and ask for another.
    public func changeServer() {
        if let server {
            sessionStore.delete(for: server)
        }
        serverStore.clear()
        server = nil
        options = .hostedOnly
        problem = nil
        phase = .needsServer
    }

    /// Try the saved session again after `cannotReach`.
    public func retry() async {
        guard let server else {
            phase = .needsServer
            return
        }
        await restoreSession(on: server)
    }

    /// Run one sign-in in the system sheet and keep the session it returns.
    public func signIn(with provider: AuthProvider?, using authenticate: WebAuthenticator) async {
        guard let server, phase == .signedOut else { return }
        problem = nil
        phase = .signingIn

        let attempt = NativeSignIn(server: server, redirect: redirect, pkce: .generate(), provider: provider)
        let callback: URL
        do {
            callback = try await authenticate(attempt.loginURL, redirect.scheme)
        } catch is WebAuthenticationCancelled {
            phase = .signedOut
            return
        } catch {
            fail(.signInFailed(reason: nil))
            return
        }

        let handoff: String
        do {
            handoff = try attempt.handoff(from: callback)
        } catch .serverReported(let reason) {
            fail(.signInFailed(reason: reason))
            return
        } catch {
            fail(.signInFailed(reason: nil))
            return
        }

        let session: NativeSession
        do {
            session = try await makeClient(server).exchange(
                handoff: handoff,
                verifier: attempt.pkce.verifier,
                device: device
            )
        } catch .notSupported {
            fail(.signInNotSupported)
            return
        } catch .unreachable {
            fail(.unreachable)
            return
        } catch {
            fail(.signInFailed(reason: nil))
            return
        }

        do {
            try sessionStore.save(session.sealed, for: server)
        } catch {
            // The status only, never the value. -34018 is a build with no
            // Keychain entitlement, e.g. one compiled with signing turned off.
            logger.error("Could not store the session: \(String(describing: error), privacy: .public)")
            fail(.couldNotSaveSession)
            return
        }
        phase = .signedIn(session.user)
    }

    /// Sign out here and ask the server to delete the session. The local copy
    /// goes first, so being offline never leaves the user signed in.
    public func signOut() async {
        guard let server, let sealed = sessionStore.load(for: server) else {
            phase = server == nil ? .needsServer : .signedOut
            return
        }
        sessionStore.delete(for: server)
        phase = .signedOut
        problem = nil
        let client = makeClient(server)
        try? await client.signOut(session: sealed)
        // A session restored at launch never fetched these.
        if let fetched = try? await client.signInOptions() {
            options = fetched
        }
    }

    /// The server answered a signed-in call with `401`: the session expired or
    /// was revoked. Same outcome as finding it expired at launch.
    public func sessionEnded() {
        guard case .signedIn = phase, let server else { return }
        sessionStore.delete(for: server)
        problem = .sessionExpired
        phase = .signedOut
        let client = makeClient(server)
        Task {
            if let fetched = try? await client.signInOptions() {
                options = fetched
            }
        }
    }

    /// What the signed-in screens call the server with.
    func conversationSource() -> ConversationSource? {
        guard let server, let sealed = sessionStore.load(for: server) else { return nil }
        return ConversationSource(client: makeClient(server), sealed: sealed) { [weak self] in
            self?.sessionEnded()
        }
    }

    // MARK: - Private

    private func restoreSession(on server: ServerConfiguration) async {
        let client = makeClient(server)
        guard let sealed = sessionStore.load(for: server) else {
            if let fetched = try? await client.signInOptions() {
                options = fetched
            }
            phase = .signedOut
            return
        }

        isBusy = true
        defer { isBusy = false }
        do {
            let user = try await client.currentUser(session: sealed)
            problem = nil
            phase = .signedIn(user)
        } catch .unauthorized {
            sessionStore.delete(for: server)
            if let fetched = try? await client.signInOptions() {
                options = fetched
            }
            problem = .sessionExpired
            phase = .signedOut
        } catch {
            // Keep the session: it may well be fine once the network is back.
            phase = .cannotReach
        }
    }

    private func fail(_ problem: Problem) {
        self.problem = problem
        phase = .signedOut
    }
}
