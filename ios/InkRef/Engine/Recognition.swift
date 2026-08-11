import CoreGraphics
import Foundation
import Vision

/// Reading a page, so we do not have to guess it.
///
/// Finding where the words are is the one part of this project that geometry is bad at. A
/// stroke bounding box says nothing about whether the gap beside it is a letter gap or a
/// word gap; clustering has to guess, and on dense mathematical writing it guesses wrong. A
/// text recogniser has already solved that problem, so use it — as a **bridge**, not as a
/// source of handwriting:
///
///     render the page -> recognise -> map the boxes back onto the ORIGINAL strokes
///
/// Nothing that comes back from here is ever drawn or written into a document. The
/// recognised string is a label; the box is a hint about which strokes belong together. The
/// user's own ink supplies every coordinate that survives (SPEC §7, §16).
///
/// Mirrors `inkref/ink/recognize.py`.

/// Image space <-> page space, in one place on purpose.
///
/// Describes one rendered rectangle of a page: its origin and size in points, and how many
/// pixels per point it was drawn at. The whole conversion rests on one invariant: **the
/// render covers exactly that rectangle, with no padding and no cropping to the ink.** Given
/// that, a recogniser's normalised coordinates are the rectangle's own coordinates times its
/// size, plus the origin — and `scale` never enters the arithmetic at all. It only decides
/// how legible the render is.
///
/// Break the invariant (crop to the ink, letterbox to a square, pad the edges) and every box
/// lands somewhere plausible but wrong, which is the failure this type exists to make
/// impossible. `PageRender.tiles` is the only thing that should build one.
public struct PageTransform: Sendable, Equatable {
    public var width: Double        // of the rendered rectangle, in points
    public var height: Double
    public var scale: Double        // pixels per point
    public var x0: Double           // where that rectangle sits on the page, in points
    public var y0: Double

    public init(width: Double, height: Double, scale: Double = 5, x0: Double = 0,
                y0: Double = 0) {
        self.width = width; self.height = height
        self.scale = scale; self.x0 = x0; self.y0 = y0
    }

    public var pixelSize: CGSize {
        CGSize(width: (width * scale).rounded(), height: (height * scale).rounded())
    }

    /// A recogniser's box -> page points.
    ///
    /// Vision reports normalised, y-**up**, origin bottom-left. Page space is y-down,
    /// origin top-left, like everything else in this project (FINDINGS §2).
    public func page(fromNormalized r: CGRect) -> InkBox {
        InkBox(x0: x0 + r.minX * width,
               y0: y0 + (1 - r.maxY) * height,
               x1: x0 + r.maxX * width,
               y1: y0 + (1 - r.minY) * height)
    }
}

public struct RecognizedWord: Sendable {
    public var text: String
    public var box: InkBox
    public var confidence: Double

    public init(text: String, box: InkBox, confidence: Double = 0) {
        self.text = text; self.box = box; self.confidence = confidence
    }
}

public struct RecognizedLine: Sendable {
    public var text: String
    public var box: InkBox
    public var words: [RecognizedWord]
    public var confidence: Double

    public init(text: String, box: InkBox, words: [RecognizedWord] = [],
                confidence: Double = 0) {
        self.text = text; self.box = box; self.words = words; self.confidence = confidence
    }
}

/// One image in, lines out. Swappable on purpose — tiling, de-duplication and the mapping
/// onto strokes all live outside, so a different recogniser changes nothing but this.
public protocol TextRecognizer: Sendable {
    func recognize(_ image: CGImage, in transform: PageTransform) throws -> [RecognizedLine]
}

/// Apple Vision. On device, no key, no network, nothing leaves the iPad — and the same
/// framework the Python engine drives through pyobjc, which keeps the two honest.
public struct VisionRecognizer: TextRecognizer {
    public var languages: [String]
    public var usesLanguageCorrection: Bool

    public init(languages: [String] = ["en-US"], usesLanguageCorrection: Bool = true) {
        self.languages = languages
        self.usesLanguageCorrection = usesLanguageCorrection
    }

    public func recognize(_ image: CGImage, in t: PageTransform) throws -> [RecognizedLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = usesLanguageCorrection
        request.recognitionLanguages = languages
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

        return (request.results ?? []).compactMap { obs -> RecognizedLine? in
            guard let candidate = obs.topCandidates(1).first else { return nil }
            let text = candidate.string
            var line = RecognizedLine(text: text, box: t.page(fromNormalized: obs.boundingBox),
                                      confidence: Double(candidate.confidence))
            for range in wordRanges(of: text) {
                guard let box = try? candidate.boundingBox(for: range) else { continue }
                line.words.append(RecognizedWord(
                    text: String(text[range]),
                    box: t.page(fromNormalized: box.boundingBox),
                    confidence: Double(candidate.confidence)))
            }
            // A line whose per-word boxes could not be resolved is still a usable line.
            if line.words.isEmpty {
                line.words = [RecognizedWord(text: text, box: line.box,
                                             confidence: line.confidence)]
            }
            return line
        }
    }

    private func wordRanges(of text: String) -> [Range<String.Index>] {
        var out: [Range<String.Index>] = []
        var start: String.Index? = nil
        for i in text.indices {
            if text[i].isWhitespace {
                if let s = start { out.append(s..<i); start = nil }
            } else if start == nil {
                start = i
            }
        }
        if let s = start { out.append(s..<text.endIndex) }
        return out
    }
}

public enum Recognition {
    /// Drop lines that repeat one already kept. -> a new list, best first.
    ///
    /// Tiles overlap so that a line straddling a seam is whole in at least one of them,
    /// which means the same line comes back twice. Keeping both would split its strokes
    /// across two groups and stop either from being spaced correctly.
    ///
    /// "Best" is confidence times area: between two readings of the same line prefer the
    /// confident one, and between a fragment and the whole line prefer the whole line.
    public static func dedupe(_ lines: [RecognizedLine], overlap: Double = 0.55) -> [RecognizedLine] {
        var kept: [RecognizedLine] = []
        for line in lines.sorted(by: { $0.confidence * area($0.box) > $1.confidence * area($1.box) }) {
            let a = area(line.box)
            if a > 0, kept.contains(where: { area(intersect(line.box, $0.box)) / a > overlap }) {
                continue
            }
            kept.append(line)
        }
        return kept
    }

    /// Join readings that are really one line of writing. -> a new list, top to bottom.
    ///
    /// A recogniser reads horizontally. Handwritten maths is not written that way: an
    /// exponent, a subscript, a limit's condition and half a fraction all sit off the run of
    /// text they belong to, and each comes back as its own reading. Left alone, the planner
    /// treats each as a line in its own right — and takes the page's line pitch from the gap
    /// between them, which is not a line gap at all. On one real page of calculus notes that
    /// halved the measured pitch, from 8.5pt to 6.2pt.
    ///
    /// Two readings are the same line when they overlap vertically by `overlap` of the
    /// shorter one *and* sit within `gap` line heights of each other horizontally. The
    /// second half is what keeps two columns from being welded together: a gutter is an
    /// order of magnitude wider than the tolerance.
    ///
    /// **Overlap, deliberately, and not proximity.** A numerator sitting cleanly above its
    /// denominator does not overlap it, and neither do two consecutive lines of prose — from
    /// boxes alone the two cases look the same, so merging by nearness would weld prose
    /// together. That case is caught further on instead: `isStacked` marks such a line
    /// rigid, so it translates whole and nothing re-spaces inside it.
    public static func mergeStacked(_ lines: [RecognizedLine], overlap: Double = 0.35,
                                    gap: Double = 1.0) -> [RecognizedLine] {
        var parent = Array(lines.indices)
        func find(_ i: Int) -> Int {
            var i = i
            while parent[i] != i { parent[i] = parent[parent[i]]; i = parent[i] }
            return i
        }
        for i in lines.indices {
            for j in (i + 1)..<lines.count {
                let a = lines[i].box, b = lines[j].box
                let short = Swift.min(a.height, b.height)
                guard short > 0 else { continue }
                let vy = Swift.min(a.y1, b.y1) - Swift.max(a.y0, b.y0)
                guard vy / short >= overlap else { continue }
                let dx = Swift.max(a.x0, b.x0) - Swift.min(a.x1, b.x1)  // < 0 when overlapping
                if dx <= gap * short { parent[find(i)] = find(j) }
            }
        }

        var buckets: [Int: [RecognizedLine]] = [:]
        for i in lines.indices { buckets[find(i), default: []].append(lines[i]) }

        var out: [RecognizedLine] = []
        for var members in buckets.values {
            if members.count == 1 { out.append(members[0]); continue }
            members.sort { $0.box.x0 < $1.box.x0 }
            var words = members.flatMap(\.words)
            words.sort { $0.box.x0 < $1.box.x0 }
            out.append(RecognizedLine(
                text: members.map(\.text).joined(separator: " "),
                box: InkBox.union(members.map(\.box)),
                words: words,
                confidence: members.map(\.confidence).min() ?? 0))
        }
        return out.sorted { $0.box.centerY < $1.box.centerY }
    }

    static func area(_ b: InkBox) -> Double {
        Swift.max(0, b.width) * Swift.max(0, b.height)
    }

    static func intersect(_ a: InkBox, _ b: InkBox) -> InkBox {
        InkBox(x0: Swift.max(a.x0, b.x0), y0: Swift.max(a.y0, b.y0),
               x1: Swift.min(a.x1, b.x1), y1: Swift.min(a.y1, b.y1))
    }
}
