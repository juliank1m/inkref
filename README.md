# InkRef

Cleans up the **layout** of handwritten notes while keeping every stroke as native editable
ink — lasso-able, movable, erasable, recolourable in GoodNotes. Not typed text, not a
handwriting font, not an AI-generated image.

Prettier for handwriting.

A page comes in with uneven baselines, drifting margins, cramped line spacing and ragged
word gaps. The engine works out the page's structure from stroke geometry — words, lines,
indent levels, headings, lists — computes one `(dx, dy)` per stroke, and applies those
offsets to the **original stroke records** inside the document. Nothing is re-authored,
scaled, rotated or regenerated. The output is the user's own handwriting, moved.

That restraint is not modesty, it is the mechanism. Translating an existing record is the
one edit confirmed in the GoodNotes app to leave ink lasso-selectable, erasable and
undeformed ([FINDINGS](FINDINGS.md) milestone 1) — and because it never has to *author* a
stroke, it works on stroke families we cannot author, including the variable-width Apple
Pencil ink that real notes are made of.

- [ARCHITECTURE.md](ARCHITECTURE.md) — layering, the AI boundary, how to work on an undocumented format
- [ROADMAP.md](ROADMAP.md) — phase status against the spec, what is next, what is deferred
- [FINDINGS.md](FINDINGS.md) — GoodNotes format facts, invariants, silent failure modes
- [PDF_FINDINGS.md](PDF_FINDINGS.md) — how note apps represent ink when they export PDF
- [SPEC.md](SPEC.md) — the product spec the two documents above are measured against

## Two implementations

| | |
|---|---|
| `ios/InkRef/` | **the shipping product.** Native iPadOS SwiftUI app, whole round trip on device — no server, no laptop. Foundation + SwiftUI only, zero packages. |
| `inkref/` | **the reference implementation.** Python research harness that established the format, plus a CLI. Stays as the lab: it is where a format question gets answered fastest. |

Both speak the same coordinate space and the same layout rules. The Swift engine is a port,
not a second design.

## Status

**Writing GoodNotes works.** Arbitrary geometry authored by this code imports into
Goodnotes 7.0.34 and renders as native pen ink, at up to 1000 strokes on a page.

**Translating an existing stroke works, and is verified in the app.** +100 units, geometry
read back out of Goodnotes' own RocksDB at exactly the authored offset, unchanged
bounding-box size, lasso-selectable, erasable, no kinking. That is the exact edit the
beautifier makes, one record at a time.

**The layout engine runs end to end** in Python: `.goodnotes` in, structure detected,
offsets planned, records translated, `.goodnotes` out, plus a standalone before/after HTML
page. On the synthetic demo page it takes baseline wobble down 40%, margin drift 29% and
word-gap irregularity 30%.

**Reading a PDF works** for GoodNotes' own Editable export: one `/Ink` annotation per
stroke, `/InkList` gives the centerline directly, colours survive bit-exact, placement
lands within one stroke width.

**The Swift engine agrees with the Python one, record for record.**
`tests/test_crosscheck.py` builds `ios/InkRef/Engine` and `AI` with `swiftc` and makes
both engines describe the same archives: 5 archives, 1010 strokes, identical geometry,
colour, width and segments. A document beautified by the Swift engine reads back in Python
with all 179 records intact, 163 moved, none deformed.

### Not confirmed — say so out loud

- **A beautified document has never been opened in GoodNotes.** Every individual edit it
  makes is the confirmed one, but the whole-page result has not been imported and checked.
  This is the single most important open item.
- **Lasso and erase on a fully synthetic stroke** are unverified. Synthetic strokes render
  (milestone 3); nobody has tried to select or erase one.
- **The iPad app has never been built or run.** The engine compiles under `swiftc` and
  cross-checks against Python, but `Views/` and the view model are only ever compiled by
  Xcode, and no round trip has happened on a device.
- **Real handwriting has never been through the layout engine.** The public samples carry
  at most five strokes, so everything is tuned against `inkref/ink/handwriting.py`
  fixtures — synthetic messy notes with seeded, known defects.
- **`run_checks.sh` runs three of the seven suites** — format, IR and PDF. The layout,
  beautify, AI and cross-check suites pass but have to be run by hand (below).
- **No check ever talks to the live Backboard API.** `tests/test_ai.py` drives the transport
  with canned replies, so parsing, validation and every degradation path are covered; the
  network call itself and classification quality are not.

## Run the iPad app

```sh
open ios/InkRef.xcodeproj
```

Pick an iPad simulator and run. iPad-only target, iOS 17 deployment, Swift 5, Xcode 26 /
iOS 26 SDK, no SwiftPM dependencies to resolve.

For a real device, set your signing team first — `DEVELOPMENT_TEAM` is deliberately empty
in the project so nobody's team id is committed. Target → Signing & Capabilities → Team,
then run. The app declares `.goodnotes` as an imported type, so a document reaches it from
Files, AirDrop or the GoodNotes share sheet.

## Run the Python CLI

```sh
python3 -m inkref demo                       # generate messy notes, beautify, preview
python3 -m inkref analyze notes.goodnotes    # print the detected structure, change nothing
python3 -m inkref beautify notes.goodnotes -o clean.goodnotes --preview clean.html
python3 -m inkref preview notes.goodnotes -o compare.html
```

Common flags: `-s light|balanced|strong` (how hard to push the layout, default `balanced`),
`--ai auto|off|heuristic|backboard`, `--vision` (send a rendered page image with the
classification request).

`demo` is the fastest way to see the whole thing: into `generated/` it writes
`demo_messy.goodnotes`, `demo_beautified.goodnotes` and a `demo.html` with a Beautify button
that animates the real offsets and a "show detected structure" overlay. Import both
`.goodnotes` files into GoodNotes to check the ink is still native.

Other entry points:

```sh
./run_checks.sh                                  # format + IR + PDF checks
python3 tests/test_layout.py                     # layout engine on seeded fixtures
python3 tests/test_beautify.py                   # document integrity, SPEC §19
python3 tests/test_ai.py                         # AI contract, network faked out
python3 tests/test_crosscheck.py                 # Swift engine == Python engine (needs swiftc)
python3 milestones/m3_synthetic_stroke.py        # synthetic ink -> .goodnotes
python3 milestones/m4_stress.py 1000             # stroke-count stress test
./venv/bin/python milestones/m5_pdf_roundtrip.py # PDF -> .goodnotes, accuracy measured
./venv/bin/python -m inkref.pdf.probe file.pdf  # what is in an unknown PDF?
```

The GoodNotes read/write/beautify path is **stdlib only**. PDF work needs `pymupdf`
(`venv/`), the `--vision` rasteriser reuses it and silently degrades to text-only without
it, and reading the live macOS GoodNotes library needs `rocksdict`.

## Configuration

Semantic classification is optional and configured **from the environment only**. No key is
ever read from a file in this repo.

| Variable | Default | |
|---|---|---|
| `BACKBOARD_API_KEY` | — | absent means the AI layer is simply off |
| `BACKBOARD_BASE_URL` | `https://app.backboard.io/api` | |
| `BACKBOARD_PROVIDER` | `anthropic` | |
| `BACKBOARD_MODEL` | `claude-sonnet-4-20250514` | |
| `BACKBOARD_TIMEOUT` | `30` | seconds |

`--ai off` must always work, and does. See [ARCHITECTURE.md](ARCHITECTURE.md) for what is
allowed to cross that boundary.

## API

```python
from inkref.goodnotes import beautify

report = beautify.beautify_file("notes.goodnotes", "clean.goodnotes", "balanced")
print(report.summary())
```

Lower down, the pieces are separable — pure geometry in, offsets out:

```python
from inkref.ink import layout

analysis, offsets = layout.beautify(boxes)      # boxes: [(x0, y0, x1, y1)] in points
```

And to author ink from scratch:

```python
from inkref.ink.model import Color, InkDocument, InkPage, InkStroke
from inkref.goodnotes.writer import GoodNotesWriter

page = InkPage(width=595, height=842)                 # PDF points, y down
page.add(InkStroke(points=[(100, 100), (160, 100)],
                   color=Color.from_hex("1d4ed8"), width=2.0))

doc = InkDocument(title="demo")
doc.add_page(page)

GoodNotesWriter("samples/test.goodnotes").write(doc, "out.goodnotes")
```

The IR is in **PDF points, origin top-left, y down**. GoodNotes' 1/132 inch unit is
converted inside its writer and nowhere else.

Authoring new strokes needs a template archive with at least one live stroke per target
page — undecoded container fields are cloned from it verbatim. Beautifying does not:
it only moves records that are already there.

## Three things that will bite you

**Coordinates are 1/132 inch inside GoodNotes, not points.** `points = units × 6/11`. The
existing public parsers are wrong by 1.8333×, and one inherited the error from the other.
Stroke *width* is a different unit again — 1/144 inch, exactly 2 units per point.

**Invalid ink fails silently.** A structurally wrong stroke imports without error, lands in
the live bucket with byte-correct geometry, and simply never draws. Assert invariants before
writing; nothing downstream will complain.

**Protobuf omits zero-valued fields.** A pure-red stroke's colour message carries no green
and no blue field at all. Index by field number and default, never positionally.
