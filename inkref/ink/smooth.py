"""Fairing a pen stroke: keep the letter, drop the shake.

This is the one transformation in the project that changes a letter's shape rather than
its position, so it is worth being exact about what it does and does not do.

It does not regenerate a letter (SPEC §16 rules that out) and it does not fit a font or a
model. It resamples the *same* path, damping the high-frequency wobble that a hand puts
into a line while leaving every deliberate curve and corner where the writer put it. The
output is the writer's own stroke with the tremble taken out — the same way a photograph
can be denoised without becoming a different photograph.

Two properties make that safe to claim:

  * **Endpoints never move.** A letter cannot drift away from its neighbours or off its
    baseline, so nothing downstream in the layout engine is invalidated.
  * **Corners are preserved.** A vertex whose turn exceeds `corner` is held fixed. Without
    that, Chaikin rounds the apex of a `4`, the join of a `k` and every crossbar into mush,
    which is exactly how "smoothing" turns handwriting into soup.

Chaikin rather than a spline fit: it is corner-cutting, so the result is bounded by the
original polyline's convex hull and cannot overshoot into a neighbouring letter — a
property a least-squares spline does not have.
"""
import math


def _turn(a, b, c):
    """Absolute turn angle at b, in radians."""
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.acos(max(-1.0, min(1.0, cos)))


def corners(points, threshold):
    """Indices that must survive: the endpoints and every sharp turn."""
    keep = {0, len(points) - 1}
    for i in range(1, len(points) - 1):
        if _turn(points[i - 1], points[i], points[i + 1]) >= threshold:
            keep.add(i)
    return keep


def fair(points, iterations=2, corner=math.radians(60), ratio=0.25):
    """-> a smoothed copy of `points`. Endpoints and sharp corners are fixed.

    `ratio` is Chaikin's cut fraction; 0.25 is the classic value and takes a quarter off
    each side of a segment per pass. Two passes removes visible jitter without noticeably
    softening the letter.
    """
    pts = [tuple(map(float, p)) for p in points]
    if len(pts) < 3 or iterations < 1:
        return pts

    for _ in range(iterations):
        fixed = corners(pts, corner)
        out = [pts[0]]
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            a_fixed, b_fixed = i in fixed, (i + 1) in fixed
            # a segment between two fixed points is left exactly alone
            if a_fixed and b_fixed:
                continue
            if not a_fixed:
                out.append((a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
            if not b_fixed:
                out.append((a[0] + (b[0] - a[0]) * (1 - ratio),
                            a[1] + (b[1] - a[1]) * (1 - ratio)))
            else:
                out.append(b)
        if out[-1] != pts[-1]:
            out.append(pts[-1])
        # collapse points the cut has driven together, or the count grows every pass
        pts = _dedupe(out)
    return pts


def _dedupe(points, eps=1e-4):
    out = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    return out


def wobble(points):
    """How much the path turns back on itself, per interior point. 0 = perfectly smooth.

    A measure of shake specifically, not of curvature: a circle turns constantly but never
    reverses, while a tremulous line reverses again and again.
    """
    if len(points) < 3:
        return 0.0
    flips, sign = 0, 0
    for i in range(1, len(points) - 1):
        ax, ay = points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]
        bx, by = points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]
        cross = ax * by - ay * bx
        s = 1 if cross > 1e-9 else (-1 if cross < -1e-9 else 0)
        if s and sign and s != sign:
            flips += 1
        if s:
            sign = s
    return flips / (len(points) - 2)
