import AgentCoreKit
import SwiftUI

/// Shows the screen for the session's current phase.
struct RootView: View {
    let session: SessionModel

    var body: some View {
        Group {
            switch session.phase {
            case .starting:
                ProgressView()
            case .needsServer:
                ServerView(session: session)
            case .signedOut, .signingIn:
                SignInView(session: session)
            case .signedIn(let user):
                SignedInView(session: session, user: user)
            case .cannotReach:
                CannotReachView(session: session)
            }
        }
        .animation(.default, value: session.phase)
    }
}

/// A saved session exists, but the server did not answer to confirm it.
struct CannotReachView: View {
    let session: SessionModel

    var body: some View {
        ContentUnavailableView {
            Label("Can't reach the server", systemImage: "wifi.exclamationmark")
        } description: {
            if let host = session.server?.baseURL.host() {
                Text("\(host) didn't answer. You're still signed in; try again when you're back online.")
            }
        } actions: {
            Button("Try again") {
                Task { await session.retry() }
            }
            .buttonStyle(.borderedProminent)
            .disabled(session.isBusy)

            Button("Use a different server", role: .destructive) {
                session.changeServer()
            }
        }
    }
}
