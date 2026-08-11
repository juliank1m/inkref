import Foundation

/// Backboard.io transport. The only file in the app that knows this vendor exists.
///
///     POST {base}/threads/messages        header: X-API-Key: <key>   (no Bearer prefix)
///
/// Two quirks of the API shape everything here:
///
///   * `json_output: true` asks for a JSON object back, but it is **ignored whenever files
///     are attached** — and a vision call attaches the page image. So the flag is only sent
///     on the text-only path and the reply is otherwise parsed leniently (`ModelOutput`).
///   * images go through the ordinary `files` multipart field; there is no vision field.
///
/// Foundation only — the GoodNotes read/write path has no dependencies and the AI layer is
/// optional, so it must not be the thing that drags one in.
public struct BackboardConfig: Sendable {
    public var apiKey: String
    public var baseURL: URL
    public var provider: String
    public var model: String
    public var timeout: TimeInterval

    public static let defaultBaseURL = URL(string: "https://app.backboard.io/api")!
    public static let defaultProvider = "anthropic"
    public static let defaultModel = "claude-sonnet-4-20250514"
    public static let defaultTimeout: TimeInterval = 30

    public init(apiKey: String = "", baseURL: URL = BackboardConfig.defaultBaseURL,
                provider: String = BackboardConfig.defaultProvider,
                model: String = BackboardConfig.defaultModel,
                timeout: TimeInterval = BackboardConfig.defaultTimeout) {
        self.apiKey = apiKey; self.baseURL = baseURL; self.provider = provider
        self.model = model; self.timeout = timeout
    }

    /// Environment first, then `UserDefaults` under the same key names: an iPad app has no
    /// shell, so the defaults store is the only way to configure a device build. A key is
    /// never read from a file in the repo and never appears in source.
    public static func fromEnvironment() -> BackboardConfig {
        func value(_ key: String) -> String? {
            if let v = ProcessInfo.processInfo.environment[key],
               !v.trimmingCharacters(in: .whitespaces).isEmpty { return v }
            if let v = UserDefaults.standard.string(forKey: key),
               !v.trimmingCharacters(in: .whitespaces).isEmpty { return v }
            return nil
        }
        let base = value("BACKBOARD_BASE_URL")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .map { url -> String in
                var s = url
                while s.hasSuffix("/") { s.removeLast() }
                return s
            }
            .flatMap(URL.init(string:)) ?? defaultBaseURL
        return BackboardConfig(
            apiKey: value("BACKBOARD_API_KEY")?.trimmingCharacters(in: .whitespaces) ?? "",
            baseURL: base,
            provider: value("BACKBOARD_PROVIDER") ?? defaultProvider,
            model: value("BACKBOARD_MODEL") ?? defaultModel,
            timeout: value("BACKBOARD_TIMEOUT").flatMap(Double.init) ?? defaultTimeout)
    }

    /// No key means the AI layer is simply off — that is a supported state, not an error.
    public var isConfigured: Bool { !apiKey.isEmpty }
}

/// Anything that stopped a usable answer coming back. Always recoverable: every caller
/// falls back to the deterministic path. Descriptions never carry the API key.
public enum BackboardError: Error, CustomStringConvertible {
    case notConfigured
    case http(Int, String)
    case unreachable(String)
    case badBody(String)

    public var description: String {
        switch self {
        case .notConfigured: return "BACKBOARD_API_KEY is not set"
        case .http(let code, let detail): return "HTTP \(code) from Backboard: \(detail)"
        case .unreachable(let why): return "cannot reach Backboard: \(why)"
        case .badBody(let why): return "Backboard returned \(why)"
        }
    }
}

/// Injected so tests never touch the network.
public protocol BackboardTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse)
}

struct URLSessionTransport: BackboardTransport {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        try await URLSession.shared.data(for: request)
    }
}

public final class BackboardClient: Sendable {
    public let config: BackboardConfig
    private let transport: any BackboardTransport

    public init(config: BackboardConfig = .fromEnvironment(), transport: BackboardTransport? = nil) {
        self.config = config
        self.transport = transport ?? URLSessionTransport()
    }

    public var isAvailable: Bool { config.isConfigured }

    /// One stateless turn. -> the model's reply text.
    ///
    /// No thread id is ever sent and memory stays off: classifying a page must not depend
    /// on, or leak into, anything the user asked before.
    public func ask(content: String, system: String? = nil, image: Data? = nil) async throws -> String {
        guard isAvailable else { throw BackboardError.notConfigured }

        var fields = [
            "content": content,
            "stream": "false",
            "memory": "off",
            "web_search": "off",
            "llm_provider": config.provider,
            "model_name": config.model,
        ]
        if let system, !system.isEmpty { fields["system_prompt"] = system }

        var request = URLRequest(url: config.baseURL.appendingPathComponent("threads/messages"))
        request.httpMethod = "POST"
        request.timeoutInterval = config.timeout
        request.setValue(config.apiKey, forHTTPHeaderField: "X-API-Key")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        if let image {
            let (contentType, body) = Self.multipart(fields: fields, image: image)
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        } else {
            var json: [String: Any] = fields
            json["stream"] = false
            json["json_output"] = true
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }

        let data: Data, response: URLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch let e as URLError {
            throw BackboardError.unreachable(e.localizedDescription)
        } catch {
            throw BackboardError.unreachable("\(error)")
        }

        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = String(decoding: data.prefix(300), as: UTF8.self)
            throw BackboardError.http(http.statusCode, detail)
        }
        guard let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw BackboardError.badBody("a non-JSON body")
        }
        if let status = payload["status"] as? String, status.uppercased() == "FAILED" {
            throw BackboardError.badBody("a FAILED run")
        }
        // `message or content`: an *empty* message still has to fall through to content,
        // which is where the reply lands on the multipart path.
        let text = [payload["message"], payload["content"]]
            .compactMap { $0 as? String }
            .first { !$0.isEmpty }
        guard let text else { throw BackboardError.badBody("no message text") }
        return text
    }

    /// Hand-rolled because it is one page image and six short fields — a form-encoder
    /// dependency for that would cost more than it saves.
    static func multipart(fields: [String: String], image: Data,
                          filename: String = "page.png") -> (String, Data) {
        let boundary = "----inkref\(UUID().uuidString.replacingOccurrences(of: "-", with: ""))"
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        for key in fields.keys.sorted() {
            append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n")
            append("\(fields[key]!)\r\n")
        }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"files\"; "
               + "filename=\"\(filename)\"\r\nContent-Type: image/png\r\n\r\n")
        body.append(image)
        append("\r\n--\(boundary)--\r\n")
        return ("multipart/form-data; boundary=\(boundary)", body)
    }
}
