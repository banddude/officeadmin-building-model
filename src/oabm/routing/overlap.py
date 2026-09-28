"""Read-only detection of overlapping same-type route runs.

Per-circuit routes that were bundled along shared corridors overlap
geometrically, and a takeoff that sums every ``Route`` over-reports conduit
on the shared stretches. Before bundled routes are consolidated (#196),
consumers must at least be told: this module *reports* the shared runs. It
never mutates the model, never invents routes, and never decides a fix.
"""

from __future__ import annotations

import math
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
    moves between them); ``shared_length_m`` is their distance, the run's
    union length. ``route_ids`` is sorted. ``double_counted_length_m`` is
    the exact coverage surplus: the sum over member routes of each route's
    covered length inside the run (its own spans unioned, so a route riding
    the run in several pieces counts once) minus the run's union length --
    the conduit a per-route takeoff counts more than once.
    """

    route_ids: tuple[str, ...]
    route_type: str
    shared_length_m: float
    double_counted_length_m: float
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
class _LineGroup:
    """Pairwise spans that sit on one reference line (within tolerance)."""

    axis: int
    fixed: tuple[float, float]  # the reference line, the first span's
    spans: tuple[_Span, ...]


def _line_groups(pairwise: list[_Span], tolerance_m: float) -> list[_LineGroup]:
    """Group pairwise spans onto their lines.

    Spans join the group whose reference line (the first span's, in
    deterministic input order) they sit on within ``tolerance_m``.
    """

    groups: list[_LineGroup] = []
    for span in sorted(pairwise, key=lambda item: (item.fixed, item.lo, item.hi, sorted(item.routes))):
        target = next(
            (index for index, group in enumerate(groups)
             if group.axis == span.axis and _same_line(group.fixed, span.fixed, tolerance_m)),
            None,
        )
        if target is None:
            groups.append(_LineGroup(axis=span.axis, fixed=span.fixed, spans=(span,)))
            continue
        joined = groups[target]
        groups[target] = _LineGroup(axis=joined.axis, fixed=joined.fixed, spans=joined.spans + (span,))
    return groups


def _coverage_intervals(
    route_spans: list[_Span], axis: int, fixed: tuple[float, float], tolerance_m: float,
) -> list[tuple[float, float]]:
    """One route's own spans on one line, as sorted ``(lo, hi)`` intervals."""

    return sorted(
        (span.lo, span.hi)
        for span in route_spans
        if span.axis == axis and _same_line(span.fixed, fixed, tolerance_m)
    )


def _union_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge intervals where they overlap or touch.

    A route's consecutive centerline segments share an endpoint, so its own
    coverage is continuous across them; a touch merges, not splits.
    """

    merged: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def member_coverage_intervals(
    route: Route,
    *,
    start: Point3,
    end: Point3,
    tolerance_m: float = _DEFAULT_TOLERANCE_M,
) -> tuple[tuple[float, float], ...]:
    """One route's own covered sub-intervals of the run from ``start`` to ``end``.

    The run's line is read off the two span endpoints: the one axis whose
    coordinates differ, and the other two coordinates. The route's own
    axis-aligned spans on that line are clipped to the run and unioned, so a
    route that rides the run in several pieces still yields one interval
    list. This is the per-member half of :func:`find_overlapping_route_runs`'s
    coverage math; route consolidation (#196) uses it to split each member
    where its own coverage of a shared run starts and stops.
    """

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
        return ()  # a degenerate span carries no line to measure against
    axis = moving[0]
    fixed = tuple(coords[other][0] for other in range(3) if other != axis)
    lo, hi = sorted((coords[axis][0], coords[axis][1]))
    clipped = sorted(
        (max(span.lo, lo), min(span.hi, hi))
        for span in _axis_spans(route)
        if span.axis == axis and _same_line(span.fixed, fixed, tolerance_m)
        and min(span.hi, hi) - max(span.lo, lo) > 0.0
    )
    return tuple(_union_intervals(clipped))


def _covered_length(
    route: Route, axis: int, fixed: tuple[float, float],
    lo: float, hi: float, tolerance_m: float,
) -> float:
    """How much of ``[lo, hi]`` the route itself covers on one line.

    Its own spans clipped to the run and unioned: a route that covers the
    run in several pieces counts its coverage once.
    """

    return math.fsum(
        high - low
        for low, high in member_coverage_intervals(
            route,
            start=_point(axis, fixed, lo),
            end=_point(axis, fixed, hi),
            tolerance_m=tolerance_m,
        )
    )


def find_overlapping_route_runs(
    model: BuildingModel,
    *,
    tolerance_m: float = _DEFAULT_TOLERANCE_M,
    excluded_route_ids: frozenset[str] = frozenset(),
) -> tuple[RouteOverlap, ...]:
    """Collinear, overlapping route runs of the same type, longest order stable.

    Every pair of same-type routes is checked for axis-aligned centerline
    segments that are collinear within ``tolerance_m`` and genuinely overlap
    (an intersection no longer than ``tolerance_m`` is a touch, not a
    share). The pairwise spans group onto their lines, and each maximal run
    is a connected component of the union of the participating routes' own
    coverage on its line: one route riding the whole corridor merges the
    stretches it shares with different routes into a single run. Each run
    lists its members, the union length, and the exact coverage surplus a
    per-route takeoff double-counts. Deterministic ordering; the model is
    never mutated.
    """

    if tolerance_m < 0.0:
        raise ValueError("tolerance_m must be non-negative")
    eligible_routes = tuple(route for route in model.routes if route.id not in excluded_route_ids)
    spans_by_route = {route.id: _axis_spans(route) for route in eligible_routes}
    routes_by_id = {route.id: route for route in eligible_routes}
    by_type: dict[str, list[Route]] = {}
    for route in sorted(eligible_routes, key=lambda item: item.id):
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

        for group in _line_groups(pairwise, tolerance_m):
            participating = sorted({route_id for span in group.spans for route_id in span.routes})
            coverage = sorted(
                interval
                for route_id in participating
                for interval in _coverage_intervals(spans_by_route[route_id], group.axis, group.fixed, tolerance_m)
            )
            for lo, hi in _union_intervals(coverage):
                members = [
                    route_id for route_id in participating
                    if _covered_length(routes_by_id[route_id], group.axis, group.fixed, lo, hi, tolerance_m) > 0.0
                ]
                if len(members) < 2:
                    continue  # a solo stretch of one participant is not a shared run
                covered = math.fsum(
                    _covered_length(routes_by_id[route_id], group.axis, group.fixed, lo, hi, tolerance_m)
                    for route_id in members
                )
                union = hi - lo
                overlaps.append(RouteOverlap(
                    route_ids=tuple(members),
                    route_type=route_type,
                    shared_length_m=union,
                    # covered >= union by construction; the floor only absorbs fsum noise
                    double_counted_length_m=max(0.0, covered - union),
                    start=_point(group.axis, group.fixed, lo),
                    end=_point(group.axis, group.fixed, hi),
                ))
    overlaps.sort(key=lambda item: (
        item.route_type,
        item.start.x, item.start.y, item.start.z,
        item.end.x, item.end.y, item.end.z,
        item.route_ids,
    ))
    return tuple(overlaps)
