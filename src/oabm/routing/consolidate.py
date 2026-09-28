"""Opt-in consolidation of bundled per-circuit routes into shared trunks.

When circuits are routed one connection at a time with bundle hints, every
circuit gets its own ``Route`` from device to panel and a shared corridor
ends up carrying one parallel centerline per circuit -- a per-route takeoff
then counts the trunk once per circuit (#196). This module is the separate,
opt-in post-process behind the read-only report in ``overlap.py``: it merges
the genuinely shared stretches into trunk ``Route`` records, splits each
member's remainder into branch ``Route`` records, and re-points conductors
and circuits so every conductor keeps its exact old length.

The consolidation is deterministic bookkeeping on existing geometry: trunks
run on the shared line the overlap detector reports, branches keep their
route's own vertices, and every cut point is an existing centerline
coordinate, so split pieces sum back to their original lengths. Nothing here
changes the canonical contract; a model that carries no overlapping runs is
returned as the same object, untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from oabm.model import (
    BuildingModel, DERIVATION_INFERRED, ElectricalDevice, ElectricalEquipment,
    Point3, Polyline3D, Port, Pose, Provenance, Route, RouteFitting, Vector3,
    stable_id,
)

from .overlap import (
    _SEGMENT_EPSILON, _same_line, find_overlapping_route_runs, member_coverage_intervals,
)
from .router import RoutingError

#: Vertex positions this close together are the same point for fitting and
#: port bookkeeping; it matches the contract's own duplicate-point epsilon.
_VERTEX_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    """What one consolidation pass did, in counts and meters.

    ``shared_length_m`` is the union length now carried by trunk routes and
    ``double_counted_length_m`` is the total length removed from per-route
    takeoff, including retraced stretches within one member route. The
    latter is also reported separately as ``within_route_repeated_length_m``.
    """

    routes_before: int
    routes_after: int
    trunk_routes: int
    branch_routes: int
    routes_left_untouched: int
    fittings_before: int
    fittings_after: int
    ports_added: int
    shared_length_m: float
    double_counted_length_m: float
    within_route_repeated_length_m: float


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    """The consolidated model plus the report describing what changed."""

    model: BuildingModel
    report: ConsolidationReport


@dataclass(frozen=True, slots=True)
class _TrunkSpec:
    """One maximal stretch a constant member set shares on one line."""

    route_type: str
    axis: int  # 0=x, 1=y, 2=z
    fixed: tuple[float, float]  # the reference line's two constant coordinates
    lo: float
    hi: float
    members: tuple[str, ...]  # sorted member route ids


@dataclass
class _BranchPiece:
    """One route's unshared remainder between two cut points."""

    origin_id: str
    ordinal: int
    points: list[Point3]


def _moving_axis(start: Point3, end: Point3) -> int:
    moving = [
        axis for axis, (first, second) in enumerate(
            ((start.x, end.x), (start.y, end.y), (start.z, end.z))
        )
        if abs(second - first) > _SEGMENT_EPSILON
    ]
    if len(moving) != 1:
        raise ValueError("run span is not axis-aligned")
    return moving[0]


def _axis_point(axis: int, fixed: tuple[float, float], at: float) -> Point3:
    coords = [0.0, 0.0, 0.0]
    coords[axis] = at
    others = [other for other in range(3) if other != axis]
    coords[others[0]] = fixed[0]
    coords[others[1]] = fixed[1]
    return Point3(x=coords[0], y=coords[1], z=coords[2])


def _axis_coordinate(point: Point3, axis: int) -> float:
    return (point.x, point.y, point.z)[axis]


def _coord_key(value: float) -> str:
    return f"{round(value, 6):.6f}"


def _point_key(point: Point3) -> str:
    return f"{_coord_key(point.x)},{_coord_key(point.y)},{_coord_key(point.z)}"


def _position_key(point: Point3) -> tuple[float, float, float]:
    return (round(point.x, 9), round(point.y, 9), round(point.z, 9))


def _distance(first: Point3, second: Point3) -> float:
    return math.sqrt(
        (first.x - second.x) ** 2 + (first.y - second.y) ** 2 + (first.z - second.z) ** 2
    )


def _route_length(route: Route) -> float:
    return math.fsum(
        _distance(first, second)
        for first, second in zip(route.centerline.points, route.centerline.points[1:])
    )


def _consolidation_provenance(model_id: str, confidence: float) -> tuple[Provenance, ...]:
    return (Provenance(
        source_kind="derived",
        source_id=model_id,
        method="route-consolidation",
        confidence=confidence,
        derivation=DERIVATION_INFERRED,
    ),)


def _derived_provenance(
    model_id: str, confidence: float, *sources,
) -> tuple[Provenance, ...]:
    """Carry every source observation forward before adding this derivation."""
    records: list[Provenance] = []
    for source in sources:
        for record in source.provenance:
            if record not in records:
                records.append(record)
    for record in _consolidation_provenance(model_id, confidence):
        if record not in records:
            records.append(record)
    return tuple(records)


def _is_turn_fitting(fitting_type: str) -> bool:
    return fitting_type.lower().startswith(("elbow", "bend"))


def _point_on_centerline(point: Point3, route: Route) -> bool:
    return any(
        abs(_distance(a, point) + _distance(point, b) - _distance(a, b)) <= 1e-7
        for a, b in zip(route.centerline.points, route.centerline.points[1:])
    )


def _constant_member_intervals(
    run, routes_by_id: dict[str, Route], tolerance_m: float,
) -> list[tuple[float, float, tuple[str, ...]]]:
    """Split one shared run where its member coverage starts and stops.

    A run is maximal shared geometry, but its members need not all ride the
    whole span: one route can join mid-corridor or drop out early, and a
    stretch covered by a single participant is not shared at all. Sweeping
    the run between coverage boundaries yields maximal stretches with a
    constant member set; only stretches with two or more members become
    trunks, which is what keeps every conductor's length reproducible from
    the pieces it is re-pointed to.
    """

    axis = _moving_axis(run.start, run.end)
    member_intervals = {
        route_id: member_coverage_intervals(
            routes_by_id[route_id], start=run.start, end=run.end, tolerance_m=tolerance_m,
        )
        for route_id in run.route_ids
    }
    lo, hi = sorted((
        _axis_coordinate(run.start, axis), _axis_coordinate(run.end, axis),
    ))
    boundaries = {lo, hi}
    for intervals in member_intervals.values():
        for start, end in intervals:
            boundaries.add(start)
            boundaries.add(end)
    edges = sorted(boundaries)
    intervals: list[tuple[float, float, tuple[str, ...]]] = []
    for start, end in zip(edges, edges[1:]):
        if end - start <= _SEGMENT_EPSILON:
            continue
        middle = (start + end) / 2.0
        members = tuple(sorted(
            route_id for route_id, covered in member_intervals.items()
            if any(low <= middle <= high for low, high in covered)
        ))
        if (
            intervals
            and intervals[-1][2] == members
            and start - intervals[-1][1] <= _SEGMENT_EPSILON
        ):
            intervals[-1] = (intervals[-1][0], end, members)
            continue
        intervals.append((start, end, members))
    return intervals


def _trunk_specs(
    runs, routes_by_id: dict[str, Route], tolerance_m: float,
) -> list[_TrunkSpec]:
    """All shared trunk stretches, in one deterministic order."""

    specs: dict[tuple, _TrunkSpec] = {}
    for run in runs:
        axis = _moving_axis(run.start, run.end)
        fixed = tuple(
            coordinate for other, coordinate in enumerate(
                (run.start.x, run.start.y, run.start.z)
            ) if other != axis
        )
        for lo, hi, members in _constant_member_intervals(run, routes_by_id, tolerance_m):
            if len(members) < 2:
                continue  # a solo stretch stays with its own route
            key = (run.route_type, axis, fixed, round(lo, 9), round(hi, 9), members)
            specs[key] = _TrunkSpec(
                route_type=run.route_type, axis=axis, fixed=fixed,
                lo=lo, hi=hi, members=members,
            )
    return [specs[key] for key in sorted(specs)]


def _split_specs_at_reversals(
    specs: list[_TrunkSpec], routes_by_id: dict[str, Route], tolerance_m: float,
) -> list[_TrunkSpec]:
    """Keep a member's out-and-back traversal as separate trunk references.

    A maximal physical trunk can cross a route's reversal vertex. Splitting
    there lets that route reference the same physical subtrunk twice while
    its conductor retains the actual traversal length.
    """
    result: list[_TrunkSpec] = []
    for spec in specs:
        cuts = {spec.lo, spec.hi}
        for route_id in spec.members:
            points = routes_by_id[route_id].centerline.points
            for before, pivot, after in zip(points, points[1:], points[2:]):
                first = _axis_coordinate(pivot, spec.axis) - _axis_coordinate(before, spec.axis)
                second = _axis_coordinate(after, spec.axis) - _axis_coordinate(pivot, spec.axis)
                if first * second >= 0:
                    continue
                fixed = tuple((pivot.x, pivot.y, pivot.z)[axis] for axis in range(3) if axis != spec.axis)
                at = _axis_coordinate(pivot, spec.axis)
                if _same_line(spec.fixed, fixed, tolerance_m) and spec.lo + _SEGMENT_EPSILON < at < spec.hi - _SEGMENT_EPSILON:
                    cuts.add(at)
        edges = sorted(cuts)
        result.extend(replace(spec, lo=lo, hi=hi) for lo, hi in zip(edges, edges[1:]))
    return result


def _portion_point(start: Point3, end: Point3, axis: int, at: float) -> Point3:
    """The point on segment ``start``->``end`` whose axis coordinate is ``at``.

    Original endpoint objects are returned where they match, so untouched
    vertices keep their exact floats through a split.
    """

    if at == _axis_coordinate(start, axis):
        return start
    if at == _axis_coordinate(end, axis):
        return end
    coords = [start.x, start.y, start.z]
    coords[axis] = at
    return Point3(x=coords[0], y=coords[1], z=coords[2])


def _segment_portions(
    route: Route, specs: list[_TrunkSpec], tolerance_m: float,
) -> list[list[tuple[_TrunkSpec | None, Point3, Point3]]]:
    """Per segment, the ordered stretches of it, as ``(spec, start, end)``.

    ``spec is None`` marks a stretch no trunk covers, which stays with the
    route as branch geometry. Cut coordinates always come from trunk spec
    bounds, which are themselves centerline coordinates of participating
    routes, so splitting never invents geometry.
    """

    points = route.centerline.points
    segment_portions: list[list[tuple[_TrunkSpec | None, Point3, Point3]]] = []
    for start, end in zip(points, points[1:]):
        coords = ((start.x, end.x), (start.y, end.y), (start.z, end.z))
        moving = [
            axis for axis in range(3)
            if abs(coords[axis][1] - coords[axis][0]) > _SEGMENT_EPSILON
        ]
        if len(moving) != 1:
            segment_portions.append([(None, start, end)])
            continue  # diagonal segments carry no aligned span to share
        axis = moving[0]
        seg_lo, seg_hi = sorted(coords[axis])
        forward = coords[axis][1] >= coords[axis][0]
        fixed = tuple(coords[other][0] for other in range(3) if other != axis)
        cuts: list[tuple[float, float, _TrunkSpec]] = []
        for spec in specs:
            if route.id not in spec.members:
                continue  # a nearby touch does not make this route a trunk member
            if spec.route_type != route.route_type:
                continue  # trunks never mix route types
            if spec.axis != axis or not _same_line(spec.fixed, fixed, tolerance_m):
                continue
            lo = max(spec.lo, seg_lo)
            hi = min(spec.hi, seg_hi)
            if hi - lo > _SEGMENT_EPSILON:
                cuts.append((lo, hi, spec))
        edges = sorted(
            {seg_lo, seg_hi, *(bound for lo, hi, _ in cuts for bound in (lo, hi))}
        )
        portions: list[tuple[_TrunkSpec | None, Point3, Point3]] = []
        for lo, hi in zip(edges, edges[1:]):
            if hi - lo <= _SEGMENT_EPSILON:
                continue
            middle = (lo + hi) / 2.0
            spec = next(
                (
                    candidate for _, _, candidate in cuts
                    if candidate.lo <= middle <= candidate.hi
                ),
                None,
            )
            # Portions come back in the segment's own direction, so a branch
            # piece keeps the traversal its conductor had before the split.
            first = _portion_point(start, end, axis, lo)
            second = _portion_point(start, end, axis, hi)
            portions.append((spec, first, second) if forward else (spec, second, first))
        if not forward:
            portions.reverse()
        segment_portions.append(portions)
    return segment_portions


def _split_route(
    route: Route, specs: list[_TrunkSpec], tolerance_m: float,
) -> tuple[list[tuple[str, object]], list[tuple[int, object]], set[int]]:
    """Split one route into ordered pieces along its own centerline.

    Returns the piece sequence (``("trunk", spec)`` or ``("branch", piece)``),
    the piece a fitting at each interior vertex belongs to (``None`` where
    the vertex is a junction cut or trunk interior), and the set of cut
    vertex indices. Piece order follows the centerline, so a conductor's
    route reference expands into the same traversal it had before.
    """

    points = route.centerline.points
    segment_portions = _segment_portions(route, specs, tolerance_m)
    pieces: list[tuple[str, object]] = []
    portion_owners: list[list[int]] = []
    current_branch: _BranchPiece | None = None
    last_trunk_direction: int | None = None
    for portions in segment_portions:
        owners: list[int] = []
        for spec, start, end in portions:
            if spec is None:
                last_trunk_direction = None
                if current_branch is None:
                    current_branch = _BranchPiece(
                        origin_id=route.id, ordinal=len(pieces), points=[start],
                    )
                    pieces.append(("branch", current_branch))
                if _distance(current_branch.points[-1], start) > _VERTEX_EPSILON:
                    current_branch.points.append(start)
                if _distance(current_branch.points[-1], end) > _VERTEX_EPSILON:
                    current_branch.points.append(end)
            else:
                current_branch = None
                direction = 1 if _axis_coordinate(end, spec.axis) > _axis_coordinate(start, spec.axis) else -1
                if not (
                    pieces and pieces[-1][0] == "trunk"
                    and pieces[-1][1] is spec and last_trunk_direction == direction
                ):
                    pieces.append(("trunk", spec))
                last_trunk_direction = direction
            owners.append(len(pieces) - 1)
        portion_owners.append(owners)

    vertex_owners: list[tuple[int, object]] = []
    cut_vertices: set[int] = set()
    for vertex in range(1, len(points) - 1):
        left = portion_owners[vertex - 1][-1] if portion_owners[vertex - 1] else None
        right = portion_owners[vertex][0] if portion_owners[vertex] else None
        if left is not None and left == right:
            kind, piece = pieces[left]
            vertex_owners.append((vertex, piece if kind == "branch" else None))
            continue
        vertex_owners.append((vertex, None))
        cut_vertices.add(vertex)
    return pieces, vertex_owners, cut_vertices


class _JunctionPorts:
    """Reuse actual route endpoints; give new junctions fitting ownership."""

    def __init__(self, model: BuildingModel) -> None:
        self._model_id = model.model_id
        self._by_position: dict[tuple[str, tuple[float, float, float]], Port] = {}
        self._endpoint_candidates: dict[
            tuple[str, tuple[float, float, float]], list[tuple[str, Port]]
        ] = {}
        ports = {port.id: port for port in model.ports}
        for route in sorted(model.routes, key=lambda item: item.id):
            for port_id in (route.start_port_id, route.end_port_id):
                port = ports[port_id]
                key = (route.route_type, _position_key(port.pose.position))
                self._endpoint_candidates.setdefault(key, []).append((route.id, port))
        self.added: list[Port] = []
        self.owner_routes: dict[str, tuple[str, str]] = {}

    def port_for(
        self, point: Point3, route_type: str, route_id: str,
        members: tuple[str, ...] = (),
    ) -> Port:
        key = (route_type, _position_key(point))
        existing = self._by_position.get(key)
        if existing is not None:
            return existing
        member_ids = set(members)
        endpoint = next((
            port for source_id, port in self._endpoint_candidates.get(key, ())
            if source_id in member_ids
        ), None)
        if endpoint is not None:
            self._by_position[key] = endpoint
            return endpoint
        owner_id = stable_id("fitting", f"route-consolidation-junction:{route_type}:{_point_key(point)}")
        port = Port(
            id=stable_id("port", f"route-consolidation:{route_type}:{_point_key(point)}"),
            owner_id=owner_id,
            domain="raceway",
            role="junction",
            pose=Pose(position=point),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
            provenance=_consolidation_provenance(self._model_id, 1.0),
            attributes={"consolidation": {"junction": True}},
        )
        self._by_position[key] = port
        self.added.append(port)
        self.owner_routes[owner_id] = (route_type, route_id)
        return port


def consolidate_bundled_routes(
    model: BuildingModel,
    *,
    tolerance_m: float = 0.01,
) -> ConsolidationResult:
    """Merge overlapping same-type routes into shared trunk raceways.

    Collinear, overlapping route runs of one ``route_type`` (as reported by
    :func:`find_overlapping_route_runs`) become trunk ``Route`` records, and
    each member route keeps its unshared ends as branch ``Route`` records.
    Every cut point is an existing centerline coordinate, so the pieces of a
    split route sum back to its exact original length, and a conductor
    re-pointed across its route's pieces in centerline order keeps its old
    length exactly.

    Trunks are sized to the largest member diameter; fill-based trade-size
    upsizing is out of scope here, and each trunk's attributes record its
    member route ids so a later fill check can upsize it. Fittings at cut
    vertices give way to one tee per trunk endpoint where a branch or a
    continuing trunk touches it. New entities carry ``inferred`` provenance
    with method ``route-consolidation``. A model with no overlapping runs is
    returned unchanged, so nothing moves unless the caller asks for it.
    """

    if tolerance_m < 0.0:
        raise ValueError("tolerance_m must be non-negative")
    runs = find_overlapping_route_runs(model, tolerance_m=tolerance_m)
    routes_before = len(model.routes)
    fittings_before = len(model.route_fittings)
    if not runs:
        return ConsolidationResult(
            model=model,
            report=ConsolidationReport(
                routes_before=routes_before,
                routes_after=routes_before,
                trunk_routes=0,
                branch_routes=0,
                routes_left_untouched=routes_before,
                fittings_before=fittings_before,
                fittings_after=fittings_before,
                ports_added=0,
                shared_length_m=0.0,
                double_counted_length_m=0.0,
                within_route_repeated_length_m=0.0,
            ),
        )

    routes_by_id = {route.id: route for route in model.routes}
    ports_by_id = {port.id: port for port in model.ports}
    model_id = model.model_id
    specs = _split_specs_at_reversals(
        _trunk_specs(runs, routes_by_id, tolerance_m), routes_by_id, tolerance_m,
    )
    # A tolerance hit is evidence of possible sharing, not permission to move
    # a branch endpoint. Without a connector in the source geometry, snapping
    # offset centerlines would create a gap or alter conductor length.
    for route in model.routes:
        for portions in _segment_portions(route, specs, tolerance_m):
            for spec, start, _ in portions:
                if spec is None:
                    continue
                fixed = tuple(
                    (start.x, start.y, start.z)[axis]
                    for axis in range(3) if axis != spec.axis
                )
                if not _same_line(spec.fixed, fixed, 1e-6):
                    raise RoutingError(
                        "offset shared centerlines need an explicit connector "
                        "before consolidation"
                    )
    # The overlap report unions each route's own coverage. Account for a
    # member that traverses a shared physical stretch more than once.
    traversed: dict[int, float] = {id(spec): 0.0 for spec in specs}
    for route in model.routes:
        for portions in _segment_portions(route, specs, tolerance_m):
            for spec, start, end in portions:
                if spec is not None:
                    traversed[id(spec)] += _distance(start, end)
    within_route_repeated = math.fsum(
        max(0.0, traversed[id(spec)] - len(spec.members) * (spec.hi - spec.lo))
        for spec in specs
    )
    junction_ports = _JunctionPorts(model)
    port_for = junction_ports.port_for

    # --- split every participating route into ordered pieces ---------------
    piece_lists: dict[str, list[tuple[str, object]]] = {}
    vertex_maps: dict[str, list[tuple[int, object]]] = {}
    cut_maps: dict[str, set[int]] = {}
    branch_pieces: list[_BranchPiece] = []
    for route in sorted(model.routes, key=lambda item: item.id):
        pieces, vertex_owners, cut_vertices = _split_route(route, specs, tolerance_m)
        if all(kind == "branch" for kind, _ in pieces):
            continue  # touches no shared stretch: the route stays as it is
        piece_lists[route.id] = pieces
        vertex_maps[route.id] = vertex_owners
        cut_maps[route.id] = cut_vertices
        branch_pieces.extend(
            piece for kind, piece in pieces if kind == "branch"
        )

    # --- trunk identity and size -------------------------------------------
    # Junction fittings are decided once all physical pieces are known.
    trunk_route_by_spec: dict[int, Route] = {}
    trunk_records: list[tuple[_TrunkSpec, Route]] = []
    for spec in specs:
        start = _axis_point(spec.axis, spec.fixed, spec.lo)
        end = _axis_point(spec.axis, spec.fixed, spec.hi)
        members = [routes_by_id[route_id] for route_id in spec.members]
        confidence = min(member.confidence for member in members)
        diameters = [
            member.nominal_diameter_m for member in members
            if member.nominal_diameter_m is not None
        ]
        diameter = max(diameters, default=None)
        trunk_id = stable_id(
            "route",
            "route-consolidation:"
            f"{spec.route_type}|{','.join(spec.members)}|"
            f"{_point_key(start)}|{_point_key(end)}",
        )
        route = Route(
            id=trunk_id,
            route_type=spec.route_type,
            start_port_id=port_for(start, spec.route_type, trunk_id, spec.members).id,
            end_port_id=port_for(end, spec.route_type, trunk_id, spec.members).id,
            centerline=Polyline3D(points=(start, end)),
            nominal_diameter_m=diameter,
            confidence=confidence,
            provenance=_derived_provenance(model_id, confidence, *members),
            attributes={
                "consolidation": {
                    "member_route_ids": list(spec.members),
                    "member_count": len(spec.members),
                },
            },
        )
        trunk_route_by_spec[id(spec)] = route
        trunk_records.append((spec, route))

    def origin_port_id(origin: Route, point: Point3, branch_id: str) -> str:
        """The origin's own port when the piece end sits on one, else a junction port."""

        for port_id, endpoint in (
            (origin.start_port_id, origin.centerline.points[0]),
            (origin.end_port_id, origin.centerline.points[-1]),
        ):
            if _distance(ports_by_id[port_id].pose.position, point) <= _VERTEX_EPSILON:
                return port_id
        matching = next((
            record for record in trunk_records
            if record[0].route_type == origin.route_type
            and (_distance(record[1].centerline.points[0], point) <= _VERTEX_EPSILON
                 or _distance(record[1].centerline.points[-1], point) <= _VERTEX_EPSILON)
        ), None)
        owner_route_id = matching[1].id if matching is not None else branch_id
        return port_for(point, origin.route_type, owner_route_id).id

    branch_routes: dict[int, Route] = {}
    for piece in branch_pieces:
        origin = routes_by_id[piece.origin_id]
        start, end = piece.points[0], piece.points[-1]
        branch_id = stable_id(
            "route",
            "route-consolidation-branch:"
            f"{piece.origin_id}|{piece.ordinal}|{_point_key(start)}|{_point_key(end)}",
        )
        branch_routes[id(piece)] = Route(
            id=branch_id,
            route_type=origin.route_type,
            start_port_id=origin_port_id(origin, start, branch_id),
            end_port_id=origin_port_id(origin, end, branch_id),
            centerline=Polyline3D(points=tuple(piece.points)),
            nominal_diameter_m=origin.nominal_diameter_m,
            confidence=origin.confidence,
            provenance=_derived_provenance(model_id, origin.confidence, origin),
            attributes={
                "consolidation": {
                    "source_route_id": piece.origin_id,
                    "piece_ordinal": piece.ordinal,
                },
            },
        )

    # --- conductors and circuits re-pointed in traversal order -------------
    def expanded(route_ids: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for route_id in route_ids:
            pieces = piece_lists.get(route_id)
            if pieces is None:
                out.append(route_id)
                continue
            for kind, piece in pieces:
                if kind == "trunk":
                    out.append(trunk_route_by_spec[id(piece)].id)
                else:
                    out.append(branch_routes[id(piece)].id)
        return tuple(out)

    circuits = tuple(
        circuit if not circuit.route_ids else replace(
            circuit, route_ids=expanded(circuit.route_ids),
        )
        for circuit in sorted(model.circuits, key=lambda item: item.id)
    )
    conductors = tuple(
        conductor if not conductor.route_ids else replace(
            conductor, route_ids=expanded(conductor.route_ids),
        )
        for conductor in sorted(model.conductors, key=lambda item: item.id)
    )

    # --- fittings: preserve branch fittings, deduplicate shared fittings ---
    kept_fittings: list[RouteFitting] = []
    moved_fittings: list[RouteFitting] = []
    shared_fittings: dict[tuple[str, str, tuple[float, float, float]], list[RouteFitting]] = {}
    original_at_junction: dict[tuple[str, tuple[float, float, float]], list[RouteFitting]] = {}
    fitting_by_id = {fitting.id: fitting for fitting in model.route_fittings}
    for route in sorted(model.routes, key=lambda item: item.id):
        if route.id not in piece_lists:
            kept_fittings.extend(fitting_by_id[fitting_id] for fitting_id in route.fitting_ids)
            continue
        points = route.centerline.points
        branch_by_vertex = {
            vertex: piece
            for vertex, piece in vertex_maps[route.id]
            if isinstance(piece, _BranchPiece)
        }
        cursor = 1
        for fitting_id in route.fitting_ids:
            fitting = fitting_by_id[fitting_id]
            vertex = next(
                (
                    index for index in range(cursor, len(points) - 1)
                    if _distance(points[index], fitting.pose.position) <= _VERTEX_EPSILON
                ),
                None,
            )
            if vertex is None:
                raise RoutingError(
                    f"{fitting.id} does not sit on a vertex of {route.id}; "
                    "consolidation can only re-parent vertex fittings"
                )
            cursor = vertex + 1
            pos_key = _position_key(fitting.pose.position)
            original_at_junction.setdefault((route.route_type, pos_key), []).append(fitting)
            if vertex in cut_maps[route.id]:
                if not _is_turn_fitting(fitting.fitting_type):
                    shared_fittings.setdefault((route.route_type, fitting.fitting_type, pos_key), []).append(fitting)
                continue
            piece = branch_by_vertex.get(vertex)
            if piece is None:
                if not _is_turn_fitting(fitting.fitting_type):
                    shared_fittings.setdefault((route.route_type, fitting.fitting_type, pos_key), []).append(fitting)
                continue
            moved_fittings.append(replace(
                fitting, route_id=branch_routes[id(piece)].id,
            ))

    # At a junction the physical outgoing directions decide the fitting.
    # Exactly one tee or elbow is placed at a position for a route type; a
    # collinear continuation needs no fitting even when member sets change.
    physical_routes = [route for _, route in trunk_records] + list(branch_routes.values())
    trunk_ids = {route.id for _, route in trunk_records}
    incident: dict[tuple[str, tuple[float, float, float]], list[tuple[Route, Vector3]]] = {}
    for route in physical_routes:
        points = route.centerline.points
        for point, adjacent in ((points[0], points[1]), (points[-1], points[-2])):
            length = _distance(point, adjacent)
            direction = Vector3(
                x=(adjacent.x - point.x) / length,
                y=(adjacent.y - point.y) / length,
                z=(adjacent.z - point.z) / length,
            )
            incident.setdefault((route.route_type, _position_key(point)), []).append((route, direction))

    junction_fittings: list[RouteFitting] = []
    for (route_type, pos_key), entries in sorted(incident.items()):
        trunks_here = sorted((route for route, _ in entries if route.id in trunk_ids),
                             key=lambda route: route.id)
        if not trunks_here:
            continue
        directions = {
            (round(direction.x, 6), round(direction.y, 6), round(direction.z, 6))
            for _, direction in entries
        }
        if len(directions) >= 3:
            kind = "tee"
            angle = None
        elif len(directions) == 2:
            first, second = tuple(sorted(directions))
            if all(abs(a + b) <= 1e-6 for a, b in zip(first, second)):
                continue  # straight-through, no physical fitting
            angle = math.acos(max(-1.0, min(1.0, -sum(a * b for a, b in zip(first, second)))))
            kind = "elbow-90" if abs(angle - math.pi / 2) <= 1e-6 else "elbow"
        else:
            continue
        owner = trunks_here[0]
        sources = original_at_junction.get((route_type, pos_key), ())
        point = Point3(x=pos_key[0], y=pos_key[1], z=pos_key[2])
        junction_fittings.append(RouteFitting(
            id=stable_id("fitting", f"route-consolidation:{route_type}:{kind}:{_point_key(point)}"),
            route_id=owner.id, fitting_type=kind, pose=Pose(position=point),
            nominal_diameter_m=max(
                (route.nominal_diameter_m or 0 for route, _ in entries), default=0
            ) or None,
            angle_radians=angle,
            confidence=min(route.confidence for route, _ in entries),
            provenance=_derived_provenance(model_id, min(route.confidence for route, _ in entries), *sources),
            attributes={"consolidation": {"source_fitting_ids": sorted(source.id for source in sources)}},
        ))

    # Same-type fittings on a shared span are one physical part. Preserve all
    # source provenance records, attach them to the shared trunk, and insert
    # their position as a centerline vertex for IFC ordering.
    shared_records: list[RouteFitting] = []
    for (route_type, kind, pos_key), sources in sorted(shared_fittings.items()):
        point = Point3(x=pos_key[0], y=pos_key[1], z=pos_key[2])
        owners = sorted((
            route for _, route in trunk_records
            if route.route_type == route_type and _point_on_centerline(point, route)
        ), key=lambda route: route.id)
        if not owners:
            raise RoutingError(f"shared fitting at {_point_key(point)} has no trunk")
        owner = owners[0]
        shared_records.append(RouteFitting(
            id=stable_id("fitting", f"route-consolidation:{route_type}:{kind}:{_point_key(point)}"),
            route_id=owner.id, fitting_type=kind, pose=Pose(position=point),
            nominal_diameter_m=max((source.nominal_diameter_m or 0 for source in sources), default=0) or None,
            confidence=min(source.confidence for source in sources),
            provenance=_derived_provenance(model_id, min(source.confidence for source in sources), *sources),
            attributes={"consolidation": {"source_fitting_ids": sorted(source.id for source in sources)}},
        ))

    # A canonical junction port must belong to the fitting at that position.
    # Straight bookkeeping cuts need a logical junction occurrence, explicitly
    # marked nonmaterial so a derived takeoff never calls it a physical part.
    trunk_types = {route.id: spec.route_type for spec, route in trunk_records}
    physical_by_position: dict[tuple[str, tuple[float, float, float]], RouteFitting] = {}
    for fitting in (*junction_fittings, *shared_records):
        key = (trunk_types[fitting.route_id], _position_key(fitting.pose.position))
        physical_by_position.setdefault(key, fitting)  # the junction fitting owns a junction
    logical_fittings: list[RouteFitting] = []
    resolved_ports: list[Port] = []
    derived_routes = {route.id: route for _, route in trunk_records}
    derived_routes.update({route.id: route for route in branch_routes.values()})
    for port in junction_ports.added:
        route_type, route_id = junction_ports.owner_routes[port.owner_id]
        key = (route_type, _position_key(port.pose.position))
        physical = physical_by_position.get(key)
        if physical is not None:
            resolved_ports.append(replace(
                port, owner_id=physical.id, confidence=physical.confidence,
                provenance=_derived_provenance(model_id, physical.confidence, physical),
            ))
            continue
        owner = derived_routes[route_id]
        logical = RouteFitting(
            id=port.owner_id, route_id=route_id, fitting_type="logical-junction",
            pose=port.pose, nominal_diameter_m=owner.nominal_diameter_m,
            confidence=owner.confidence,
            provenance=_derived_provenance(model_id, owner.confidence, owner),
            attributes={"consolidation": {"logical": True, "nonmaterial": True}},
        )
        logical_fittings.append(logical)
        resolved_ports.append(replace(
            port, confidence=owner.confidence,
            provenance=_derived_provenance(model_id, owner.confidence, owner),
        ))

    trunk_fittings: dict[str, list[RouteFitting]] = {}
    for fitting in (*junction_fittings, *shared_records, *logical_fittings):
        trunk_fittings.setdefault(fitting.route_id, []).append(fitting)
    updated_trunks: list[tuple[_TrunkSpec, Route]] = []
    for spec, route in trunk_records:
        fittings = sorted(trunk_fittings.get(route.id, ()), key=lambda fitting: (
            _axis_coordinate(fitting.pose.position, spec.axis), fitting.id,
        ))
        interior = sorted({
            _axis_coordinate(fitting.pose.position, spec.axis)
            for fitting in fittings
            if spec.lo + _VERTEX_EPSILON < _axis_coordinate(fitting.pose.position, spec.axis) < spec.hi - _VERTEX_EPSILON
        })
        points = (route.centerline.points[0], *(
            _axis_point(spec.axis, spec.fixed, at) for at in interior
        ), route.centerline.points[-1])
        updated = replace(route, centerline=Polyline3D(points=points),
                          fitting_ids=tuple(fitting.id for fitting in fittings))
        updated_trunks.append((spec, updated))
        trunk_route_by_spec[id(spec)] = updated
    trunk_records = updated_trunks

    # A Route owns the ordered fitting_ids of every fitting re-parented to it.
    # Updating RouteFitting.route_id alone leaves the canonical model invalid.
    moved_by_route: dict[str, list[str]] = {}
    for fitting in moved_fittings:
        moved_by_route.setdefault(fitting.route_id, []).append(fitting.id)
    for fitting in logical_fittings:
        if fitting.route_id in {route.id for route in branch_routes.values()}:
            moved_by_route.setdefault(fitting.route_id, []).append(fitting.id)
    for key, route in list(branch_routes.items()):
        branch_routes[key] = replace(
            route, fitting_ids=tuple(moved_by_route.get(route.id, ())),
        )

    consolidated = replace(
        model,
        ports=tuple(sorted(
            (*model.ports, *resolved_ports), key=lambda item: item.id,
        )),
        routes=tuple(sorted(
            (
                *(route for route in model.routes if route.id not in piece_lists),
                *(route for _, route in trunk_records),
                *branch_routes.values(),
            ),
            key=lambda item: item.id,
        )),
        route_fittings=tuple(sorted(
            (*kept_fittings, *moved_fittings, *junction_fittings, *shared_records, *logical_fittings), key=lambda item: item.id,
        )),
        circuits=circuits,
        conductors=conductors,
    )
    report = ConsolidationReport(
        routes_before=routes_before,
        routes_after=len(consolidated.routes),
        trunk_routes=len(trunk_records),
        branch_routes=len(branch_routes),
        routes_left_untouched=sum(
            1 for route in model.routes if route.id not in piece_lists
        ),
        fittings_before=fittings_before,
        fittings_after=len(consolidated.route_fittings),
        ports_added=len(junction_ports.added),
        shared_length_m=math.fsum(spec.hi - spec.lo for spec, _ in trunk_records),
        double_counted_length_m=math.fsum(run.double_counted_length_m for run in runs) + within_route_repeated,
        within_route_repeated_length_m=within_route_repeated,
    )
    # A valid canonical model can still carry a mistaken electrical traversal.
    # Refuse the result if any conductor length or the takeoff identity drifts.
    new_routes = {route.id: route for route in consolidated.routes}
    old_conductors = {conductor.id: conductor for conductor in model.conductors}
    for conductor in consolidated.conductors:
        old_length = math.fsum(_route_length(routes_by_id[route_id]) for route_id in old_conductors[conductor.id].route_ids)
        new_length = math.fsum(_route_length(new_routes[route_id]) for route_id in conductor.route_ids)
        if abs(old_length - new_length) > 1e-6 * max(1, len(old_conductors[conductor.id].route_ids)):
            raise RoutingError(f"consolidation changed conductor length on {conductor.id}")
    old_total = math.fsum(_route_length(route) for route in model.routes)
    new_total = math.fsum(_route_length(route) for route in consolidated.routes)
    if abs(new_total - (old_total - report.double_counted_length_m)) > 1e-6 * max(1, len(model.routes)):
        raise RoutingError(
            "consolidation route totals do not match overlap coverage: "
            f"before={old_total:.6f} after={new_total:.6f} "
            f"double_counted={report.double_counted_length_m:.6f}"
        )
    return ConsolidationResult(model=consolidated, report=report)
