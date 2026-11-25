import Foundation
import Testing

@testable import AgentCoreKit

@Suite("PKCE")
struct PKCETests {
    @Test("matches the RFC 7636 appendix B example")
    func rfcVector() {
        let pair = PKCEPair(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        #expect(pair.challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    @Test("a generated verifier is 43 url-safe characters and never repeats")
    func generated() {
        let first = PKCEPair.generate()
        let second = PKCEPair.generate()
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        #expect(first.verifier.count == 43)
        #expect(first.verifier.unicodeScalars.allSatisfy { allowed.contains($0) })
        #expect(first.verifier != second.verifier)
    }
}

@Suite("NativeSignIn")
struct NativeSignInTests {
    private func attempt(provider: AuthProvider? = nil) throws -> NativeSignIn {
        NativeSignIn(
            server: try Fixtures.server,
            redirect: Fixtures.redirect,
            pkce: PKCEPair(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            provider: provider
        )
    }

    @Test("the redirect scheme is the lowercased bundle id")
    func redirect() {
        #expect(Fixtures.redirect.uri == "org.example.agentcore://auth/callback")
    }

    @Test("the login URL carries the native parameters under the server's path")
    func loginURL() throws {
        let components = try #require(URLComponents(url: try attempt().loginURL, resolvingAgainstBaseURL: false))
        #expect(components.host == "ai.example.edu")
        #expect(components.path == "/api/auth/login")
        let query = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value ?? "") })
        #expect(
            query == [
                "client": "native",
                "redirect_uri": "org.example.agentcore://auth/callback",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            ]
        )
    }

    @Test("the verifier never appears in the login URL")
    func verifierStaysLocal() throws {
        #expect(!(try attempt().loginURL.absoluteString.contains("dBjftJeZ4CVP")))
    }

    @Test("a provider goes straight to its sign-in page")
    func provider() throws {
        let url = try attempt(provider: AuthProvider(id: "ms-entra-id", displayName: "Example")).loginURL
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))
        #expect(components.queryItems?.first(where: { $0.name == "provider" })?.value == "ms-entra-id")
    }

    @Test("reads the handoff code from the callback")
    func handoff() throws {
        let callback = try #require(URL(string: "org.example.agentcore://auth/callback?handoff=abc123"))
        #expect(try attempt().handoff(from: callback) == "abc123")
    }

    @Test("surfaces the server's reason when the sign-in failed")
    func serverError() throws {
        let callback = try #require(URL(string: "org.example.agentcore://auth/callback?error=exchange_failed"))
        #expect(throws: NativeSignInError.serverReported("exchange_failed")) {
            try attempt().handoff(from: callback)
        }
    }

    @Test(
        "refuses a callback this sign-in did not ask for",
        arguments: [
            "other.app://auth/callback?handoff=abc",
            "org.example.agentcore://elsewhere/callback?handoff=abc",
            "org.example.agentcore://auth/other?handoff=abc",
            "org.example.agentcore://auth/callback",
            "org.example.agentcore://auth/callback?handoff=",
        ]
    )
    func unexpected(raw: String) throws {
        let callback = try #require(URL(string: raw))
        #expect(throws: NativeSignInError.unexpectedCallback) {
            try attempt().handoff(from: callback)
        }
    }
}
