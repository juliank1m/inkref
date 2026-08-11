import Foundation

/// Semantic analysis: what is each detected line?
///
/// The rest of InkRef depends on `SemanticAnalyzer` and never on Backboard. Two
/// implementations satisfy it: `HeuristicAnalyzer` (geometry only, no network, always
/// available — this is the floor) and `BackboardAnalyzer` (a vision model, with the
/// heuristic underneath it).
///
/// The second degrades into the first on every failure worth naming: no API key, network
/// down, timeout, non-JSON reply, invented block ids, unknown types, low confidence. The
/// deterministic formatter has to keep working when the model does not, so `analyze` never
/// throws. The model is asked WHAT a region is — never where anything goes, never for a
/// coordinate — and recognized `text` is metadata that never redraws ink.

/// What a model is allowed to say a region is.
public enum BlockType: String, Codable, Sendable, CaseIterable {
    case heading, paragraph
    case bulletList = "bullet_list"
    // A list and an item in it are the same thing at line granularity, which is the only
    // granularity we classify at. Accept both names rather than argue with the model.
    case bulletItem = "bullet_item"
    case equation, diagram, annotation, unknown
    // tolerated synonyms — liberal in what we accept, strict in what we act on
    case drawing, other

    /// What that means to the layout engine.
    ///
    /// `unknown` is the floor, not `paragraph`. A model that saw the page and could not
    /// name a region has told us something, and answering "prose" on its behalf would
    /// license the full prose treatment on a guess we invented. An unnamed region still
    /// gets the ordinary within-line cleanup (see `Role.isUnnamed`) — just no semantic rule.
    public var role: Role {
        switch self {
        case .heading: return .heading
        case .bulletList, .bulletItem: return .bullet
        case .equation: return .equation
        case .diagram, .drawing: return .diagram
        case .paragraph: return .paragraph
        case .annotation: return .annotation
        case .unknown, .other: return .unknown
        }
    }

    public init(role: Role) {
        switch role {
        case .heading: self = .heading
        case .bullet: self = .bulletList
        case .equation: self = .equation
        case .diagram: self = .diagram
        case .paragraph: self = .paragraph
        case .annotation: self = .annotation
        case .unknown: self = .unknown
        }
    }
}

public struct SemanticBlock: Sendable {
    public let id: String
    public let type: BlockType
    public let confidence: Double
    public let text: String

    public init(id: String, type: BlockType, confidence: Double = 0, text: String = "") {
        self.id = id; self.type = type; self.confidence = confidence; self.text = text
    }

    public var role: Role { type.role }
}

/// What the layout engine consumes. `roles` is one role per line, in order.
public struct SemanticResult: Sendable {
    public var groups: [[Int]] = []      // line indices that must move as one unit
    public var roles: [Role]
    public var blocks: [SemanticBlock]      // only what survived validation
    public var source: String               // backboard | heuristic | none
    public var warnings: [String]

    public init(roles: [Role] = [], blocks: [SemanticBlock] = [],
                source: String = "none", warnings: [String] = []) {
        self.roles = roles; self.blocks = blocks; self.source = source; self.warnings = warnings
    }

    public func label(_ index: Int) -> Role {
        index >= 0 && index < roles.count ? roles[index] : .paragraph
    }
}

public protocol SemanticAnalyzer: Sendable {
    var name: String { get }
    func analyze(_ blocks: [BlockDescription], image: Data?) async -> SemanticResult
}

/// Geometry-only classification. Deliberately timid — it only claims what the shape of the
/// page makes obvious, and everything else stays prose.
public struct HeuristicAnalyzer: SemanticAnalyzer {
    public let name = "heuristic"
    public init() {}

    public func analyze(_ blocks: [BlockDescription], image: Data? = nil) async -> SemanticResult {
        var roles: [Role] = [], out: [SemanticBlock] = []
        for (i, b) in blocks.enumerated() {
            var role = Role.paragraph, confidence = 0.0
            if b.startsWithMark {
                (role, confidence) = (.bullet, 0.75)
            } else if b.heightRatio >= 1.25 && b.words <= 4 {
                (role, confidence) = (.heading, 0.6)
            } else if i == 0 && b.words <= 4 {
                (role, confidence) = (.heading, 0.55)
            }
            roles.append(role)
            if confidence > 0 {
                out.append(SemanticBlock(id: b.id, type: BlockType(role: role),
                                         confidence: confidence))
            }
        }
        return SemanticResult(roles: roles, blocks: out, source: name)
    }
}

/// Vision classification through Backboard, with the heuristic as the floor.
///
/// One retry: a model that answers with prose or a fenced block usually complies when told
/// so plainly. After that the heuristic result is returned — a slightly worse layout is
/// always better than a failed one.
public struct BackboardAnalyzer: SemanticAnalyzer {
    public let name = "backboard"
    public let client: BackboardClient
    public let fallback: HeuristicAnalyzer
    public let attempts: Int

    public init(client: BackboardClient? = nil, fallback: HeuristicAnalyzer = HeuristicAnalyzer(),
                attempts: Int = 2) {
        self.client = client ?? BackboardClient()
        self.fallback = fallback
        self.attempts = attempts
    }

    public var isAvailable: Bool { client.isAvailable }

    public func analyze(_ blocks: [BlockDescription], image: Data? = nil) async -> SemanticResult {
        var base = await fallback.analyze(blocks, image: image)
        guard !blocks.isEmpty else { return base }
        guard client.isAvailable else {
            base.warnings.append("BACKBOARD_API_KEY not set; used geometry heuristics")
            return base
        }

        let prompt = Self.prompt(for: blocks)
        let ids = blocks.map(\.id)
        var warnings: [String] = []

        for attempt in 0..<max(1, attempts) {
            let found: [SemanticBlock], notes: [String]
            do {
                let reply = try await client.ask(
                    content: attempt == 0 ? prompt : prompt + "\nReturn raw JSON only.",
                    system: Self.system, image: image)
                (found, notes) = try ModelOutput.parseBlocks(reply, validIDs: ids)
            } catch {
                warnings.append("attempt \(attempt + 1): \(error)")
                continue
            }
            guard !found.isEmpty else {
                warnings.append("attempt \(attempt + 1): nothing survived validation")
                continue
            }
            // A region the model named gets that name. A region it did not is `unknown`,
            // not prose — the model saw the page, and answering on its behalf would
            // license the full prose treatment on a guess we invented. The exception is a
            // region the geometry heuristic made a positive claim about (a bullet mark, an
            // oversized first line): that claim stands on its own evidence and survives.
            let byID = Dictionary(found.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            let claimed = Set(base.blocks.map(\.id))
            var roles: [Role] = []
            var unnamed = 0
            for (i, b) in blocks.enumerated() {
                if let hit = byID[b.id] {
                    roles.append(hit.role)
                } else if claimed.contains(b.id), i < base.roles.count {
                    roles.append(base.roles[i])
                } else {
                    roles.append(.unknown)
                    unnamed += 1
                }
            }
            if unnamed > 0 {
                warnings.append("\(unnamed) of \(blocks.count) regions unnamed by the "
                                + "model; left unclassified")
            }
            return SemanticResult(roles: roles, blocks: found, source: name,
                                  warnings: warnings + notes)
        }

        base.warnings.append(contentsOf: warnings + ["fell back to geometry heuristics"])
        return base
    }

    static let system = """
        You classify regions of a handwritten page. You are given the geometry of each \
        detected line and, when available, an image of the page. Reply with JSON only. \
        Never invent an id that was not given to you. Never suggest coordinates, layout or \
        corrections — another system owns those. If unsure, say 'other' with low confidence.
        """

    static func prompt(for blocks: [BlockDescription]) -> String {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let geometry = (try? encoder.encode(blocks)).map { String(decoding: $0, as: UTF8.self) } ?? "[]"
        let types = BlockType.allCases.map(\.rawValue).joined(separator: ", ")
        return """
            Classify each line of this handwritten page.

            Lines detected by our geometry engine (coordinates in points, y down, origin top-left):
            \(geometry)

            Reply with JSON matching exactly this shape and nothing else:
            {
              "regions": [
                {"id": "<one of the given ids>",
                 "role": "\(types.replacingOccurrences(of: ", ", with: "|"))",
                 "confidence": 0.0-1.0}
              ]
            }

            Rules:
            - one entry per line id above, using those ids verbatim
            - `type` must be one of: \(types)
            - `confidence` is your own 0-1 estimate
            - `text` is optional and is metadata only; it is never used to redraw anything
            """
    }
}

public enum AIMode: String, CaseIterable, Sendable {
    case auto, off, heuristic, backboard
}

/// `auto` uses Backboard when a key is configured and the heuristic otherwise, which is
/// what makes the AI layer genuinely optional rather than optional-in-the-README.
public func makeAnalyzer(_ mode: AIMode, client: BackboardClient? = nil) -> SemanticAnalyzer? {
    switch mode {
    case .off: return nil
    case .heuristic: return HeuristicAnalyzer()
    case .backboard: return BackboardAnalyzer(client: client)
    case .auto:
        let analyzer = BackboardAnalyzer(client: client)
        return analyzer.isAvailable ? analyzer : HeuristicAnalyzer()
    }
}

/// The contract between a model and the layout engine. Nothing downstream ever sees a
/// Backboard response or a sentence of English: a model's job is finished the moment its
/// answer becomes `[SemanticBlock]`, and anything that does not validate dies here rather
/// than three layers in, where it would be moving real ink.
public enum ModelOutput {
    public static let minConfidence = 0.55   // below this the deterministic default wins

    public struct Invalid: Error, CustomStringConvertible {
        public let description: String
        init(_ description: String) { self.description = description }
    }

    /// Pull the first JSON object out of a model reply.
    ///
    /// `json_output` is ignored whenever files are attached and a vision call attaches the
    /// page image, so a ```json fence or a sentence of preamble is the norm rather than the
    /// exception — both are simply skipped by scanning to the first `{`. Balanced-brace
    /// scan, not a regex, so nested objects survive.
    public static func extractJSON(_ text: String) throws -> Any {
        let chars = Array(text)
        guard let start = chars.firstIndex(of: "{") else {
            throw Invalid(chars.isEmpty ? "empty response" : "no JSON object in response")
        }
        var depth = 0, inString = false, escaped = false
        for i in start..<chars.count {
            let ch = chars[i]
            if inString {
                if escaped { escaped = false }
                else if ch == "\\" { escaped = true }
                else if ch == "\"" { inString = false }
                continue
            }
            switch ch {
            case "\"": inString = true
            case "{": depth += 1
            case "}":
                depth -= 1
                guard depth == 0 else { break }
                let slice = Data(String(chars[start...i]).utf8)
                guard let object = try? JSONSerialization.jsonObject(with: slice) else {
                    throw Invalid("unparseable JSON")
                }
                return object
            default: break
            }
        }
        throw Invalid("unterminated JSON object")
    }

    /// -> (blocks, warnings). Throws only on unusable structure; bad *content* is dropped
    /// with a warning. An id not in `validIDs` is provably invented, so it goes.
    public static func parseBlocks(_ text: String, validIDs: [String],
                                   minConfidence: Double = minConfidence)
        throws -> ([SemanticBlock], [String]) {
        guard let payload = try extractJSON(text) as? [String: Any],
              // `regions`/`role` is the shape we ask for; `blocks`/`type` is accepted
              // too, because tolerating it costs one lookup and turns a whole retry —
              // a second billed call — into a non-event.
              let raws = (payload["regions"] as? [Any]) ?? (payload["blocks"] as? [Any]) else {
            throw Invalid("expected an object with a 'regions' array")
        }

        let valid = Set(validIDs)
        var blocks: [SemanticBlock] = [], warnings: [String] = [], seen = Set<String>()
        for raw in raws {
            guard let entry = raw as? [String: Any] else {
                warnings.append("dropped a non-object entry")
                continue
            }
            let id = string(entry["id"]) ?? ""
            guard valid.contains(id) else {
                warnings.append("dropped unknown block id '\(id)'")
                continue
            }
            guard !seen.contains(id) else {
                warnings.append("dropped duplicate block id '\(id)'")
                continue
            }
            let name = (string(entry["role"]) ?? string(entry["type"]) ?? "unknown")
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
                .replacingOccurrences(of: " ", with: "_")
            var type = BlockType.unknown
            if let known = BlockType(rawValue: name) {
                type = known
            } else {
                warnings.append("\(id): unrecognised role '\(name)', left unclassified")
            }
            let confidence = min(max((entry["confidence"] as? NSNumber)?.doubleValue
                ?? Double(string(entry["confidence"]) ?? "") ?? 0, 0), 1)
            if confidence < minConfidence {
                warnings.append(String(format:
                    "%@: %@ at %.2f below threshold, left unclassified",
                    id, type.rawValue, confidence))
                type = .unknown
            }
            seen.insert(id)
            blocks.append(SemanticBlock(id: id, type: type, confidence: confidence,
                                        text: entry["text"] as? String ?? ""))
        }
        return (blocks, warnings)
    }

    /// JSONSerialization hands back NSString/NSNumber; a numeric id is still an id.
    private static func string(_ value: Any?) -> String? {
        if let s = value as? String { return s }
        if let n = value as? NSNumber { return n.stringValue }
        return nil
    }
}

#if DEBUG
/// Runnable check for the whole degradation boundary: empty result == pass.
public enum SemanticSelfCheck {
    private struct Stub: BackboardTransport {
        let reply: String?      // nil == the network is down
        func send(_ r: URLRequest) async throws -> (Data, URLResponse) {
            guard let reply else { throw URLError(.notConnectedToInternet) }
            let body = try JSONSerialization.data(withJSONObject: ["message": reply])
            return (body, HTTPURLResponse(url: r.url!, statusCode: 200,
                                          httpVersion: nil, headerFields: nil)!)
        }
    }

    private static func analyzer(_ reply: String?) -> BackboardAnalyzer {
        BackboardAnalyzer(client: BackboardClient(config: BackboardConfig(apiKey: "test-only"),
                                                  transport: Stub(reply: reply)))
    }

    public static func run() async -> [String] {
        func line(_ id: String, words: Int, ratio: Double) -> BlockDescription {
            BlockDescription(id: id, bbox: [0, 0, 100, 20], words: words, strokes: 5,
                             heightRatio: ratio, indentLevel: 0, gapAbove: nil,
                             startsWithMark: false, nearby: [])
        }
        let lines = [line("L0", words: 2, ratio: 1.4), line("L1", words: 9, ratio: 1.0)]
        let good = #"{"blocks":[{"id":"L0","type":"heading","confidence":0.9},"# +
                   #"{"id":"L1","type":"bullet_list","confidence":0.8}]}"#
        var bad: [String] = []
        func expect(_ ok: Bool, _ why: String) { if !ok { bad.append(why) } }

        let strict = await analyzer(good).analyze(lines, image: nil)
        expect(strict.source == "backboard" && strict.roles == [.heading, .bullet],
               "strict JSON not applied: \(strict.roles)")

        let fenced = await analyzer("Here you go:\n```json\n\(good)\n```").analyze(lines, image: nil)
        expect(fenced.roles == [.heading, .bullet], "fenced reply not parsed: \(fenced.roles)")

        let garbage = await analyzer("I cannot help with that.").analyze(lines, image: nil)
        expect(garbage.source == "heuristic" && garbage.roles == [.heading, .paragraph],
               "garbage did not fall back: \(garbage.source) \(garbage.roles)")

        let invented = #"{"blocks":[{"id":"L9","type":"equation","confidence":0.99},"# +
                       #"{"id":"L1","type":"heading","confidence":0.9}]}"#
        let mixed = await analyzer(invented).analyze(lines, image: nil)
        expect(mixed.blocks.count == 1 && mixed.roles == [.heading, .heading],
               "invented id survived: \(mixed.blocks.map(\.id))")

        let offline = await analyzer(nil).analyze(lines, image: nil)
        expect(offline.source == "heuristic" && !offline.warnings.isEmpty,
               "network error did not fall back: \(offline.source)")

        let unconfigured = await BackboardAnalyzer(client: BackboardClient(config: BackboardConfig()))
            .analyze(lines, image: nil)
        expect(unconfigured.source == "heuristic", "missing key did not fall back")
        return bad
    }
}
#endif
