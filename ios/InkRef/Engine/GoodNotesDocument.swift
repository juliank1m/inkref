import CoreGraphics
import Foundation

/// GoodNotes stores coordinates in **1/132 inch**, not points. `points = units * 6/11`.
/// Both public parsers of this format get it wrong by 1.8333x, and one inherited the error
/// from the other (FINDINGS §2). The conversion happens here and nowhere else — everything
/// above this file speaks points.
public let pointsPerUnit = 6.0 / 11.0
public let unitsPerPoint = 11.0 / 6.0

/// Stroke width is stored in a DIFFERENT unit from coordinates — 1/144 inch, exactly two
/// units per point. Calibrated over a 48x range with spread 0.0000 (FINDINGS §2). Using the
/// 11/6 coordinate scale here is a 9% error, and it is an easy one to make.
///
/// The variable-width family looks different again: one measured stroke came back at
/// roughly 1:1. That is a single observation on a family this code never authors, so it is
/// LIKELY, not confirmed, and it only affects how thick a preview looks.
func widthInPoints(_ units: Double, family: String) -> Double {
    switch family {
    case StrokeFamily.constantWidth, StrokeFamily.constantWidthV1: return units / 2
    default: return units
    }
}

/// One drawable stroke, in points, y down.
public struct StrokePath: Sendable {
    public let index: Int            // stable index within its page
    public let box: InkBox
    public let segments: [PathSeg]
    public let width: Double
    public let red: Double, green: Double, blue: Double, alpha: Double
}

/// A page's paper: its real size in points and the template PDF drawn behind the ink.
public struct Paper: Sendable {
    public let size: CGSize?
    public let name: String?
    public let attachment: String?
}

public final class GNPage {
    enum Entry {
        case stroke(StrokeRecord)
        case other(descriptor: [UInt8], item: [UInt8])
    }

    public let id: String
    public internal(set) var paper: Paper?
    let memberPath: String
    var entries: [Entry]
    private var drawable: [Int] = []          // indices into `entries`, in drawable order

    init(id: String, memberPath: String, messages: [[UInt8]]) throws {
        self.id = id
        self.memberPath = memberPath
        guard messages.count % 2 == 0 else {
            throw GNError.format("page \(id): odd record count \(messages.count)")
        }
        var entries: [Entry] = []
        for i in stride(from: 0, to: messages.count, by: 2) {
            let descriptor = messages[i], item = messages[i + 1]
            if StrokeRecord.isPenStroke(item) {
                entries.append(.stroke(StrokeRecord(descriptor: descriptor, item: item)))
            } else {
                entries.append(.other(descriptor: descriptor, item: item))
            }
        }
        self.entries = entries
    }

    /// Every live pen stroke that actually has geometry, in paint order.
    ///
    /// Tombstones fall out here for free: deleting a stroke empties its arrays, so it has
    /// no bounds and no path. They are still carried through to the output untouched.
    public func drawableStrokes() throws -> [StrokePath] {
        var out: [StrokePath] = []
        var indices: [Int] = []
        for (i, entry) in entries.enumerated() {
            guard case .stroke(let record) = entry, !record.isDeleted else { continue }
            guard let geometry = try? record.geometry(), let box = geometry.bounds else { continue }
            let (segments, width) = geometry.path
            guard !segments.isEmpty else { continue }
            let c = record.color ?? (0, 0, 0, 1)
            out.append(StrokePath(
                index: out.count,
                box: InkBox(x0: box.x0 * pointsPerUnit, y0: box.y0 * pointsPerUnit,
                            x1: box.x1 * pointsPerUnit, y1: box.y1 * pointsPerUnit),
                segments: segments.map {
                    switch $0 {
                    case .move(let x, let y):
                        return .move(x: x * pointsPerUnit, y: y * pointsPerUnit)
                    case .quad(let cx, let cy, let x, let y):
                        return .quad(cx: cx * pointsPerUnit, cy: cy * pointsPerUnit,
                                     x: x * pointsPerUnit, y: y * pointsPerUnit)
                    }
                },
                width: max(widthInPoints(width, family: geometry.signature), 0.4),
                red: c.0, green: c.1, blue: c.2, alpha: c.3))
            indices.append(i)
        }
        drawable = indices
        return out
    }

    /// Move one drawable stroke. `index` is its position in the last `drawableStrokes()`
    /// result; `dx`/`dy` are in points.
    public func translate(_ index: Int, dx: Double, dy: Double) throws {
        if drawable.isEmpty { _ = try drawableStrokes() }
        guard index >= 0, index < drawable.count else {
            throw GNError.format("stroke index \(index) out of range")
        }
        guard case .stroke(let record) = entries[drawable[index]] else { return }
        try record.translate(dx: dx * unitsPerPoint, dy: dy * unitsPerPoint)
    }

    func serialize() -> [UInt8] {
        var messages: [[UInt8]] = []
        for entry in entries {
            switch entry {
            case .stroke(let r): messages += [r.descriptor, r.item]
            case .other(let d, let i): messages += [d, i]
            }
        }
        return PB.writeStream(messages)
    }
}

public final class GoodNotesDocument {
    static let indexNotes = "index.notes.pb"
    static let indexEvents = "index.events.pb"
    static let schemaMember = "schema.pb"

    var archive: ZipArchive
    public private(set) var pages: [GNPage] = []

    init(archive: ZipArchive) throws {
        self.archive = archive
        guard let index = archive.data(named: Self.indexNotes) else {
            throw GNError.format("no \(Self.indexNotes): not a GoodNotes archive")
        }
        for message in try PB.readStream(index) {
            guard let uuid = try PB.lastBytes(1, in: message),
                  let path = try PB.lastBytes(2, in: message),
                  let id = String(bytes: uuid, encoding: .utf8),
                  let member = String(bytes: path, encoding: .utf8) else { continue }
            let raw = archive.data(named: member) ?? []
            let messages = raw.isEmpty ? [] : try PB.readStream(raw)
            pages.append(try GNPage(id: id, memberPath: member, messages: messages))
        }
        guard !pages.isEmpty else { throw GNError.format("document has no pages") }

        // A page does not name its background directly: the page-created event (54) holds a
        // paper uuid, and a paper-definition event (2) subjected to THAT uuid holds the
        // template attachment and the page size. The page-created event is subjected to an
        // id one lower in its last hex digit than the page id, so the stable prefix is the
        // key. LIKELY — measured on 3 of 3 pages of one real notebook.
        if let events = archive.data(named: Self.indexEvents) {
            var pageToPaper: [String: String] = [:]
            var papers: [String: Paper] = [:]
            for message in (try? PB.readStream(events)) ?? [] {
                guard let subjectRaw = try? PB.lastBytes(1, in: message),
                      let subject = String(bytes: subjectRaw, encoding: .utf8) else { continue }
                if let body = try? PB.lastBytes(54, in: message) {
                    if let ref = try? PB.lastBytes(3, in: body),
                       let paperRaw = try? PB.lastBytes(1, in: ref),
                       let paper = String(bytes: paperRaw, encoding: .utf8) {
                        pageToPaper[String(subject.dropLast())] = paper
                    }
                } else if let body = try? PB.lastBytes(2, in: message) {
                    let attachment = (try? PB.lastBytes(4, in: body))
                        .flatMap { $0 }.flatMap { String(bytes: $0, encoding: .utf8) }
                    let name = (try? PB.lastBytes(9, in: body))
                        .flatMap { $0 }.flatMap { String(bytes: $0, encoding: .utf8) }
                    var size: CGSize? = nil
                    if let dims = try? PB.lastBytes(8, in: body) {
                        let values = ((try? PB.split(dims)) ?? [])
                            .filter { $0.wire == PB.wire64 || $0.wire == PB.wire32 }
                        if values.count >= 2, let w = PB.double(values[0], in: dims),
                           let h = PB.double(values[1], in: dims) {
                            size = CGSize(width: w * pointsPerUnit, height: h * pointsPerUnit)
                        }
                    }
                    papers[subject] = Paper(size: size, name: name, attachment: attachment)
                }
            }
            for page in pages {
                if let key = pageToPaper[String(page.id.dropLast())] {
                    page.paper = papers[key]
                }
            }
        }
    }

    /// The page's template as PDF bytes. One-page PDFs, produced by svg2pdf.
    public func background(for page: GNPage) -> Data? {
        guard let uuid = page.paper?.attachment,
              let bytes = archive.attachment(uuid,
                  index: archive.data(named: "index.attachments.pb")) else { return nil }
        return Data(bytes)
    }

    public static func open(_ url: URL) throws -> GoodNotesDocument {
        try GoodNotesDocument(archive: ZipArchive.read([UInt8](try Data(contentsOf: url))))
    }

    public var schema: UInt64? {
        guard let raw = archive.data(named: Self.schemaMember) else { return nil }
        return (try? PB.lastVarint(1, in: raw)) ?? nil
    }

    /// Writes a copy. The document the user picked is never modified — SPEC §15, and the
    /// reason every transform here operates on an in-memory archive.
    public func write(to url: URL) throws {
        for page in pages where archive.index(of: page.memberPath) != nil {
            archive.replace(page.memberPath, with: page.serialize())
        }
        try Data(try archive.write()).write(to: url, options: .atomic)
    }
}
