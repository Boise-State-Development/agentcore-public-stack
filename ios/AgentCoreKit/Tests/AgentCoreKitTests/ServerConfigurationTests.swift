import Testing

@testable import AgentCoreKit

@Suite("ServerConfiguration")
struct ServerConfigurationTests {
    @Test("accepts an https root and keeps its path")
    func acceptsHTTPS() throws {
        let config = try ServerConfiguration(validating: "https://ai.example.edu/api")
        #expect(config.baseURL.absoluteString == "https://ai.example.edu/api")
    }

    @Test("trims whitespace and trailing slashes")
    func normalizes() throws {
        let config = try ServerConfiguration(validating: "  HTTPS://ai.example.edu/api// \n")
        #expect(config.baseURL.absoluteString == "https://ai.example.edu/api")
    }

    @Test("allows plain http only on loopback", arguments: ["http://localhost:8000", "http://127.0.0.1:8000/api"])
    func allowsLoopbackHTTP(raw: String) throws {
        let config = try ServerConfiguration(validating: raw)
        #expect(config.baseURL.scheme == "http")
    }

    @Test("rejects plain http on a real host")
    func rejectsInsecureHost() {
        #expect(throws: ServerConfigurationError.insecureScheme(host: "ai.example.edu")) {
            try ServerConfiguration(validating: "http://ai.example.edu/api")
        }
    }

    @Test("rejects empty input")
    func rejectsEmpty() {
        #expect(throws: ServerConfigurationError.empty) {
            try ServerConfiguration(validating: "   ")
        }
    }

    @Test("rejects schemes other than http(s)")
    func rejectsUnsupportedScheme() {
        #expect(throws: ServerConfigurationError.unsupportedScheme("ftp")) {
            try ServerConfiguration(validating: "ftp://ai.example.edu")
        }
    }

    @Test("rejects malformed input", arguments: ["not a url", "https://", "https://ai.example.edu/api?x=1"])
    func rejectsMalformed(raw: String) {
        #expect(throws: ServerConfigurationError.malformed) {
            try ServerConfiguration(validating: raw)
        }
    }
}
