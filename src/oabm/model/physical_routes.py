"""Opt-in exact physical route contract. Structural validity is not clearance certification."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Mapping

from .common import (ContractError, Entity, Point3, Provenance, Vector3,
                     PHYSICAL_ROUTE_SCHEMA_VERSION, _validate_id)

PHYSICAL_ROUTE_NUMERICAL_PROFILE = "physical-routing-decimal-nm-v1"
TICKS_PER_METRE = 1_000_000_000
MAX_COORDINATE_M = 1000
MAX_PHYSICAL_ROUTE_SEGMENTS = 1024


def exact_ticks(value: Any, label: str, *, minimum: str = "-1000", maximum: str = "1000") -> int:
    """Read authored decimal metres, never rounded or snapped to the grid.

    JSON calls this with Decimal before binary conversion. Direct Python callers
    use the canonical decimal rendering of their int/float snapshot. No global
    Decimal precision setting participates in the exact integer/rational test.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ContractError(f"{label} must be a decimal number")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractError(f"{label} must be a finite decimal number") from exc
    if not decimal.is_finite() or not Decimal(minimum) <= decimal <= Decimal(maximum):
        raise ContractError(f"{label} is outside [{minimum}, {maximum}] metres")
    # Inspect the finite decimal coefficient/exponent BEFORE any power or
    # Fraction conversion. A short token with an enormous negative exponent
    # must not allocate an enormous denominator merely to discover it is off-grid.
    sign, digits, exponent = decimal.as_tuple()
    significant = len(digits)
    while significant and digits[significant - 1] == 0:
        significant -= 1
    if not significant:
        ticks = 0
    else:
        shift = exponent + len(digits) - significant + 9
        if shift < 0:
            raise ContractError(f"{label} must be an exact decimal nanometre multiple; no snapping")
        # The largest supported authored length is 2000 m. After the range
        # check above, at most 13 decimal digits can contribute to integer ticks.
        if significant + shift > 13:
            raise ContractError(f"{label} exceeds the bounded integer tick profile")
        ticks = 0
        for digit in digits[:significant]:
            ticks = ticks * 10 + digit
        ticks *= 10 ** shift
        if sign:
            ticks = -ticks
    # Successful authored input must survive canonical JSON numeric rendering.
    if Decimal(str(float(decimal))) != decimal:
        raise ContractError(f"{label} cannot preserve exact ticks through serialization")
    return ticks


def point_ticks(point: Point3, label: str = "physical route point") -> tuple[int, int, int]:
    if not isinstance(point, Point3):
        raise ContractError(f"{label} must be a typed Point3")
    return tuple(exact_ticks(getattr(point, axis), f"{label}.{axis}") for axis in ("x", "y", "z"))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _delta(a, b):
    return tuple(y - x for x, y in zip(a, b))


def _cardinal(delta, label):
    if sum(value != 0 for value in delta) != 1:
        raise ContractError(f"{label} must be a nonzero cardinal vector")
    return tuple((value > 0) - (value < 0) for value in delta)


def _owned_provenance(owner: Any, *, supplied_fields: tuple[str, ...] = ()) -> None:
    reserved = {item.name for item in fields(owner)} | {"centerline", "arc_center", "sharp_vertex"}
    if reserved.intersection(getattr(owner, "attributes", {})):
        raise ContractError("physical attributes cannot shadow canonical geometry or fields")
    if not isinstance(owner.provenance, tuple) or not owner.provenance or any(
        not isinstance(record, Provenance) for record in owner.provenance
    ):
        raise ContractError("physical route records require typed nonempty provenance")
    for record in owner.provenance:
        record.__post_init__()
    for name in supplied_fields:
        if not any(
            record.derivation in {"observed", "user"}
            and record.scope_paths is not None
            and name in record.scope_paths
            for record in owner.provenance
        ):
            raise ContractError(f"{name} requires field-qualified supplied provenance")


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRouteJoint(Entity):
    route_id: str
    position: Point3

    def __post_init__(self) -> None:
        super(PhysicalRouteJoint, self).__post_init__()
        _validate_id(self.route_id, "joint.route_id")
        point_ticks(self.position)
        _owned_provenance(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class LineSegment3D(Entity):
    route_id: str
    start_joint_id: str
    end_joint_id: str
    kind: str = field(default="line", init=False)

    def __post_init__(self) -> None:
        super(LineSegment3D, self).__post_init__()
        for name in ("route_id", "start_joint_id", "end_joint_id"):
            _validate_id(getattr(self, name), name)
        if self.start_joint_id == self.end_joint_id:
            raise ContractError("physical line must reference distinct joints")
        _owned_provenance(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class QuarterArc3D(Entity):
    route_id: str
    start_joint_id: str
    end_joint_id: str
    plane_normal: "Vector3"
    turn: str
    kind: str = field(default="quarter_arc", init=False)

    def __post_init__(self) -> None:
        super(QuarterArc3D, self).__post_init__()
        for name in ("route_id", "start_joint_id", "end_joint_id"):
            _validate_id(getattr(self, name), name)
        if self.start_joint_id == self.end_joint_id:
            raise ContractError("physical arc must reference distinct joints")
        if not isinstance(self.plane_normal, Vector3):
            raise ContractError("quarter arc plane_normal must be Vector3")
        normal = (self.plane_normal.x, self.plane_normal.y, self.plane_normal.z)
        if any(isinstance(v, bool) for v in normal) or normal not in (
            (1, 0, 0), (0, 1, 0), (0, 0, 1)
        ):
            raise ContractError("quarter arc normal must be a canonical positive unit axis")
        if self.turn not in {"positive_quarter", "negative_quarter"}:
            raise ContractError("quarter arc turn must be positive_quarter or negative_quarter")
        _owned_provenance(self)


REQUIREMENT_DIMENSIONS = (
    "outside_diameter_m", "centerline_radius_m", "clearance_m",
    "start_straight_min_m", "end_straight_min_m", "between_bends_straight_min_m",
)
REQUIREMENT_BOUNDS = {
    "outside_diameter_m": ("0.001", "1"),
    "centerline_radius_m": ("0.001", "10"),
    "clearance_m": ("0", "100"),
    "start_straight_min_m": ("0", "2000"),
    "end_straight_min_m": ("0", "2000"),
    "between_bends_straight_min_m": ("0", "2000"),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRouteRequirements(Entity):
    route_id: str
    outside_diameter_m: float
    centerline_radius_m: float
    clearance_m: float
    start_straight_min_m: float
    end_straight_min_m: float
    between_bends_straight_min_m: float
    profile: str = field(default=PHYSICAL_ROUTE_NUMERICAL_PROFILE, init=False)

    def __post_init__(self) -> None:
        super(PhysicalRouteRequirements, self).__post_init__()
        _validate_id(self.route_id, "requirements.route_id")
        values = {}
        for name, (low, high) in REQUIREMENT_BOUNDS.items():
            values[name] = exact_ticks(getattr(self, name), name, minimum=low, maximum=high)
        if 2 * values["centerline_radius_m"] - values["outside_diameter_m"] < 20_000:
            raise ContractError("centerline radius minus outside radius must be at least 0.00001 m")
        _owned_provenance(self, supplied_fields=REQUIREMENT_DIMENSIONS)


# Imported after numerical helpers so the separate context module can reuse them.
from .physical_route_context import PhysicalRoutingContext, validate_request_binding


@dataclass(frozen=True, slots=True, kw_only=True)
class PhysicalRoute(Entity):
    route_type: str
    start_port_id: str
    end_port_id: str
    joints: tuple[PhysicalRouteJoint, ...]
    segments: tuple[LineSegment3D | QuarterArc3D, ...]
    requirements: PhysicalRouteRequirements
    context: PhysicalRoutingContext
    geometry_kind: str = field(default="line_arc_v1", init=False)

    def __post_init__(self) -> None:
        super(PhysicalRoute, self).__post_init__()
        if not isinstance(self.route_type, str) or not self.route_type:
            raise ContractError("physical route_type is required")
        for name in ("start_port_id", "end_port_id"):
            _validate_id(getattr(self, name), name)
        if self.start_port_id == self.end_port_id:
            raise ContractError("physical route requires distinct endpoint ports")
        if not isinstance(self.requirements, PhysicalRouteRequirements):
            raise ContractError("physical route requires typed requirements")
        if not isinstance(self.context, PhysicalRoutingContext):
            raise ContractError("physical route requires typed context")
        if not isinstance(self.joints, tuple) or any(not isinstance(j, PhysicalRouteJoint) for j in self.joints):
            raise ContractError("physical route joints must be a typed tuple")
        if not isinstance(self.segments, tuple) or not 1 <= len(self.segments) <= MAX_PHYSICAL_ROUTE_SEGMENTS:
            raise ContractError("physical route requires 1..1024 segments")
        if any(not isinstance(s, (LineSegment3D, QuarterArc3D)) for s in self.segments):
            raise ContractError("physical route segments require explicit supported types")
        if len(self.joints) != len(self.segments) + 1 or len({j.id for j in self.joints}) != len(self.joints):
            raise ContractError("physical route joints must form one unrepeated ordered chain")
        # Frozen records may still contain mutable JSON metadata. Revalidate
        # each actual owner so validate_model and serialize/reload agree.
        self.context.__post_init__()
        self.context.allowed_domain.__post_init__()
        for owner in (*self.joints, *self.segments, self.requirements):
            owner.__post_init__()
            if owner.route_id != self.id:
                raise ContractError("nested physical route owner belongs to another route")
        if len({owner.id for owner in (self, *self.joints, *self.segments, self.requirements)}) != 2 + len(self.joints) + len(self.segments):
            raise ContractError("duplicate physical route nested identity")
        for index, segment in enumerate(self.segments):
            if (segment.start_joint_id, segment.end_joint_id) != (self.joints[index].id, self.joints[index + 1].id):
                raise ContractError("physical segments must use adjacent shared joints in order")
        if not isinstance(self.segments[0], LineSegment3D) or not isinstance(self.segments[-1], LineSegment3D):
            raise ContractError("physical route must start and end with positive straight segments")
        facts = physical_segment_facts(self)
        for index, (left, right) in enumerate(zip(self.segments, self.segments[1:])):
            if type(left) is type(right):
                raise ContractError("adjacent lines must be coalesced and arcs need an intervening line")
            if facts[index]["end_tangent"] != facts[index + 1]["start_tangent"]:
                raise ContractError("physical segment tangents must meet exactly; no kinks")
        for index, segment in enumerate(self.segments):
            if isinstance(segment, LineSegment3D):
                minimum = 0
                if index == 0:
                    minimum = max(minimum, exact_ticks(self.requirements.start_straight_min_m, "start minimum", minimum="0", maximum="2000"))
                if index == len(self.segments) - 1:
                    minimum = max(minimum, exact_ticks(self.requirements.end_straight_min_m, "end minimum", minimum="0", maximum="2000"))
                if 0 < index < len(self.segments) - 1:
                    minimum = exact_ticks(self.requirements.between_bends_straight_min_m, "inter-bend minimum", minimum="0", maximum="2000")
                if facts[index]["length_ticks"] < minimum:
                    raise ContractError("physical straight segment is shorter than its supplied minimum")
        if self.context.route_type != self.route_type:
            raise ContractError("physical context route_type disagrees with route")
        if (self.context.start_port_id, self.context.end_port_id) != (self.start_port_id, self.end_port_id):
            raise ContractError("physical context endpoint references disagree with route")
        _owned_provenance(self)
        validate_request_binding(self.context, self.requirements)


def physical_segment_facts(route: PhysicalRoute) -> tuple[dict[str, Any], ...]:
    """Derived exact centreline facts only. These are not solid-clearance results."""
    points = {joint.id: point_ticks(joint.position) for joint in route.joints}
    rows = []
    expected_radius = exact_ticks(route.requirements.centerline_radius_m, "centerline radius", minimum="0.001", maximum="10")
    for segment in route.segments:
        a, b = points[segment.start_joint_id], points[segment.end_joint_id]
        delta = _delta(a, b)
        if isinstance(segment, LineSegment3D):
            tangent = _cardinal(delta, "physical line")
            rows.append({"start_tangent": tangent, "end_tangent": tangent,
                         "length_ticks": sum(abs(v) for v in delta)})
            continue
        normal = tuple(int(v) for v in (segment.plane_normal.x, segment.plane_normal.y, segment.plane_normal.z))
        axis = normal.index(1)
        varying = [i for i in range(3) if i != axis]
        if delta[axis] != 0 or not delta[varying[0]] or abs(delta[varying[0]]) != abs(delta[varying[1]]):
            raise ContractError("quarter arc endpoints must have equal nonzero cardinal-plane deltas")
        radius = abs(delta[varying[0]])
        if radius != expected_radius:
            raise ContractError("derived quarter arc radius does not equal supplied radius")
        sign = 1 if segment.turn == "positive_quarter" else -1
        perpendicular = _cross(normal, delta)
        centre = tuple(Fraction(a[i] + b[i] + sign * perpendicular[i], 2) for i in range(3))
        if any(abs(v) > MAX_COORDINATE_M * TICKS_PER_METRE for v in centre):
            raise ContractError("derived quarter arc centre exceeds coordinate profile")
        start_tangent = tuple(sign * v / radius for v in _cross(normal, _delta(centre, a)))
        end_tangent = tuple(sign * v / radius for v in _cross(normal, _delta(centre, b)))
        rows.append({"centre_ticks": centre, "radius_ticks": radius,
                     "start_tangent": start_tangent, "end_tangent": end_tangent})
    return tuple(rows)


def validate_physical_route_ports(route: PhysicalRoute, ports: Mapping[str, Any], frame_id: str) -> None:
    if route.context.coordinate_frame_id != frame_id or route.context.allowed_domain.coordinate_frame_id != frame_id:
        raise ContractError("physical route context must use the model coordinate frame")
    facts = physical_segment_facts(route)
    for port_id, joint, tangent, sign in (
        (route.start_port_id, route.joints[0], facts[0]["start_tangent"], 1),
        (route.end_port_id, route.joints[-1], facts[-1]["end_tangent"], -1),
    ):
        port = ports[port_id]
        if point_ticks(port.pose.position) != point_ticks(joint.position):
            raise ContractError("physical route endpoint must equal its port exactly")
        values = (port.direction.x, port.direction.y, port.direction.z)
        if values not in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
            raise ContractError("physical route port direction must be an exact signed unit axis")
        if tuple(sign * v for v in values) != tangent:
            raise ContractError("physical route endpoint tangent conflicts with outward port direction")


def reject_physical_routes(model: Any, consumer: str) -> None:
    """Fail before unsupported consumers filter, mutate, invoke callbacks or write."""
    if any(isinstance(route, PhysicalRoute) for route in model.routes):
        raise ContractError(f"{consumer} does not support physical routes (line_arc_v1)")


def _raw_point(data: Any, label: str) -> None:
    if not isinstance(data, Mapping):
        raise ContractError(f"{label} must be a point object")
    for axis in ("x", "y", "z"):
        exact_ticks(data.get(axis), f"{label}.{axis}")


def _raw_box(data: Any) -> None:
    if not isinstance(data, Mapping):
        raise ContractError("physical box must be an object")
    pose = data.get("pose", {})
    _raw_point(pose.get("position"), "physical box position")
    for axis in ("x", "y", "z"):
        exact_ticks(data.get("size", {}).get(axis), "physical box size", minimum="0.000000001", maximum="2000")
    rotation = pose.get("rotation", {})
    values = tuple(rotation.get(axis, 1 if axis == "w" else 0) for axis in ("x", "y", "z", "w"))
    if values != (0, 0, 0, 1):
        raise ContractError("unsupported physical box rotation")


def _validate_physical_json_numbers(document: Mapping[str, Any]) -> None:
    """Validate only consumed physical fields, using ORIGINAL JSON decimals.

    Legacy geometry in a lossless upgraded model remains legacy. Raw pre-route
    source intake has a separate explicit admission boundary.
    """
    endpoint_ids = set()
    for route in document.get("routes", ()):
        if not isinstance(route, Mapping) or route.get("geometry_kind") != "line_arc_v1":
            continue
        endpoint_ids.update((route.get("start_port_id"), route.get("end_port_id")))
        for joint in route.get("joints", ()):
            _raw_point(joint.get("position"), "physical joint")
        for segment in route.get("segments", ()):
            if segment.get("kind") == "quarter_arc":
                normal = segment.get("plane_normal", {})
                values = tuple(normal.get(axis) for axis in ("x", "y", "z"))
                if values not in ((1,0,0),(0,1,0),(0,0,1)):
                    raise ContractError("quarter arc normal must be an exact positive unit axis")
        requirements = route.get("requirements", {})
        for name, (low, high) in REQUIREMENT_BOUNDS.items():
            exact_ticks(requirements.get(name), name, minimum=low, maximum=high)
        context = route.get("context", {})
        for box in context.get("allowed_domain", {}).get("volumes", ()):
            _raw_box(box)
        exact_ticks(context.get("objective", {}).get("bend_penalty_m"), "bend penalty", minimum="0", maximum="1000")
    for port in document.get("ports", ()):
        if port.get("id") in endpoint_ids:
            _raw_point(port.get("pose", {}).get("position"), "physical endpoint port")
            direction = port.get("direction", {})
            values = tuple(direction.get(axis) for axis in ("x", "y", "z"))
            if values not in ((1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)):
                raise ContractError("physical port direction must be an exact signed unit axis")


def validate_physical_json_numbers(document: Mapping[str, Any]) -> None:
    try:
        _validate_physical_json_numbers(document)
    except (AttributeError, TypeError, KeyError) as exc:
        raise ContractError("malformed physical route JSON structure") from exc
