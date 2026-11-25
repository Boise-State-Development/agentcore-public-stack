import Foundation

/// One sign-in option from `GET /auth/providers`, e.g. the organisation's
/// Entra ID. `id` is what the sign-in URL sends as `provider`.
public struct AuthProvider: Sendable, Equatable, Identifiable, Decodable {
    public let id: String
    public let displayName: String

    public init(id: String, displayName: String) {
        self.id = id
        self.displayName = displayName
    }

    enum CodingKeys: String, CodingKey {
        case id = "providerId"
        case displayName
    }
}

/// The deployment's sign-in options.
public struct SignInOptions: Sendable, Equatable, Decodable {
    public let providers: [AuthProvider]
    /// Whether to offer the hosted sign-in page as well as the providers.
    public let offersHostedSignIn: Bool

    public static let hostedOnly = SignInOptions(providers: [], offersHostedSignIn: true)

    public init(providers: [AuthProvider], offersHostedSignIn: Bool) {
        self.providers = providers
        self.offersHostedSignIn = offersHostedSignIn
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.providers = try container.decode([AuthProvider].self, forKey: .providers)
        // The server defaults this to true; an older one omits it.
        self.offersHostedSignIn = try container.decodeIfPresent(Bool.self, forKey: .cognitoLoginVisible) ?? true
    }

    enum CodingKeys: String, CodingKey {
        case providers
        case cognitoLoginVisible
    }
}

/// The signed-in user, in the shape `GET /auth/session` returns.
public struct SessionUser: Sendable, Equatable, Decodable {
    public let userId: String
    public let email: String?
    public let name: String?
    public let roles: [String]

    public init(userId: String, email: String?, name: String?, roles: [String]) {
        self.userId = userId
        self.email = email
        self.name = name
        self.roles = roles
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.userId = try container.decode(String.self, forKey: .userId)
        self.email = try container.decodeIfPresent(String.self, forKey: .email)
        self.name = try container.decodeIfPresent(String.self, forKey: .name)
        self.roles = try container.decodeIfPresent([String].self, forKey: .roles) ?? []
    }

    enum CodingKeys: String, CodingKey {
        case userId
        case email
        case name
        case roles
    }
}

/// A session the app holds: the sealed value it presents as
/// `Authorization: BFF <value>`, never a Cognito token.
public struct NativeSession: Sendable, Equatable {
    public let sealed: String
    public let expiresAt: Date
    public let user: SessionUser
}

/// What the app tells the server about itself when it signs in, for the
/// signed-in devices list. Deliberately no device name: on iOS it is often
/// the owner's name, and the system no longer gives it to apps anyway.
public struct DeviceDescription: Sendable, Equatable {
    public let model: String
    public let appVersion: String

    public init(model: String, appVersion: String) {
        self.model = model
        self.appVersion = appVersion
    }
}
