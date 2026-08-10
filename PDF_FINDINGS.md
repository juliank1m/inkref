# PDF source-format findings

How note apps represent handwriting when they export to PDF. Living document.
GoodNotes' own container format is in [FINDINGS.md](FINDINGS.md).

Claims are labelled **CONFIRMED** (measured here, or explicit documentation / source read),
**LIKELY** (credible corroborating reports), **SPECULATIVE** (plausible, unverified).

The GoodNotes section has now been **measured against a real export** on this machine; the
other apps remain documentary.

---

## The decisive axis: centerline vs filled outline

A **centerline** path stroked with `S` and a line width `w` maps almost directly onto a
GoodNotes stroke — geometry, width and colour all fall out.

A **filled outline** is the closed boundary of a variable-width nib, painted with `f`. There
is no centerline and no `w` to read; recovering a stroke means skeletonization. That is a
different and much harder pipeline.

**Determine which one a source produces before writing any parsing code for it.**

---

## Per app

| App | Export | Structure |
|---|---|---|
| **GoodNotes, Apple platforms** | vector | **one `/Ink` annotation per stroke**, centerline, `S` + `w` — CONFIRMED |
| GoodNotes, Android / Windows / Web | raster | vendor feature request confirms Apple-only vector — CONFIRMED |
| **Notability** | vector | Bézier engine; imports into Affinity as editable paths. Centerline vs outline **unknown** — LIKELY vector, SPECULATIVE structure |
| **OneNote** | vector, **filled outlines** | "exports regular strokes as `<path>`s filled with a solid color" (rnote maintainer) — CONFIRMED for SVG, LIKELY for PDF |
| **Apple Notes / PencilKit** | **raster** | `PKDrawing` renders to a `UIImage`; no public vector path out. Every PDF report describes rasterization — LIKELY (strong) |
| **Samsung Notes** | **raster** | third-party converter documents native exports as raster — CONFIRMED |
| **Concepts** | user-selectable | explicit "Vector Paths" export; centerline, but **variable width is discarded** — CONFIRMED |
| **Nebo / MyScript** | vector | SVG is a first-class target — LIKELY |
| Cairo-based apps (Xournal++, rnote) | mixed | `cairo_stroke()` computes the border then fills; expect outlines wherever width varies — LIKELY |

**Consequence:** Apple Notes and Samsung Notes are dead ends for PDF-based stroke recovery.
Do not ask users for those. OneNote needs skeletonization. GoodNotes' own export is the
easiest and highest-value first target.

---

## GoodNotes "Editable" PDF export — MEASURED

Verified against a real export: Goodnotes 7.0.34 -> `macOS Version 26.6 Quartz PDFContext`,
of a notebook whose geometry we authored ourselves, so ground truth was exact.

**All CONFIRMED by measurement:**

| | |
|---|---|
| Ink location | `/Ink` **annotations**, not the page content stream |
| Strokes per annotation | **exactly one** — 3 authored strokes gave 3 annotations |
| `/InkList` | **populated** — this was the open SPECULATIVE question. It is the flattened centerline polyline, so no content-stream parsing and no `/Matrix`->`/BBox`->`/Rect`->`cm` chain is needed |
| Coordinate space | PyMuPDF's `annot.vertices` returns page points, y-down from top-left — matches the IR directly, **no flip needed** |
| Coordinate accuracy | authored `y=100` units came back as `54.55` pt; `100 x 6/11 = 54.5454` |
| Colour | **bit-exact**: authored `(0.10, 0.35, 0.90)` and `(0.85, 0.15, 0.15)` returned unchanged |
| Curve fidelity | GoodNotes flattens densely — 4 authored points became 31, 9 became 81. Simplify or a round trip inflates ~10x |
| Endpoint inset | extracted bbox sits **inside** the authored one by 0.55 pt (line) and 1.62 pt (zigzag corners), roughly half a stroke width. **Not caused by our simplification** — identical at RDP tolerance 0.0 and 1.0, so it is inset by GoodNotes' exporter |

Round trip `PDF -> InkDocument -> .goodnotes` drifts **0.0000 pt** through our own write+read.

Prior evidence from the source of
[handwriting-neatener](https://github.com/idoschwartz11/handwriting-neatener), which agrees:

- Ink lives in **annotations**, not the page content stream. `page.Annots()` filtered to
  `/Subtype /Ink`. **One annotation per stroke, with its own colour, width and shape.**
- Geometry is inside each annotation's `/AP /N` **Form XObject** appearance stream.
- Only `S`/`s`-painted paths are ink; `f`/`f*`/`B`/`b` are left alone.
- Pen width is read directly off the `w` operator.
- Cubic Béziers are present: the tokenizer handles `m l c v y re h`, and `re` is commented as
  usually a clip rect.
- **Highlighter discrimination:** annotation-level `/CA < 0.99` **or** `w > 2.5`. That is the
  tool's actual classifier.
- Colour operators (`rg RG g G CS SC`) and graphics state (`q Q cm gs`) appear in the streams.

Vendor description of Editable export: objects "can be selected and moved/resized in other
PDF viewers" — LIKELY the same mechanism.

**Transform chain must be composed** to get page coordinates (PDF 32000 §12.5.5):
`Form /Matrix` → transformed `/BBox` → fit into the annotation `/Rect` → any `cm` inside the
stream. Getting this wrong is the classic wrong-place/wrong-scale bug.

### Width uses a different unit from coordinates — RESOLVED

Coordinates scale by 11/6 (1/132 inch). **Width scales by exactly 2 (1/144 inch.)**

```
width_points = width_units / 2
```

Calibrated with ten constant-width strokes from 0.5 to 24 units, exported and measured:

| authored (u) | 0.5 | 1 | 2 | 3 | 4 | 6 | 8 | 12 | 16 | 24 |
|---|---|---|---|---|---|---|---|---|---|---|
| measured `w` (pt) | 0.25 | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 | 4.0 | 6.0 | 8.0 | 12.0 |

Ratio 2.0000 on every row, spread 0.0000 across a 48x range. Linear, not quantised.

The variable-width family looks different again — one stroke storing ~0.20 exported at
0.2054 pt, roughly 1:1 — but that is a single data point on a family we do not author.
UNRESOLVED, and it does not block anything.

### Still open

- What the **Flattened** export looks like — still vector (LIKELY), but in the page content
  stream rather than annotations.
- Highlighter detection is **uncalibrated** — no real highlighter sample examined. The
  borrowed `w > 2.5` threshold misclassified an ordinary 3 pt pen, so width is now only a
  weak fallback at 8 pt and alpha is the primary signal.

---

## Why this is the ideal first sample

A GoodNotes export gives **perfect ground truth**. We can read the source `.goodnotes` strokes
directly with our own reader, export the same notebook to PDF, convert PDF → `.goodnotes`, and
compare geometry against the known original — no eyeballing. That validates the whole
extraction and transform chain numerically before any third-party PDF is involved.

Best of all, we can generate the source notebook ourselves with known synthetic geometry.

---

## Tooling

`inkport/pdf/` needs a PDF library; the GoodNotes write path stays stdlib-only.

- **PyMuPDF** — best fit. `page.get_drawings()` yields per-path dicts with `items`
  (`l`/`c`/`re`/`qu`, Béziers preserved), `color` (stroke), `fill`, `width`, `opacity`,
  `lineCap`, `lineJoin`, `closePath`. Distinguishes fill from stroke, which is exactly the
  decisive axis. Installed in `venv/`.
- **pikepdf** — content-stream tokenizer plus a CTM helper; needed for annotation appearance
  streams and for writing streams back. Installed.
- **pdfplumber** — reads page content streams only, so it will likely see **nothing** in a
  GoodNotes Editable export where ink lives in annotations. LIKELY blind spot.
- `mutool trace` dumps every device call as XML — excellent for inspecting a sample by hand.
  Not installed (`brew install mupdf-tools`).

## Prior art

`handwriting-neatener` (GoodNotes `/Ink` rewriting — closest to this problem), `sdocx2pdf`
(Samsung native → vector PDF), `InkExtrakt`, `PdfToSvg.NET`. Nothing found that reconstructs
editable strokes from a filled-outline handwriting PDF.
