import SwiftUI

/// The web preview's curve (inkref/preview.py): long enough that the eye tracks a word
/// travelling to its new baseline, short enough that a demo never waits on it.
extension Animation {
    static let beautify = Animation.timingCurve(0.2, 0.8, 0.25, 1, duration: 0.85)
}

extension Role {
    /// Same palette as the HTML preview, so screenshots of either read the same.
    var tint: Color {
        switch self {
        case .heading: return .orange
        case .bullet: return .purple
        case .equation, .diagram: return .red
        case .paragraph: return .cyan
        }
    }
}

/// Vector preview of one page. Nothing here is rasterised and nothing is redrawn: "after"
/// is the very same `Path` translated by the engine's per-stroke `Offset` — the identical
/// edit that gets written into the document, so the preview cannot disagree with the file
/// (SPEC §8.2, §8.9).
struct PreviewCanvas: View {
    let strokes: [StrokePath]
    let offsets: [Offset]
    let analysis: InkLayout.Analysis?
    let roles: [Role]
    var showStructure = false
    /// What the recogniser found and what it was mapped onto. Drawn only when asked; see
    /// `RecognitionLayer` for why each layer is worth its ink.
    var recognized: [RecognizedLine] = []
    var groups: [WordGroup] = []
    var unmatched: [Int] = []
    var showRecognition = false
    var progress: Double = 0        // 0 = original, 1 = refactored
    /// The page's own size and template. Cropping to the ink instead is what made a page
    /// with a printed grid come out as bare strokes on white, at the wrong proportions.
    var paperSize: CGSize? = nil
    var background: Data? = nil
    var zoomable = true

    @State private var zoom: CGFloat = 1
    @State private var committedZoom: CGFloat = 1
    @State private var pan: CGSize = .zero
    @State private var committedPan: CGSize = .zero

    var body: some View {
        let page = pageBox
        return GeometryReader { geo in
            let fit = PageFit(page: page, view: geo.size)
            ZStack {
                // the real paper first, so ink sits on the grid the user drew it on
                if let background {
                    PDFPage(data: background)
                } else {
                    Color.white
                }
                InkLayer(ink: ink, fit: fit, progress: progress)
                if let analysis, showStructure {
                    StructureLayer(analysis: analysis, roles: roles, fit: fit)
                }
                if showRecognition {
                    RecognitionLayer(lines: recognized, groups: groups,
                                     unmatched: unmatched.map { strokes[$0].box },
                                     fit: fit)
                }
            }
        }
        .aspectRatio(page.width / max(page.height, 1), contentMode: .fit)
        .scaleEffect(zoom)
        .offset(pan)
        .gesture(zoomable ? zoomGesture : nil)
        .simultaneousGesture(zoomable && zoom > 1 ? panGesture : nil)
        .onTapGesture(count: 2) { resetOrFill() }
        .clipped()
        .animation(.easeOut(duration: 0.18), value: zoom)
    }

    // Handwriting is small; a fit-to-page view of a dense sheet is unreadable without this.
    private var zoomGesture: some Gesture {
        MagnifyGesture()
            .onChanged { zoom = min(max(committedZoom * $0.magnification, 1), 12) }
            .onEnded { _ in
                committedZoom = zoom
                if zoom == 1 { pan = .zero; committedPan = .zero }
            }
    }

    private var panGesture: some Gesture {
        DragGesture()
            .onChanged { pan = CGSize(width: committedPan.width + $0.translation.width,
                                      height: committedPan.height + $0.translation.height) }
            .onEnded { _ in committedPan = pan }
    }

    private func resetOrFill() {
        if zoom > 1 {
            zoom = 1; committedZoom = 1; pan = .zero; committedPan = .zero
        } else {
            zoom = 3; committedZoom = 3
        }
    }

    /// The real page when the document declares one, and only otherwise the ink's own
    /// extent. The union of before *and* after positions keeps the frame from jumping
    /// mid-animation.
    private var pageBox: InkBox {
        if let paperSize, paperSize.width > 1, paperSize.height > 1 {
            return InkBox(x0: 0, y0: 0, x1: paperSize.width, y1: paperSize.height)
        }
        guard !strokes.isEmpty else { return InkBox(x0: 0, y0: 0, x1: 1, y1: 1) }
        let u = InkBox.union(strokes.map(\.box) + zip(strokes, offsets).map { $0.box.offset(by: $1) })
        let pad = 16 + 0.02 * max(u.width, u.height)
        return InkBox(x0: u.x0 - pad, y0: u.y0 - pad, x1: u.x1 + pad, y1: u.y1 + pad)
    }

    /// Built once per view update. The animation only mutates `InkLayer.progress`, so these
    /// paths are not rebuilt per frame — which is the difference between 60fps and a slide show.
    private var ink: [InkLayer.Ink] {
        strokes.enumerated().map { k, s in
            InkLayer.Ink(path: Self.path(s),
                         offset: k < offsets.count ? offsets[k] : Offset(),
                         width: s.width,
                         color: Color(red: s.red, green: s.green, blue: s.blue, opacity: s.alpha))
        }
    }

    private static func path(_ s: StrokePath) -> Path {
        var p = Path()
        for seg in s.segments {
            switch seg {
            case let .move(x, y):
                p.move(to: CGPoint(x: x, y: y))
            case let .quad(cx, cy, x, y):
                // A curve with no current point is silently dropped by CoreGraphics; start
                // one at the control point rather than losing the stroke.
                if p.isEmpty { p.move(to: CGPoint(x: cx, y: cy)) }
                p.addQuadCurve(to: CGPoint(x: x, y: y), control: CGPoint(x: cx, y: cy))
            }
        }
        return p
    }
}

/// Page points -> view points, aspect-correct and centred.
private struct PageFit {
    let scale: Double, tx: Double, ty: Double

    init(page: InkBox, view: CGSize) {
        let vw = Double(view.width), vh = Double(view.height)
        let s = min(vw / max(page.width, 1), vh / max(page.height, 1))
        scale = s
        tx = (vw - s * page.width) / 2 - s * page.x0
        ty = (vh - s * page.height) / 2 - s * page.y0
    }

    func point(_ x: Double, _ y: Double) -> CGPoint {
        CGPoint(x: tx + scale * x, y: ty + scale * y)
    }

    func rect(_ b: InkBox) -> CGRect {
        CGRect(origin: point(b.x0, b.y0),
               size: CGSize(width: b.width * scale, height: b.height * scale))
    }
}

/// `Animatable` on the view, not on the data: SwiftUI interpolates `progress` and re-runs
/// only this body, so the strokes slide continuously instead of cutting between two layouts
/// (SPEC §9 step 4).
private struct InkLayer: View, Animatable {
    struct Ink {
        let path: Path
        let offset: Offset
        let width: Double
        let color: Color
    }

    let ink: [Ink]
    let fit: PageFit
    var progress: Double

    var animatableData: Double {
        get { progress }
        set { progress = newValue }
    }

    var body: some View {
        Canvas { ctx, _ in
            ctx.translateBy(x: fit.tx, y: fit.ty)
            ctx.scaleBy(x: fit.scale, y: fit.scale)     // pen widths scale with the page too
            for k in ink {
                let moved = k.path.applying(CGAffineTransform(translationX: k.offset.dx * progress,
                                                              y: k.offset.dy * progress))
                ctx.stroke(moved, with: .color(k.color),
                           style: StrokeStyle(lineWidth: k.width, lineCap: .round, lineJoin: .round))
            }
        }
    }
}

/// What the geometry engine found (SPEC §9 step 3). Drawn in the *original* positions and
/// never offset — this is evidence about the page as written, not about the plan.
private struct StructureLayer: View {
    let analysis: InkLayout.Analysis
    let roles: [Role]
    let fit: PageFit

    var body: some View {
        Canvas { ctx, size in
            for x in analysis.levels {
                var guide = Path()
                guide.move(to: CGPoint(x: fit.point(x, 0).x, y: 0))
                guide.addLine(to: CGPoint(x: fit.point(x, 0).x, y: size.height))
                ctx.stroke(guide, with: .color(.pink.opacity(0.7)),
                           style: StrokeStyle(lineWidth: 1, dash: [6, 6]))
            }

            for (k, line) in analysis.lines.enumerated() {
                let role = k < roles.count ? roles[k] : .paragraph
                let tint = role.tint
                let box = fit.rect(line.box)
                ctx.fill(Path(box), with: .color(tint.opacity(0.10)))
                ctx.stroke(Path(box), with: .color(tint), lineWidth: 1)

                var baseline = Path()
                baseline.move(to: fit.point(line.box.x0, line.baseline))
                baseline.addLine(to: fit.point(line.box.x1, line.baseline))
                ctx.stroke(baseline, with: .color(.orange), lineWidth: 1.6)

                for word in line.words {
                    ctx.stroke(Path(fit.rect(word.box)), with: .color(.green),
                               style: StrokeStyle(lineWidth: 0.9, dash: [3, 3]))
                }

                if role != .paragraph {
                    let at = fit.point(line.box.x1, line.baseline)
                    ctx.draw(Text(role.rawValue).font(.caption2.weight(.semibold))
                                .foregroundStyle(tint),
                             at: CGPoint(x: at.x + 6, y: at.y), anchor: .leading)
                }
            }
        }
        .allowsHitTesting(false)
    }
}


/// Seeing which stage is wrong, instead of guessing from the final result.
///
/// The pipeline has several places to be wrong — the render, the recogniser, the coordinate
/// transform, the mapping onto strokes — and every one of them fails the same way from the
/// outside: the page comes out looking much like it went in. Each layer here answers one
/// question, so a wrong page can be attributed rather than debated:
///
///   red    recognised line  — did it find the writing, and in the right place?
///                             (a box in the wrong place is a transform bug)
///   amber  recognised word  — did it split the line into words?
///   green  word group       — did the right ORIGINAL strokes get attached to that word?
///                             drawn from the strokes, not from the recogniser's box, so
///                             a green box adrift from its red one is a mapping bug
///   blue   unmatched        — what was deliberately left alone. Expected over a diagram,
///                             a problem over prose.
private struct RecognitionLayer: View {
    let lines: [RecognizedLine]
    let groups: [WordGroup]
    let unmatched: [InkBox]
    let fit: PageFit

    var body: some View {
        Canvas { ctx, _ in
            for box in unmatched {
                let r = Path(fit.rect(box))
                ctx.fill(r, with: .color(.blue.opacity(0.16)))
            }
            for line in lines {
                for word in line.words {
                    ctx.stroke(Path(fit.rect(word.box)), with: .color(.orange.opacity(0.8)),
                               style: StrokeStyle(lineWidth: 0.7, dash: [4, 3]))
                }
                ctx.stroke(Path(fit.rect(line.box)), with: .color(.red.opacity(0.85)),
                           lineWidth: 1)
            }
            for group in groups {
                let r = Path(fit.rect(group.box))
                ctx.fill(r, with: .color(.green.opacity(0.12)))
                ctx.stroke(r, with: .color(.green), lineWidth: 0.8)
            }
        }
        .allowsHitTesting(false)
    }
}


/// The page template, drawn straight from its one-page PDF with CoreGraphics.
///
/// GoodNotes stores paper as a PDF attachment, so this is the actual grid or ruling the
/// user wrote on rather than an approximation of it.
private struct PDFPage: View {
    let data: Data

    var body: some View {
        Canvas { context, size in
            guard let provider = CGDataProvider(data: data as CFData),
                  let doc = CGPDFDocument(provider),
                  let page = doc.page(at: 1) else { return }
            let box = page.getBoxRect(.mediaBox)
            guard box.width > 0, box.height > 0 else { return }
            context.withCGContext { cg in
                cg.saveGState()
                // PDF space is y-up from the bottom left; the canvas is y-down
                cg.translateBy(x: 0, y: size.height)
                cg.scaleBy(x: size.width / box.width, y: -size.height / box.height)
                cg.drawPDFPage(page)
                cg.restoreGState()
            }
        }
    }
}
