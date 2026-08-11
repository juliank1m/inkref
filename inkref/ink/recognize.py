"""Reading a page, so we do not have to guess it.

Finding where the words are is the one part of this project that geometry is bad at. A
stroke bounding box says nothing about whether the gap beside it is a letter gap or a word
gap; clustering has to guess, and on dense mathematical writing it guesses wrong. A text
recogniser has already solved that problem, so use it — as a **bridge**, not as a source of
handwriting:

    render the page -> recognise -> map the boxes back onto the ORIGINAL strokes

What comes back from here is never drawn and never written to a document. The recognised
string is a label; the box is a hint about which strokes belong together. The user's own
ink supplies every coordinate that survives (SPEC §7, §16).

The recogniser is behind `TextRecognizer` so it can be swapped — Apple's Vision today,
because it is on device, free, and the same framework the iPad app uses, which keeps the
two implementations honest with each other.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PageTransform:
    """Image space <-> page space, in one place on purpose.

    Describes one rendered rectangle of a page: its origin and size in points, and how
    many pixels per point it was drawn at. The whole conversion rests on one invariant:
    **the render covers exactly that rectangle, with no padding and no cropping to the
    ink.** Given that, a recogniser's normalised coordinates are the rectangle's own
    coordinates times its size, plus the origin — and `scale` never enters the arithmetic
    at all. It only decides how legible the render is.

    Break the invariant (crop to the ink, letterbox to a square, pad the edges) and every
    box lands somewhere plausible but wrong, which is the failure this type exists to make
    impossible. `beautify.page_tiles` is the only thing that should build one.
    """
    width: float        # of the rendered rectangle, in points
    height: float
    scale: float = 5.0  # pixels per point
    x0: float = 0.0     # where that rectangle sits on the page, in points
    y0: float = 0.0

    @property
    def pixels(self):
        return (round(self.width * self.scale), round(self.height * self.scale))

    def from_normalized(self, x, y, w, h):
        """A recogniser's box -> (x0, y0, x1, y1) in page points.

        Vision reports normalised, y-**up**, origin bottom-left. Page space is y-down,
        origin top-left, like everything else in this project (FINDINGS §2).
        """
        return (self.x0 + x * self.width,
                self.y0 + (1.0 - y - h) * self.height,
                self.x0 + (x + w) * self.width,
                self.y0 + (1.0 - y) * self.height)


@dataclass
class RecognizedWord:
    text: str
    box: tuple            # x0, y0, x1, y1 in page points
    confidence: float = 0.0


@dataclass
class RecognizedLine:
    text: str
    box: tuple
    words: list = field(default_factory=list)
    confidence: float = 0.0


class TextRecognizer:
    """recognize(png_bytes, transform) -> [RecognizedLine], boxes in page points."""

    def recognize(self, png, transform):
        raise NotImplementedError


class VisionRecognizer(TextRecognizer):
    """Apple Vision. On device, no key, no network, and identical to what the iPad runs.

    `available()` is a real question, not defensive habit: pyobjc is a macOS-only
    dependency, so on any other machine the pipeline has to fall back to geometry rather
    than fail.
    """

    def __init__(self, languages=("en-US",), correct=True):
        self.languages = list(languages)
        self.correct = correct

    @staticmethod
    def available():
        try:
            import Vision  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, png, transform):
        import Vision
        from Foundation import NSData, NSRange

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(self.correct)
        req.setRecognitionLanguages_(self.languages)
        data = NSData.dataWithBytes_length_(png, len(png))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        ok, _ = handler.performRequests_error_([req], None)
        if not ok:
            return []

        out = []
        for obs in req.results() or []:
            best = obs.topCandidates_(1)
            if not best:
                continue
            cand = best[0]
            text = cand.string()
            b = obs.boundingBox()
            line = RecognizedLine(
                text=text,
                box=transform.from_normalized(b.origin.x, b.origin.y,
                                              b.size.width, b.size.height),
                confidence=float(cand.confidence()))
            for start, token in _tokens(text):
                r = NSRange(start, len(token))
                wobs, _err = cand.boundingBoxForRange_error_(r, None)
                if wobs is None:
                    continue
                wb = wobs.boundingBox()
                line.words.append(RecognizedWord(
                    text=token,
                    box=transform.from_normalized(wb.origin.x, wb.origin.y,
                                                  wb.size.width, wb.size.height),
                    confidence=float(cand.confidence())))
            # A line whose per-word boxes could not be resolved is still a usable line.
            if not line.words:
                line.words = [RecognizedWord(text, line.box, line.confidence)]
            out.append(line)
        return out


def _tokens(text):
    """-> [(start index, token)] for whitespace-separated runs."""
    out, start = [], None
    for i, ch in enumerate(text):
        if ch.isspace():
            if start is not None:
                out.append((start, text[start:i]))
                start = None
        elif start is None:
            start = i
    if start is not None:
        out.append((start, text[start:]))
    return out


def dedupe(lines, overlap=0.55):
    """Drop lines that repeat one already kept. -> a new list, best first.

    Tiles overlap so that a line straddling a seam is whole in at least one of them, which
    means the same line comes back twice. Keeping both would split its strokes across two
    groups and stop either from being spaced correctly.

    "Best" is confidence times area: between two readings of the same line, prefer the
    confident one, and between a fragment and the whole line, prefer the whole line.
    """
    kept = []
    for line in sorted(lines, key=lambda l: -l.confidence * _area(l.box)):
        a = _area(line.box)
        if a and any(_area(_intersect(line.box, k.box)) / a > overlap for k in kept):
            continue
        kept.append(line)
    return kept


def merge_stacked(lines, overlap=0.35, gap=1.0):
    """Join readings that are really one line of writing. -> a new list, top to bottom.

    A recogniser reads horizontally. Handwritten maths is not written that way: an
    exponent, a subscript, a limit's condition and half a fraction all sit off the run of
    text they belong to, and each comes back as its own reading. Left alone, the planner
    treats each as a line in its own right — and takes the page's line pitch from the gap
    between them, which is not a line gap at all. On one real page of calculus notes that
    halved the measured pitch, from 8.5pt to 6.2pt.

    Two readings are the same line when they overlap vertically by `overlap` of the
    shorter one *and* sit within `gap` line heights of each other horizontally. The second
    half is what keeps two columns from being welded together: a gutter is an order of
    magnitude wider than the tolerance.

    **Overlap, deliberately, and not proximity.** A numerator sitting cleanly above its
    denominator does not overlap it, and neither do two consecutive lines of prose — from
    boxes alone the two cases look the same, so merging by nearness would weld prose
    together. That case is caught further on instead: `layout._is_stacked` marks such a
    line rigid, so it translates whole and nothing re-spaces inside it.
    """
    parent = list(range(len(lines)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = lines[i].box, lines[j].box
            short = min(a[3] - a[1], b[3] - b[1])
            if short <= 0:
                continue
            vy = min(a[3], b[3]) - max(a[1], b[1])
            if vy / short < overlap:
                continue
            dx = max(a[0], b[0]) - min(a[2], b[2])      # negative when they overlap
            if dx <= gap * short:
                parent[find(i)] = find(j)

    buckets = {}
    for i, line in enumerate(lines):
        buckets.setdefault(find(i), []).append(line)

    out = []
    for members in buckets.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        members.sort(key=lambda l: l.box[0])
        words = [w for m in members for w in m.words]
        words.sort(key=lambda w: w.box[0])
        out.append(RecognizedLine(
            text=" ".join(m.text for m in members),
            box=(min(m.box[0] for m in members), min(m.box[1] for m in members),
                 max(m.box[2] for m in members), max(m.box[3] for m in members)),
            words=words,
            confidence=min(m.confidence for m in members)))
    out.sort(key=lambda l: (l.box[1] + l.box[3]) / 2)
    return out


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersect(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def recognizer():
    """The best recogniser this machine has, or None. Never raises."""
    return VisionRecognizer() if VisionRecognizer.available() else None
