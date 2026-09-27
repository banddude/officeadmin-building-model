"""Read-only detection of overlapping same-type route runs.

Per-circuit routes that were bundled along shared corridors overlap
geometrically, and a takeoff that sums every ``Route`` over-reports conduit
on the shared stretches. Before bundled routes are consolidated (#196),
consumers must at least be told: this module *reports* the shared runs. It
never mutates the model, never invents routes, and never decides a fix.
"""

from __future__ import annotations

from dataclasses import dataclass

from oabm.model import BuildingModel, Point3, Route

#: Two centerline coordinates this close together sit on the same line, and
#: two spans whose intersection is at most this long only touch, not share.
_DEFAULT_TOLERANCE_M = 0.01

#: A segment counts as axis-aligned only when the other two coordinate
#: deltas are exactly this flat. The deterministic rectilinear router emits
#: flat segments; anything else is out of scope for overlap detection.
_SEGMENT_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class RouteOverlap:
    """One maximal shared run between two or more same-type routes.

    ``start`` and ``end`` are the run's span endpoints (the varying axis
    moves between them); ``shared_length_m`` is their distance. ``route_ids``
    is sorted.
    """

    route_ids: tuple[str, ...]
    route_type: str
    shared_length_m: float
    start: Point3
    end: Point3


@dataclass(frozen=True, slots=True)
class _Span:
    """One axis-aligned extent of one route segment."""

    axis: int  # 0=x, 1=y, 2=z
    fixed: tuple[float, float]  # the two constant coordinates, axis order
    lo: float
    hi: float
    routes: frozenset[str]


def _axis_spans(route: Route) -> list[_Span]:
    """Axis-aligned spans of one route's centerline segments."""

    spans: list[_Span] = []
    for start, end in zip(route.centerline.points, route.centerline.points[1:]):
        coords = (
            (start.x, end.x),
            (start.y, end.y),
            (start.z, end.z),
        )
        moving = [
            axis for axis in range(3)
            if abs(coords[axis][1] - coords[axis][0]) > _SEGMENT_EPSILON
        ]
        if len(moving) != 1:
            continue  # degenerate or diagonal segments carry no aligned span
        axis = moving[0]
        fixed = tuple(coords[other][0] for other in range(3) if other != axis)
        lo, hi = sorted(coords[axis])
        spans.append(_Span(axis=axis, fixed=fixed, lo=lo, hi=hi, routes=frozenset({route.id})))
    return spans


def _same_line(a: tuple[float, float], b: tuple[float, float], tolerance_m: float) -> bool:
    return all(abs(first - second) <= tolerance_m for first, second in zip(a, b))


def _point(axis: int, fixed: tuple[float, float], at: float) -> Point3:
    coords = [0.0, 0.0, 0.0]
    coords[axis] = at
    others = [other for other in range(3) if other != axis]
    coords[others[0]] = fixed[0]
    coords[others[1]] = fixed[1]
    return Point3(x=coords[0], y=coords[1], z=coords[2])


@dataclass(frozen=True, slots=True)
class _Cluster:
    """A growing maximal run: one reference line plus one merged extent."""

    axis: int
    fixed: tuple[float, float]
    lo: float
    hi: float
    spans: tuple[_Span, ...]


def _cluster_spans(spans: list[_Span], tolerance_m: float) -> list[_Cluster]:
    """Merge spans into clusters of overlapping extents on one reference line.

    Spans merge when they are collinear within ``tolerance_m`` of the
    cluster's reference line and their extents genuinely overlap (more than
    the tolerance, so two runs that merely touch stay separate). The
    reference line is the first span's, in deterministic input order; graded
    extents union their member routes into one maximal run.
    """

    clusters: list[_Cluster] = []
    for span in sorted(spans, key=lambda item: (item.fixed, item.lo, item.hi, sorted(item.routes))):
        target = None
        for index, cluster in enumerate(clusters):
            if cluster.axis != span.axis:
                continue
            if not _same_line(cluster.fixed, span.fixed, tolerance_m):
                continue
            if min(cluster.hi, span.hi) - max(cluster.lo, span.lo) <= tolerance_m:
                continue
            target = index
            break
        if target is None:
            clusters.append(_Cluster(
                axis=span.axis,
                fixed=span.fixed,
                lo=span.lo,
                hi=span.hi,
                spans=(span,),
            ))
            continue
        merged = clusters[target]
        clusters[target] = _Cluster(
            axis=merged.axis,
            fixed=merged.fixed,
            lo=min(merged.lo, span.lo),
            hi=max(merged.hi, span.hi),
            spans=merged.spans + (span,),
        )
    return clusters


def find_overlapping_route_runs(
    model: BuildingModel,
    *,
    tolerance_m: float = _DEFAULT_TOLERANCE_M,
) -> tuple[RouteOverlap, ...]:
    """Collinear, overlapping route runs of the same type, longest order stable.

    Every pair of same-type routes is checked for axis-aligned centerline
    segments that are collinear within ``tolerance_m`` and genuinely overlap
    (an intersection no longer than ``tolerance_m`` is a touch, not a
    share). Pairwise spans are then merged into maximal shared runs, each
    listing the union of the routes that ride it. Deterministic ordering;
    the model is never mutated.
    """

    if tolerance_m < 0.0:
        raise ValueError("tolerance_m must be non-negative")
    spans_by_route = {route.id: _axis_spans(route) for route in model.routes}
    by_type: dict[str, list[Route]] = {}
    for route in sorted(model.routes, key=lambda item: item.id):
        by_type.setdefault(route.route_type, []).append(route)

    overlaps: list[RouteOverlap] = []
    for route_type in sorted(by_type):
        routes = by_type[route_type]
        pairwise: list[_Span] = []
        for first_index, first in enumerate(routes):
            for second in routes[first_index + 1:]:
                for span_first in spans_by_route[first.id]:
                    for span_second in spans_by_route[second.id]:
                        if span_first.axis != span_second.axis:
                            continue
                        if not _same_line(span_first.fixed, span_second.fixed, tolerance_m):
                            continue
                        lo = max(span_first.lo, span_second.lo)
                        hi = min(span_first.hi, span_second.hi)
                        if hi - lo <= tolerance_m:
                            continue
                        pairwise.append(_Span(
                            axis=span_first.axis,
                            fixed=span_first.fixed,
                            lo=lo,
                            hi=hi,
                            routes=span_first.routes | span_second.routes,
                        ))
        for cluster in _cluster_spans(pairwise, tolerance_m):
            members = sorted({route_id for span in cluster.spans for route_id in span.routes})
            if len(members) < 2:
                continue  # defensive: every pairwise span carries two routes
            overlaps.append(RouteOverlap(
                route_ids=tuple(members),
                route_type=route_type,
                shared_length_m=cluster.hi - cluster.lo,
                start=_point(cluster.axis, cluster.fixed, cluster.lo),
                end=_point(cluster.axis, cluster.fixed, cluster.hi),
            ))
    overlaps.sort(key=lambda item: (
        item.route_type,
        item.start.x, item.start.y, item.start.z,
        item.end.x, item.end.y, item.end.z,
        item.route_ids,
    ))
    return tuple(overlaps)
