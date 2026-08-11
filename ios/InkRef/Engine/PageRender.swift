import CoreGraphics
import Foundation

/// The page as a recogniser needs to see it: black ink on white, in tiles.
///
/// Deliberately not the preview. A printed grid is a wall of horizontal rules to a text
/// recogniser and the writer's pen colour has nothing to do with reading, so this draws
/// neither. `PreviewCanvas` renders faithfully for the human, and for the vision model,
/// which are looking at something else.
///
/// Mirrors `page_tiles` in `inkref/goodnotes/beautify.py`.
public enum PageRender {
    /// A recogniser normalises its input to a fixed working size, so what decides whether
    /// small writing survives is the height of a line **as a fraction of the image**, not
    /// its pixel height. That is why a whole page of dense notes reads badly at any scale
    /// and a slice of it reads well: measured on a real 595x842pt page of maths, one image
    /// gave 37% of strokes grouped and 24 tiles gave 86%, with the render scale making
    /// almost no difference.
    ///
    /// A tile aims to hold about this many lines of writing. Tuning knob: fewer means more,
    /// smaller tiles — better recall on cramped writing, more recogniser calls.
    public static let linesPerTile = 25.0
    public static let tileOverlap = 0.08   // of a tile, so a line on a seam is whole in one
    public static let scale = 5.0          // pixels per point

    /// The page rectangle in points: what the document declares, or the ink's own extent.
    public static func pageSize(paper: CGSize?, boxes: [InkBox]) -> CGSize {
        if let paper, paper.width > 1, paper.height > 1 { return paper }
        guard !boxes.isEmpty else { return CGSize(width: 1, height: 1) }
        // No declared paper. The ink is still measured from the page origin, so the
        // PageTransform invariant holds; only the outer edge is a guess.
        return CGSize(width: (boxes.map(\.x1).max() ?? 1) * 1.02,
                      height: (boxes.map(\.y1).max() ?? 1) * 1.02)
    }

    /// Where the tiles are, without drawing any of them.
    ///
    /// Split from the drawing so a caller can render one tile at a time, in parallel, and
    /// never hold more than a few bitmaps at once. Rendering all of them up front is
    /// simpler but keeps every tile of a page in memory simultaneously for no benefit —
    /// each one is used once and discarded.
    public static func plan(_ strokes: [StrokePath], paper: CGSize?, refH: Double,
                            scale: Double = scale,
                            linesPerTile: Double = linesPerTile) -> [PageTransform] {
        guard !strokes.isEmpty else { return [] }
        let size = pageSize(paper: paper, boxes: strokes.map(\.box))
        let span = Swift.max(linesPerTile * Swift.max(refH, 1) * 1.6, 60)   // ~1.6 refH/line
        let nx = Swift.max(1, Int((size.width / span).rounded()))
        let ny = Swift.max(1, Int((size.height / span).rounded()))
        let tw = size.width / Double(nx), th = size.height / Double(ny)

        var out: [PageTransform] = []
        for iy in 0..<ny {
            for ix in 0..<nx {
                let x0 = Swift.max(0, Double(ix) * tw - tileOverlap * tw)
                let y0 = Swift.max(0, Double(iy) * th - tileOverlap * th)
                let x1 = Swift.min(size.width, Double(ix + 1) * tw + tileOverlap * tw)
                let y1 = Swift.min(size.height, Double(iy + 1) * th + tileOverlap * th)
                out.append(PageTransform(width: x1 - x0, height: y1 - y0, scale: scale,
                                         x0: x0, y0: y0))
            }
        }
        return out
    }

    /// One tile, drawn on demand. Only the strokes that can appear in it, so a dense page
    /// does not redraw all ten thousand of them once per tile.
    public static func tile(_ strokes: [StrokePath], _ t: PageTransform) -> CGImage? {
        let visible = strokes.filter { (s: StrokePath) -> Bool in
            let b: InkBox = s.box
            if b.x1 < t.x0 || b.x0 > t.x0 + t.width { return false }
            return !(b.y1 < t.y0 || b.y0 > t.y0 + t.height)
        }
        return draw(visible, in: t)
    }

    /// -> one image per tile, each paired with the transform that puts it back on the page.
    /// Convenience for callers that are not doing their own concurrency (the cross-check).
    public static func tiles(_ strokes: [StrokePath], paper: CGSize?, refH: Double,
                             scale: Double = scale,
                             linesPerTile: Double = linesPerTile) -> [(CGImage, PageTransform)] {
        plan(strokes, paper: paper, refH: refH, scale: scale, linesPerTile: linesPerTile)
            .compactMap { t in tile(strokes, t).map { ($0, t) } }
    }

    /// Draws exactly the transform's rectangle — no padding, no crop to the ink. That is the
    /// invariant `PageTransform` depends on; see its documentation.
    static func draw(_ strokes: [StrokePath], in t: PageTransform) -> CGImage? {
        let size = t.pixelSize
        guard size.width >= 1, size.height >= 1 else { return nil }
        guard let ctx = CGContext(data: nil, width: Int(size.width), height: Int(size.height),
                                  bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceGray(),
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else {
            return nil
        }
        ctx.setFillColor(gray: 1, alpha: 1)
        ctx.fill(CGRect(origin: .zero, size: size))
        // page points -> pixels, y-down (CoreGraphics bitmaps are y-up)
        ctx.translateBy(x: 0, y: size.height)
        ctx.scaleBy(x: t.scale, y: -t.scale)
        ctx.translateBy(x: -t.x0, y: -t.y0)
        ctx.setStrokeColor(gray: 0, alpha: 1)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)

        for s in strokes {
            ctx.setLineWidth(Swift.max(s.width, 0.6))
            ctx.beginPath()
            var started = false
            for seg in s.segments {
                switch seg {
                case let .move(x, y):
                    ctx.move(to: CGPoint(x: x, y: y)); started = true
                case let .quad(cx, cy, x, y):
                    if !started { ctx.move(to: CGPoint(x: cx, y: cy)); started = true }
                    ctx.addQuadCurve(to: CGPoint(x: x, y: y), control: CGPoint(x: cx, y: cy))
                }
            }
            if started { ctx.strokePath() }
        }
        return ctx.makeImage()
    }
}
