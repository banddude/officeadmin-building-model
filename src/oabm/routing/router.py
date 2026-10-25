from __future__ import annotations

import dataclasses
import hashlib
import heapq
import json
import math
from dataclasses import dataclass
from typing import Iterable

from oabm.model import (
    DERIVATION_INFERRED,
    Box3D,
    BuildingModel,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    RouteConstraint,
    RouteFitting,
    Vector3,
    stable_id,
)
from oabm.model.entities import WALL_CONSTRUCTION_GLAZED

_EPS = 1e-9
_ALGORITHM = "deterministic-rectilinear-v1"
_KEEP_OUT_TYPES = {"keep-out", "keepout", "no-go", "nogo", "forbidden"}
_REQUIRED_TYPES = {"required-corridor", "required", "must-pass", "must-use"}
_PREFERRED_TYPES = {"preferred-corridor", "preferred", "corridor"}
_AVOID_TYPES = {"avoid", "soft-avoid", "discouraged"}
_MAX_ALLOWED_DOMAIN_VOLUMES = 128
_MAX_ALLOWED_DOMAIN_REMAINDERS = 4096
_MAX_ALLOWED_DOMAIN_GRID_NODES = 200_000
_MAX_ALLOWED_DOMAIN_SEARCH_STATES = 500_000
_MAX_RECORDED_BLOCKER_IDS = 256
_ALLOWED_DOMAIN_ALIGNMENT_EPSILON_M = 1e-9


class RoutingError(ValueError):
    """Base error for deterministic routing failures."""


class NoRouteError(RoutingError):
    """Raised when the canonical geometry admits no route under the limits."""

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        blocker_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        # In a bounded-domain search, these are hard blockers encountered on
        # rejected candidate edges, not a minimal cut set or an engineering
        # diagnosis.
        self.blocker_ids = tuple(sorted(set(blocker_ids)))


@dataclass(frozen=True, slots=True)
class RoutingOptions:
    """Algorithm controls that do not alter the canonical model contract."""

    bend_penalty_m: float = 0.30
    max_bends: int | None = 12
    port_stub_m: float = 0.15
    clearance_m: float = 0.0
    search_margin_m: float = 0.25
    corridor_tolerance_m: float = 0.05
    bundle_alignment_tolerance_m: float = 0.01
    preferred_corridor_discount: float = 0.20
    surface_path_discount: float = 0.05
    # Cost multiplier for a segment whose midpoint lies inside a glazed wall's
    # padded bounds. Conduit cannot be concealed in glazing, so glazing is a
    # soft penalty, never a hard block; use Obstacle for true no-go regions.
    glazed_wall_penalty: float = 4.0
    soft_obstacle_penalty_factor: float = 3.0
    vertical_cost_factor: float = 1.0
    coordinate_precision: int = 9

    def __post_init__(self) -> None:
        if isinstance(self.bundle_alignment_tolerance_m, bool) or not isinstance(
            self.bundle_alignment_tolerance_m, (int, float)
        ):
            raise RoutingError("bundle_alignment_tolerance_m must be a number")
        for name in (
            "bend_penalty_m",
            "port_stub_m",
            "clearance_m",
            "search_margin_m",
            "corridor_tolerance_m",
            "bundle_alignment_tolerance_m",
            "soft_obstacle_penalty_factor",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise RoutingError(f"{name} must be finite and >= 0")
        if self.max_bends is not None:
            if isinstance(self.max_bends, bool) or not isinstance(self.max_bends, int) or self.max_bends < 0:
                raise RoutingError("max_bends must be an integer >= 0 or None")
        for name in ("preferred_corridor_discount", "surface_path_discount"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value < 1:
                raise RoutingError(f"{name} must be in [0, 1)")
        if not math.isfinite(self.glazed_wall_penalty) or self.glazed_wall_penalty <= 0:
            raise RoutingError("glazed_wall_penalty must be finite and > 0")
        if not math.isfinite(self.vertical_cost_factor) or self.vertical_cost_factor <= 0:
            raise RoutingError("vertical_cost_factor must be finite and > 0")
        if not 3 <= self.coordinate_precision <= 12:
            raise RoutingError("coordinate_precision must be between 3 and 12")


@dataclass(frozen=True, slots=True)
class BundleHints:
    """Earlier runs a new route may follow at reduced cost. Caller input only; never persisted."""

    paths: tuple[Polyline3D, ...] = ()
    discount: float = 0.25
    route_type: str | None = None

    def __post_init__(self) -> None:
        if self.route_type is not None and (not isinstance(self.route_type, str) or not self.route_type.strip()):
            raise RoutingError("route_type must be a nonempty string when supplied")
        if not isinstance(self.paths, tuple) or not all(
            isinstance(item, Polyline3D) for item in self.paths
        ):
            raise RoutingError("paths must be a tuple of Polyline3D centerlines")
        if isinstance(self.discount, bool) or not isinstance(self.discount, (int, float)):
            raise RoutingError("discount must be an int or float")
        if not math.isfinite(self.discount) or not 0 <= self.discount < 1:
            raise RoutingError("discount must be finite and in [0, 1)")


@dataclass(frozen=True, slots=True)
class AllowedRouteDomain:
    """Caller-supplied union of finite volumes that a route must stay inside.

    This is a route-request constraint, not a physical building entity. A
    future canonical assembly can adapt its explicitly generated free-space
    volumes to this input. The router accepts only axis-aligned ``Box3D``
    volumes in the first bounded implementation and records the exact request
    in route provenance.
    """

    id: str
    kind: str
    volumes: tuple[Box3D, ...]
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise RoutingError("allowed route domain id must be a nonempty string")
        if len(self.id) > 128:
            raise RoutingError("allowed route domain id exceeds 128 characters")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise RoutingError("allowed route domain kind must be a nonempty string")
        if not isinstance(self.volumes, tuple) or not self.volumes:
            raise RoutingError("allowed route domain volumes must be a nonempty tuple")
        if len(self.volumes) > _MAX_ALLOWED_DOMAIN_VOLUMES:
            raise RoutingError(
                "allowed route domain exceeds the limit of "
                f"{_MAX_ALLOWED_DOMAIN_VOLUMES} boxes"
            )
        if any(not isinstance(volume, Box3D) for volume in self.volumes):
            raise RoutingError("allowed route domain volumes must be Box3D values")
        if not isinstance(self.source_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.source_ids
        ):
            raise RoutingError("allowed route domain source_ids must be nonempty strings")
        if len(self.source_ids) > _MAX_ALLOWED_DOMAIN_VOLUMES:
            raise RoutingError(
                "allowed route domain exceeds the limit of "
                f"{_MAX_ALLOWED_DOMAIN_VOLUMES} source ids"
            )
        # A canonical ordering makes the provenance receipt independent of the
        # caller's tuple ordering. No boxes are merged or otherwise rewritten.
        ordered_volumes = tuple(sorted(self.volumes, key=_allowed_box_sort_key))
        ordered_sources = tuple(sorted(set(self.source_ids)))
        object.__setattr__(self, "volumes", ordered_volumes)
        object.__setattr__(self, "source_ids", ordered_sources)


@dataclass(frozen=True, slots=True)
class _Bounds:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def expanded(self, value: float) -> "_Bounds":
        return _Bounds(
            self.min_x - value,
            self.min_y - value,
            self.min_z - value,
            self.max_x + value,
            self.max_y + value,
            self.max_z + value,
        )

    @property
    def center(self) -> Point3:
        return Point3(
            x=(self.min_x + self.max_x) / 2,
            y=(self.min_y + self.max_y) / 2,
            z=(self.min_z + self.max_z) / 2,
        )


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    bounds: _Bounds
    geometry: Box3D | Polyline3D | Polygon3D | None = None
    tolerance_m: float = 0.0
    source_type: str | None = None


@dataclass(frozen=True, slots=True)
class _RoutingGeometry:
    hard_blockers: tuple[_Rule, ...]
    soft_blockers: tuple[_Rule, ...]
    required: tuple[_Rule, ...]
    preferred: tuple[_Rule, ...]
    surfaces: tuple[_Rule, ...]
    glazed: tuple[_Rule, ...] = ()
    bundle: _BundleIndex | None = None
    allowed_domain: _AllowedDomainIndex | None = None


@dataclass(frozen=True, slots=True)
class _AllowedDomainIndex:
    request: AllowedRouteDomain
    volumes: tuple[_Bounds, ...]
    route_radius_m: float
    clearance_m: float
    sweep_padding_m: float
    containment_epsilon_m: float
    axis_alignment_epsilon_m: float


# For an axis key, the two fixed coordinates in x, y, z order.
_BUNDLE_FIXED_AXES = {0: (1, 2), 1: (0, 2), 2: (0, 1)}


@dataclass(frozen=True, slots=True)
class _BundleIndex:
    lookup: dict
    vertices: tuple
    factor: float


def _axis_key(a: Point3, b: Point3, precision: int) -> tuple | None:
    """Return ``(key, lo, hi)`` when the segment is axis aligned, else ``None``.

    ``key`` is ``(axis, c1, c2)``: the moving axis plus the other two
    canonical coordinates in x, y, z order. ``lo < hi`` are the moving
    coordinate's two canonical values.
    """

    first = (_canon(a.x, precision), _canon(a.y, precision), _canon(a.z, precision))
    second = (_canon(b.x, precision), _canon(b.y, precision), _canon(b.z, precision))
    differing = [axis for axis in range(3) if first[axis] != second[axis]]
    if len(differing) != 1:
        return None
    axis = differing[0]
    fixed_first, fixed_second = _BUNDLE_FIXED_AXES[axis]
    key = (axis, first[fixed_first], first[fixed_second])
    lo, hi = sorted((first[axis], second[axis]))
    return (key, lo, hi)


def _bundle_interval_point(key: tuple, moving: float) -> tuple[float, float, float]:
    axis, c1, c2 = key
    fixed_first, fixed_second = _BUNDLE_FIXED_AXES[axis]
    coordinates = [0.0, 0.0, 0.0]
    coordinates[axis] = moving
    coordinates[fixed_first] = c1
    coordinates[fixed_second] = c2
    return (coordinates[0], coordinates[1], coordinates[2])


def _bundle_index(hints: BundleHints | None, precision: int) -> _BundleIndex | None:
    if hints is None or hints.discount == 0:
        return None
    intervals: dict[tuple, list[tuple[float, float]]] = {}
    for path in hints.paths:
        for a, b in zip(path.points, path.points[1:]):
            axis_key = _axis_key(a, b, precision)
            if axis_key is None:
                continue
            key, lo, hi = axis_key
            intervals.setdefault(key, []).append((lo, hi))
    if not intervals:
        return None
    lookup: dict[tuple, tuple[tuple[float, float], ...]] = {}
    vertices: set[tuple[float, float, float]] = set()
    for key in sorted(intervals):
        merged: list[tuple[float, float]] = []
        for lo, hi in sorted(intervals[key]):
            if merged and lo <= merged[-1][1] + _EPS:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        lookup[key] = tuple(merged)
        for lo, hi in merged:
            vertices.add(_bundle_interval_point(key, lo))
            vertices.add(_bundle_interval_point(key, hi))
    return _BundleIndex(
        lookup=lookup,
        vertices=tuple(sorted(vertices)),
        factor=1.0 - hints.discount,
    )


def _along_bundle(a: Point3, b: Point3, index: _BundleIndex, precision: int) -> bool:
    axis_key = _axis_key(a, b, precision)
    if axis_key is None:
        return False
    key, s, e = axis_key
    return any(lo - _EPS <= s and e <= hi + _EPS for lo, hi in index.lookup.get(key, ()))


def _bundle_shared_m(points, index: _BundleIndex, precision: int) -> float:
    total = 0.0
    for a, b in zip(points, points[1:]):
        axis_key = _axis_key(a, b, precision)
        if axis_key is None:
            continue
        key, s, e = axis_key
        for lo, hi in index.lookup.get(key, ()):
            total += max(0.0, min(e, hi) - max(s, lo))
    return total


def route_between_ports(
    model: BuildingModel,
    start_port_id: str,
    end_port_id: str,
    route_type: str,
    *,
    nominal_diameter_m: float | None = None,
    options: RoutingOptions | None = None,
    bundle_hints: BundleHints | None = None,
    allowed_domain: AllowedRouteDomain | None = None,
) -> tuple[Route, tuple[RouteFitting, ...]]:
    """Route one canonical port-to-port connection deterministically.

    The input is the canonical ``BuildingModel``. The outputs are canonical
    ``Route`` and ``RouteFitting`` objects only; callers decide whether and how
    to attach them to a model document.

    ``bundle_hints`` is optional caller input: earlier runs a new route may
    follow at reduced cost. It never changes route identity or provenance and
    is not persisted.

    ``allowed_domain`` is an optional caller-supplied union of finite routing
    volumes. When supplied, the whole swept route (including both port stubs)
    must fit inside that union as well as avoid modeled hard geometry. It is a
    route constraint, not a physical object, and is preserved in provenance.
    """

    if not route_type:
        raise RoutingError("route_type is required")
    options = options or RoutingOptions()
    if allowed_domain is not None and not isinstance(allowed_domain, AllowedRouteDomain):
        raise RoutingError("allowed_domain must be an AllowedRouteDomain value")
    ports = {port.id: port for port in model.ports}
    try:
        start_port = ports[start_port_id]
        end_port = ports[end_port_id]
    except KeyError as exc:
        raise RoutingError(f"unknown port id {exc.args[0]!r}") from exc
    if start_port_id == end_port_id:
        raise RoutingError("start and end ports must be different")
    if start_port.domain != end_port.domain:
        raise RoutingError(
            f"port domains differ: {start_port.domain!r} != {end_port.domain!r}"
        )

    diameter = _resolve_diameter(start_port, end_port, nominal_diameter_m)
    if allowed_domain is not None and diameter is None:
        raise RoutingError(
            "allowed route domain requires a known positive nominal diameter "
            "from nominal_diameter_m or port data"
        )
    route_radius = (diameter or 0.0) / 2.0
    geometry = _collect_routing_geometry(
        model, route_type, route_radius, options, allowed_domain=allowed_domain,
    )
    type_mismatch = bundle_hints is not None and bundle_hints.route_type is not None and bundle_hints.route_type != route_type
    bundle = _bundle_index(None if type_mismatch else bundle_hints, options.coordinate_precision)
    if bundle is not None:
        geometry = dataclasses.replace(geometry, bundle=bundle)

    start = start_port.pose.position
    end = end_port.pose.position
    start_dir = _unit(start_port.direction)
    end_dir = _unit(end_port.direction)
    start_anchor = _offset(start, start_dir, options.port_stub_m)
    end_anchor = _offset(end, end_dir, options.port_stub_m)

    if geometry.allowed_domain is not None:
        domain_id = geometry.allowed_domain.request.id
        if not _point_within_allowed_domain(start, geometry.allowed_domain):
            raise NoRouteError(
                f"start port lies outside allowed route domain {domain_id!r}",
                reason="start_outside_allowed_domain",
            )
        if not _point_within_allowed_domain(end, geometry.allowed_domain):
            raise NoRouteError(
                f"end port lies outside allowed route domain {domain_id!r}",
                reason="end_outside_allowed_domain",
            )
        if not _segment_within_allowed_domain(start, start_anchor, geometry.allowed_domain):
            raise NoRouteError(
                f"start port stub leaves allowed route domain {domain_id!r}",
                reason="start_stub_outside_allowed_domain",
            )
        if not _segment_within_allowed_domain(end_anchor, end, geometry.allowed_domain):
            raise NoRouteError(
                f"end port stub leaves allowed route domain {domain_id!r}",
                reason="end_stub_outside_allowed_domain",
            )

    start_endpoint_blockers = tuple(
        rule.id for rule in geometry.hard_blockers if _rule_contains_point(start, rule)
    )
    end_endpoint_blockers = tuple(
        rule.id for rule in geometry.hard_blockers if _rule_contains_point(end, rule)
    )
    if start_endpoint_blockers or end_endpoint_blockers:
        if geometry.allowed_domain is not None:
            if start_endpoint_blockers:
                raise NoRouteError(
                    "a route endpoint lies inside hard obstacle/no-go geometry",
                    reason="start_endpoint_inside_hard_blocker",
                    blocker_ids=start_endpoint_blockers,
                )
            raise NoRouteError(
                "a route endpoint lies inside hard obstacle/no-go geometry",
                reason="end_endpoint_inside_hard_blocker",
                blocker_ids=end_endpoint_blockers,
            )
        raise NoRouteError("a route endpoint lies inside hard obstacle/no-go geometry")
    if not _segment_clear(start, start_anchor, geometry.hard_blockers):
        if geometry.allowed_domain is not None:
            raise NoRouteError(
                "start port direction stub intersects hard obstacle/no-go geometry",
                reason="start_stub_intersects_hard_blocker",
                blocker_ids=_segment_blocker_ids(start, start_anchor, geometry.hard_blockers),
            )
        raise NoRouteError("start port direction stub intersects hard obstacle/no-go geometry")
    if not _segment_clear(end_anchor, end, geometry.hard_blockers):
        if geometry.allowed_domain is not None:
            raise NoRouteError(
                "end port direction stub intersects hard obstacle/no-go geometry",
                reason="end_stub_intersects_hard_blocker",
                blocker_ids=_segment_blocker_ids(end_anchor, end, geometry.hard_blockers),
            )
        raise NoRouteError("end port direction stub intersects hard obstacle/no-go geometry")

    xs, ys, zs = _candidate_coordinates(
        model,
        start_anchor,
        end_anchor,
        geometry,
        options,
    )
    start_node = _node_for_point(start_anchor, xs, ys, zs, options.coordinate_precision)
    end_node = _node_for_point(end_anchor, xs, ys, zs, options.coordinate_precision)

    points = _search(
        start,
        start_anchor,
        start_dir,
        end,
        end_anchor,
        end_dir,
        start_node,
        end_node,
        xs,
        ys,
        zs,
        geometry,
        options,
    )
    points = _simplify(points)
    alignment = None
    if type_mismatch:
        alignment = {"status": "unchanged", "reason": "route_type_mismatch"}
    elif bundle is not None and bundle_hints.route_type == route_type:
        points, alignment = _align_bundle_source(points,bundle,geometry,options,start_anchor,end_anchor)
    if geometry.allowed_domain is not None and not _polyline_within_allowed_domain(
        points, geometry.allowed_domain,
    ):
        # Search edges and any alignment proposal are checked. Retain this
        # final guard so future path transforms cannot silently escape.
        raise NoRouteError(
            f"final route leaves allowed route domain {geometry.allowed_domain.request.id!r}",
            reason="final_route_outside_allowed_domain",
        )
    bend_count = _count_bends(points)
    if options.max_bends is not None and bend_count > options.max_bends:
        raise NoRouteError(
            f"best route requires {bend_count} bends, exceeding max_bends={options.max_bends}"
        )

    route_id = stable_id(
        "route",
        f"{model.model_id}:{route_type}:{start_port_id}:{end_port_id}",
    )
    router_attributes: dict[str, object] = {"route_type": route_type}
    if geometry.allowed_domain is not None:
        router_attributes["allowed_route_domain"] = _allowed_domain_receipt(
            geometry.allowed_domain,
        )
        router_attributes["bounded_routing_context"] = _bounded_routing_context_receipt(
            model,
            start_port,
            end_port,
            route_type,
            diameter,
            options,
            bundle_hints,
            geometry.allowed_domain.request,
        )
    provenance = (
        Provenance(
            source_kind="router",
            source_id=model.model_id,
            source_element_id=f"{start_port_id}->{end_port_id}",
            method=_ALGORITHM,
            confidence=1.0,
            # A routed path is a proposal, never an observation. No source
            # shows this centerline; we computed it. It stays inferred until
            # something in the source actually shows the run.
            derivation=DERIVATION_INFERRED,
            attributes=router_attributes,
        ),
    )
    if alignment is not None and alignment["status"] == "aligned":
        provenance += (Provenance(source_kind="router",source_id=model.model_id,
            source_element_id=f"{start_port_id}->{end_port_id}",method="bundle-source-alignment",
            confidence=1.0,derivation=DERIVATION_INFERRED,attributes=alignment),)
    fittings = _build_fittings(route_id, points, diameter, provenance)
    length_m = sum(_distance(a, b) for a, b in zip(points, points[1:]))
    attributes = {
        "routing_engine": _ALGORITHM,
        "bend_count": bend_count,
        "length_m": round(length_m, options.coordinate_precision),
        "required_constraint_ids": [item.id for item in geometry.required],
    }
    if alignment is not None:
        attributes["bundle_alignment"] = alignment
    if bundle is not None:
        p = options.coordinate_precision
        attributes["bundle_hint_discount"] = float(bundle_hints.discount)
        attributes["bundle_hint_shared_m"] = round(_bundle_shared_m(points, bundle, p), p)
    if geometry.allowed_domain is not None:
        request = geometry.allowed_domain.request
        attributes["allowed_route_domain"] = {
            "id": request.id,
            "kind": request.kind,
            "containment": "swept-aabb-union-v1",
            "volume_count": len(request.volumes),
        }
        attributes["geometry_check"] = {
            **_checked_blocker_receipt(geometry),
            "status": "contained_and_clear_of_checked_blockers",
            "scope": "caller_allowed_volume_and_supported_hard_blocker_classes_only",
            "envelope_scope": "nominal_radius_only; actual_outer_diameter_not_verified",
            "revalidation": "generation_time_only; not_live_after_model_changes",
            "code_review": "not_evaluated",
        }
    if geometry.glazed:
        # Diagnostic, not an error: the route is valid, but each segment whose
        # midpoint remains inside a glazed wall's padded bounds is reported so
        # a caller can review concealment in glazing.
        glazed_hits = [
            {"wall_id": rule.id, "segment_index": index}
            for index, (a, b) in enumerate(zip(points, points[1:]))
            for rule in geometry.glazed
            if _segment_midpoint_in_bounds(a, b, rule.bounds)
        ]
        if glazed_hits:
            attributes["route_in_glazed_wall"] = glazed_hits
    route = Route(
        id=route_id,
        route_type=route_type,
        start_port_id=start_port_id,
        end_port_id=end_port_id,
        centerline=Polyline3D(points=tuple(points)),
        nominal_diameter_m=diameter,
        fitting_ids=tuple(fitting.id for fitting in fittings),
        provenance=provenance,
        attributes=attributes,
    )
    return route, fittings


def _align_bundle_source(points, index, geometry, options, start_anchor, end_anchor):
    """Align typed, near-coincident runs before canonical Route construction.

    Actual port stubs, hard clearances, required corridors and bend limits remain
    authoritative. Short connectors belong to the resulting measured path.
    """
    tolerance = options.bundle_alignment_tolerance_m
    if tolerance == 0:
        return points, {"status": "unchanged", "reason": "disabled"}
    expanded = [points[0]]
    for a, b in zip(points, points[1:]):
        middle = [
            p
            for p in (start_anchor, end_anchor)
            if _distance(a, p) > _EPS
            and _distance(p, b) > _EPS
            and abs(_distance(a, p) + _distance(p, b) - _distance(a, b)) <= _EPS
        ]
        expanded.extend(sorted(set(middle), key=lambda p: _distance(a, p)))
        expanded.append(b)
    result = list(expanded)
    changes = []
    refusals = set()
    # Work backwards so replacing an interior segment keeps earlier indexes.
    for position in range(len(expanded) - 3, 0, -1):
        a, b = expanded[position], expanded[position + 1]
        axis = _axis_key(a, b, options.coordinate_precision)
        if axis is None:
            continue
        source_key, lo, hi = axis
        # Never leave an already shared corridor for a nearby alternative.
        if _bundle_shared_m((a, b), index, options.coordinate_precision) > _EPS:
            continue
        candidates = []
        for key, intervals in sorted(index.lookup.items()):
            if key[0] != source_key[0]:
                continue
            offset = math.hypot(key[1] - source_key[1], key[2] - source_key[2])
            for left, right in intervals:
                overlap = (max(lo, left), min(hi, right))
                if overlap[1] - overlap[0] <= _EPS:
                    continue
                if offset <= _EPS:
                    continue
                if offset > tolerance:
                    refusals.add("outside_tolerance")
                    continue
                candidates.append((offset, -(overlap[1] - overlap[0]), key, overlap))
        if not candidates:
            continue
        candidates.sort()
        best = candidates[0]
        if any(abs(c[0] - best[0]) <= _EPS and c[2] != best[2] for c in candidates[1:]):
            refusals.add("ambiguous_corridor")
            continue
        _, _, key, (left, right) = best

        def point(k, value):
            x, y, z = _bundle_interval_point(k, value)
            return Point3(x=x, y=y, z=z)

        direction = getattr(b, ("x", "y", "z")[key[0]]) > getattr(
            a, ("x", "y", "z")[key[0]]
        )
        first, last = (left, right) if direction else (right, left)
        replacement = [
            a,
            point(source_key, first),
            point(key, first),
            point(key, last),
            point(source_key, last),
            b,
        ]
        # Two-coordinate offsets use a deterministic rectilinear connector.
        connector = []
        for p in replacement:
            if connector and _distance(connector[-1], p) <= _EPS:
                continue
            if connector:
                previous = connector[-1]
                xyz = [previous.x, previous.y, previous.z]
                target = (p.x, p.y, p.z)
                for dimension in range(3):
                    if abs(xyz[dimension] - target[dimension]) > _EPS:
                        xyz[dimension] = target[dimension]
                        q = Point3(x=xyz[0], y=xyz[1], z=xyz[2])
                        if q != p:
                            connector.append(q)
            connector.append(p)
        proposal = result[:position] + connector + result[position + 2 :]
        proposal = _simplify(proposal)
        if any(
            not _segment_clear(p, q, geometry.hard_blockers)
            for p, q in zip(proposal, proposal[1:])
        ):
            refusals.add("obstacle_clearance")
            continue
        if geometry.allowed_domain is not None and not _polyline_within_allowed_domain(
            proposal, geometry.allowed_domain,
        ):
            refusals.add("allowed_route_domain")
            continue
        mask = 0
        for p, q in zip(proposal, proposal[1:]):
            mask = _required_mask(mask, p, q, geometry.required)
        if mask != (1 << len(geometry.required)) - 1:
            refusals.add("required_corridor")
            continue
        if options.max_bends is not None and _count_bends(proposal) > options.max_bends:
            refusals.add("bend_limit")
            continue
        # Retain unsimplified indexing until the whole pass is complete.
        result = result[:position] + connector + result[position + 2 :]
        changes.append(
            {
                "source_start": [a.x, a.y, a.z],
                "source_end": [b.x, b.y, b.z],
                "aligned_start": list(_bundle_interval_point(key, first)),
                "aligned_end": list(_bundle_interval_point(key, last)),
                "offset_m": best[0],
            }
        )
    if not changes:
        return points, {
            "status": "unchanged",
            "reason": (
                sorted(refusals)[0] if refusals else "already_aligned_or_no_overlap"
            ),
        }
    result = _simplify(result)
    old_length = sum(_distance(a, b) for a, b in zip(points, points[1:]))
    new_length = sum(_distance(a, b) for a, b in zip(result, result[1:]))
    return result, {
        "status": "aligned",
        "tolerance_m": tolerance,
        "length_delta_m": round(new_length - old_length, options.coordinate_precision),
        "adjustments": list(reversed(changes)),
        "refusal_reasons": sorted(refusals),
    }


def _resolve_diameter(start: Port, end: Port, explicit: float | None) -> float | None:
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0:
            raise RoutingError("nominal_diameter_m must be finite and > 0")
        return float(explicit)
    values = [
        value
        for value in (start.nominal_diameter_m, end.nominal_diameter_m)
        if value is not None
    ]
    return max(values) if values else None


def _normalize_constraint_type(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _applies(constraint: RouteConstraint, route_type: str) -> bool:
    return not constraint.applies_to or route_type in constraint.applies_to or "*" in constraint.applies_to


def _constraint_rule(constraint: RouteConstraint, tolerance_m: float) -> _Rule:
    if isinstance(constraint.geometry, Polygon3D):
        _polygon_plane(constraint.geometry, constraint.id)
    return _Rule(
        constraint.id,
        _geometry_bounds(constraint.geometry).expanded(tolerance_m),
        constraint.geometry,
        tolerance_m,
        "RouteConstraint",
    )


def _collect_routing_geometry(
    model: BuildingModel,
    route_type: str,
    route_radius: float,
    options: RoutingOptions,
    *,
    allowed_domain: AllowedRouteDomain | None = None,
) -> _RoutingGeometry:
    hard: list[_Rule] = []
    soft: list[_Rule] = []
    required: list[_Rule] = []
    preferred: list[_Rule] = []

    for obstacle in sorted(model.obstacles, key=lambda item: item.id):
        padding = obstacle.clearance_m + options.clearance_m + route_radius
        rule = _Rule(
            obstacle.id,
            _geometry_bounds(obstacle.geometry).expanded(padding),
            source_type="Obstacle",
        )
        if obstacle.obstacle_type.strip().lower() in {"soft", "advisory"}:
            soft.append(rule)
        else:
            hard.append(rule)

    for member in sorted(model.structural_members, key=lambda item: item.id):
        # A confirmed framing member is conservative hard no-go geometry
        # around its canonical identity. A rotated member's axis-aligned
        # bounds cover more volume than the member itself; that deliberate
        # overexclusion never penetrates a rotated flange, and no
        # penetration/boring/notching permission is implied.
        bounds = member.axis_aligned_bounds()
        hard.append(_Rule(
            member.id,
            _Bounds(*bounds).expanded(options.clearance_m + route_radius),
            source_type="StructuralMember",
        ))

    for constraint in sorted(model.route_constraints, key=lambda item: item.id):
        if not _applies(constraint, route_type):
            continue
        kind = _normalize_constraint_type(constraint.constraint_type)
        padding = constraint.clearance_m + options.clearance_m + route_radius
        if kind in _KEEP_OUT_TYPES:
            rule = _constraint_rule(constraint, padding)
            if constraint.hard:
                hard.append(rule)
            else:
                soft.append(rule)
        elif kind in _REQUIRED_TYPES:
            rule = _constraint_rule(
                constraint,
                padding + options.corridor_tolerance_m,
            )
            if constraint.hard:
                required.append(rule)
            else:
                preferred.append(rule)
        elif kind in _PREFERRED_TYPES:
            preferred.append(
                _constraint_rule(
                    constraint,
                    padding + options.corridor_tolerance_m,
                )
            )
        elif kind in _AVOID_TYPES:
            rule = _constraint_rule(constraint, padding)
            if constraint.hard:
                hard.append(rule)
            else:
                soft.append(rule)
        else:
            raise RoutingError(
                f"unsupported active route constraint type {constraint.constraint_type!r} "
                f"on {constraint.id}"
            )

    surfaces: list[_Rule] = []
    glazed: list[_Rule] = []
    allowed_domain_index = None
    if allowed_domain is not None:
        if not isinstance(allowed_domain, AllowedRouteDomain):
            raise RoutingError("allowed_domain must be an AllowedRouteDomain value")
        allowed_domain_index = _AllowedDomainIndex(
            request=allowed_domain,
            volumes=tuple(_axis_aligned_box_bounds(box) for box in allowed_domain.volumes),
            route_radius_m=route_radius,
            clearance_m=options.clearance_m,
            sweep_padding_m=route_radius + options.clearance_m,
            containment_epsilon_m=_EPS,
            axis_alignment_epsilon_m=_ALLOWED_DOMAIN_ALIGNMENT_EPSILON_M,
        )
    level_by_id = {level.id: level for level in model.levels}
    surface_pad = options.corridor_tolerance_m + route_radius
    for wall in sorted(model.walls, key=lambda item: item.id):
        base = _geometry_bounds(wall.centerline)
        level = level_by_id.get(wall.level_id)
        base_z = min(point.z for point in wall.centerline.points)
        if level is not None:
            base_z = min(base_z, level.elevation_m)
        wall_bounds = _Bounds(
            base.min_x,
            base.min_y,
            base_z,
            base.max_x,
            base.max_y,
            base_z + wall.height_m,
        ).expanded(wall.thickness_m / 2 + surface_pad)
        if wall.construction == WALL_CONSTRUCTION_GLAZED:
            # Conduit cannot be concealed in glazing: a glazed wall gets no
            # surface-pathway rule, only the soft glazed_wall_penalty.
            glazed.append(_Rule(wall.id, wall_bounds))
        else:
            surfaces.append(_Rule(f"wall:{wall.id}", wall_bounds))
    for ceiling in sorted(model.ceilings, key=lambda item: item.id):
        bounds = _geometry_bounds(ceiling.footprint)
        z = sum(point.z for point in ceiling.footprint.points) / len(ceiling.footprint.points)
        ceiling_bounds = _Bounds(
            bounds.min_x,
            bounds.min_y,
            z - surface_pad,
            bounds.max_x,
            bounds.max_y,
            z + surface_pad,
        )
        surfaces.append(_Rule(f"ceiling:{ceiling.id}", ceiling_bounds))

    return _RoutingGeometry(
        hard_blockers=tuple(hard),
        soft_blockers=tuple(soft),
        required=tuple(required),
        preferred=tuple(preferred),
        surfaces=tuple(surfaces),
        glazed=tuple(glazed),
        allowed_domain=allowed_domain_index,
    )


def _geometry_bounds(geometry: Box3D | Polyline3D | Polygon3D) -> _Bounds:
    if isinstance(geometry, Box3D):
        hx, hy, hz = geometry.size.x / 2, geometry.size.y / 2, geometry.size.z / 2
        corners = []
        for x in (-hx, hx):
            for y in (-hy, hy):
                for z in (-hz, hz):
                    rx, ry, rz = _rotate_vector((x, y, z), geometry.pose.rotation)
                    center = geometry.pose.position
                    corners.append(Point3(x=center.x + rx, y=center.y + ry, z=center.z + rz))
        return _bounds_from_points(corners)
    return _bounds_from_points(geometry.points)


def _axis_aligned_box_bounds(box: Box3D) -> _Bounds:
    """Cardinal-axis bounds, allowing only rounding-sized quarter-turn error.

    Positional deviation is measured at all eight corners against the nearest
    signed axis permutation. Accepted deviations fit inside the explicit
    domain alignment epsilon, which is also honored by containment checks.
    This avoids treating a genuinely rotated volume's wider AABB as allowed
    free space.
    """

    rotation = box.pose.rotation
    local_axes = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    world_axis_for_local: list[int] = []
    world_sign_for_local: list[float] = []
    for axis in local_axes:
        rotated = _rotate_vector(axis, rotation)
        world_axis = max(range(3), key=lambda index: abs(rotated[index]))
        if world_axis in world_axis_for_local:
            raise RoutingError(
                "allowed route domain boxes must be cardinally axis-aligned; "
                "rotated volumes are unsupported"
            )
        world_axis_for_local.append(world_axis)
        world_sign_for_local.append(1.0 if rotated[world_axis] >= 0 else -1.0)

    local_half = (box.size.x / 2.0, box.size.y / 2.0, box.size.z / 2.0)
    world_half = [0.0, 0.0, 0.0]
    for local_axis, world_axis in enumerate(world_axis_for_local):
        world_half[world_axis] = local_half[local_axis]

    maximum_corner_error = 0.0
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                corner_signs = (sx, sy, sz)
                actual = _rotate_vector(tuple(
                    sign * half for sign, half in zip(corner_signs, local_half)
                ), rotation)
                ideal = [0.0, 0.0, 0.0]
                for local_axis, world_axis in enumerate(world_axis_for_local):
                    ideal[world_axis] = (
                        corner_signs[local_axis]
                        * local_half[local_axis]
                        * world_sign_for_local[local_axis]
                    )
                error = math.sqrt(sum(
                    (actual[index] - ideal[index]) ** 2 for index in range(3)
                ))
                maximum_corner_error = max(maximum_corner_error, error)

    if maximum_corner_error > _ALLOWED_DOMAIN_ALIGNMENT_EPSILON_M:
        raise RoutingError(
            "allowed route domain boxes must be cardinally axis-aligned within "
            f"{_ALLOWED_DOMAIN_ALIGNMENT_EPSILON_M:g} m positional error; "
            "rotated volumes are unsupported"
        )

    center = box.pose.position
    bounds = _Bounds(
        center.x - world_half[0], center.y - world_half[1], center.z - world_half[2],
        center.x + world_half[0], center.y + world_half[1], center.z + world_half[2],
    )
    if not all(math.isfinite(value) for value in dataclasses.astuple(bounds)):
        raise RoutingError("allowed route domain world bounds must be finite")
    return bounds


def _allowed_box_sort_key(box: Box3D) -> tuple[float, ...]:
    bounds = _axis_aligned_box_bounds(box)
    center = box.pose.position
    rotation = box.pose.rotation
    return (
        bounds.min_x, bounds.min_y, bounds.min_z,
        bounds.max_x, bounds.max_y, bounds.max_z,
        center.x, center.y, center.z,
        rotation.x, rotation.y, rotation.z, rotation.w,
        box.size.x, box.size.y, box.size.z,
    )


def _allowed_domain_receipt(domain: _AllowedDomainIndex) -> dict[str, object]:
    """JSON-safe exact caller receipt; do not replace boxes with broad-phase bounds."""

    request = domain.request
    return {
        **_allowed_route_domain_request(request),
        "geometry_scope": "caller_volume_containment_only; not physical assembly or code approval",
        "route_radius_m": domain.route_radius_m,
        "clearance_m": domain.clearance_m,
        "sweep_padding_m": domain.sweep_padding_m,
        "containment_epsilon_m": domain.containment_epsilon_m,
        "axis_alignment_epsilon_m": domain.axis_alignment_epsilon_m,
        "envelope_scope": "nominal_radius_only; actual_outer_diameter_not_verified",
    }


def _allowed_route_domain_request(
    request: AllowedRouteDomain | None,
) -> dict[str, object] | None:
    """Normalized caller request, suitable for user-decision idempotence."""

    if request is None:
        return None
    if not isinstance(request, AllowedRouteDomain):
        raise RoutingError("allowed_domain must be an AllowedRouteDomain value")
    return {
        "id": request.id,
        "kind": request.kind,
        "source_ids": list(request.source_ids),
        "containment": "swept-aabb-union-v1",
        "volumes": [
            {
                "pose": {
                    "position": {
                        "x": box.pose.position.x,
                        "y": box.pose.position.y,
                        "z": box.pose.position.z,
                    },
                    "rotation": {
                        "x": box.pose.rotation.x,
                        "y": box.pose.rotation.y,
                        "z": box.pose.rotation.z,
                        "w": box.pose.rotation.w,
                    },
                },
                "size": {"x": box.size.x, "y": box.size.y, "z": box.size.z},
            }
            for box in request.volumes
        ],
    }


def _bounded_routing_context_receipt(
    model: BuildingModel,
    start_port: Port,
    end_port: Port,
    route_type: str,
    nominal_diameter_m: float | None,
    options: RoutingOptions,
    bundle_hints: BundleHints | None,
    allowed_domain: AllowedRouteDomain,
) -> dict[str, object]:
    """Fingerprint every model/request input used to compute a bounded route.

    This receipt lets explicit design idempotence fail closed when its bounded
    routing context changes. It intentionally excludes unrelated model
    collections which the router does not read.
    """

    def rows(items) -> list[dict[str, object]]:
        return [dataclasses.asdict(item) for item in sorted(items, key=lambda item: item.id)]

    def json_compatible(value):
        if isinstance(value, dict):
            return {key: json_compatible(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [json_compatible(item) for item in value]
        return value

    options_data = json_compatible(dataclasses.asdict(options))
    bundle_data = json_compatible(dataclasses.asdict(bundle_hints)) if bundle_hints is not None else None

    routing_geometry = {
        "coordinate_system": dataclasses.asdict(model.coordinate_system),
        "levels": rows(model.levels),
        "walls": rows(model.walls),
        "ceilings": rows(model.ceilings),
        "obstacles": rows(model.obstacles),
        "structural_members": rows(model.structural_members),
        "active_route_constraints": rows(
            item for item in model.route_constraints if _applies(item, route_type)
        ),
    }
    payload = {
        "version": "bounded-routing-context-v1",
        "model_id": model.model_id,
        "start_port": dataclasses.asdict(start_port),
        "end_port": dataclasses.asdict(end_port),
        "route_type": route_type,
        "nominal_diameter_m": nominal_diameter_m,
        "routing_options": options_data,
        "bundle_hints": bundle_data,
        "allowed_route_domain": _allowed_route_domain_request(allowed_domain),
        "routing_geometry": routing_geometry,
    }
    canonical_payload = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return {
        "version": "bounded-routing-context-v1",
        "sha256": hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
        "model_id": model.model_id,
        "route_type": route_type,
        "start_port_id": start_port.id,
        "end_port_id": end_port.id,
        "nominal_diameter_m": nominal_diameter_m,
        "routing_options": options_data,
        "bundle_hints": bundle_data,
        "allowed_route_domain": _allowed_route_domain_request(allowed_domain),
        "routing_geometry_counts": {
            "levels": len(model.levels),
            "walls": len(model.walls),
            "ceilings": len(model.ceilings),
            "obstacles": len(model.obstacles),
            "structural_members": len(model.structural_members),
            "active_route_constraints": sum(
                _applies(item, route_type) for item in model.route_constraints
            ),
        },
    }


def _checked_blocker_receipt(geometry: _RoutingGeometry) -> dict[str, object]:
    ids = tuple(sorted({item.id for item in geometry.hard_blockers}))
    return {
        "checked_blocker_classes": sorted({
            item.source_type for item in geometry.hard_blockers
            if item.source_type is not None
        }),
        "checked_blocker_ids": list(ids[:_MAX_RECORDED_BLOCKER_IDS]),
        "checked_blocker_count": len(ids),
        "checked_blocker_ids_truncated": len(ids) > _MAX_RECORDED_BLOCKER_IDS,
        "limitations": [
            "Wall and ceiling geometry is a pathway preference, not a hard collision check.",
            "Slabs and architectural openings are not checked as route blockers.",
            "The caller volume does not authorize a structural penetration.",
            "Code compliance and installation engineering were not evaluated.",
        ],
    }


def _swept_bounds(a: Point3, b: Point3, padding_m: float) -> _Bounds:
    return _Bounds(
        min(a.x, b.x) - padding_m,
        min(a.y, b.y) - padding_m,
        min(a.z, b.z) - padding_m,
        max(a.x, b.x) + padding_m,
        max(a.y, b.y) + padding_m,
        max(a.z, b.z) + padding_m,
    )


def _bounds_contains(outer: _Bounds, inner: _Bounds) -> bool:
    return (
        inner.min_x >= outer.min_x - _EPS
        and inner.min_y >= outer.min_y - _EPS
        and inner.min_z >= outer.min_z - _EPS
        and inner.max_x <= outer.max_x + _EPS
        and inner.max_y <= outer.max_y + _EPS
        and inner.max_z <= outer.max_z + _EPS
    )


def _bounds_intersection(left: _Bounds, right: _Bounds) -> _Bounds | None:
    minimum = (
        max(left.min_x, right.min_x),
        max(left.min_y, right.min_y),
        max(left.min_z, right.min_z),
    )
    maximum = (
        min(left.max_x, right.max_x),
        min(left.max_y, right.max_y),
        min(left.max_z, right.max_z),
    )
    if any(low > high + _EPS for low, high in zip(minimum, maximum)):
        return None
    # Normalize sub-nanometre roundoff to a closed, zero-width interval.
    maximum = tuple(max(low, high) for low, high in zip(minimum, maximum))
    return _Bounds(*minimum, *maximum)


def _subtract_bounds(source: _Bounds, cutter: _Bounds) -> tuple[_Bounds, ...]:
    """Return nonoverlapping AABBs for ``source`` minus ``cutter``."""

    intersection = _bounds_intersection(source, cutter)
    if intersection is None:
        return (source,)
    if _bounds_contains(cutter, source):
        return ()
    source_spans = (
        source.max_x - source.min_x,
        source.max_y - source.min_y,
        source.max_z - source.min_z,
    )
    intersection_spans = (
        intersection.max_x - intersection.min_x,
        intersection.max_y - intersection.min_y,
        intersection.max_z - intersection.min_z,
    )
    # A face-only intersection cannot cover any positive-volume part of the
    # swept box. (Degenerate route sweeps are epsilon-padded before this call.)
    if any(
        source_span > _EPS and intersection_span <= _EPS
        for source_span, intersection_span in zip(source_spans, intersection_spans)
    ):
        return (source,)

    pieces: list[_Bounds] = []

    def add(min_x, min_y, min_z, max_x, max_y, max_z) -> None:
        if max_x < min_x - _EPS or max_y < min_y - _EPS or max_z < min_z - _EPS:
            return
        pieces.append(_Bounds(
            min_x, min_y, min_z,
            max(min_x, max_x), max(min_y, max_y), max(min_z, max_z),
        ))

    # Peel disjoint x, then y, then z slabs. This partitions only the part not
    # covered by the current box and preserves the closed union at seams.
    if source.min_x < intersection.min_x - _EPS:
        add(source.min_x, source.min_y, source.min_z,
            intersection.min_x, source.max_y, source.max_z)
    if intersection.max_x < source.max_x - _EPS:
        add(intersection.max_x, source.min_y, source.min_z,
            source.max_x, source.max_y, source.max_z)
    if source.min_y < intersection.min_y - _EPS:
        add(intersection.min_x, source.min_y, source.min_z,
            intersection.max_x, intersection.min_y, source.max_z)
    if intersection.max_y < source.max_y - _EPS:
        add(intersection.min_x, intersection.max_y, source.min_z,
            intersection.max_x, source.max_y, source.max_z)
    if source.min_z < intersection.min_z - _EPS:
        add(intersection.min_x, intersection.min_y, source.min_z,
            intersection.max_x, intersection.max_y, intersection.min_z)
    if intersection.max_z < source.max_z - _EPS:
        add(intersection.min_x, intersection.min_y, intersection.max_z,
            intersection.max_x, intersection.max_y, source.max_z)
    return tuple(pieces)


def _bounds_covered_by_union(
    target: _Bounds,
    volumes: tuple[_Bounds, ...],
) -> bool:
    """Exact deterministic AABB-union coverage with a bounded remainder set."""

    if any(_bounds_contains(volume, target) for volume in volumes):
        return True
    uncovered = [target]
    for volume in volumes:
        next_uncovered: list[_Bounds] = []
        for item in uncovered:
            next_uncovered.extend(_subtract_bounds(item, volume))
            if len(next_uncovered) > _MAX_ALLOWED_DOMAIN_REMAINDERS:
                raise RoutingError(
                    "allowed route domain coverage exceeded the bounded "
                    f"remainder limit ({_MAX_ALLOWED_DOMAIN_REMAINDERS})"
                )
        uncovered = next_uncovered
        if not uncovered:
            return True
    return False


def _point_within_allowed_domain(
    point: Point3,
    domain: _AllowedDomainIndex,
) -> bool:
    return _bounds_covered_by_union(
        _swept_bounds(point, point, domain.sweep_padding_m), domain.volumes,
    )


def _segment_within_allowed_domain(
    start: Point3,
    end: Point3,
    domain: _AllowedDomainIndex,
) -> bool:
    return _bounds_covered_by_union(
        _swept_bounds(start, end, domain.sweep_padding_m), domain.volumes,
    )


def _polyline_within_allowed_domain(
    points: Iterable[Point3],
    domain: _AllowedDomainIndex,
) -> bool:
    points = tuple(points)
    return all(
        _segment_within_allowed_domain(start, end, domain)
        for start, end in zip(points, points[1:])
    )


def _rotate_vector(vector: tuple[float, float, float], quaternion) -> tuple[float, float, float]:
    x, y, z = vector
    qx, qy, qz, qw = quaternion.x, quaternion.y, quaternion.z, quaternion.w
    # q * v * q^-1, expanded without temporary quaternion allocation.
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def _bounds_from_points(points: Iterable[Point3]) -> _Bounds:
    values = tuple(points)
    if not values:
        raise RoutingError("geometry has no points")
    return _Bounds(
        min(point.x for point in values),
        min(point.y for point in values),
        min(point.z for point in values),
        max(point.x for point in values),
        max(point.y for point in values),
        max(point.z for point in values),
    )


def _candidate_coordinates(
    model: BuildingModel,
    start: Point3,
    end: Point3,
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    p = options.coordinate_precision
    xs = {_canon(start.x, p), _canon(end.x, p)}
    ys = {_canon(start.y, p), _canon(end.y, p)}
    zs = {_canon(start.z, p), _canon(end.z, p)}
    escape = max(10 ** (-p + 2), 1e-6)

    all_rules = (
        *geometry.hard_blockers,
        *geometry.soft_blockers,
        *geometry.required,
        *geometry.preferred,
        *geometry.surfaces,
        *geometry.glazed,
    )
    for rule in all_rules:
        bounds = rule.bounds
        xs.update({_canon(bounds.min_x - escape, p), _canon(bounds.max_x + escape, p), _canon(bounds.center.x, p)})
        ys.update({_canon(bounds.min_y - escape, p), _canon(bounds.max_y + escape, p), _canon(bounds.center.y, p)})
        zs.update({_canon(bounds.min_z - escape, p), _canon(bounds.max_z + escape, p), _canon(bounds.center.z, p)})
        if isinstance(rule.geometry, (Polyline3D, Polygon3D)):
            for point in rule.geometry.points:
                xs.add(_canon(point.x, p))
                ys.add(_canon(point.y, p))
                zs.add(_canon(point.z, p))

    for wall in model.walls:
        for point in wall.centerline.points:
            xs.add(_canon(point.x, p))
            ys.add(_canon(point.y, p))
            zs.add(_canon(point.z, p))
    for ceiling in model.ceilings:
        for point in ceiling.footprint.points:
            xs.add(_canon(point.x, p))
            ys.add(_canon(point.y, p))
            zs.add(_canon(point.z, p))
    for level in model.levels:
        zs.add(_canon(level.elevation_m, p))
        if level.height_m is not None:
            zs.add(_canon(level.elevation_m + level.height_m, p))

    if geometry.allowed_domain is not None:
        padding = geometry.allowed_domain.sweep_padding_m
        for bounds in geometry.allowed_domain.volumes:
            for coordinates, low, high in (
                (xs, bounds.min_x, bounds.max_x),
                (ys, bounds.min_y, bounds.max_y),
                (zs, bounds.min_z, bounds.max_z),
            ):
                coordinates.update({
                    _canon(low, p),
                    _canon(high, p),
                    _canon((low + high) / 2, p),
                })
                if low + padding <= high - padding:
                    coordinates.update({
                        _canon(low + padding, p),
                        _canon(high - padding, p),
                    })

    if geometry.bundle is not None:
        # Bundle vertices are already canonical; they open lattice lines along
        # hinted trunks so the search can reach them.
        for x, y, z in geometry.bundle.vertices:
            xs.add(x)
            ys.add(y)
            zs.add(z)

    xs.update({_canon(min(xs) - options.search_margin_m, p), _canon(max(xs) + options.search_margin_m, p)})
    ys.update({_canon(min(ys) - options.search_margin_m, p), _canon(max(ys) + options.search_margin_m, p)})
    zs.update({_canon(min(zs) - options.search_margin_m, p), _canon(max(zs) + options.search_margin_m, p)})
    sorted_xs, sorted_ys, sorted_zs = tuple(sorted(xs)), tuple(sorted(ys)), tuple(sorted(zs))
    if (
        geometry.allowed_domain is not None
        and len(sorted_xs) * len(sorted_ys) * len(sorted_zs) > _MAX_ALLOWED_DOMAIN_GRID_NODES
    ):
        raise RoutingError(
            "allowed route domain candidate grid exceeds the bounded "
            f"node limit ({_MAX_ALLOWED_DOMAIN_GRID_NODES})"
        )
    return sorted_xs, sorted_ys, sorted_zs


def _node_for_point(
    point: Point3,
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
    precision: int,
) -> tuple[int, int, int]:
    maps = ({value: index for index, value in enumerate(xs)}, {value: index for index, value in enumerate(ys)}, {value: index for index, value in enumerate(zs)})
    try:
        return (
            maps[0][_canon(point.x, precision)],
            maps[1][_canon(point.y, precision)],
            maps[2][_canon(point.z, precision)],
        )
    except KeyError as exc:
        raise RoutingError("endpoint anchor was not represented in routing grid") from exc


def _search(
    start: Point3,
    start_anchor: Point3,
    start_direction: tuple[float, float, float],
    end: Point3,
    end_anchor: Point3,
    end_direction: tuple[float, float, float],
    start_node: tuple[int, int, int],
    end_node: tuple[int, int, int],
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> list[Point3]:
    start_stub_exists = _distance(start, start_anchor) > _EPS
    end_stub_exists = _distance(end_anchor, end) > _EPS
    start_vector = _vector(start, start_anchor) if start_stub_exists else None
    terminal_vector = _vector(end_anchor, end) if end_stub_exists else None

    initial_mask = _required_mask(0, start, start_anchor, geometry.required)
    initial_cost = _edge_base_cost(start, start_anchor, geometry, options) if start_stub_exists else 0.0
    start_prev = -2 if start_stub_exists else -1
    start_state = (start_node, start_prev, 0, initial_mask)
    best: dict[tuple[tuple[int, int, int], int, int, int], float] = {start_state: initial_cost}
    predecessor: dict[
        tuple[tuple[int, int, int], int, int, int],
        tuple[tuple[int, int, int], int, int, int],
    ] = {}
    heap: list[tuple[float, int, int, int, int, int, int, tuple[tuple[int, int, int], int, int, int]]] = []
    _push(heap, initial_cost, start_state)
    pushed_states = 1

    full_required_mask = (1 << len(geometry.required)) - 1
    best_goal_cost = math.inf
    best_goal_state = None
    considered_blockers: set[str] = set()

    while heap:
        cost, _, _, _, _, _, _, state = heapq.heappop(heap)
        if cost > best.get(state, math.inf) + 1e-12:
            continue
        if cost >= best_goal_cost - 1e-12:
            break
        node, prev_code, bends, mask = state
        point = _point_for(node, xs, ys, zs)

        if node == end_node:
            final_mask = _required_mask(mask, end_anchor, end, geometry.required)
            terminal_bends = bends
            terminal_cost = cost
            if end_stub_exists:
                if prev_code == -2:
                    prior_vector = start_vector
                elif prev_code == -1:
                    prior_vector = None
                else:
                    prior_vector = _direction_vector(prev_code)
                turn = _is_turn(prior_vector, terminal_vector)
                terminal_bends += 1 if turn else 0
                if options.max_bends is not None and terminal_bends > options.max_bends:
                    pass
                else:
                    terminal_cost += _edge_base_cost(end_anchor, end, geometry, options)
                    if turn:
                        terminal_cost += options.bend_penalty_m
            if final_mask == full_required_mask and (
                options.max_bends is None or terminal_bends <= options.max_bends
            ):
                if terminal_cost < best_goal_cost - 1e-12:
                    best_goal_cost = terminal_cost
                    best_goal_state = state

        for next_node, direction_code in _neighbors(node, xs, ys, zs):
            next_point = _point_for(next_node, xs, ys, zs)
            if (
                geometry.allowed_domain is not None
                and not _segment_within_allowed_domain(point, next_point, geometry.allowed_domain)
            ):
                continue
            if _point_blocked(next_point, geometry.hard_blockers):
                if geometry.allowed_domain is not None:
                    considered_blockers.update(
                        item.id for item in geometry.hard_blockers
                        if _rule_contains_point(next_point, item)
                    )
                continue
            if not _segment_clear(point, next_point, geometry.hard_blockers):
                if geometry.allowed_domain is not None:
                    considered_blockers.update(
                        _segment_blocker_ids(point, next_point, geometry.hard_blockers)
                    )
                continue
            if prev_code == -2:
                prior_vector = start_vector
            elif prev_code == -1:
                prior_vector = None
            else:
                prior_vector = _direction_vector(prev_code)
            next_vector = _direction_vector(direction_code)
            turn = _is_turn(prior_vector, next_vector)
            next_bends = bends + (1 if turn else 0)
            if options.max_bends is not None and next_bends > options.max_bends:
                continue
            next_mask = _required_mask(mask, point, next_point, geometry.required)
            next_cost = cost + _edge_base_cost(point, next_point, geometry, options)
            if turn:
                next_cost += options.bend_penalty_m
            next_state = (next_node, direction_code, next_bends, next_mask)
            old_cost = best.get(next_state)
            if old_cost is None or next_cost < old_cost - 1e-12:
                if (
                    geometry.allowed_domain is not None
                    and pushed_states >= _MAX_ALLOWED_DOMAIN_SEARCH_STATES
                ):
                    raise RoutingError(
                        "allowed route domain search exceeded the bounded "
                        f"state limit ({_MAX_ALLOWED_DOMAIN_SEARCH_STATES})"
                    )
                best[next_state] = next_cost
                predecessor[next_state] = state
                _push(heap, next_cost, next_state)
                pushed_states += 1

    if best_goal_state is None:
        required_text = ""
        if geometry.required:
            required_text = "; required corridors=" + ",".join(item.id for item in geometry.required)
        bend_text = "unlimited" if options.max_bends is None else str(options.max_bends)
        if geometry.allowed_domain is None:
            raise NoRouteError(
                f"no route from {start_node} to {end_node} under hard geometry and max_bends={bend_text}{required_text}"
            )
        blocker_text = (
            "; considered hard blockers=" + ",".join(sorted(considered_blockers))
            if considered_blockers else ""
        )
        raise NoRouteError(
            f"no route from {start_node} to {end_node} within allowed route domain "
            f"{geometry.allowed_domain.request.id!r} under hard geometry and "
            f"max_bends={bend_text}{required_text}{blocker_text}",
            reason="no_path_within_allowed_domain",
            blocker_ids=tuple(sorted(considered_blockers)),
        )

    states = [best_goal_state]
    while states[-1] != start_state:
        states.append(predecessor[states[-1]])
    states.reverse()
    path = [_point_for(state[0], xs, ys, zs) for state in states]
    points = [start]
    points.extend(path)
    points.append(end)
    return _dedupe(points)


def _push(heap, cost: float, state) -> None:
    node, direction, bends, mask = state
    heapq.heappush(
        heap,
        (cost, bends, node[0], node[1], node[2], direction, mask, state),
    )


def _neighbors(
    node: tuple[int, int, int],
    xs: tuple[float, ...],
    ys: tuple[float, ...],
    zs: tuple[float, ...],
):
    ix, iy, iz = node
    candidates = []
    if ix > 0:
        candidates.append(((ix - 1, iy, iz), 1))  # -X
    if ix + 1 < len(xs):
        candidates.append(((ix + 1, iy, iz), 0))  # +X
    if iy > 0:
        candidates.append(((ix, iy - 1, iz), 3))  # -Y
    if iy + 1 < len(ys):
        candidates.append(((ix, iy + 1, iz), 2))  # +Y
    if iz > 0:
        candidates.append(((ix, iy, iz - 1), 5))  # -Z
    if iz + 1 < len(zs):
        candidates.append(((ix, iy, iz + 1), 4))  # +Z
    # Coordinate-first ordering makes equal-cost ties independent of input list order.
    yield from sorted(candidates, key=lambda item: item[0])


def _point_for(node, xs, ys, zs) -> Point3:
    return Point3(x=xs[node[0]], y=ys[node[1]], z=zs[node[2]])


def _edge_base_cost(
    a: Point3,
    b: Point3,
    geometry: _RoutingGeometry,
    options: RoutingOptions,
) -> float:
    length = _distance(a, b)
    if length <= _EPS:
        return 0.0
    vertical = abs(a.z - b.z) > _EPS and abs(a.x - b.x) <= _EPS and abs(a.y - b.y) <= _EPS
    cost = length * (options.vertical_cost_factor if vertical else 1.0)
    if geometry.soft_blockers:
        hits = sum(_rule_intersects_segment(a, b, item) for item in geometry.soft_blockers)
        cost += length * options.soft_obstacle_penalty_factor * hits
    if geometry.preferred and any(_rule_intersects_segment(a, b, item) for item in geometry.preferred):
        cost *= 1.0 - options.preferred_corridor_discount
    if geometry.surfaces and any(_segment_midpoint_in_bounds(a, b, item.bounds) for item in geometry.surfaces):
        cost *= 1.0 - options.surface_path_discount
    if geometry.glazed and any(_segment_midpoint_in_bounds(a, b, item.bounds) for item in geometry.glazed):
        cost *= options.glazed_wall_penalty
    if geometry.bundle is not None and _along_bundle(a, b, geometry.bundle, options.coordinate_precision):
        cost *= geometry.bundle.factor
    return cost


def _required_mask(mask: int, a: Point3, b: Point3, required: tuple[_Rule, ...]) -> int:
    if _distance(a, b) <= _EPS:
        for index, item in enumerate(required):
            if _rule_contains_point(a, item):
                mask |= 1 << index
        return mask
    for index, item in enumerate(required):
        if _rule_intersects_segment(a, b, item):
            mask |= 1 << index
    return mask


def _point_blocked(point: Point3, blockers: tuple[_Rule, ...]) -> bool:
    return any(_rule_contains_point(point, item) for item in blockers)


def _segment_clear(a: Point3, b: Point3, blockers: tuple[_Rule, ...]) -> bool:
    if _distance(a, b) <= _EPS:
        return not _point_blocked(a, blockers)
    return not any(_rule_intersects_segment(a, b, item) for item in blockers)


def _segment_blocker_ids(
    a: Point3,
    b: Point3,
    blockers: tuple[_Rule, ...],
) -> tuple[str, ...]:
    if _distance(a, b) <= _EPS:
        return tuple(sorted(item.id for item in blockers if _rule_contains_point(a, item)))
    return tuple(sorted(
        item.id for item in blockers if _rule_intersects_segment(a, b, item)
    ))


def _point_in_bounds(point: Point3, bounds: _Bounds) -> bool:
    return (
        bounds.min_x - _EPS <= point.x <= bounds.max_x + _EPS
        and bounds.min_y - _EPS <= point.y <= bounds.max_y + _EPS
        and bounds.min_z - _EPS <= point.z <= bounds.max_z + _EPS
    )


def _segment_midpoint_in_bounds(a: Point3, b: Point3, bounds: _Bounds) -> bool:
    return _point_in_bounds(
        Point3(x=(a.x + b.x) / 2, y=(a.y + b.y) / 2, z=(a.z + b.z) / 2),
        bounds,
    )


def _segment_intersects_bounds(a: Point3, b: Point3, bounds: _Bounds) -> bool:
    t_min = 0.0
    t_max = 1.0
    for start, end, minimum, maximum in (
        (a.x, b.x, bounds.min_x, bounds.max_x),
        (a.y, b.y, bounds.min_y, bounds.max_y),
        (a.z, b.z, bounds.min_z, bounds.max_z),
    ):
        delta = end - start
        if abs(delta) <= _EPS:
            if start < minimum - _EPS or start > maximum + _EPS:
                return False
            continue
        first = (minimum - start) / delta
        second = (maximum - start) / delta
        if first > second:
            first, second = second, first
        t_min = max(t_min, first)
        t_max = min(t_max, second)
        if t_min > t_max + _EPS:
            return False
    return t_max >= -_EPS and t_min <= 1.0 + _EPS


def _inverse_rotate_vector(vector: tuple[float, float, float], quaternion) -> tuple[float, float, float]:
    x, y, z = vector
    qx, qy, qz, qw = -quaternion.x, -quaternion.y, -quaternion.z, quaternion.w
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


def _box_local_coordinates(point: Point3, box: Box3D) -> tuple[float, float, float]:
    center = box.pose.position
    return _inverse_rotate_vector(
        (point.x - center.x, point.y - center.y, point.z - center.z),
        box.pose.rotation,
    )


def _point_box_distance(point: Point3, box: Box3D) -> float:
    local = _box_local_coordinates(point, box)
    half_sizes = (box.size.x / 2, box.size.y / 2, box.size.z / 2)
    outside = tuple(
        max(abs(value) - half_size, 0.0)
        for value, half_size in zip(local, half_sizes)
    )
    return math.sqrt(_dot(outside, outside))


def _segment_aabb_distance(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    half_sizes: tuple[float, float, float],
) -> float:
    delta = tuple(right - left for left, right in zip(start, end))

    t_min = 0.0
    t_max = 1.0
    intersects = True
    for value, change, half_size in zip(start, delta, half_sizes):
        if abs(change) <= _EPS:
            if value < -half_size - _EPS or value > half_size + _EPS:
                intersects = False
                break
            continue
        first = (-half_size - value) / change
        second = (half_size - value) / change
        if first > second:
            first, second = second, first
        t_min = max(t_min, first)
        t_max = min(t_max, second)
        if t_min > t_max + _EPS:
            intersects = False
            break
    if intersects and t_max >= -_EPS and t_min <= 1.0 + _EPS:
        return 0.0

    breakpoints = {0.0, 1.0}
    for value, change, half_size in zip(start, delta, half_sizes):
        if abs(change) <= _EPS:
            continue
        for boundary in (-half_size, half_size):
            parameter = (boundary - value) / change
            if _EPS < parameter < 1.0 - _EPS:
                breakpoints.add(parameter)
    ordered = sorted(breakpoints)

    def squared_distance(parameter: float) -> float:
        total = 0.0
        for value, change, half_size in zip(start, delta, half_sizes):
            coordinate = value + change * parameter
            outside = max(abs(coordinate) - half_size, 0.0)
            total += outside * outside
        return total

    best = min(squared_distance(parameter) for parameter in ordered)
    for left, right in zip(ordered, ordered[1:]):
        if right - left <= _EPS:
            continue
        midpoint = (left + right) / 2
        numerator = 0.0
        denominator = 0.0
        for value, change, half_size in zip(start, delta, half_sizes):
            coordinate = value + change * midpoint
            if coordinate < -half_size:
                boundary = -half_size
            elif coordinate > half_size:
                boundary = half_size
            else:
                continue
            numerator += change * (value - boundary)
            denominator += change * change
        if denominator <= _EPS:
            continue
        parameter = -numerator / denominator
        if left - _EPS <= parameter <= right + _EPS:
            parameter = max(left, min(right, parameter))
            best = min(best, squared_distance(parameter))
    return math.sqrt(max(best, 0.0))


def _segment_box_distance(a: Point3, b: Point3, box: Box3D) -> float:
    return _segment_aabb_distance(
        _box_local_coordinates(a, box),
        _box_local_coordinates(b, box),
        (box.size.x / 2, box.size.y / 2, box.size.z / 2),
    )


def _rule_contains_point(point: Point3, rule: _Rule) -> bool:
    if not _point_in_bounds(point, rule.bounds):
        return False
    if isinstance(rule.geometry, Box3D):
        return _point_box_distance(point, rule.geometry) <= rule.tolerance_m + _EPS
    if isinstance(rule.geometry, Polyline3D):
        return any(
            _point_segment_distance(point, start, end) <= rule.tolerance_m + _EPS
            for start, end in zip(rule.geometry.points, rule.geometry.points[1:])
        )
    if isinstance(rule.geometry, Polygon3D):
        return _point_polygon_distance(point, rule.geometry, rule.id) <= rule.tolerance_m + _EPS
    return True


def _rule_intersects_segment(a: Point3, b: Point3, rule: _Rule) -> bool:
    if not _segment_intersects_bounds(a, b, rule.bounds):
        return False
    if isinstance(rule.geometry, Box3D):
        return _segment_box_distance(a, b, rule.geometry) <= rule.tolerance_m + _EPS
    if isinstance(rule.geometry, Polyline3D):
        return any(
            _segment_segment_distance(a, b, start, end) <= rule.tolerance_m + _EPS
            for start, end in zip(rule.geometry.points, rule.geometry.points[1:])
        )
    if isinstance(rule.geometry, Polygon3D):
        return _segment_polygon_distance(a, b, rule.geometry, rule.id) <= rule.tolerance_m + _EPS
    return True


def _delta(a: Point3, b: Point3) -> tuple[float, float, float]:
    return (b.x - a.x, b.y - a.y, b.z - a.z)


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: tuple[float, float, float], right: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _point_segment_distance(point: Point3, start: Point3, end: Point3) -> float:
    segment = _delta(start, end)
    length_sq = _dot(segment, segment)
    if length_sq <= _EPS:
        return _distance(point, start)
    from_start = _delta(start, point)
    t = max(0.0, min(1.0, _dot(from_start, segment) / length_sq))
    closest = Point3(
        x=start.x + segment[0] * t,
        y=start.y + segment[1] * t,
        z=start.z + segment[2] * t,
    )
    return _distance(point, closest)


def _segment_segment_distance(a: Point3, b: Point3, c: Point3, d: Point3) -> float:
    u = _delta(a, b)
    v = _delta(c, d)
    w = _delta(c, a)
    aa = _dot(u, u)
    cc = _dot(v, v)
    if aa <= _EPS:
        return _point_segment_distance(a, c, d)
    if cc <= _EPS:
        return _point_segment_distance(c, a, b)

    bb = _dot(u, v)
    dd = _dot(u, w)
    ee = _dot(v, w)
    denominator = aa * cc - bb * bb
    if denominator <= _EPS:
        s = 0.0
    else:
        s = max(0.0, min(1.0, (bb * ee - cc * dd) / denominator))
    t = (bb * s + ee) / cc
    if t < 0.0:
        t = 0.0
        s = max(0.0, min(1.0, -dd / aa))
    elif t > 1.0:
        t = 1.0
        s = max(0.0, min(1.0, (bb - dd) / aa))

    left = Point3(x=a.x + u[0] * s, y=a.y + u[1] * s, z=a.z + u[2] * s)
    right = Point3(x=c.x + v[0] * t, y=c.y + v[1] * t, z=c.z + v[2] * t)
    return _distance(left, right)


def _polygon_plane(
    polygon: Polygon3D,
    rule_id: str,
) -> tuple[Point3, tuple[float, float, float]]:
    origin = polygon.points[0]
    normal = None
    for first, second in zip(polygon.points[1:], polygon.points[2:]):
        candidate = _cross(_delta(origin, first), _delta(origin, second))
        magnitude = math.sqrt(_dot(candidate, candidate))
        if magnitude > _EPS:
            normal = tuple(value / magnitude for value in candidate)
            break
    if normal is None:
        raise RoutingError(f"route constraint {rule_id!r} has degenerate Polygon3D geometry")
    for point in polygon.points:
        if abs(_dot(_delta(origin, point), normal)) > 1e-7:
            raise RoutingError(f"route constraint {rule_id!r} has non-planar Polygon3D geometry")
    return origin, normal


def _project_2d(point: Point3, normal: tuple[float, float, float]) -> tuple[float, float]:
    drop = max(range(3), key=lambda index: abs(normal[index]))
    if drop == 0:
        return point.y, point.z
    if drop == 1:
        return point.x, point.z
    return point.x, point.y


def _point_on_segment_2d(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    cross = (point[0] - start[0]) * (end[1] - start[1]) - (point[1] - start[1]) * (end[0] - start[0])
    if abs(cross) > 1e-9:
        return False
    dot = (point[0] - start[0]) * (point[0] - end[0]) + (point[1] - start[1]) * (point[1] - end[1])
    return dot <= 1e-9


def _point_in_polygon_projection(
    point: Point3,
    polygon: Polygon3D,
    normal: tuple[float, float, float],
) -> bool:
    target = _project_2d(point, normal)
    vertices = [_project_2d(vertex, normal) for vertex in polygon.points]
    for start, end in zip(vertices, (*vertices[1:], vertices[0])):
        if _point_on_segment_2d(target, start, end):
            return True

    inside = False
    x, y = target
    previous = vertices[-1]
    for current in vertices:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _point_polygon_distance(point: Point3, polygon: Polygon3D, rule_id: str) -> float:
    origin, normal = _polygon_plane(polygon, rule_id)
    signed_distance = _dot(_delta(origin, point), normal)
    projected = Point3(
        x=point.x - normal[0] * signed_distance,
        y=point.y - normal[1] * signed_distance,
        z=point.z - normal[2] * signed_distance,
    )
    if _point_in_polygon_projection(projected, polygon, normal):
        return abs(signed_distance)
    edges = zip(polygon.points, (*polygon.points[1:], polygon.points[0]))
    return min(_point_segment_distance(point, start, end) for start, end in edges)


def _segment_polygon_distance(a: Point3, b: Point3, polygon: Polygon3D, rule_id: str) -> float:
    origin, normal = _polygon_plane(polygon, rule_id)
    distance_a = _dot(_delta(origin, a), normal)
    distance_b = _dot(_delta(origin, b), normal)

    if abs(distance_a) <= _EPS and _point_in_polygon_projection(a, polygon, normal):
        return 0.0
    if abs(distance_b) <= _EPS and _point_in_polygon_projection(b, polygon, normal):
        return 0.0
    denominator = distance_a - distance_b
    if abs(denominator) > _EPS:
        t = distance_a / denominator
        if -_EPS <= t <= 1.0 + _EPS:
            intersection = Point3(
                x=a.x + (b.x - a.x) * t,
                y=a.y + (b.y - a.y) * t,
                z=a.z + (b.z - a.z) * t,
            )
            if _point_in_polygon_projection(intersection, polygon, normal):
                return 0.0

    distances = [
        _point_polygon_distance(a, polygon, rule_id),
        _point_polygon_distance(b, polygon, rule_id),
    ]
    for start, end in zip(polygon.points, (*polygon.points[1:], polygon.points[0])):
        distances.append(_segment_segment_distance(a, b, start, end))
    return min(distances)


def _unit(vector: Vector3) -> tuple[float, float, float]:
    magnitude = vector.magnitude
    return (vector.x / magnitude, vector.y / magnitude, vector.z / magnitude)


def _offset(point: Point3, direction: tuple[float, float, float], distance: float) -> Point3:
    return Point3(
        x=point.x + direction[0] * distance,
        y=point.y + direction[1] * distance,
        z=point.z + direction[2] * distance,
    )


def _vector(a: Point3, b: Point3) -> tuple[float, float, float]:
    dx, dy, dz = b.x - a.x, b.y - a.y, b.z - a.z
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length <= _EPS:
        raise RoutingError("zero-length vector has no direction")
    return (dx / length, dy / length, dz / length)


def _direction_vector(code: int) -> tuple[float, float, float]:
    return {
        0: (1.0, 0.0, 0.0),
        1: (-1.0, 0.0, 0.0),
        2: (0.0, 1.0, 0.0),
        3: (0.0, -1.0, 0.0),
        4: (0.0, 0.0, 1.0),
        5: (0.0, 0.0, -1.0),
    }[code]


def _is_turn(
    previous: tuple[float, float, float] | None,
    current: tuple[float, float, float] | None,
) -> bool:
    if previous is None or current is None:
        return False
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(previous, current))))
    return abs(dot - 1.0) > 1e-9


def _distance(a: Point3, b: Point3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _canon(value: float, precision: int) -> float:
    rounded = round(float(value), precision)
    return 0.0 if rounded == -0.0 else rounded


def _dedupe(points: list[Point3]) -> list[Point3]:
    result: list[Point3] = []
    for point in points:
        if not result or _distance(result[-1], point) > _EPS:
            result.append(point)
    return result


def _simplify(points: list[Point3]) -> list[Point3]:
    points = _dedupe(points)
    if len(points) <= 2:
        return points
    result = [points[0]]
    for index in range(1, len(points) - 1):
        a = result[-1]
        b = points[index]
        c = points[index + 1]
        ab = _vector(a, b)
        bc = _vector(b, c)
        dot = sum(x * y for x, y in zip(ab, bc))
        if abs(dot - 1.0) <= 1e-9:
            continue
        result.append(b)
    result.append(points[-1])
    return result


def _count_bends(points: list[Point3]) -> int:
    bends = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        if _is_turn(_vector(a, b), _vector(b, c)):
            bends += 1
    return bends


def _direction_token(a: Point3, b: Point3) -> str:
    vector = _vector(a, b)
    axes = {
        (1.0, 0.0, 0.0): "+x",
        (-1.0, 0.0, 0.0): "-x",
        (0.0, 1.0, 0.0): "+y",
        (0.0, -1.0, 0.0): "-y",
        (0.0, 0.0, 1.0): "+z",
        (0.0, 0.0, -1.0): "-z",
    }
    for known, token in axes.items():
        if all(abs(left - right) <= 1e-9 for left, right in zip(vector, known)):
            return token
    return "v({:.6f},{:.6f},{:.6f})".format(*vector)


def _build_fittings(
    route_id: str,
    points: list[Point3],
    diameter: float | None,
    provenance: tuple[Provenance, ...],
) -> tuple[RouteFitting, ...]:
    if len(points) < 3:
        return ()
    directions = [_direction_token(a, b) for a, b in zip(points, points[1:])]
    fittings: list[RouteFitting] = []
    for index, (a, vertex, c) in enumerate(zip(points, points[1:], points[2:]), start=1):
        incoming = _vector(a, vertex)
        outgoing = _vector(vertex, c)
        dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(incoming, outgoing))))
        angle = math.acos(dot)
        if angle <= 1e-9:
            continue
        signature = ">".join(directions[: index + 1])
        fitting_id = stable_id("fitting", f"{route_id}:turn:{signature}")
        if abs(angle - math.pi / 2) <= 1e-6:
            fitting_type = "elbow-90"
        elif abs(angle - math.pi / 4) <= 1e-6:
            fitting_type = "elbow-45"
        else:
            fitting_type = "elbow"
        fittings.append(
            RouteFitting(
                id=fitting_id,
                route_id=route_id,
                fitting_type=fitting_type,
                pose=Pose(position=vertex),
                nominal_diameter_m=diameter,
                angle_radians=angle,
                provenance=provenance,
                attributes={"routing_turn_signature": signature},
            )
        )
    return tuple(fittings)
