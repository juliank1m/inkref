# FINDINGS

Verified results only. Anything not confirmed against real bytes is under
[Open questions](#open-questions).

Target: Goodnotes 7.0.34, macOS. Sources: the app's live on-disk store and four public
`.goodnotes` archives (`samples/`).

---

## Milestone status

| # | Goal | Output | Verified locally | Confirmed in Goodnotes |
|---|---|---|---|---|
| 1 | Move an existing stroke | `generated/01_moved_stroke.goodnotes` | yes | **PASS** — imports, moved, lasso-selectable, erasable, shape clean |
| 2 | Duplicate a stroke | `generated/02_duplicated_stroke.goodnotes` | yes | **PASS** — imports, both strokes present, stored as two independent records |
| 3 | Synthetic geometry | `generated/03_synthetic_stroke.goodnotes` | yes | **RENDERS** — both synthetic strokes draw correctly. Lasso/erase on them not yet confirmed |

**Arbitrary geometry authored by our own program imports into Goodnotes and renders as pen
ink.** A 4-point horizontal line and a 9-point zigzag, encoded as `PConstantWidthStroke` and
placed in a page whose other stroke is `PVariableWidthStroke`, both draw at the authored
coordinates. The zigzag keeps sharp corners, confirming that encoding a polyline as quadratic
segments with the control point at each segment midpoint reproduces it exactly — Goodnotes
applies no smoothing of its own.

Mixing stroke families within a single page is fine: dispatch is per-record, off the tpl
signature, provided each record's field 3 agrees with its own blob (§0b).

Getting there took two silent failures, both listed below. Neither produced an error at any
layer; in both cases the geometry was byte-correct in Goodnotes' own database and simply did
not draw.

Milestone 2 confirmed: read back from Goodnotes' own RocksDB as **three distinct records**, the
generated UUID preserved verbatim, the copy at the offset position, pairing invariant holding
on all three.

**Paint order is renumbered on import.** Written orders `5872231 / 5872233 / 5872236` came back
as `911604 / 911605 / 911606`, assigned by record *stream position* rather than by the stored
value — the degenerate record moved from the middle to first, matching its position in the
stream. So exact order values need not be right; the sequence of records in the page member is
what determines paint order. (Single observation.)

Milestone 1 confirmed in the app: imported without error; geometry read back out of Goodnotes'
own RocksDB at bounds `(580.43, 410.57)–(615.58, 712.15)` — exactly +100 units — with span
`35.15 × 301.58`, identical to the source, so no deformation. Lasso selected it as a stroke
object and the eraser cut it like native ink. Rendered smooth with no kinking, which means
translating the populated render-outline member (8) produces no artifact.

### Milestone 1 — exactly what changed

Source `samples/test.goodnotes`, page `372C578D-B142-459C-8EE4-311E0ABC05CD`.

That page holds two pen-stroke records: one drawable (a 165 pt squiggle) and one **tombstone**
(see §0 — a deleted stroke with its geometry arrays emptied). Only the drawable one was
touched. (`test2.goodnotes` contains literally one stroke, but it is a zero-length dot 0.35 pt
wide — useless for judging a move by eye.)

Stroke family `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` (variable width).

**Changed — nothing but coordinates:**

| tpl member | Content | Edit |
|---|---|---|
| 2 | anchor `(x, y, width)` | `x += 100` |
| 3 | operands, stride 3 `(x, y, width)` | `x += 100` on each triplet |
| 6 | stride 2 `(x, y)` | `x += 100` |
| 8 | render outline, stride 2 `(x, y)` | `x += 100` (84 floats = 42 pairs, populated) |
| 9 | stride 5 `(x, y, width, angle, angle)` | `x += 100`; width and both angles untouched |

Bounds moved `(480.43, 410.57)–(515.58, 712.15)` → `(580.43, 410.57)–(615.58, 712.15)`.
Exactly +100 units = +54.55 pt. Bounding-box width and height unchanged, so the shape did not
deform.

**Unchanged, and asserted in code:** item UUID, paint order, `{replica, clock}` version,
colour, stroke width, tpl signature, and every member's element count.

**Archive:** 9 of 10 ZIP members byte-identical; only the one page member differs. Member
order, timestamps and compression settings preserved.

Widths, angles, opcodes (member 1) and index members (4, 5, 10) were deliberately left alone —
they carry no coordinates.

---

## 0. Deletion — a tombstone looks exactly like an empty stroke

**Deletion is marked by the mere presence of two fields:**

```
descriptor field 3   varint 1
item body field 14   varint 1
```

Verified by correlation across ~69,000 sampled records in a real library: present on
**11,003 / 11,003** records in the `deleted` bucket and **0 / 58,064** in `normal`. No
exceptions either way.

A tombstoned stroke **keeps its record but has its geometry arrays emptied**, so it decodes as
a stroke whose every array member has count 0. That is not a "degenerate" or "empty" stroke —
it is a deleted one, and it must never be used as a structural template.

Cost of getting this wrong (milestone 3, first attempt): cloning the tombstone in
`samples/test.goodnotes` produced strokes that imported with **byte-correct geometry** — read
back out of Goodnotes' own RocksDB at exactly the authored coordinates — but landed in the
`deleted` bucket and therefore never rendered. The failure is silent: no error, no warning,
correct data, invisible result.

`retag()` now clears both markers on every creation path, and template selection draws from
live records only.

---

## 0b. Item body field 3 declares the stroke family

**Field 3 of the item body must agree with the tpl signature inside the geometry blob.**

```
absent    PConstantWidthStroke
1         PVariableWidthStroke
```

Establishing this required a controlled experiment, because the two candidate fields are
confounded in the available data: a real library is 100% constant-width (field 3 never
present, descriptor field 14 always 5381) while all four public samples are 100%
variable-width (field 3 always 1, no descriptor field 14).

One archive, five constant-width horizontal lines identical except for these fields:

| Variant | item body f3 | descriptor f14 | Rendered |
|---|---|---|---|
| A control | 1 | — | **no** |
| B | — | — | **yes** |
| C | 1 | 5381 | **no** |
| D | — | 5381 | **yes** |
| E — full app-created container | — | 5381 | **yes** |

Rendering tracks the absence of field 3 exactly. Descriptor field 14 is irrelevant to it.

**The failure mode is silent.** A record whose field 3 disagrees with its blob imports
without error, lands in the `normal` bucket, keeps byte-correct geometry readable straight
back out of Goodnotes' own RocksDB — and simply never draws. Nothing anywhere reports a
problem.

This also explains an earlier observation: field 3 "present in exports, absent in the live
store" was never about exports at all — it was the family marker, and the two corpora happen
to use different families.

`StrokeRecord.geometry`'s setter now rewrites field 3 from the signature on every assignment,
and refuses families whose marker value has not been established.

---

## 1. Two serializations of one model

| | Live store | Export |
|---|---|---|
| Container | RocksDB + SQLite + loose files | ZIP |
| Ink | `notes_main.db` (RocksDB) | `notes/<uuid>` members |
| Item records | keys `<page>.<bucket>.<item>.{item,descriptor}` | varint-delimited stream |

**The records are the same shape in both.** A descriptor from RocksDB has fields
`{1,2,8,9,14,16}` with `14=5381`, `16=24`; a header record inside a public `.goodnotes` has
exactly the same field set and constants. That equivalence is what makes writing tractable.

The live store also holds an event-sourced op log (`events_main.db`, `streams_main.db`), a
search index, a GRDB projection (`projection.sqlite`) whose `notes_items` table is always
empty, and loose attachments. The app links `gnyjsFFI` (Yjs) for CRDT sync.

RocksDB SSTs are `format_version: 6` — newer than Homebrew's `sst_dump`. Read on a copy.

### Export layout, schema 24

Ten members in writer order:

```
index.search.pb        0 bytes
index.notes.pb         uuid -> zip path, one per page
notes/<uuid>           page content (0 bytes = empty page)
index.events.pb        document history
thumbnail.jpg          notebook cover, not the page
index.attachments.pb   uuid -> zip path
attachments/<uuid>     raw originals
schema.pb              exactly 08 18  =  {1: 24}
```

Schema 25 (Mac-written) adds a leading zero-byte `document.info.pb`, a non-empty
`index.search.pb`, a `search/<uuid>` member, and `schema.pb` = `08 19`.

All `.pb` members are **streams of varint-length-delimited messages**, not single messages.

---

## 2. Coordinates are 1/132 inch

```
points = units * 6/11        units = points * 11/6
```

Verified three independent ways:

1. Declared page sizes in a real library are exact 11/6 multiples of standard paper:

   | Declared (units) | × 6/11 | Paper |
   |---|---|---|
   | 1122 × 1452 | 612 × 792 | US Letter |
   | 1091.339 × 1543.465 | 595.28 × 841.89 | A4 |
   | 1543.465 × 2182.675 | 841.89 × 1190.55 | A3 |
   | 1452 × 2244 | 792 × 1224 | US Legal |
   | 384.567 × 545.670 | 209.76 × 297.64 | A6 |

   `1122 / 612 = 1.8333… = 11/6` exactly.

2. The paper-definition event inside a `.goodnotes` declares `1091.3466 × 1543.4649` for a
   page whose template attachment is A4.

3. Goodnotes' own PDF export of a document whose paper record reads `834.24 × 1078.825` has
   `/MediaBox [0 0 455.04 588.45]` — ratio exactly 11/6.

**Stroke widths use a DIFFERENT unit — 1/144 inch, not 1/132.**

```
width_points = width_units / 2        width_units = width_points * 2
```

CONFIRMED by calibration: ten constant-width strokes authored from 0.5 to 24 units, exported
to PDF, every one measured at exactly `units / 2` points. Mean ratio 2.0000, spread 0.0000
over a 48x range — a clean linear factor, not quantised pen sizes.

An earlier claim here that widths shared the coordinate unit was an inference, never measured,
and was wrong by 9%. A 1.8189-unit pen is 0.91 pt, not 0.99 pt.

The variable-width family appears to use yet another scale — a stroke storing ~0.20 exported
at 0.2054 pt, i.e. roughly 1:1 — but that is one data point on a family we do not author.
UNRESOLVED.

Origin top-left, y down. Both public parsers (`goodparse`, and `inkterop` which inherited the
assumption from it) treat these as 72 dpi points and are wrong by 1.8333×.

---

## 3. Records

Page content is a stream of **strictly alternating (descriptor, item)** messages. Verified on
every record of every sample; an identity rewrite of all four archives is byte-exact.

Linked two ways: same UUID in field 1, and `descriptor.f2` byte-identical to
`item.<type>.f15`.

### Descriptor

```
1  string  item uuid
2  {1: replicaId, 2: lamportClock}     == item body f15
8  varint  large stable id, constant across a document
9  varint  paint order, ascending
14 varint  5381    (constant in all observed data)
16 varint  24      (schema)
```

### Item — the top-level field number **is** the type

| Field | Type | Count in a 1.28M-item library |
|---|---|---|
| 7 | pen stroke | 759,290 |
| 1 | image — attachment uuid + 2 rects | 374 |
| 8 | text box — body is raw RTF | 2 |
| 11 | recognized math group — LaTeX-ish string + child strokes | 12 |
| 21, 22 | element / group blocks | 30 |

Pen-stroke body:

```
1  string  uuid
2  bytes   Apple-framed LZ4 -> tpl image
3  varint  present in exports, absent in the live store
4  {1..4: float32}  RGBA
6, 9, 20  empty
7  nested version-ish message
15 {1: replicaId, 2: lamportClock}
21 varint 24
```

**Colour:** protobuf omits zero-valued `fixed32` fields, so a pure-red stroke has no field 1.
Index by field number and default, never positionally.

---

## 4. Compression

```
bv41 <u32 raw> <u32 comp> <lz4 block>    compressed chunk
bv4- <u32 size> <bytes>                  stored chunk
bv4$                                     terminator
```

Exactly what `/usr/lib/libcompression.dylib` emits for `COMPRESSION_LZ4` (`0x100`). Do not
write an encoder; call the system one.

Verified: our frames decode correctly, and 330/500 come out byte-identical to Goodnotes' own.
The rest differ only in the `bv41` vs `bv4-` decision on tiny inputs; total size within 0.5%.

---

## 5. Geometry: `tpl`

Not proprietary. It is [`tpl`](https://github.com/troydhanson/tpl), Troy D. Hanson's C
serialization library, vendored into Goodnotes — its verbatim error strings are in the binary.

```
"tpl\0" <u32 total length> <NUL-terminated ASCII signature> <members in order>
```

Type chars: `c` int8, `j` int16, `v` uint16, `i` int32, `u` uint32, `I` int64, `U` uint64,
`f` **double (8 bytes)**, `A(x)` count-prefixed array, `S(...)` struct.

**Self-describing — parse the signature, never hardcode offsets.** `u` slots routinely carry
float32 bit patterns.

### Families

Signatures lifted from the shipped binary, matching `PenStrokeShape.TypeEnum` 1:1:

```
PConstantWidthStroke      vuA(v)A(S(uu))A(S(uuuu))            (v2 adds: vA(f))
PDynamicWidthStroke       vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)
PVariableWidthStroke      vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)
PPencilStroke             vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)
PEraserStroke             vS(vvvvvvvv)S(uuuuuu)
PLegacyVariableWidthLine   A(A(S(iiiiii))S(iiu)S(iiu))
PLegacyVariableWidthLine2  A(S(uu))A(S(uu))A(v)
```

A real user library was 100% constant-width; all four public samples are 100% variable-width.
Dispatch on the signature.

### `PConstantWidthStroke` v2 — solved

```
0  v          version, always 2
1  u          float32 stroke width
2  A(v)       opcodes: 0 = moveTo, 1 = quadCurveTo, one per point
3  A(S(uu))   start point (x, y) — exactly 1 element
4  A(S(uuuu)) quadratic segments (ctrl.x, ctrl.y, end.x, end.y)
5  v          trailing flag, observed 1
6  A(f)       doubles — pressures; empty in all observed data
```

Byte math closes with zero residual. **Re-encoding 2000 real strokes from a live library
reproduced Goodnotes' own bytes 2000/2000 identically.**

### `PVariableWidthStroke` — coordinates solved

Confirmed against a single-segment stroke small enough to read every member whole:

```
0  v      version, always 2
1  A(v)   opcodes, count = segments + 1
2  A(u)   anchor: float32 (x, y, width)             3 slots
3  A(u)   operands: float32 (x, y, width), 2 per segment, as (control, end)
4  A(v)   per-segment length index into member 5; sum(m4) == len(m5)
5  A(v)   per-segment sub-stream, small ints {0,2,3}
6  A(u)   float32 (x, y), one per segment; on a dot, x is offset by exactly −width
7  A(u)   empty in every observed stroke
8  A(u)   float32 (x, y) pairs — precomputed render outline
9  A(u)   float32 stride 5: (x, y, width, angle, angle), angles in radians
10 A(v)   all 1s in every observed sample
```

Coordinate-bearing members are 2, 3, 6, 8, 9. Members 4, 5, 10 hold no coordinates.

---

## 6. Events

`index.events.pb` records are `{1: subject-uuid, <TYPE>: payload}` — the **second field number
is the event type**.

| Type | Meaning |
|---|---|
| 2 | paper definition — f4 = template attachment uuid, f8 = page size in units, f9 = template name |
| 6 | attachment added — f5 = exact byte size |
| 30 | document created |
| 31 | rename |
| 54 | page created |
| 56 | — |
| 102 | page content (schema 24) |
| 104, 105 | schema 25 only |

### Page -> background: two hops

A page does not name its background. The chain is:

```
page id  ->  page-created event (54), field 3  ->  paper uuid
paper uuid  ->  paper-definition event (2) subjected to it
                f4 = template attachment uuid   f8 = page size (units)   f9 = template name
attachment uuid  ->  index.attachments.pb  ->  attachments/<uuid>, a one-page PDF
```

Missing either hop renders a document as bare strokes on white at the wrong proportions.

**The page-created event is subjected to an id one lower in its final hex digit than the
page id** in `index.notes.pb` — `...DC2B` against `...DC2C`, on 3 of 3 pages of the one
real notebook measured. Keying on everything but that last character is still unique per
page and avoids arithmetic on a uuid. LIKELY: one document is not a sample.

Measured on a real 3-page notebook: pages carried 455.0 x 588.4 pt and 595.3 x 842.0 pt
(A4) with different templates each, so page size is per page and not per document.

**Type 102 carries no stroke state** — only page uuid, timestamp, a version uuid, clock and
schema. This is why strokes can be replaced without regenerating the event log.

Page background templates are one-page PDFs in `attachments/`, produced by a tool whose
`/Producer` is `svg2pdf`.

---

## 7. Confirmed end-to-end behaviour

- A rebuilt archive is parsed and accepted: Goodnotes stages it under
  `Data/tmp/Imports/<uuid>/` and extracts `thumbnail.jpg` (SHA256 identical to the input)
  before any user interaction.
- An imported synthesized document appeared in the library, and its strokes were readable back
  out of Goodnotes' own RocksDB matching what was written.
- In the app: strokes are individually selectable with the lasso and removable with the
  eraser — **native editable ink**, not a flattened image.
- The standard eraser **trims stroke geometry in place** rather than deleting records: after
  erasing, the page still held the same number of live stroke records, none tombstoned.
- ZIP member order is not enforced on import.
- No GN5 → GN6 format break. Same binary behind a runtime toggle; the export `documentType`
  enum still has only `GOOD_NOTES_5`. Schema 24 vs 25 tracks the writing platform.

That earlier import used a **clone-and-patch** file: every undecoded field was copied verbatim
from a real record.

---

## 8. Scale

A single page carrying 1000 synthetic constant-width strokes imports and renders correctly,
with six distinct colours and five distinct widths all preserved. Writing 1000 strokes takes
~0.1s and 5000 takes ~0.5s; the page member is ~356 KB at 1000 strokes, ~1.8 MB at 5000.

No UUID, version-stamp or paint-order collisions at any size tested. CONFIRMED.

---

## Open questions

- Whether a wholly synthesized archive — no cloned record — imports.
- `PVariableWidthStroke` members 4, 5, 7, 10: semantics unknown. Whether member 8 (render
  outline) must stay consistent with the path or is regenerated on load. Milestone 1 shifts it
  along with everything else, so a stale-cache failure would surface as a visual artifact.
- Pressure: `A(f)` is empty in every observed stroke. Which family and version populates it?
- Item types 21 and 22.
- Descriptor field 8, and the constant `14 = 5381`.
- Writing to the live store — would require forging Yjs/CRDT state across four databases under
  active sync. Not attempted; the export path avoids it.
