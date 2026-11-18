import Foundation
import Security

/// The Frame log constants shared by the token store, the settings and the
/// CLI bridge.
enum FrameLogCredentials {
    /// The environment variable the CLI reads the bearer token from. The
    /// token travels here and never in argv.
    static let tokenEnvironmentKey = "SCANNY_BOY_FRAMELOG_TOKEN"
    /// The Keychain service of the installed app. Development appends
    /// `.dev`, so a development build never reads or overwrites the
    /// installed app's token.
    static let productionService = "com.lonniesmith.scanny-boy.framelog"
    /// `fl_` plus eight characters: the only part of a saved token the
    /// Settings pane ever shows.
    static let displayPrefixLength = 11
}

/// Where the Frame log bearer token is kept.
protocol FrameLogTokenStore: Sendable {
    func token() -> String?
    func setToken(_ token: String) throws
    func clear() throws
}

extension FrameLogTokenStore {
    /// The token as the Settings pane displays it (`fl_` plus eight
    /// characters), or `nil` when no token is saved.
    var tokenPrefix: String? {
        token().map { String($0.prefix(FrameLogCredentials.displayPrefixLength)) }
    }
}

enum FrameLogCredentialError: LocalizedError, Equatable {
    case keychain(OSStatus)

    var errorDescription: String? {
        switch self {
        case .keychain(let status): "The Keychain refused the token (status \(status))."
        }
    }
}

/// The token as a generic-password item in the user's login Keychain.
struct KeychainFrameLogTokenStore: FrameLogTokenStore {
    let service: String
    private let account = "token"

    init(service: String) {
        self.service = service
    }

    func token() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func setToken(_ token: String) throws {
        let data = Data(token.utf8)
        let status = SecItemCopyMatching(baseQuery as CFDictionary, nil)
        if status == errSecSuccess {
            let update = SecItemUpdate(
                baseQuery as CFDictionary,
                [kSecValueData as String: data] as CFDictionary
            )
            guard update == errSecSuccess else {
                throw FrameLogCredentialError.keychain(update)
            }
            return
        }
        guard status == errSecItemNotFound else {
            throw FrameLogCredentialError.keychain(status)
        }
        var item = baseQuery
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let added = SecItemAdd(item as CFDictionary, nil)
        guard added == errSecSuccess else {
            throw FrameLogCredentialError.keychain(added)
        }
    }

    func clear() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw FrameLogCredentialError.keychain(status)
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

/// A process-local token, for tests and the `.test` profile, so that no
/// test ever touches the Keychain.
final class InMemoryFrameLogTokenStore: FrameLogTokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var stored: String?

    init(token: String? = nil) {
        stored = token
    }

    func token() -> String? {
        lock.lock()
        defer { lock.unlock() }
        return stored
    }

    func setToken(_ token: String) throws {
        lock.lock()
        defer { lock.unlock() }
        stored = token
    }

    func clear() throws {
        lock.lock()
        defer { lock.unlock() }
        stored = nil
    }
}

/// The Frame log settings that are not secret: the base URL, kept in the
/// profile's preferences.
struct FrameLogSettings {
    static let baseURLKey = "framelogBaseURL"
    static let defaultBaseURL = "https://lonnie-smith.com"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = AppEnvironment.defaults) {
        self.defaults = defaults
    }

    /// Clearing the field (an empty or blank value) restores the default.
    var baseURL: String {
        get { defaults.string(forKey: Self.baseURLKey) ?? Self.defaultBaseURL }
        nonmutating set {
            let trimmed = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                defaults.removeObject(forKey: Self.baseURLKey)
            } else {
                defaults.set(trimmed, forKey: Self.baseURLKey)
            }
        }
    }
}
