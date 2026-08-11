"""Scratch audit: can one stroke receive a flow offset twice?"""
import sys
sys.path.insert(0, "/Users/julian/projects/inkref")

from inkref.ink import flow, layout, collide
from inkref.ai import schemas


def page(baselines, n=6, h=10.0, x0=50.0, w=8.0, gap=4.0):
    boxes = []
    for b in baselines:
        x = x0
        for _ in range(n):
            boxes.append((x, b - h, x + w, b))
            x += w + gap
    return boxes


BASELINES = [100.0, 145.0, 175.0, 205.0, 235.0]
boxes = page(BASELINES)
a = layout.analyze(boxes)
print("lines", len(a.lines), "ref_h", round(a.ref_h, 2), "pitch", round(a.pitch, 2),
      "blocks", a.blocks, "is_text", [l.is_text for l in a.lines])

# --- 1. does the model layer let one line be named twice inside ONE group? -------------
raw, notes = schemas.parse_groups(
    {"groups": [{"lines": ["L1", "L1"], "role": "equation", "confidence": 0.9}]},
    [f"L{k}" for k in range(len(a.lines))])
print("parse_groups ->", raw, notes)
order = {f"L{k}": k for k in range(len(a.lines))}      # analyzer.py:220-222
print("analyzer maps to ->", [[order[i] for i in g if i in order] for g in raw])

# --- 2. merge_groups with that group ---------------------------------------------------
m = layout.merge_groups(a, [[1, 1]])
for k, line in enumerate(m.lines):
    idx = line.indices
    dup = sorted(i for i in set(idx) if idx.count(i) > 1)
    print(f"merged line {k}: {len(idx)} strokes, duplicates {dup[:5]}"
          f"{'...' if len(dup) > 5 else ''}")

# --- 3. run stage 8 on it and measure what each stroke actually moved -------------------
offsets = [(0.0, 0.0)] * m.n_boxes
out, report = flow.space(m, boxes, offsets, page=(600.0, 800.0))
print("report", report)

by_line = {}
for k, line in enumerate(m.lines):
    dys = {round(out[i][1], 4) for i in set(line.indices)}
    by_line[k] = dys
    print(f"line {k} baseline {line.baseline}: dy set {sorted(dys)}")

cap = min(flow.MAX_BLOCK_SHIFT, layout.BALANCED.max_shift) * m.ref_h
print("cap", cap, "max |dy| applied", max(abs(o[1]) for o in out))

# what a clean (deduplicated) group would have produced
m2 = layout.merge_groups(a, [[1, 2]])   # a normal, non-degenerate group for contrast
out2, rep2 = flow.space(m2, boxes, [(0.0, 0.0)] * m2.n_boxes, page=(600.0, 800.0))
print("contrast report", rep2, "max |dy|", max(abs(o[1]) for o in out2))
