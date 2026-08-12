# Roadmap

Living document. Format facts: [FINDINGS.md](FINDINGS.md), [PDF_FINDINGS.md](PDF_FINDINGS.md)
— both outrank this file. Layering: [ARCHITECTURE.md](ARCHITECTURE.md). The spec this is
measured against: [SPEC.md](SPEC.md).

## Product

**Now:** clean the layout of handwritten GoodNotes notes — baselines, word spacing, line
spacing, margins, headings, bullet indents — by translating the original stroke records, so
the result is still native editable ink. Shipping as a native iPadOS app that does the whole
round trip on device.

**Also working:** convert handwritten PDFs from other note apps into `.goodnotes` notebooks
with the same native-ink property. A second input path into the same engine rather than the
headline, and a useful product on its own.

**Later:** other sources and targets. The ink model is the only thing that has to be shared.

Explicitly **not** the product: handwriting → typed text, handwriting → generic handwriting
font, or handwriting → AI-generated image. An image model can misspell a word, change a
number, or mangle a formula. The system manipulates structured objects instead.

---

## MVP status against SPEC §8

`done` = implemented and exercised. `partial` = works but with a named gap. `untested` = the
code path exists and nobody has checked the result where it matters.

| § | Feature | Python | iPad app | Note |
|---|---|---|---|---|
| 8.1 | Import a GoodNotes document | done | ported | parses, preserves unknown fields, never touches the input |
| 8.2 | Render page preview | done | ported | SVG/HTML, Canvas on iOS; stroke geometry exact, page background not drawn |
| 8.3 | Detect handwritten lines | partial | ported | rows → words → indent levels; only ever run on synthetic fixtures |
| 8.4 | Baseline alignment | done | ported | per word within its line, deadband + gain |
| 8.5 | Normalize word spacing | done | ported | cumulative along the line |
| 8.6 | Normalize line spacing | done | ported | ordering preserved by construction; section breaks kept |
| 8.7 | Margin alignment | done | ported | clusters indent levels instead of forcing one margin |
| 8.8 | Formatting strength | done | ported | light / balanced / strong |
| 8.9 | Before / after comparison | done | ported | HTML page animates the real offsets, plus a structure overlay |
| 8.10 | Export back to GoodNotes | **partial** | ported | writes; **no beautified document has been imported into GoodNotes yet** |

`ported` = the Swift code exists and, for everything under `Engine/` and `AI/`, compiles and
produces byte-identical results to Python (`tests/test_crosscheck.py`). The SwiftUI layer on
top of it has never been built in Xcode or run, on a simulator or a device.

8.10 is the one that matters and it is the one with the gap. Every edit the beautifier makes
is the exact edit FINDINGS milestone 1 confirmed in the app — one record translated, geometry
verified out of Goodnotes' own RocksDB, lasso-selectable, erasable, undeformed. A whole page
of those edits at once has not been opened in the app. Until it has, 8.10 is not done.

## Success criteria (SPEC §20)

| Step | Status |
|---|---|
| 1. Write messy notes in GoodNotes | not done — synthetic fixtures stand in (`ink/handwriting.py`); the public samples carry at most 5 strokes |
| 2. Export them | fine, nothing to build |
| 3. Open them in InkRef | CLI yes; iPad app written, never run |
| 4. Lines identified automatically | yes, on fixtures |
| 5. Press Beautify | CLI and HTML preview yes; app written, never run |
| 6. Baselines, spacing, alignment visibly improve | measured on the demo page: baseline wobble −40%, margin drift −29%, word gaps −30%, pitch −3% |
| 7. Preview the difference | yes |
| 8. Export the transformed file | yes |
| 9. Open it in GoodNotes | **untested** |
| 10. Handwriting remains editable native ink | CONFIRMED for a single translated record; **untested for a beautified page** |

---

## Phase 1 — GoodNotes writer

**Status: proven.** Verified by importing generated files into Goodnotes 7.0.34 on macOS.

| | Result |
|---|---|
| Move an existing stroke | imports, moves, lasso-selectable, erasable, undeformed |
| Duplicate a stroke | stored as an independent record; generated UUID survives verbatim |
| Fully synthetic geometry | 4-point line + 9-point zigzag render at authored coordinates, sharp corners preserved |
| 1000 strokes | writes in 0.1s; imports and renders correctly — 6 colours, 5 widths, all present |

Remaining:

- [ ] confirm lasso/erase on a **fully synthetic** stroke (rendering is confirmed; interaction is not)
- [ ] multi-page authored output — blocked on the template constraint

Known limits: authoring is constant-width only; variable-width is readable and translatable
but not authorable; images and text boxes decode but do not write; every authored output
clones an existing record for undecoded container fields, so a template archive is required;
live-database writing is out of scope.

---

## Phase 2 — Layout engine ← **current focus**

```
.goodnotes → parse → boxes → rows/words/levels → (optional semantics) → offsets
           → translate the original records → .goodnotes
```

`ink/layout.py` is pure geometry: stroke bounding boxes in, one `(dx, dy)` per stroke out.
`goodnotes/beautify.py` applies them to real records. Three strengths, each a
(deadband, gain) pair per transform so natural variation survives and the response stays
continuous. `preview.py` renders the page once and hands the same offsets to CSS, so the
animation is the transform.

Done:

- [x] rows → words → indent levels, robust to crossbars, dots and hyphens
- [x] baseline, word-spacing, line-spacing and margin corrections, composed per word
- [x] section breaks and heading spacing preserved rather than normalised away
- [x] bullet lists hung off a shared per-level offset
- [x] equations and diagrams frozen — never moved
- [x] before/after metrics that go down when a page gets cleaner, excluding the gaps the
      engine is supposed to leave irregular
- [x] synthetic messy-notes fixtures with seeded, known defects
- [x] CLI: `demo`, `analyze`, `beautify`, `preview`
- [x] checks: `tests/test_layout.py` (structure, plan, its guarantees) and
      `tests/test_beautify.py` (SPEC §19 integrity against the real transform)

Still to do:

- [ ] **import a beautified document into GoodNotes and check the ink** — the blocking item
- [ ] run it on a page of real handwriting; every threshold is tuned against fixtures
- [ ] `run_checks.sh` still runs only the format, IR and PDF suites; the layout, beautify,
      AI and cross-check suites have to be invoked by hand
- [ ] indent clustering is single-link; it merges levels when drift approaches the indent
      step (`ponytail:` note in `layout.py`)

---

## Phase 3 — Semantic layer (optional, Backboard)

A vision model classifies each detected line: heading, paragraph, bullet_list, equation,
diagram, annotation, drawing, other. **The model decides what something is; the geometry
engine decides where it goes; the stroke engine moves the original ink.** The model is never
asked for a coordinate and recognized text is metadata only.

Done: the closed-set contract and validation, the heuristic floor, the Backboard transport
(stdlib `urllib`, environment-only config), retry, and degradation to geometry on every
failure worth naming. `--ai off` works. `tests/test_ai.py` drives the whole path with canned
replies — well-formed, fenced, chatty, hallucinated, transport failure — and holds
`analyze()` to never raising.

Still to do:

- [ ] any check against the **live** API; only the faked transport is exercised here
- [ ] measure whether vision classification actually beats the heuristic, and on what
- [ ] latency budget on device — a page classification cannot sit in front of Beautify

---

## Phase 4 — The iPad app

Native SwiftUI, iPad-only, iOS 17, Foundation + SwiftUI only, no packages. The engine is
ported from `inkref/`: protobuf surgery, Apple LZ4 framing, tpl, records, document, the
layout engine, and the same Backboard contract.

- [x] project, iPad-only target, `.goodnotes` imported UTI so documents arrive from Files,
      AirDrop and the GoodNotes share sheet
- [x] shared geometry types, protobuf, Apple LZ4, Backboard transport
- [x] zip, tpl, stroke families, records, document — the whole format stack
- [x] the layout engine and beautifier port
- [x] import → preview → strength → Beautify → before/after → export, written
- [x] the port is held to Python's output by `tests/test_crosscheck.py`, not by eye
- [ ] **build it in Xcode and run it** — `Engine/` and `AI/` compile under `swiftc`, but
      `Views/` and the view model have never been through a compiler at all
- [ ] the round trip on a real device

---

## Phase 5 — Vector PDF → GoodNotes

```
input.pdf → extract vector paths → InkDocument → GoodNotes writer → output.goodnotes
```

**Core proven for GoodNotes-exported PDFs.** `milestones/m5_pdf_roundtrip.py` runs the whole
chain against geometry we authored ourselves, so accuracy is measured not eyed:

| | |
|---|---|
| `/Ink` annotations found | 3 of 3 strokes, one each |
| `/InkList` populated | yes — centerline handed over directly, no content-stream parsing |
| Colour fidelity | 3/3 bit-exact |
| Placement error | 0.55 pt (line), 1.62 pt (zigzag) — under one stroke width, and inset by GoodNotes' exporter, not by us |
| Our write+read drift | 0.0000 pt |
| Stroke width | calibrated: exactly 2 units per point (1/144 in), spread 0.0000 over a 48x range |

See [PDF_FINDINGS.md](PDF_FINDINGS.md) for the measurements.

Still to do:

- [ ] a non-GoodNotes source: Notability, to learn centerline vs filled outline
- [ ] multi-page, blocked on the writer's template constraint
- [ ] highlighter classification against a real highlighter sample — the current threshold is
      uncalibrated and a borrowed one already misclassified an ordinary 3 pt pen

Apple Notes and Samsung Notes export **raster** PDFs and are dead ends for stroke recovery.
OneNote exports **filled outlines**, which need skeletonization — a separate, much harder
pipeline. Notability is probably vector but its structure is unverified. `inkref.pdf.probe`
answers the decisive question (centerline vs filled outline) for an unknown file in one run.

---

## Phase 6 — Real-world PDF support

Multi-page. Page-size preservation. Highlights. Drawings. Different exporters. Clipping,
masks, transforms, groups, backgrounds, embedded images.

---

## Phase 7 — Raster / scanned notes

```
photo or scan → cleanup → ink segmentation → stroke reconstruction → native ink
```

Substantially harder than vector PDF. Do not start here.

---

## Phase 8 — Beyond translation

Only once the translate-only engine is genuinely stable, and only where translation provably
cannot do the job: slight per-word scaling and rotation (SPEC §7 allows both), diagram
cleanup (§17.3), smart reflow — select a region, insert space, everything below moves (§17.4).
Reflow is pure translation and is the most valuable of the three; it is listed here only
because it needs the structure engine to be trustworthy first.

Handwriting **generation** stays out until beautification genuinely needs words that were
never written. Not before.

---

## Deliberately deferred

Decoding every GoodNotes protobuf field. Live-database editing. Variable-width stroke
authoring. Handwriting synthesis. Arbitrary photo support. Supporting every note app.
Notability, Apple Notes and OneNote as sources. Collaboration, sync, accounts.
Handwriting-to-LaTeX, summarization, flashcards — all of SPEC §16.

---

## Backlog

**Assistant-level memory of formatting preferences.** Backboard stores memories against an
assistant rather than a thread, so what one document learns is available to the next. The
useful thing to remember is not what the notes *say* — that is the study-assistant product
SPEC §16 rules out — but how this particular person writes: headings underlined rather than
enlarged, hanging indents on bullets, a habitual left margin. Fed by what the user undoes,
which is the only signal in the app that says "you got that wrong".

Deferred deliberately, and the reason is worth keeping: a formatter that adapts to you is
worth nothing until the formatter works on a real device. Revisit after the round trip is
proven. If it is built, it must store preferences only — never a line of anyone's notes.

## Immediate next actions

1. **[needs a person, blocked]** The physical-device round trip: GoodNotes -> share ->
   InkRef -> Beautify -> export -> GoodNotes, then lasso, drag and partially erase a moved
   stroke. This is the only claim in the project that has never been tested on hardware, and
   it is the one the whole design rests on. Blocked on the iPad appearing over USB and on
   Developer Mode being enabled.
2. **[needs a person, blocked]** Real recognition timing on the iPad. The simulator reads a
   page in about 14s and macOS reads the same tiles in 3.5s, because the simulator has no
   Neural Engine. Neither number describes the target hardware, so no recognition tuning
   should be done against them.
3. Write a page of genuinely messy notes in GoodNotes and run `analyze` on it. The
   thresholds in `layout.py` are tuned against one real notebook and a synthetic fixture.
4. Reinstate heading whitespace on real pages. It exists and is tested, but it needs a
   `heading` role, and the geometry heuristic only names bullets and obvious headings —
   so on a dense page nothing asks for it.
