import CryptoKit
import Foundation
import Security

/// The app-side proof key for one sign-in (RFC 7636, S256).
///
/// The verifier never leaves the app's memory. Only the challenge goes into
/// the sign-in URL, so a handoff code captured by another app that registered
/// the same URL scheme cannot be exchanged (docs/specs/native-app-auth.md, D3).
public struct PKCEPair: Sendable, Equatable {
    public let verifier: String
    public let challenge: String

    /// A fresh pair from 32 random bytes: a 43-character verifier.
    public static func generate() -> PKCEPair {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(status == errSecSuccess, "The system random number generator failed")
        return PKCEPair(verifier: Data(bytes).base64URLEncoded())
    }

    init(verifier: String) {
        self.verifier = verifier
        self.challenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncoded()
    }
}

extension Data {
    /// Base64url without padding, as PKCE and most of OAuth use it.
    func base64URLEncoded() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
