"""Checks for the layout engine: structure detection, the plan, and its guarantees.

Fixtures come from `inkport.ink.handwriting` at a fixed seed, so every number below is
reproducible. Three checks use hand-built boxes instead, because the defect they guard
against cannot be produced by the synthetic page: a shift large enough to hit the cap,
a run of corrections big enough to walk a line onto a frozen one, and a zero-height
crossbar sitting on a row's top edge.

Run: python3 tests/test_layout.py     (stdlib only)
"""
import os
import sys
from statistics import median

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkport.ink import handwriting, layout                    # noqa: E402
from inkport.ink.handwriting import LECTURE, Mess              # noqa: E402

SEED = 7


def lecture(mess=None):
    """-> stroke boxes for the SPEC §9 lecture page. Default mess is the demo's."""
    page = handwriting.page(LECTURE, mess=Mess() if mess is None else mess, seed=SEED)
    return [s.bounds for s in page.strokes]


def baselines_of(lines, boxes, ref_h):
    """Each line's baseline recomputed from `boxes` — the engine's own rule, so that a
    line can be followed across a translation without re-running the grouper."""
    out = []
    for line in lines:
        tall = [boxes[i][3] for i in line.indices
                if boxes[i][3] - boxes[i][1] >= layout.TALL * ref_h]
        out.append(median(tall or [boxes[i][3] for i in line.indices]))
    return out


def total_movement(offsets):
    return sum(abs(dx) + abs(dy) for dx, dy in offsets)


def worst_shift(offsets):
    return max((max(abs(dx), abs(dy)) for dx, dy in offsets), default=0.0)


def test_grouping_finds_the_written_lines():
    """The LECTURE fixture has 8 non-blank lines; the blanks must not become lines."""
    boxes = lecture()
    a = layout.analyze(boxes)
    assert len(a.lines) == 8, f"expected 8 lines, got {len(a.lines)}"
    assert [len(l.words) for l in a.lines] == [2, 2, 2, 3, 2, 2, 2, 2], \
        [len(l.words) for l in a.lines]

    seen = [i for line in a.lines for w in line.words for i in w.indices]
    assert sorted(seen) == list(range(len(boxes))), "words must partition the strokes"
    assert len(seen) == len(set(seen)), "a stroke landed in two words"
    print("  grouping: 8 lines, 2/2/2/3/2/2/2/2 words, every stroke in exactly one word")


def test_a_clean_page_is_left_alone():
    """Mess.none() is already ruled. Light must not touch it at all; balanced may only
    trim the sub-point gap variation the glyph sidebearings leave behind."""
    boxes = lecture(Mess.none())
    a, offsets = layout.beautify(boxes, "light")
    assert worst_shift(offsets) < 1e-9, f"light moved a tidy page: {worst_shift(offsets)}"

    _, offsets = layout.beautify(boxes, "balanced")
    worst = worst_shift(offsets)
    assert worst <= 0.5 * a.ref_h, f"balanced fidgeted by {worst / a.ref_h:.2f} x ref_h"
    still = sum(1 for o in offsets if max(abs(o[0]), abs(o[1])) < 1e-9)
    assert still >= 0.85 * len(offsets), f"only {still}/{len(offsets)} strokes held still"
    print("  clean page: light is a no-op, balanced moves <0.5 ref_h and holds 85%+ still")


def test_beautify_reduces_the_spreads():
    boxes = lecture()
    before = layout.metrics(boxes)
    a, offsets = layout.beautify(boxes, "balanced")
    after = layout.metrics(layout.moved(boxes, offsets))
    for key in ("baseline_spread", "margin_spread", "gap_spread"):
        assert before[key] > 0, f"{key} fixture is already clean, the check proves nothing"
        assert after[key] < 0.9 * before[key], \
            f"{key}: {before[key]:.2f} -> {after[key]:.2f}"
    assert len(layout.analyze(layout.moved(boxes, offsets)).lines) == len(a.lines), \
        "cleaning must not merge or split lines"
    print("  beautify: baseline, margin and word-gap spread all drop by >10%")


def test_strength_ladder():
    boxes = lecture()
    light, balanced, strong = (total_movement(layout.beautify(boxes, s)[1])
                               for s in ("light", "balanced", "strong"))
    assert 0 < light < balanced < strong, (light, balanced, strong)
    print(f"  strength: total movement {light:.0f} < {balanced:.0f} < {strong:.0f} pt")


def test_ordering_is_preserved():
    """Lines may be nudged but may never swap, nor collapse into each other."""
    boxes = lecture()
    a, offsets = layout.beautify(boxes, "strong")
    after = baselines_of(a.lines, layout.moved(boxes, offsets), a.ref_h)
    assert after == sorted(after), after
    floor = layout.MIN_LINE_GAP * a.pitch
    for k, (top, bot) in enumerate(zip(after, after[1:])):
        assert bot - top >= floor - 1e-6, f"lines {k}/{k + 1} collapsed to {bot - top:.1f}"

    # On an all-prose page the two asserts above hold by construction — the targets are
    # accumulated forward, each already a floor below the next — so on their own they prove
    # nothing. A frozen line is the one anchor that accumulation does not control: it keeps
    # its original baseline while the lines above it are opened up and walk down onto it.
    # The cramped page asks for exactly that much movement.
    boxes = cramped_page()
    a = layout.analyze(boxes)
    floor = layout.MIN_LINE_GAP * a.pitch
    for frozen in (5, 8, 12):
        roles = [layout.PARAGRAPH] * len(a.lines)
        roles[frozen] = layout.EQUATION
        after = baselines_of(a.lines, layout.moved(boxes, layout.plan(a, "strong", roles)),
                             a.ref_h)
        gaps = [b - t for t, b in zip(after, after[1:])]
        assert min(gaps) >= floor - 1e-6, \
            f"frozen L{frozen} was overrun: tightest gap {min(gaps):.1f} of {floor:.1f}"
    print("  ordering: baselines ascending, floor held even where a frozen line anchors one")


def cramped_page():
    """Eight tight lines then twelve normal ones, one word each.

    The run of tight lines is what makes this fixture worth having: the corrections
    accumulate down the page, so the last lines are asked to move much further than the
    cap allows. Nothing the synthetic writer produces gets anywhere near it.
    """
    boxes, y = [], 100.0
    gaps = [40.0] * 8 + [60.0] * 12
    for k in range(len(gaps) + 1):
        boxes += [(80.0 + j * 40, y - 20, 110.0 + j * 40, y) for j in range(4)]
        if k < len(gaps):
            y += gaps[k]
    return boxes


def test_shift_never_exceeds_the_cap():
    boxes = cramped_page()
    for name in ("light", "balanced", "strong"):
        s = layout.strength(name)
        a, offsets = layout.beautify(boxes, s)
        cap = s.max_shift * a.ref_h
        worst = worst_shift(offsets)
        assert worst <= cap + 1e-9, f"{name}: moved {worst:.1f} > cap {cap:.1f}"
        if name != "light":                 # light never wants to move this far
            assert abs(worst - cap) < 1e-9, \
                f"{name}: fixture no longer reaches the cap ({worst:.1f} of {cap:.1f})"
    print("  cap: max_shift x ref_h is reached at balanced/strong and never exceeded")


def test_section_breaks_survive():
    """A gap the engine reads as a deliberate break must stay obviously wider than one
    line of prose — normalising it would erase the page's structure."""
    boxes = lecture()
    a, offsets = layout.beautify(boxes, "strong")
    before = [l.baseline for l in a.lines]
    after = baselines_of(a.lines, layout.moved(boxes, offsets), a.ref_h)
    wide = layout.STRONG.para_ratio * a.pitch
    breaks = [k for k in range(1, len(before)) if before[k] - before[k - 1] > wide]
    assert len(breaks) == 2, f"fixture should hold 2 section breaks, found {breaks}"
    for k in breaks:
        # > pitch is not enough: normalising a break to one line leaves it a hair over
        # pitch and still reads as body text. It has to stay a break.
        assert after[k] - after[k - 1] > wide, \
            f"break above L{k} shrank to {after[k] - after[k - 1]:.1f} (break is >{wide:.1f})"
    print("  section breaks: both paragraph gaps still read as breaks, not as body pitch")


def test_frozen_roles_are_never_touched():
    boxes = lecture()
    a = layout.analyze(boxes)
    frozen = 3                                       # "- NEURAL NETS", mid-page
    for role in (layout.EQUATION, layout.DIAGRAM):
        roles = [layout.PARAGRAPH] * len(a.lines)
        roles[frozen] = role
        offsets = layout.plan(a, "strong", roles)
        assert all(offsets[i] == (0.0, 0.0) for i in a.lines[frozen].indices), \
            f"{role} line was moved"
        for k in (frozen - 1, frozen + 1):
            assert any(offsets[i] != (0.0, 0.0) for i in a.lines[k].indices), \
                f"L{k} beside the {role} stopped being cleaned"
    print("  roles: equation/diagram lines get (0,0) while their neighbours are cleaned")


def test_zero_height_stroke_joins_its_row():
    """Regression: a T-bar, crossbar or hyphen has no baseline of its own. Seeding a row
    with one puts a phantom line 22 pt above the text it belongs to, and that row then
    refuses every real letter that arrives after it."""
    row_a = [(80.0 + i * 40, 78.0, 110.0 + i * 40, 100.0) for i in range(5)]
    bar = (200.0, 78.0, 214.0, 78.0)                 # zero height, 22 pt above the baseline
    row_b = [(80.0 + i * 40, 130.0, 110.0 + i * 40, 152.0) for i in range(5)]
    boxes = row_a + [bar] + row_b

    a = layout.analyze(boxes)
    assert len(a.lines) == 2, f"the crossbar seeded its own line: {len(a.lines)} lines"
    home = [k for k, l in enumerate(a.lines) if len(row_a) in l.indices]
    assert home == [0], f"crossbar landed in line {home}, not the row it sits on"
    assert abs(a.lines[0].baseline - 100.0) < 1e-9, \
        f"the crossbar dragged its row's baseline to {a.lines[0].baseline}"
    print("  rows: a zero-height crossbar joins the row below it instead of seeding one")


def test_describe_matches_the_lines():
    boxes = lecture()
    a = layout.analyze(boxes)
    blocks = layout.describe(a)
    assert len(blocks) == len(a.lines)
    assert [b["id"] for b in blocks] == [f"L{k}" for k in range(len(a.lines))]
    for b, line in zip(blocks, a.lines):
        assert len(b["bbox"]) == 4 and all(v is not None for v in b["bbox"]), b
        assert b["words"] == len(line.words) and b["strokes"] == len(line.indices)
        assert b["indent_level"] == line.level
        assert all(n in {x["id"] for x in blocks} for n in b["nearby"]), b["nearby"]
    assert blocks[0]["gap_above"] is None and blocks[1]["gap_above"] is not None
    print("  describe: one block per line, ids L0..Ln, bboxes complete, ids self-consistent")


def test_degenerate_input():
    for boxes in ([], [(10.0, 10.0, 30.0, 32.0)], [(10.0, 10.0, 30.0, 32.0)] * 5):
        a, offsets = layout.beautify(boxes)
        assert len(offsets) == len(boxes)
        assert all(o == (0.0, 0.0) for o in offsets), offsets
        assert len(a.lines) == (0 if not boxes else 1)
        layout.metrics(boxes, a)
        assert len(layout.describe(a)) == len(a.lines)
    print("  degenerate: empty, single and all-identical box lists are handled quietly")


if __name__ == "__main__":
    for fn in [test_grouping_finds_the_written_lines,
               test_a_clean_page_is_left_alone,
               test_beautify_reduces_the_spreads,
               test_strength_ladder,
               test_ordering_is_preserved,
               test_shift_never_exceeds_the_cap,
               test_section_breaks_survive,
               test_frozen_roles_are_never_touched,
               test_zero_height_stroke_joins_its_row,
               test_describe_matches_the_lines,
               test_degenerate_input]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")
