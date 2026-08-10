import Foundation

/// Glue: document -> structure -> (optional semantics) -> plan -> translated ink.
///
/// Planning and applying are deliberately separate. The preview animates the plan without
/// touching the document, so "Beautify" and "Undo" cost nothing and the file is only ever
/// rewritten when the user actually exports.
public struct PageResult: Sendable {
    public let pageId: String
    public let strokes: [StrokePath]
    public let offsets: [Offset]
    public let analysis: InkLayout.Analysis
    public let roles: [Role]
    public let semanticSource: String?
    public let warnings: [String]
    public let before: LayoutMetrics
    public let after: LayoutMetrics

    public var movedCount: Int { offsets.filter { !$0.isZero }.count }
    public var maxShift: Double { offsets.map(\.magnitude).max() ?? 0 }
    public var improvement: LayoutMetrics { after.improvement(over: before) }
}

public struct BeautifyResult: Sendable {
    public let strength: String
    public let pages: [PageResult]

    public var strokeCount: Int { pages.reduce(0) { $0 + $1.strokes.count } }
    public var movedCount: Int { pages.reduce(0) { $0 + $1.movedCount } }
}

public enum Beautifier {
    /// Analyse every page and compute its offsets. Nothing is modified.
    ///
    /// `pageImage` renders a page for a vision model; it is a closure so the engine never
    /// has to import UIKit, and passing nil simply means the classifier works from
    /// geometry alone.
    public static func plan(document: GoodNotesDocument,
                            strength: InkLayout.Strength,
                            analyzer: SemanticAnalyzer?,
                            pageImage: (@Sendable (Int) -> Data?)? = nil) async -> BeautifyResult {
        var results: [PageResult] = []
        for (number, page) in document.pages.enumerated() {
            let strokes = (try? page.drawableStrokes()) ?? []
            let boxes = strokes.map(\.box)
            let analysis = InkLayout.analyze(boxes)

            var roles = [Role](repeating: .paragraph, count: analysis.lines.count)
            var source: String?
            var warnings: [String] = []
            if let analyzer, !analysis.lines.isEmpty {
                let semantic = await analyzer.analyze(InkLayout.describe(analysis),
                                                      image: pageImage?(number))
                if semantic.roles.count == analysis.lines.count { roles = semantic.roles }
                source = semantic.source
                warnings = semantic.warnings
            }

            let offsets = InkLayout.plan(analysis, strength: strength, roles: roles)
            results.append(PageResult(
                pageId: page.id, strokes: strokes, offsets: offsets, analysis: analysis,
                roles: roles, semanticSource: source, warnings: warnings,
                before: InkLayout.metrics(boxes, analysis: analysis, roles: roles),
                after: InkLayout.metrics(InkLayout.moved(boxes, offsets), analysis: nil, roles: roles)))
        }
        return BeautifyResult(strength: strength.name, pages: results)
    }

    /// Apply a plan by translating the original records. Nothing is re-authored, which is
    /// the whole reason the output survives as native editable ink.
    public static func apply(_ result: BeautifyResult, to document: GoodNotesDocument) throws {
        for (page, planned) in zip(document.pages, result.pages) {
            _ = try page.drawableStrokes()          // re-establish the index mapping
            for (i, offset) in planned.offsets.enumerated() where !offset.isZero {
                try page.translate(i, dx: offset.dx, dy: offset.dy)
            }
        }
    }
}
