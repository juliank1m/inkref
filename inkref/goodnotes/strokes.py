"""Stroke-family dispatch: which tpl members hold geometry, and how to read/move/build them.

Families are identified by their tpl signature. Signatures were lifted verbatim from the
shipped GoodNotes binary and match its PenStrokeShape type enum.
"""
from dataclasses import dataclass, field as dc_field

from . import tplfmt
from .tplfmt import f32, bits

# --- signatures ---------------------------------------------------------------
CONSTANT_WIDTH_V1 = "vuA(v)A(S(uu))A(S(uuuu))"
CONSTANT_WIDTH = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"
VARIABLE_WIDTH = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
DYNAMIC_WIDTH = "vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
PENCIL = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"

MOVE_TO, QUAD_TO = 0, 1


# --- coordinate layout --------------------------------------------------------
# ('flat', member, stride, x_index)  : y is assumed at x_index+1
# ('pairs', member)                  : A(S(uu)) -> [[x, y], ...]
# ('quads', member)                  : A(S(uuuu)) -> [[cx, cy, ex, ey], ...]
_LAYOUT = {
    CONSTANT_WIDTH:    [("pairs", 3), ("quads", 4)],
    CONSTANT_WIDTH_V1: [("pairs", 3), ("quads", 4)],
    # verified on real samples: m2 anchor(x,y,w), m3 operands(x,y,w),
    # m6 (x,y), m8 render outline (x,y), m9 (x,y,w,angle,angle)
    VARIABLE_WIDTH:    [("flat", 2, 3, 0), ("flat", 3, 3, 0), ("flat", 6, 2, 0),
                        ("flat", 8, 2, 0), ("flat", 9, 5, 0)],
    DYNAMIC_WIDTH:     [("flat", 3, 3, 0), ("flat", 4, 3, 0), ("flat", 7, 2, 0),
                        ("flat", 9, 2, 0), ("flat", 10, 5, 0)],
}

SUPPORTED = set(_LAYOUT)


def is_supported(sig):
    return sig in _LAYOUT


def translate(sig, members, dx, dy):
    """Move every coordinate-bearing member. Widths, angles, opcodes and index
    members are left untouched. Returns new members; input is not mutated."""
    if sig not in _LAYOUT:
        raise ValueError(f"unsupported stroke family: {sig}")
    out = [list(m) if isinstance(m, list) else m for m in members]
    for spec in _LAYOUT[sig]:
        if spec[0] == "flat":
            _, idx, stride, xoff = spec
            arr = list(out[idx])
            for base in range(0, len(arr) - stride + 1, stride):
                arr[base + xoff] = bits(f32(arr[base + xoff]) + dx)
                arr[base + xoff + 1] = bits(f32(arr[base + xoff + 1]) + dy)
            out[idx] = arr
        elif spec[0] == "pairs":
            out[spec[1]] = [[bits(f32(x) + dx), bits(f32(y) + dy)] for x, y in out[spec[1]]]
        elif spec[0] == "quads":
            out[spec[1]] = [[bits(f32(cx) + dx), bits(f32(cy) + dy),
                             bits(f32(ex) + dx), bits(f32(ey) + dy)]
                            for cx, cy, ex, ey in out[spec[1]]]
    return out


def bounds(sig, members):
    """-> (min_x, min_y, max_x, max_y) over on-curve geometry, or None if empty."""
    pts = on_curve_points(sig, members)
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def on_curve_points(sig, members):
    """Anchor plus each segment endpoint, in draw order."""
    if sig in (CONSTANT_WIDTH, CONSTANT_WIDTH_V1):
        start = [(f32(x), f32(y)) for x, y in members[3]]
        return start + [(f32(q[2]), f32(q[3])) for q in members[4]]
    if sig in (VARIABLE_WIDTH, DYNAMIC_WIDTH):
        off = 0 if sig == VARIABLE_WIDTH else 1
        anchor, ops = members[2 + off], members[3 + off]
        if len(anchor) < 3:
            return []
        pts = [(f32(anchor[0]), f32(anchor[1]))]
        # operands are consumed as (control, end) triplets per segment
        for i in range(0, len(ops) - 5, 6):
            pts.append((f32(ops[i + 3]), f32(ops[i + 4])))
        return pts
    return []


def svg_path(sig, members):
    """-> (d, nominal_width) in GoodNotes units, or (None, w) when empty."""
    if sig in (CONSTANT_WIDTH, CONSTANT_WIDTH_V1):
        width = f32(members[1])
        if not members[3]:
            return None, width
        sx, sy = f32(members[3][0][0]), f32(members[3][0][1])
        d = f"M {sx:.3f} {sy:.3f}"
        for cx, cy, ex, ey in members[4]:
            d += f" Q {f32(cx):.3f} {f32(cy):.3f} {f32(ex):.3f} {f32(ey):.3f}"
        return d, width
    if sig in (VARIABLE_WIDTH, DYNAMIC_WIDTH):
        off = 0 if sig == VARIABLE_WIDTH else 1
        anchor, ops = members[2 + off], members[3 + off]
        if len(anchor) < 3 or len(ops) < 6:
            return None, 1.0
        d = f"M {f32(anchor[0]):.3f} {f32(anchor[1]):.3f}"
        widths = [f32(anchor[2])]
        for i in range(0, len(ops) - 5, 6):
            d += (f" Q {f32(ops[i]):.3f} {f32(ops[i+1]):.3f}"
                  f" {f32(ops[i+3]):.3f} {f32(ops[i+4]):.3f}")
            widths.append(f32(ops[i + 5]))
        return d, sum(widths) / len(widths)
    return None, 1.0


# --- building new geometry ----------------------------------------------------
@dataclass
class Stroke:
    """A stroke in GoodNotes units (1/132 inch, y down).

    points: polyline of (x, y). Encoded as quadratic segments whose control point
    sits at the segment midpoint, which reproduces the polyline exactly.
    """
    points: list
    color: tuple = (0.0, 0.0, 0.0, 1.0)
    width: float = 2.0
    family: str = CONSTANT_WIDTH
    pressures: list = dc_field(default_factory=list)

    def to_tpl(self):
        if self.family != CONSTANT_WIDTH:
            raise NotImplementedError(f"writing {self.family} is not implemented")
        if len(self.points) < 2:
            raise ValueError("a stroke needs at least 2 points")
        segments = []
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            segments.append([bits((x0 + x1) / 2), bits((y0 + y1) / 2), bits(x1), bits(y1)])
        members = [
            2,                                          # version
            bits(self.width),
            [MOVE_TO] + [QUAD_TO] * len(segments),      # opcodes, one per point
            [[bits(self.points[0][0]), bits(self.points[0][1])]],
            segments,
            1,
            list(self.pressures),
        ]
        return self.family, members

    def encode(self):
        sig, members = self.to_tpl()
        return tplfmt.dump(sig, members)


def from_tpl(sig, members):
    """Lossy: geometry, width and family only. For inspection, not round-tripping."""
    pts = on_curve_points(sig, members)
    _, width = svg_path(sig, members)
    return Stroke(points=pts, width=width, family=sig)
