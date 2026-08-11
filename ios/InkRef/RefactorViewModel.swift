import SwiftUI
import UIKit

enum Status: Equatable {
    case idle, loading, analyzing, ready
    case failed(String)
}

/// One page, ready to draw and ready to export. `strokes` and `offsets` are parallel: the
/// offset at index k belongs to `strokes[k]`, whose own `.index` is what the engine's
/// `translate` addresses.
struct PagePreview: Identifiable {
    let id: String
    var strokes: [StrokePath]
    var offsets: [Offset]
    var paperSize: CGSize?              // the page's own size, when the document declares one
    var background: Data?               // its template PDF
    var analysis: InkLayout.Analysis
    var roles: [Role]
    var strengthUsed: String?           // may be gentler than asked
    var declined: String?               // the measure that stopped the plan being kept
    var before: LayoutMetrics
    var after: LayoutMetrics
    var source: String                  // which classifier named the roles
    var structure = "geometry"          // "ocr" or "geometry" — who found the lines
    var recognized: [RecognizedLine] = []   // for the debug overlay
    var groups: [WordGroup] = []
    var unmatched: [Int] = []           // strokes no group claimed; deliberately untouched
    var constrained = Collide.Report()  // moves the collision gate reduced or cancelled
    var spacing = Flow.Report()         // what Stage 8 line spacing did

    var improvement: LayoutMetrics { after.improvement(over: before) }
    var moved: Int { offsets.filter { !$0.isZero }.count }

    var caption: String {
        let words = analysis.lines.reduce(0) { $0 + $1.words.count }
        let named = Set(roles.filter { $0 != .paragraph }.map(\.rawValue)).sorted()
        var parts = ["\(strokes.count) strokes", "\(analysis.lines.count) lines",
                     "\(words) words", "\(moved) moved"]
        if structure == "ocr" {
            let read = groups.reduce(0) { $0 + $1.indices.count }
            let share = strokes.isEmpty ? 0 : Double(read) / Double(strokes.count)
            // The share is the honest headline: the rest of the page was left untouched
            // on purpose, and saying so is the difference between "safe" and "broken".
            parts.append(String(format: "read %.0f%% of the ink", share * 100))
        }
        // A move the page had no room for is a decision, not a rounding error; showing it
        // is the difference between "nothing happened" and "nothing could safely happen".
        if constrained.touched > 0 {
            parts.append("\(constrained.touched) moves held back to clear other ink")
        }
        if spacing.lines > 0 {
            parts.append("\(spacing.lines) lines re-spaced"
                         + (spacing.followers > 0
                            ? ", \(spacing.followers) unread strokes travelling with them" : ""))
        }
        parts.append(named.isEmpty ? "structure via \(source)"
                                   : "\(source): " + named.joined(separator: ", "))
        // Saying nothing here would make a deliberately untouched page look like a bug.
        if let declined {
            parts.append("left unchanged — refactoring would have worsened \(declined)")
        } else if let strengthUsed, strengthUsed != "balanced" {
            parts.append("eased to \(strengthUsed)")
        }
        return parts.joined(separator: " · ")
    }
}

/// Geometry for one page, produced entirely off the main actor. Deliberately holds no
/// reference to the document — once this exists the parser is out of the picture.
private struct PageGeometry {
    let id: String
    let paperSize: CGSize?
    let background: Data?
    let strokes: [StrokePath]
    let analysis: InkLayout.Analysis      // from geometry; the fallback, and sizes the tiles
}

/// What reading the page found. Empty when recognition is off or found nothing, in which
/// case the geometry analysis stands.
private struct PageReading {
    var lines: [RecognizedLine] = []
    var groups: [WordGroup] = []
    var unmatched: [Int] = []
    var analysis: InkLayout.Analysis? = nil
}

private enum RefactorError: LocalizedError {
    case noInk

    var errorDescription: String? {
        "No page in this document has enough handwriting to analyse."
    }
}

@MainActor
@Observable
final class RefactorViewModel {
    var documentName: String?
    var pageCount = 0
    var strength: InkLayout.Strength = .balanced
    var aiMode: AIMode = .auto
    var useVision = false
    /// Read the page with Vision to find its lines and words, instead of clustering stroke
    /// boxes. On by default: it is the difference between guessing where a word ends and
    /// knowing. On device, so it costs nothing but a few seconds.
    var readPage = true
    var showStructure = false
    var showRecognition = false
    var showRefactored = false
    var status: Status = .idle
    /// What the long run is doing right now. Reading a dense page takes several seconds and
    /// a bare spinner for half a minute is indistinguishable from a hang — which, in front
    /// of an audience, is the same thing as a hang.
    var progress: String?
    var progressFraction: Double?
    var pages: [PagePreview] = []
    var exportURL: URL?
    private(set) var isExporting = false

    /// The app's own copy of the picked file. SPEC §15: the user's original is read once,
    /// copied, and never opened for writing again.
    private var sourceCopy: URL?
    private var geometry: [PageGeometry] = []

    func load(_ url: URL) async {
        reset()
        status = .loading
        do {
            let copy = try await Self.copyIn(url)
            let scan = try await Self.scan(copy)
            guard !scan.pages.isEmpty else { throw RefactorError.noInk }
            sourceCopy = copy
            geometry = scan.pages
            documentName = url.deletingPathExtension().lastPathComponent
            pageCount = scan.count
            status = .ready
        } catch {
            status = .failed(Self.message(error))
        }
    }

    /// A messy page that ships with the app.
    ///
    /// Demo insurance, and worth the few hundred kilobytes: sharing a document out of
    /// GoodNotes in front of an audience depends on the share sheet, the Files provider and
    /// whatever the iPad feels like doing, and none of that is the thing being demonstrated.
    func loadSample() async {
        guard let url = Bundle.main.url(forResource: "demo", withExtension: "goodnotes") else {
            status = .failed("The bundled sample is missing from this build.")
            return
        }
        await load(url)
        documentName = "Sample lecture notes"
    }

    /// Classify, then plan. Produces offsets and metrics only — nothing is written here, so
    /// the user can flip between before and after as long as they like (SPEC §15, Undo).
    func refactor() async {
        guard !geometry.isEmpty, status != .analyzing else { return }
        status = .analyzing
        exportURL = nil
        withAnimation(.beautify) { showRefactored = false }

        let analyzer = makeAnalyzer(aiMode)
        let requested = strength      // one run, one strength, even if the picker moves
        let reading = readPage      // one run, one setting
        var built: [PagePreview] = []
        for (n, page) in geometry.enumerated() {
            progressFraction = Double(n) / Double(geometry.count)
            progress = geometry.count == 1 ? "Reading the page…"
                                           : "Reading page \(n + 1) of \(geometry.count)…"
            // Reading the page comes first: it decides where the lines and words are, and
            // every role, block description and metric below is about *those* lines.
            let read = reading ? await Self.read(page) : PageReading()
            let analysis = read.analysis ?? page.analysis
            var roles = [Role](repeating: .paragraph, count: analysis.lines.count)
            var groups: [[Int]] = []
            var source = analyzer == nil ? "geometry only" : "geometry"
            if let analyzer {
                // The page image is the only thing that leaves on an explicit opt-in (the
                // line geometry goes with every call), and only the vision analyzer can use
                // it — rendering one per page for the heuristic is pure waste.
                let image = useVision && analyzer is BackboardAnalyzer
                    ? pageImage(page.strokes) : nil
                let result = await analyzer.analyze(
                    InkLayout.describe(analysis, texts: Self.lineTexts(analysis, read.groups)),
                    image: image)
                if result.roles.count == roles.count { roles = result.roles }
                source = result.source
                groups = result.groups
            }
            progress = geometry.count == 1 ? "Working out the layout…"
                                           : "Laying out page \(n + 1) of \(geometry.count)…"
            built.append(await Self.finish(page, analysis: analysis, read: read,
                                           strength: requested, roles: roles,
                                           groups: groups, source: source))
            // Shown as each page lands, so a multi-page document is visibly progressing
            // rather than silently accumulating.
            pages = built
        }

        progress = nil
        progressFraction = nil
        status = .ready
        // The picker moved while this run was in flight, so the layout on screen is not the
        // strength the control claims. Redo it rather than leave the two disagreeing.
        if strength != requested { return await refactor() }
        // The cards were only just inserted at progress 0; flipping in this same turn would
        // draw them already refactored, with nothing for SwiftUI to interpolate from. The
        // strokes travelling into place is the demo (SPEC §9 step 4), so yield a frame first.
        try? await Task.sleep(for: .milliseconds(80))
        withAnimation(.beautify) { showRefactored = true }
    }

    /// Applies the plan to a *fresh* open of the copy, so exporting twice cannot translate
    /// the same stroke twice.
    func export() async {
        guard let source = sourceCopy, !pages.isEmpty, !isExporting else { return }
        // Exporting twice in a demo is normal; writing it twice is not, and the second
        // write would translate every stroke a second time if it reused the same copy.
        if exportURL != nil { return }
        isExporting = true
        defer { isExporting = false }
        // GoodNotes imports by filename, so this is the name the user will see in their
        // library. Keeping the original title first makes it sort next to the source.
        let name = (documentName ?? "Notes") + " (InkRef).goodnotes"
        do {
            exportURL = try await Self.write(source: source, pages: pages, named: name)
        } catch {
            status = .failed(Self.message(error))
        }
    }

    func reset() {
        progress = nil
        progressFraction = nil
        documentName = nil
        pageCount = 0
        pages = []
        geometry = []
        sourceCopy = nil
        exportURL = nil
        showRefactored = false
        showStructure = false
        status = .idle
    }

    /// A flat rendering of the page for the vision classifier. Reuses the preview so the
    /// model sees exactly what the user sees, and stays a view-layer concern — the engine
    /// never learns how to draw.
    private func pageImage(_ strokes: [StrokePath]) -> Data? {
        let renderer = ImageRenderer(content:
            PreviewCanvas(strokes: strokes, offsets: [], analysis: nil, roles: [],
                          zoomable: false)
                .frame(width: 900, height: 1200)
                .background(.white))
        renderer.scale = 1
        return renderer.uiImage?.pngData()
    }

    // MARK: - off the main actor
    // These are `nonisolated async`, so they run on the cooperative pool no matter who calls
    // them (SE-0338). Parsing and writing a document must never touch the UI thread.

    private nonisolated static func workDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("InkRef", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private nonisolated static func copyIn(_ url: URL) async throws -> URL {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let dest = try workDir().appendingPathComponent("source-" + url.lastPathComponent)
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.copyItem(at: url, to: dest)
        return dest
    }

    private nonisolated static func scan(_ url: URL) async throws -> (count: Int, pages: [PageGeometry]) {
        let doc = try GoodNotesDocument.open(url)
        var out: [PageGeometry] = []
        for page in doc.pages {
            let strokes = try page.drawableStrokes()
            guard strokes.count >= 2 else { continue }   // a page with a scribble has no layout
            let analysis = InkLayout.analyze(strokes.map(\.box))
            out.append(PageGeometry(id: page.id, paperSize: page.paper?.size,
                                    background: doc.background(for: page),
                                    strokes: strokes, analysis: analysis))
        }
        return (doc.pages.count, out)
    }

    /// Render the page, read it, and map what came back onto its own strokes.
    ///
    /// Everything Vision needs is on device, so this costs nothing but time and nothing
    /// leaves the iPad. A page it cannot read comes back empty and geometry stands.
    private nonisolated static func read(_ page: PageGeometry) async -> PageReading {
        let boxes = page.strokes.map(\.box)
        let recognizer = VisionRecognizer()
        var lines: [RecognizedLine] = []
        // The *cheap* per-stroke estimate, deliberately, and not `analysis.refH`. The
        // tile size that was measured to work (86-90% of strokes grouped, against 37% for
        // a single image) was calibrated against this number, and it is also the only one
        // available before recognition has happened. Using the refined writing height here
        // made tiles twice as big and cost the Swift engine nineteen points of coverage
        // against Python on the same page.
        for (image, transform) in PageRender.tiles(page.strokes, paper: page.paperSize,
                                                   refH: refHeight(boxes)) {
            lines += (try? recognizer.recognize(image, in: transform)) ?? []
        }
        guard !lines.isEmpty else { return PageReading() }
        lines = Recognition.mergeStacked(Recognition.dedupe(lines))
        let (groups, unmatched) = StrokeMapper.map(lines, boxes: boxes)
        guard !groups.isEmpty else { return PageReading() }
        return PageReading(lines: lines, groups: groups, unmatched: unmatched,
                           analysis: StrokeMapper.analysis(groups, boxes: boxes))
    }

    /// What each line says, from the words mapped onto it. Rough transcription is fine —
    /// it is read by a classifier, never redrawn.
    private nonisolated static func lineTexts(_ a: InkLayout.Analysis,
                                              _ groups: [WordGroup]) -> [String]? {
        guard !groups.isEmpty else { return nil }
        var owner = [Int: String]()
        for g in groups { for i in g.indices { owner[i] = g.text } }
        return a.lines.map { line in
            // ordered de-duplication: a line's words must stay in the order they were written
            var seen = Set<String>(), words: [String] = []
            for i in line.indices {
                if let t = owner[i], !t.isEmpty, seen.insert(t).inserted { words.append(t) }
            }
            return words.joined(separator: " ")
        }
    }

    private nonisolated static func finish(_ page: PageGeometry,
                                           analysis read: InkLayout.Analysis,
                                           read reading: PageReading,
                                           strength: InkLayout.Strength,
                                           roles: [Role], groups: [[Int]] = [],
                                           source: String) async -> PagePreview {
        let boxes = page.strokes.map(\.box)
        // what the model grouped becomes one rigid line, so the plan cannot reach inside
        // an equation to re-space it
        // Merging renumbers the lines, so the roles are carried across by stroke. Dropping
        // them instead — which this used to do — unfreezes every equation and diagram on the
        // page the moment the model returns any group at all.
        var analysis = read
        var planRoles: [Role]? = roles
        if !groups.isEmpty {
            var was = [Int: Role]()
            for (k, line) in read.lines.enumerated() where k < roles.count {
                for i in line.indices { was[i] = roles[k] }
            }
            analysis = InkLayout.mergeGroups(read, groups)
            planRoles = analysis.lines.map { was[$0.indices.first ?? -1] ?? .paragraph }
        }
        // verifiedPlan, not plan: a plan measured to make the page worse is eased and then
        // dropped, so a page can come back unchanged but never degraded.
        let (planned, used, declined) = InkLayout.verifiedPlan(
            analysis, boxes: boxes, strength: strength, roles: planRoles,
            skip: reading.analysis == nil ? [] : ["line"])
        // Last gate before anything moves: the planner reasons about the ink it grouped,
        // the page also holds ink nobody grouped, and only this sees both.
        let paper = PageRender.pageSize(paper: page.paperSize, boxes: boxes)
        // Followers are decided before the gate, not after, so a word and the punctuation
        // that belongs to it are one group from the first move onward.
        let follow = Flow.followers(analysis, boxes: boxes,
                                    unmatched: reading.unmatched, roles: planRoles)
        let wordGroups = Flow.wordGroups(analysis, follow, boxes)
        let seeded = Flow.adopt(wordGroups, planned, follow)
        let (gated, gate) = Collide.constrain(
            analysis, boxes: boxes, offsets: seeded, roles: planRoles, page: paper,
            groups: wordGroups, followers: follow)
        // Stage 8 on top of the within-line corrections, never instead of them: it moves
        // each finished line as one piece, with the unread ink that follows it.
        let (offsets, spacing) = Flow.space(
            analysis, boxes: boxes, offsets: gated, roles: planRoles,
            unmatched: reading.unmatched, page: paper, strength: strength, follow: follow)
        return PagePreview(
            id: page.id, strokes: page.strokes, offsets: offsets,
            paperSize: page.paperSize, background: page.background, analysis: read,
            roles: roles, strengthUsed: used?.name, declined: declined,
            // Scored on the structure the plan was actually made against, and on the same
            // lines before and after — the numbers must agree with the guard that accepted
            // the plan, and re-analysing the result compares two different structures.
            before: InkLayout.metrics(boxes, analysis: analysis, roles: planRoles),
            after: {
                let shifted = zip(boxes, offsets).map { $0.offset(by: $1) }
                return InkLayout.metrics(shifted,
                                         analysis: reproject(analysis, shifted),
                                         roles: planRoles)
            }(),
            source: source,
            structure: reading.analysis == nil ? "geometry" : "ocr",
            recognized: reading.lines, groups: reading.groups, unmatched: reading.unmatched,
            constrained: gate, spacing: spacing)
    }

    private nonisolated static func write(source: URL, pages: [PagePreview],
                                          named: String) async throws -> URL {
        let doc = try GoodNotesDocument.open(source)
        let plan = Dictionary(pages.map { ($0.id, Array(zip($0.strokes.map(\.index), $0.offsets))) },
                              uniquingKeysWith: { first, _ in first })
        for page in doc.pages {
            for (index, offset) in plan[page.id] ?? [] where !offset.isZero {
                try page.translate(index, dx: offset.dx, dy: offset.dy)
            }
        }
        let out = try workDir().appendingPathComponent(named)
        try? FileManager.default.removeItem(at: out)
        try doc.write(to: out)
        return out
    }

    private nonisolated static func message(_ error: Error) -> String {
        switch error as? GNError {
        case let .format(detail):
            return "That doesn't look like a GoodNotes document we can read — \(detail)"
        case let .unsupported(detail):
            return "This document uses something InkRef doesn't handle yet — \(detail)"
        case nil:
            return error.localizedDescription
        }
    }
}
