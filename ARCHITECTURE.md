# Architecture

Living document. Format discoveries live in [FINDINGS.md](FINDINGS.md) and
[PDF_FINDINGS.md](PDF_FINDINGS.md), which outrank this file; plan and status live in
[ROADMAP.md](ROADMAP.md).

## The one rule

Source parsing, internal representation, transformation and target serialization stay
separate. Nothing that parses a PDF may know about GoodNotes protobuf, nothing that writes
GoodNotes may know where the strokes came from, and the layout engine may know about
neither.

```
.goodnotes ┐
PDF ───────┼─► parser ─► ink model ─► geometry / spatial analysis
image ─────┘             (points,      (words, lines, indent levels, baselines)
                          y down)                    │
                                                     ▼
                                    (optional) Backboard semantics
                                          role per line, no coordinates
                                                     │
                                                     ▼
                                     deterministic layout engine
                                          one (dx, dy) per stroke
                                                     │
                                                     ▼
                            coordinate transforms on the ORIGINAL stroke records
                                                     │
                                                     ▼
                                      serializer ─► .goodnotes
                                        (later: SVG, PDF, our own format)
```

Everything converges on the ink model. Adding a source or a target means adding one
adapter, not touching the middle.

## Two implementations

| | |
|---|---|
| `ios/InkRef/` | the shipping iPadOS app. Whole round trip on device. Foundation + SwiftUI, no packages. |
| `inkref/` | the Python reference implementation, research harness and CLI. |

They are the same design twice, not two designs. When they disagree, the Python one is the
one that has been run against thousands of real records, and the Swift one is the one that
has to be right on device — reconcile, don't fork.

Forking is caught mechanically rather than by discipline. `tests/test_crosscheck.py` builds
`Engine/` + `AI/` with `swiftc`, makes both engines print the same canonical description of
the same archives, and diffs the text; a Swift-beautified document is then read back by
Python. Currently 5 archives, 1010 strokes, identical geometry, colour, width and segments.
It is the only defence that suits a format whose failure mode is silent — a divergent stroke
imports cleanly and simply never draws. Skipped, not failed, where there is no Swift
toolchain, so the Python side stays runnable without Xcode.

```
inkref/
  ink/          the intermediate representation and the layout engine.
    model.py      Color, InkStroke, InkPage, InkDocument
    layout.py     structure detection + layout plan. Pure geometry, knows no file format
    handwriting.py synthetic messy-notes fixtures
  pdf/          source adapter: PDF /Ink annotations -> InkDocument
    extract.py    GoodNotes "Editable" export -> InkDocument
    probe.py      what is actually inside an unknown PDF?
  goodnotes/    target adapter: InkDocument -> .goodnotes, and in-place editing
    protobuf.py   schema-less protobuf: parse, write, field-level patch, upsert
    lz4.py        Apple bv41 framing via the system libcompression
    tplfmt.py     troydhanson/tpl reader + writer
    strokes.py    stroke-family dispatch, coordinate layout, geometry builders
    records.py    descriptor/item pairs and their invariants
    ids.py        UUID and {replica, clock} version allocation
    archive.py    .goodnotes ZIP packaging
    document.py   Document / Page — mutate an existing archive
    writer.py     InkDocument -> .goodnotes  (the only bilingual module)
    beautify.py   layout plan -> translated records  (the second bilingual module)
    render.py     SVG preview, diagnostics only
  ai/           optional semantic layer. Never produces a coordinate.
    schemas.py    the contract: Block, validation, what a model is allowed to say
    analyzer.py   HeuristicAnalyzer (the floor) and BackboardAnalyzer (on top of it)
    backboard.py  the only file that knows this vendor exists
  preview.py    standalone before/after HTML
  cli.py        analyze | beautify | preview | demo
milestones/     one script per proven capability; each emits a file for manual import
tests/          stdlib-only checks, no GoodNotes required (PDF needs pymupdf, the
                cross-check needs swiftc; both skip rather than fail without them)

ios/InkRef/
  Engine/       Protobuf, AppleLZ4, Geometry, and the format + layout stack ported from
                inkref/goodnotes/ and inkref/ink/
  AI/           Backboard transport and the same validated Block contract
  Views/        SwiftUI: import, preview, before/after, export
ios/Tools/
  CrossCheck.swift  CLI harness: the same canonical dump Python emits, plus --beautify
```

`writer.py` and `beautify.py` are the only modules that speak both languages. If a third
starts converting units or building tpl blobs, the separation has broken.

## Why translation-only is load-bearing

The layout engine is only ever allowed to emit `(dx, dy)`. It never scales, rotates or
regenerates a stroke. SPEC §7 permits slight scaling and rotation; the engine does not do
them, on purpose, and three separate things fall out of that restraint:

**The output is still the user's handwriting.** Every letter keeps the exact geometry the
pen produced. There is no "close enough" reconstruction to argue about.

**It is the one edit confirmed to survive.** FINDINGS milestone 1 moved a record +100 units
and Goodnotes rendered it smooth, lasso-selectable, erasable, with an unchanged
bounding-box span read back out of Goodnotes' own RocksDB. Nothing else about record
mutation has that level of evidence.

**It works on families we cannot author.** Authoring is constant-width only. Translating
works on any family whose coordinate layout is known — constant, variable and dynamic width
— so a page of variable-width Apple Pencil ink beautifies without ever being converted.
Pencil and eraser families are parsed but not translated.

Mechanically, a translated record keeps its geometry, colour, width, pressure, precomputed
render outline, identity, paint order and every protobuf field nobody has decoded. Only
coordinates move, and only in the members FINDINGS §5 lists as coordinate-bearing.

Preview inherits this for free. `preview.py` renders the page **once** and hands the same
per-stroke offsets to CSS, so what an audience watches animate is the exact translation that
gets written into the document — not a second drawing that could disagree with it.

## Where the AI boundary sits

> The model decides **what** something is. The geometry engine decides **where** it goes.
> The stroke engine moves the original ink.

| | |
|---|---|
| Crosses inward | per-line geometry only: id, bbox, word count, stroke count, height ratio, indent level, gap above, "starts with a mark", neighbour ids |
| Crosses outward | a role per line, from a closed set, with a confidence |
| Never crosses | coordinates, offsets, layout instructions, ordering, anything the model made up |

`layout.describe()` builds the inward payload and `ai/schemas.py` validates the outward one.
Every id a model may answer with appears in the payload, so an answer naming anything else
is provably invented and is dropped at the edge — not three layers in, where it would move
real ink. Unknown types become prose. Confidence below 0.55 becomes prose. Recognized text
is accepted as metadata and used for nothing.

A role never supplies a number. It only selects which deterministic rule applies: a heading
gets more room above and below, a bullet list hangs its text off a shared offset, an
equation or diagram is frozen and never moved at all. The thresholds, gains and deadbands
stay in `layout.py` where they can be read and tuned.

The layer is optional in the strong sense. `HeuristicAnalyzer` is the floor and needs no
network; `BackboardAnalyzer` sits on top of it and falls back to it on every failure worth
naming — no API key, network down, timeout, non-JSON reply, invented ids, unknown types,
low confidence — and starts from the heuristic result so a model that classifies half a page
still helps with that half. `analyze()` never raises. `--ai off` disables it entirely.
Configuration is environment-only; no key is ever read from a file in the repo.

## Coordinates and units

**The ink model uses PDF points (1/72 inch), origin top-left, y increasing downward.**
Points everywhere, in both implementations, all the way to the serializer.

- PDF is a first-class source, so extraction needs no scaling
- image sources are also top-left, y-down
- GoodNotes' 1/132 inch unit is a serialization detail: `points = units × 6/11`, converted
  inside `goodnotes/` and nowhere else

Two conversions are easy to conflate and one of them was already wrong once:

| | Factor | |
|---|---|---|
| coordinates | `units = points × 11/6` | 1/132 inch, CONFIRMED three independent ways |
| stroke width | `units = points × 2` | 1/144 inch, CONFIRMED by calibration over a 48x range |

Do not "unify" them. `tests/test_ink.py::test_width_scale_is_calibrated` exists solely to
stop someone doing it.

PDF user space is y-**up** from the bottom-left, so a PDF parser must flip — except that
GoodNotes' `/Ink` annotation vertices come back from PyMuPDF already in page space, y-down,
so that path needs no flip (PDF_FINDINGS). Where a flip is needed it belongs in the parser.

## Working with an undocumented format

The GoodNotes work established a method worth keeping:

1. **Assume nothing renders just because it parses.** Invalid combinations import silently,
   store byte-correct data, and never draw. Two separate bugs did exactly this.
2. **Assert invariants at write time.** The format will not tell you. Current invariants are
   enforced in `records.py`:
   - descriptor `f2` must equal item body `f15`
   - the family marker (item body `f3`) must agree with the tpl signature — the setter
     rewrites it and refuses unknown families
   - new records must never carry a tombstone marker
3. **When data confounds two hypotheses, build an experiment.** Field 3 vs field 14 could not
   be separated by inspection; one archive with five controlled variants settled it in a
   single import.
4. **Label everything CONFIRMED / LIKELY / SPECULATIVE.** Never promote a guess because it is
   plausible.
5. **Preserve unknown fields verbatim.** Records are held as raw bytes and only field-patched,
   so anything undecoded survives. `pb.patch` and `pb.upsert` exist for this.
6. **Measure the transform against geometry you authored yourself.** The PDF chain was
   validated by generating a notebook with known synthetic coordinates, exporting it, and
   converting back — numbers, not eyeballs.

Data integrity follows from the same habit (SPEC §15): the input file is never modified,
`beautify_file` refuses to write over its own source, unmodified pages are re-serialized
byte-identically, and anything the parser does not understand passes through untouched.

## Performance

Bulk writing was O(n²) — every `add_stroke` rescanned the page to find a template and the
next paint order, and each scan re-parsed every record. Fixed by memoizing parses on
`StrokeRecord` (invalidated on mutation) and keeping a per-page order cursor and template
cache on `Document`. 400 strokes went 6.65s → 0.04s; 5000 strokes now write in ~0.5s,
linear.

If record mutation ever moves outside the `descriptor`/`item` setters, the caches must be
invalidated there too.

The layout engine is O(n·rows) in the number of strokes and has not needed attention;
a 179-stroke demo page analyses and plans in well under the write cost. Line clustering is
single-link over line starts — see the `ponytail:` note in `layout.py` for the ceiling.

## Known structural constraint

Authoring new strokes **clones an existing record** for container fields whose semantics
are not established, so it needs a template archive with at least one live stroke on each
target page. Producing a document with no template is untested. This caps multi-page
authored output at the template's page count, and it is why `GoodNotesWriter` raises rather
than guessing.

Beautification is not affected: it only moves records that are already in the document, so
it needs no template and has no page-count limit.
