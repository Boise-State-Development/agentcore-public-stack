import AgentCoreKit
import AuthenticationServices
import SwiftUI

/// Sign in to the chosen server, through the organisation's identity provider
/// when it has one.
struct SignInView: View {
    let session: SessionModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            VStack(spacing: 12) {
                Image(systemName: "bubble.left.and.text.bubble.right")
                    .font(.system(size: 56))
                    .foregroundStyle(.tint)
                    .accessibilityHidden(true)
                Text("Sign in")
                    .font(.largeTitle.bold())
                if let host = session.server?.baseURL.host() {
                    Text(host)
                        .foregroundStyle(.secondary)
                }
            }

            if let problem = session.problem {
                ProblemText(problem: problem)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
            }

            VStack(spacing: 12) {
                ForEach(session.options.providers) { provider in
                    signInButton("Continue with \(provider.displayName)", provider: provider)
                }
                if session.options.offersHostedSignIn || session.options.providers.isEmpty {
                    signInButton(
                        session.options.providers.isEmpty ? "Continue" : "Other sign-in options",
                        provider: nil,
                        prominent: session.options.providers.isEmpty
                    )
                }
            }
            .padding(.horizontal)
            .disabled(session.phase == .signingIn)

            if session.phase == .signingIn {
                ProgressView("Signing in…")
            }

            Spacer()

            Button("Use a different server") {
                session.changeServer()
            }
            .font(.footnote)
            .disabled(session.phase == .signingIn)
        }
        .padding()
    }

    @ViewBuilder
    private func signInButton(_ title: String, provider: AuthProvider?, prominent: Bool = true) -> some View {
        let button = Button {
            Task { await session.signIn(with: provider, using: authenticate) }
        } label: {
            Text(title)
                .frame(maxWidth: .infinity)
        }
        .controlSize(.large)

        if prominent {
            button.buttonStyle(.borderedProminent)
        } else {
            button.buttonStyle(.bordered)
        }
    }

    /// Opens the system sign-in sheet. Shared with Safari, not ephemeral, so
    /// an existing sign-in, the Enterprise SSO plug-in and passkeys all work
    /// (docs/specs/native-app-auth.md, D2b).
    private func authenticate(_ url: URL, _ callbackScheme: String) async throws -> URL {
        do {
            return try await webAuthenticationSession.authenticate(
                using: url,
                callback: .customScheme(callbackScheme),
                preferredBrowserSession: .shared,
                additionalHeaderFields: [:]
            )
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            throw WebAuthenticationCancelled()
        }
    }
}
