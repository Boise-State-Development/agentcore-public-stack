import Foundation
import Security

/// Where the sealed session lives between launches, one per server.
public protocol SessionStore: Sendable {
    func load(for server: ServerConfiguration) -> String?
    func save(_ sealed: String, for server: ServerConfiguration) throws
    func delete(for server: ServerConfiguration)
}

/// The Keychain, readable only after the first unlock and only on this device:
/// no iCloud Keychain sync and no restore onto another phone.
public struct KeychainSessionStore: SessionStore {
    private let service: String

    public init(service: String) {
        self.service = service
    }

    public func load(for server: ServerConfiguration) -> String? {
        var query = baseQuery(for: server)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
            let data = result as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    public func save(_ sealed: String, for server: ServerConfiguration) throws {
        delete(for: server)
        var item = baseQuery(for: server)
        item[kSecValueData as String] = Data(sealed.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainError(status: status)
        }
    }

    public func delete(for server: ServerConfiguration) {
        SecItemDelete(baseQuery(for: server) as CFDictionary)
    }

    private func baseQuery(for server: ServerConfiguration) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: server.baseURL.absoluteString,
            kSecAttrSynchronizable as String: false,
        ]
    }
}

public struct KeychainError: Error, Equatable, Sendable {
    public let status: OSStatus
}

/// Where the chosen server's address lives. Not a secret.
@MainActor
public protocol ServerStore {
    func load() -> ServerConfiguration?
    func save(_ server: ServerConfiguration)
    func clear()
}

/// `UserDefaults` (required-reason API CA92.1 in the app's privacy manifest).
@MainActor
public struct UserDefaultsServerStore: ServerStore {
    private static let key = "serverBaseURL"
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> ServerConfiguration? {
        guard let raw = defaults.string(forKey: Self.key) else { return nil }
        return try? ServerConfiguration(validating: raw)
    }

    public func save(_ server: ServerConfiguration) {
        defaults.set(server.baseURL.absoluteString, forKey: Self.key)
    }

    public func clear() {
        defaults.removeObject(forKey: Self.key)
    }
}
