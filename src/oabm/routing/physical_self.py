"""Certified separation of actual flat-ended normal-disc routing solids.

Internal admitted-primitive API. AABBs overcover; overlapping bounds are never
collision evidence. Each unresolved product is covered by its two children.
"""
from __future__ import annotations

from fractions import Fraction as F

from .physical_verify_types import Primitive, WorkLedger, VerificationStop


def _stop(reason, *ps):
    raise VerificationStop("invalid_input", reason, tuple(p.segment_id for p in ps))


def _shape(p):
    """Re-establish the geometric hypotheses used by the seam/local proofs."""
    r = p.tube_radius
    if r <= 0:
        _stop("self_invalid_radius", p)
    if p.kind == "line":
        a = p.axis
        if a not in (0, 1, 2) or p.axial is None:
            _stop("self_invalid_line", p)
        d = p.end[a] - p.start[a]
        t = tuple((1 if d > 0 else -1) if i == a else 0 for i in range(3))
        if (d == 0 or p.axial != (min(p.start[a], p.end[a]), max(p.start[a], p.end[a]))
                or any(p.start[i] != p.end[i] or p.center[i] != p.start[i]
                       for i in range(3) if i != a)
                or p.start_tangent != t or p.end_tangent != t):
            _stop("self_invalid_line", p)
        return
    if (p.kind != "quarter_arc" or p.major_radius is None or p.major_radius <= r
            or p.plane_axes is None or len(set(p.plane_axes)) != 2
            or any(a not in (0, 1, 2) for a in p.plane_axes)
            or p.signs is None or len(p.signs) != 2 or any(s not in (-1, 1) for s in p.signs)):
        _stop("self_invalid_quarter_arc", p)
    a, b = p.plane_axes
    sa, sb = p.signs
    R = p.major_radius
    u, v = list(p.center), list(p.center)
    u[a] += sa*R
    v[b] += sb*R
    forward = (p.start, p.end) == (tuple(u), tuple(v))
    reverse = (p.start, p.end) == (tuple(v), tuple(u))
    ts = tuple(sb if i == b else 0 for i in range(3))
    te = tuple(-sa if i == a else 0 for i in range(3))
    if reverse:
        ts, te = tuple(-x for x in te), tuple(-x for x in ts)
    if not (forward or reverse) or p.start_tangent != ts or p.end_tangent != te:
        _stop("self_invalid_quarter_arc", p)


def _seam(a, b):
    if ({a.kind, b.kind} != {"line", "quarter_arc"} or a.end != b.start
            or a.end_tangent != b.start_tangent or a.tube_radius != b.tube_radius):
        _stop("self_invalid_seam", a, b)


def _gap2(a, b):
    return sum((max(F(0), x[0]-y[1], y[0]-x[1]))**2 for x, y in zip(a, b))


def _membership(p, q):
    if p.kind == "line":
        return (p.axial[0] <= q[p.axis] <= p.axial[1]
                and sum((q[i]-p.center[i])**2 for i in range(3) if i != p.axis)
                <= p.tube_radius**2)
    a, b = p.plane_axes
    z = 3-a-b
    x, y = (p.signs[0]*(q[a]-p.center[a]), p.signs[1]*(q[b]-p.center[b]))
    if x < 0 or y < 0:
        return False
    rho2 = x*x+y*y
    A = rho2+(q[z]-p.center[z])**2+p.major_radius**2-p.tube_radius**2
    return A > 0 and A*A <= 4*p.major_radius**2*rho2


def _root(p):
    return (p.axial if p.kind == "line" else (F(0), F(1)),
            (-p.tube_radius, p.tube_radius), (-p.tube_radius, p.tube_radius))


def _outside(p, cell):
    return sum((max(F(0), lo, -hi))**2 for lo, hi in cell[1:]) > p.tube_radius**2


def _point(p, s, u, v):
    q = list(p.center)
    if p.kind == "line":
        a, b = (i for i in range(3) if i != p.axis)
        q[p.axis], q[a], q[b] = s, q[a]+u, q[b]+v
    else:
        a, b = p.plane_axes
        q[a] += p.signs[0]*(p.major_radius+u)*(1-s*s)/(1+s*s)
        q[b] += p.signs[1]*(p.major_radius+u)*2*s/(1+s*s)
        q[3-a-b] += v
    return tuple(q)


def _bounds(p, cell):
    """Outward exact rational bounds. No half-tick kernel coercion."""
    (lo, hi), (ul, uh), (vl, vh) = cell
    if p.kind == "line":
        a, b = (i for i in range(3) if i != p.axis)
        out = list((c, c) for c in p.center)
        out[p.axis] = (lo, hi)
        out[a] = (p.center[a]+ul, p.center[a]+uh)
        out[b] = (p.center[b]+vl, p.center[b]+vh)
        return tuple(out)
    a, b = p.plane_axes
    R = p.major_radius
    local = (((R+ul)*(1-hi*hi)/(1+hi*hi), (R+uh)*(1-lo*lo)/(1+lo*lo)),
             ((R+ul)*2*lo/(1+lo*lo), (R+uh)*2*hi/(1+hi*hi)))
    out = [(c, c) for c in p.center]
    for axis, sign, (low, high) in zip((a, b), p.signs, local):
        out[axis] = ((p.center[axis]+low, p.center[axis]+high) if sign == 1
                     else (p.center[axis]-high, p.center[axis]-low))
    z = 3-a-b
    out[z] = (p.center[z]+vl, p.center[z]+vh)
    return tuple(out)


def _witnesses(a, b, ca, cb, ba, bb):
    # Exactly at most 13 candidates, independent of refinement depth.
    for p, cell in ((a, ca), (b, cb)):
        yield p.start
        yield p.end
        s, u, v = ((lo+hi)/2 for lo, hi in cell)
        yield _point(p, s, u, v)
        yield _point(p, s, F(0), F(0))
        yield tuple((x+y)/2 for x, y in zip(p.start, p.end))
    yield tuple((max(x[0], y[0])+min(x[1], y[1]))/2 for x, y in zip(ba, bb))
    if a.kind == b.kind == "line" and a.axis != b.axis:
        for p, other in ((a, b), (b, a)):
            q = list(p.center)
            q[p.axis] = other.center[p.axis]
            yield tuple(q)


def _contact(a, b, q):
    raise VerificationStop("rejected_route", "self_contact_or_overlap",
                           (a.segment_id, b.segment_id),
                           tuple((f"witness_{i}_m", x) for i, x in enumerate(q)))


def _parallel(a, b):
    if a.kind != "line" or b.kind != "line" or a.axis != b.axis:
        return None
    if a.tube_radius != b.tube_radius:
        return None
    radial2 = sum((a.center[i]-b.center[i])**2 for i in range(3) if i != a.axis)
    if radial2 > (2*a.tube_radius)**2:
        return (("radial_center_distance_squared_m2", radial2),)
    lo, hi = max(a.axial[0], b.axial[0]), min(a.axial[1], b.axial[1])
    if lo <= hi:
        q = tuple((lo+hi)/2 if i == a.axis else (a.center[i]+b.center[i])/2 for i in range(3))
        # Membership is deliberately rechecked, rather than relying on bounds.
        if _membership(a, q) and _membership(b, q):
            _contact(a, b, q)
    return None


def _local(a, m, b):
    _seam(a, m)
    _seam(m, b)
    if a.tube_radius != b.tube_radius:
        return None
    if a.kind == b.kind == "line" and m.kind == "quarter_arc":
        d = m.major_radius-m.tube_radius
        # Inward seam-disc rim points attain the two orthogonal support gaps.
        pa = tuple(m.center[i]+(a.end[i]-m.center[i])*d/m.major_radius for i in range(3))
        pb = tuple(m.center[i]+(b.start[i]-m.center[i])*d/m.major_radius for i in range(3))
        return "self_line_arc_line", (("distance_squared_m2", 2*d*d), ("major_radius_m", m.major_radius),
                                          ("tube_radius_m", m.tube_radius)) + tuple(
            (f"witness_{name}_{i}_m", x) for name, q in (("a", pa), ("b", pb)) for i, x in enumerate(q))
    if a.kind == b.kind == "quarter_arc" and m.kind == "line" and a.major_radius == b.major_radius:
        length = m.axial[1]-m.axial[0]
        return "self_arc_line_arc", (("distance_m", length), ("major_radius_m", a.major_radius),
                                           ("tube_radius_m", a.tube_radius)) + tuple(
            (f"witness_{name}_{i}_m", x) for name, q in (("a", a.end), ("b", b.start)) for i, x in enumerate(q))
    return None


def _cover(a, b, ledger, max_depth):
    # Root is already charged by caller. Only O(depth) pending products exist.
    stack = [(_root(a), _root(b), 0)]
    while stack:
        ca, cb, depth = stack.pop()
        if depth:
            ledger.debit("self_child_cell")
        if _outside(a, ca) or _outside(b, cb):
            continue
        ba, bb = _bounds(a, ca), _bounds(b, cb)
        if _gap2(ba, bb) > 0:
            continue
        for q in _witnesses(a, b, ca, cb, ba, bb):
            if _membership(a, q) and _membership(b, q):
                _contact(a, b, q)
        if depth >= max_depth:
            raise VerificationStop("numerically_unresolved", "self_refinement_depth",
                                   (a.segment_id, b.segment_id), (("depth", F(depth)),))
        # Charge BEFORE allocating children. Stable ID then parameter tie order.
        ledger.debit("self_split")
        choice = None
        for body, p, cell in ((0, a, ca), (1, b, cb)):
            for axis, (lo, hi) in enumerate(cell):
                width = hi-lo
                if axis == 0 and p.kind == "quarter_arc":
                    width *= 2*(p.major_radius+p.tube_radius)
                key = (-width, p.segment_id, axis, body)
                if choice is None or key < choice[0]:
                    choice = (key, body, axis)
        _, body, axis = choice
        cell = ca if body == 0 else cb
        lo, hi = cell[axis]
        mid = (lo+hi)/2
        low, high = list(cell), list(cell)
        low[axis], high[axis] = (lo, mid), (mid, hi)
        if body == 0:
            stack.append((tuple(high), cb, depth+1))
            stack.append((tuple(low), cb, depth+1))
        else:
            stack.append((ca, tuple(high), depth+1))
            stack.append((ca, tuple(low), depth+1))
    ledger.record("self_parameter_cover", (a.segment_id, b.segment_id))


def verify_self(primitives: tuple[Primitive, ...], ledger: WorkLedger, *, max_depth: int) -> None:
    """Return only after every actual-solid self obligation is certified."""
    if type(max_depth) is not int or not 0 <= max_depth <= 64:
        raise VerificationStop("invalid_input", "self_invalid_depth")
    for p in primitives:
        ledger.debit("self_shape")
        _shape(p)
    for i in range(len(primitives)-1):
        ledger.debit("self_adjacent_seam")
        a, b = primitives[i:i+2]
        _seam(a, b)
        ledger.record("self_exact_seam_disc", (a.segment_id, b.segment_id),
                      exact=(("tube_radius_m", a.tube_radius),) + tuple(
                          (f"joint_{k}_m", x) for k, x in enumerate(a.end)))
    for i, a in enumerate(primitives):
        for j in range(i+2, len(primitives)):
            ledger.debit("self_root_pair")
            b = primitives[j]
            ids = (a.segment_id, b.segment_id)
            if j == i+2:
                proof = _local(a, primitives[i+1], b)
                if proof is not None:
                    kind, exact = proof
                    ledger.record(kind, (a.segment_id, primitives[i+1].segment_id, b.segment_id), exact=exact)
                    continue
            gap = _gap2(a.bounds, b.bounds)
            if gap > 0:
                ledger.record("self_aabb_separation", ids, exact=(("lower_distance_squared_m2", gap),))
                continue
            parallel = _parallel(a, b)
            if parallel is not None:
                ledger.record("self_parallel_separation", ids, exact=parallel)
                continue
            _cover(a, b, ledger, max_depth)
