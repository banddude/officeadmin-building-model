"""Streaming exact union-complement checks, never boxwise erosion."""
from __future__ import annotations

from fractions import Fraction
from .physical_distance import cylinder_aabb_distance, quarter_torus_aabb_distance, classify_distance
from .physical_verify_types import Primitive, Box, WorkLedger, VerificationStop, reserved_sort


def check_box(primitive: Primitive, box: Box, threshold: Fraction,
              ledger: WorkLedger, *, ids: tuple[str, ...], max_depth: int, kind: str):
    ledger.debit(kind + "_pair")
    for depth, bits in enumerate((32, 64, 128)):
        if depth > max_depth:
            break
        ledger.debit("distance_refinement")
        if primitive.kind == "line":
            bounds = cylinder_aabb_distance(axis=primitive.axis, axial=primitive.axial,
                center=primitive.center, radius=primitive.tube_radius, box=box, bits=bits)
        else:
            bounds = quarter_torus_aabb_distance(center=primitive.center,
                plane_axes=primitive.plane_axes, signs=primitive.signs,
                major_radius=primitive.major_radius, radius=primitive.tube_radius,
                box=box, bits=bits)
        status = classify_distance(bounds, threshold)
        facts = (("lower_m", bounds.lower), ("upper_m", bounds.upper),
                 ("required_m", threshold))
        if status == "safe_separation":
            ledger.record(kind + "_separated", ids, exact=facts)
            return
        if status in ("contact_or_overlap", "insufficient_clearance"):
            raise VerificationStop("rejected_route", kind + "_" + status, ids, facts)
    raise VerificationStop("numerically_unresolved", kind + "_distance_unresolved", ids, facts)


def _unique(values, ledger):
    ordered = reserved_sort(values, ledger)
    result = []
    for item in ordered:
        ledger.debit("boundary_dedup")
        if not result or item != result[-1]:
            result.append(item)
    return result


def verify_domain(primitives: tuple[Primitive, ...], boxes: tuple[Box, ...],
                  threshold: Fraction, ledger: WorkLedger, *, max_depth: int):
    boxes = tuple(reserved_sort(boxes, ledger))
    ledger.debit("domain_bbox_scan", 6*len(boxes))
    exterior = tuple((min(box[i][0] for box in boxes),
                      max(box[i][1] for box in boxes)) for i in range(3))
    for primitive in primitives:
        ledger.debit("primitive_extrema")
        bounds = primitive.bounds
        for axis in range(3):
            for face, gap in (("lo", bounds[axis][0]-exterior[axis][0]),
                              ("hi", exterior[axis][1]-bounds[axis][1])):
                ledger.debit("domain_exterior_face")
                ids = (primitive.segment_id, str(axis), face)
                facts = (("gap_m", gap), ("required_m", threshold))
                if gap < threshold:
                    raise VerificationStop("rejected_route", "domain_exterior_clearance", ids, facts)
                if gap == threshold:
                    raise VerificationStop("numerically_unresolved", "domain_exterior_equality", ids, facts)
                ledger.record("domain_exterior_separated", ids, exact=facts)

    xs = _unique([v for box in boxes for v in box[0]], ledger)
    gap_index = 0
    for xlo, xhi in zip(xs, xs[1:]):
        ledger.debit("domain_x_slab")
        active_x = []
        for box in boxes:
            ledger.debit("domain_box_coverage")
            if box[0][0] <= xlo and box[0][1] >= xhi:
                active_x.append(box)
        # B's full y interval is required even where no x-active box exists.
        ys = _unique([exterior[1][0], exterior[1][1]] +
                     [v for box in active_x for v in box[1]], ledger)
        for ylo, yhi in zip(ys, ys[1:]):
            ledger.debit("domain_y_slab")
            intervals = []
            for box in active_x:
                ledger.debit("domain_box_coverage")
                if box[1][0] <= ylo and box[1][1] >= yhi:
                    intervals.append(box[2])
            intervals = reserved_sort(intervals, ledger)
            cursor = exterior[2][0]
            # One synthetic terminal interval emits any final positive gap.
            for zlo, zhi in intervals + [(exterior[2][1], exterior[2][1])]:
                ledger.debit("domain_interval_merge")
                if zlo > cursor:
                    ledger.debit("domain_gap_cell")
                    gap = ((xlo, xhi), (ylo, yhi), (cursor, zlo))
                    for primitive in primitives:
                        check_box(primitive, gap, threshold, ledger,
                            ids=(primitive.segment_id, "domain-gap:"+str(gap_index)),
                            max_depth=max_depth, kind="domain")
                    gap_index += 1
                cursor = max(cursor, zhi)
    ledger.record("domain_union_complete", (), exact=(("gap_cells", Fraction(gap_index)),))
