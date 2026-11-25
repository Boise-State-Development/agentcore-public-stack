import Foundation

/// The URL the server redirects to at the end of a sign-in.
///
/// The scheme is the app's bundle id, so two deployments' apps on one device
/// never claim each other's sign-ins. The server only redirects to a URI on
/// its allowlist (`NATIVE_AUTH_REDIRECT_URIS`), compared exactly.
public struct NativeRedirect: Sendable, Equatable {
    public let scheme: String

    public init(bundleIdentifier: String) {
        self.scheme = bundleIdentifier.lowercased()
    }

    public var uri: String { "\(scheme)://auth/callback" }
}

/// One sign-in attempt: where to send the user, and how to read what comes back.
///
/// See docs/specs/native-app-auth.md (D2, D3). The server half
/// (`client=native` and `POST /auth/native/exchange`) is the spec's PR 1.
public struct NativeSignIn: Sendable, Equatable {
    public let server: ServerConfiguration
    public let redirect: NativeRedirect
    public let pkce: PKCEPair
    public let provider: AuthProvider?

    public init(server: ServerConfiguration, redirect: NativeRedirect, pkce: PKCEPair, provider: AuthProvider?) {
        self.server = server
        self.redirect = redirect
        self.pkce = pkce
        self.provider = provider
    }

    /// `GET /auth/login` with the native parameters, opened in the system's
    /// web authentication sheet.
    public var loginURL: URL {
        var components = URLComponents(
            url: server.baseURL.appending(path: "auth/login"),
            resolvingAgainstBaseURL: false
        )
        var items = [
            URLQueryItem(name: "client", value: "native"),
            URLQueryItem(name: "redirect_uri", value: redirect.uri),
            URLQueryItem(name: "code_challenge", value: pkce.challenge),
        ]
        if let provider {
            items.append(URLQueryItem(name: "provider", value: provider.id))
        }
        components?.queryItems = items
        guard let url = components?.url else {
            preconditionFailure("A validated server URL always forms a login URL")
        }
        return url
    }

    /// Reads the redirect the sign-in sheet handed back.
    public func handoff(from callback: URL) throws(NativeSignInError) -> String {
        guard
            callback.scheme?.lowercased() == redirect.scheme,
            callback.host() == "auth",
            callback.path() == "/callback",
            let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems
        else { throw .unexpectedCallback }

        if let reason = items.first(where: { $0.name == "error" })?.value {
            throw .serverReported(reason)
        }
        guard let code = items.first(where: { $0.name == "handoff" })?.value, !code.isEmpty else {
            throw .unexpectedCallback
        }
        return code
    }
}

public enum NativeSignInError: Error, Equatable, Sendable {
    /// The redirect was not the one this sign-in asked for.
    case unexpectedCallback
    /// The server ended the sign-in with a reason, e.g. `exchange_failed`.
    case serverReported(String)
}

/// The sign-in sheet was closed before it finished. Not an error to show.
public struct WebAuthenticationCancelled: Error, Equatable, Sendable {
    public init() {}
}

/// Opens a URL in the system's web authentication sheet and returns the
/// redirect it ends on. The app provides it, since only a view can present one.
public typealias WebAuthenticator = @MainActor (_ url: URL, _ callbackScheme: String) async throws -> URL
