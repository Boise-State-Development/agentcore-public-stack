import Foundation

/// Where the app-api lives. Same meaning as the TUI's `--base-url`: the
/// app-api root, which on a CloudFront deployment usually ends in `/api`.
///
/// Nothing is hard-coded. A deployment's host is entered by the user (or
/// supplied by MDM configuration later), never compiled into a public build.
public struct ServerConfiguration: Sendable, Equatable {
    /// Normalized root URL: lowercase scheme, no trailing slash, no query or fragment.
    public let baseURL: URL

    /// Validates and normalizes a user-entered base URL.
    ///
    /// Accepts `https://` anywhere and `http://` only on loopback hosts, so a
    /// developer can point the app at a local stack (`http://localhost:8000`)
    /// while a production host can never be reached in the clear.
    public init(validating rawValue: String) throws(ServerConfigurationError) {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw .empty }
        guard
            let components = URLComponents(string: trimmed),
            let scheme = components.scheme?.lowercased(),
            let host = components.host, !host.isEmpty
        else { throw .malformed }

        switch scheme {
        case "https":
            break
        case "http" where Self.isLoopback(host):
            break
        case "http":
            throw .insecureScheme(host: host)
        default:
            throw .unsupportedScheme(scheme)
        }

        guard components.query == nil, components.fragment == nil else { throw .malformed }

        var normalized = components
        normalized.scheme = scheme
        normalized.path = Self.trimmingTrailingSlashes(components.path)
        guard let url = normalized.url else { throw .malformed }
        self.baseURL = url
    }

    private static func isLoopback(_ host: String) -> Bool {
        ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host.lowercased())
    }

    private static func trimmingTrailingSlashes(_ path: String) -> String {
        var path = Substring(path)
        while path.hasSuffix("/") {
            path = path.dropLast()
        }
        return String(path)
    }
}

public enum ServerConfigurationError: Error, Equatable, Sendable {
    case empty
    case malformed
    case unsupportedScheme(String)
    case insecureScheme(host: String)
}
