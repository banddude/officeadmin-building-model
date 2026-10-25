"""Bounded, derived mesh math for straight building-assembly pieces.

This module is deliberately below the semantic-model and preset layers. It
accepts only explicit metre dimensions and poses, preserves the caller's
profile ordering as input data, and returns deterministic local triangle meshes
plus derived analytic area/volume and bounds. It does not choose dimensions,
materials, construction rules, or claim that a mesh describes a manufacturer
product or compliant assembly.

The supported solid section is one simple closed 2D outline extruded along its
local +Z axis. Concave outlines (including C/U and T profiles) are supported;
inner rings, holes, curved sweeps, and general B-reps are not. Caps use the
shared deterministic ear-clip kernel, but unlike the display exporter this
strict constructor rejects invalid polygons instead of using a fan fallback.
Straight round wire meshes reuse the shared straight-cylinder primitive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Sequence, TypeAlias

from oabm.geometry_primitives import indexed_cylinder_mesh, plan_ear_triangles

Point2: TypeAlias = tuple[float, float]
Vertex3: TypeAlias = tuple[float, float, float]
Triangle: TypeAlias = tuple[int, int, int]
Bounds3: TypeAlias = tuple[float, float, float, float, float, float]
QuaternionXYWZ: TypeAlias = tuple[float, float, float, float]
PoseTuple: TypeAlias = tuple[Vertex3, QuaternionXYWZ]


class _Vector3Like(Protocol):
    x: float
    y: float
    z: float


class _QuaternionLike(Protocol):
    x: float
    y: float
    z: float
    w: float


class PoseLike(Protocol):
    """Structural canonical-pose input without a model-package dependency."""

    position: _Vector3Like
    rotation: _QuaternionLike


PoseInput: TypeAlias = PoseLike | PoseTuple

# The limits bound input scanning, triangulation work, and generated mesh size.
# They are resource ceilings, not design recommendations or typical sizes.
MAX_SECTION_VERTICES = 128
MIN_WIRE_SIDES = 6
MAX_WIRE_SIDES = 64
DEFAULT_WIRE_SIDES = 10  # same facet count as the existing GLB cylinder mesh
_GEOMETRY_EPSILON = 1e-12
_INTERSECTION_EPSILON = 1e-9  # keep the strict checks aligned with GLB ear clipping


class GeometryInputError(ValueError):
    """Raised when explicit geometry is invalid or outside the bounded kernel."""


@dataclass(frozen=True, slots=True)
class MeshPose:
    """Dependency-neutral rigid placement represented as position and xyzw tuples."""

    position_m: Vertex3
    rotation_xyzw: QuaternionXYWZ


@dataclass(frozen=True, slots=True)
class DerivedMesh:
    """One local triangle mesh and its explicit placement.

    ``vertices_m`` are local-frame metres; ``pose`` places that frame in the
    canonical right-handed +Z-up world. Bounds are min/max tuples in the same
    axis order, first in local coordinates and then in world coordinates.
    Section bounds follow the exact outline vertices. Round-wire bounds use
    the full analytic circular-cylinder envelope, not the inscribed mesh's
    vertex extrema. Area and volume are analytic cross-section quantities, not
    mass/material takeoff facts; the wire mesh is a polygonal approximation of
    its exact circular section.
    """

    vertices_m: tuple[Vertex3, ...]
    triangles: tuple[Triangle, ...]
    pose: MeshPose
    cross_section_area_m2: float
    volume_m3: float
    local_bounds_m: Bounds3
    world_bounds_m: Bounds3
    input_profile_xy_m: tuple[Point2, ...] | None = None


def extrude_simple_section_m(
    profile_xy_m: Sequence[Sequence[float]],
    length_m: float,
    pose: PoseInput,
) -> DerivedMesh:
    """Extrude a simple explicit 2D outline from local z=0 to ``length_m``.

    The mesh vertices are independent of ``pose``; its position is the world
    origin of the section at its local z=0 face, and its rotation
    rotates the local +Z extrusion axis. The original point order is retained
    in ``input_profile_xy_m`` while mesh ordering is canonicalized across
    cyclic shifts and reversed winding.

    Profile closure is implicit. Repeated endpoints, repeated vertices,
    self-crossing/touching edges, zero area, non-finite values, and resource
    overruns are rejected. A profile is one outline only; holes are refused.
    """

    points = _validate_profile(profile_xy_m)
    length = _positive_finite(length_m, "length_m")
    placement = _pose_components(pose)
    canonical = _canonical_ccw(points)
    area = _polygon_area(canonical)
    _ensure_finite(area, "cross_section_area_m2")
    if area <= _GEOMETRY_EPSILON:
        raise GeometryInputError("profile_xy_m must have non-zero area")
    volume = area * length
    _ensure_finite(volume, "volume_m3")
    if volume <= 0.0:
        raise GeometryInputError("volume_m3 underflowed to zero")

    # Triangulate relative to a local anchor. The shared GLB triangulator's
    # legacy implementation remains byte-identical; this strict path avoids
    # global-coordinate shoelace cancellation for translated profiles.
    anchor_x, anchor_y = canonical[0]
    relative = tuple((x - anchor_x, y - anchor_y) for x, y in canonical)
    cap_triangles = plan_ear_triangles(list(relative))
    if cap_triangles is None:
        raise GeometryInputError("profile_xy_m is not a triangulable simple outline")

    # The shared ear clipper removes harmless collinear vertices. Preserve
    # those facts in ``input_profile_xy_m``, but let the mesh omit them so no
    # degenerate side panel is emitted.
    used_points = sorted({index for triangle in cap_triangles for index in triangle})
    if len(used_points) < 3 or len(cap_triangles) != len(used_points) - 2:
        raise GeometryInputError("profile_xy_m is not a triangulable simple outline")
    try:
        cap_area = math.fsum(
            abs(_cross(relative[a], relative[b], relative[c])) / 2.0
            for a, b, c in cap_triangles
        )
    except (OverflowError, ValueError) as exc:
        raise GeometryInputError("profile cap area is outside the finite numeric range") from exc
    _ensure_finite(cap_area, "profile cap area")
    if not math.isclose(cap_area, area, rel_tol=1e-9, abs_tol=1e-12):
        raise GeometryInputError("profile cap triangulation does not cover its outline")
    remap = {old: new for new, old in enumerate(used_points)}
    mesh_profile = tuple(canonical[index] for index in used_points)
    cap_triangles = [tuple(remap[index] for index in triangle) for triangle in cap_triangles]

    count = len(mesh_profile)
    vertices: tuple[Vertex3, ...] = tuple(
        [(x, y, 0.0) for x, y in mesh_profile]
        + [(x, y, length) for x, y in mesh_profile]
    )
    faces: list[Triangle] = []
    # Canonical outline is CCW: top caps face +Z and bottom caps face -Z.
    for a, b, c in cap_triangles:
        faces.append((c, b, a))
        faces.append((count + a, count + b, count + c))
    # Two outward-facing triangles per rectangular side panel.
    for index in range(count):
        nxt = (index + 1) % count
        faces.append((index, nxt, count + nxt))
        faces.append((index, count + nxt, count + index))

    frozen_faces = tuple(faces)
    _check_mesh_limits(vertices, frozen_faces)
    _validate_triangle_areas(vertices, frozen_faces)
    local_bounds = _bounds(vertices)
    world_bounds = _bounds(tuple(_transform(vertex, placement) for vertex in vertices))
    return DerivedMesh(
        vertices_m=vertices,
        triangles=frozen_faces,
        pose=placement,
        cross_section_area_m2=area,
        volume_m3=volume,
        local_bounds_m=local_bounds,
        world_bounds_m=world_bounds,
        input_profile_xy_m=tuple(points),
    )


def straight_round_wire_m(
    start: _Vector3Like | Sequence[float],
    end: _Vector3Like | Sequence[float],
    radius_m: float,
    *,
    sides: int = DEFAULT_WIRE_SIDES,
) -> DerivedMesh:
    """Build one bounded straight circular wire mesh between explicit points.

    Geometry is returned in a local cylinder frame and posed from ``start`` to
    ``end``. Its strict indexed mesh is closed, non-degenerate, and outward
    wound; GLB retains its legacy triangle-soup primitive for byte compatibility.
    Bounds, disk area and cylinder volume describe the smooth circle, not its
    inscribed polygon mesh. This helper does not infer conductor gauge,
    material, bundling, or routing.
    """

    start_xyz = _vector3(start, "start")
    end_xyz = _vector3(end, "end")
    radius = _positive_finite(radius_m, "radius_m")
    _validate_sides(sides)
    axis = tuple(end_xyz[index] - start_xyz[index] for index in range(3))
    length = math.hypot(*axis)
    _ensure_finite(length, "wire length")
    if length <= _GEOMETRY_EPSILON:
        raise GeometryInputError("wire endpoints must define a non-zero length")

    rotation = _quaternion_xyzw(_rotation_from_local_z(axis, length), "wire pose.rotation")
    pose = MeshPose(position_m=start_xyz, rotation_xyzw=rotation)
    area = math.pi * radius * radius
    _ensure_finite(area, "cross_section_area_m2")
    if area <= 0.0:
        raise GeometryInputError("wire cross_section_area_m2 underflowed to zero")
    volume = area * length
    _ensure_finite(volume, "volume_m3")
    if volume <= 0.0:
        raise GeometryInputError("wire volume_m3 underflowed to zero")

    vertices, triangles = indexed_cylinder_mesh(radius, length, sides)
    _check_mesh_limits(vertices, triangles)
    _validate_triangle_areas(vertices, triangles)
    # An analytic radius bounds every angle, not just polygon vertices.
    local_bounds = (-radius, -radius, 0.0, radius, radius, length)
    unit_axis = tuple(component / length for component in axis)
    world_bounds = _cylinder_world_bounds(start_xyz, end_xyz, unit_axis, radius)
    return DerivedMesh(
        vertices_m=vertices,
        triangles=triangles,
        pose=pose,
        cross_section_area_m2=area,
        volume_m3=volume,
        local_bounds_m=local_bounds,
        world_bounds_m=world_bounds,
    )


def _validate_profile(raw: Sequence[Sequence[float]]) -> tuple[Point2, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise GeometryInputError("profile_xy_m must be a sequence of 2D points")
    if not 3 <= len(raw) <= MAX_SECTION_VERTICES:
        raise GeometryInputError(
            f"profile_xy_m must contain 3..{MAX_SECTION_VERTICES} vertices"
        )
    points: list[Point2] = []
    for index, point in enumerate(raw):
        if isinstance(point, (str, bytes)) or not isinstance(point, Sequence) or len(point) != 2:
            raise GeometryInputError(f"profile_xy_m[{index}] must be an (x, y) pair")
        x = _finite_number(point[0], f"profile_xy_m[{index}].x")
        y = _finite_number(point[1], f"profile_xy_m[{index}].y")
        points.append((x, y))
    if len(set(points)) != len(points):
        raise GeometryInputError("profile_xy_m cannot repeat a vertex")
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        if math.dist(point, nxt) <= _GEOMETRY_EPSILON:
            raise GeometryInputError("profile_xy_m has a zero-length edge")
    if _has_nonadjacent_intersection(points):
        raise GeometryInputError("profile_xy_m must be a simple, non-touching outline")
    area = _polygon_area(points)
    _ensure_finite(area, "profile signed area")
    if abs(area) <= _GEOMETRY_EPSILON:
        raise GeometryInputError("profile_xy_m must have non-zero area")
    for index, current in enumerate(points):
        previous = points[index - 1]
        following = points[(index + 1) % len(points)]
        if (
            abs(_cross(previous, current, following)) <= _INTERSECTION_EPSILON
            and (previous[0] - current[0]) * (following[0] - current[0])
            + (previous[1] - current[1]) * (following[1] - current[1]) > 0.0
        ):
            raise GeometryInputError("profile_xy_m cannot backtrack along an overlapping edge")
    return tuple(points)


def _has_nonadjacent_intersection(points: Sequence[Point2]) -> bool:
    count = len(points)
    eps = _INTERSECTION_EPSILON
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or j == (i + 1) % count or (j + 1) % count == i:
                continue  # consecutive edges share their authored endpoint
            c, d = points[j], points[(j + 1) % count]
            if (
                max(a[0], b[0]) + eps < min(c[0], d[0])
                or max(c[0], d[0]) + eps < min(a[0], b[0])
                or max(a[1], b[1]) + eps < min(c[1], d[1])
                or max(c[1], d[1]) + eps < min(a[1], b[1])
            ):
                continue
            o1, o2 = _cross(a, b, c), _cross(a, b, d)
            o3, o4 = _cross(c, d, a), _cross(c, d, b)
            if (
                ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps))
                and ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps))
            ):
                return True
            if (
                (abs(o1) <= eps and _on_segment(a, b, c, eps))
                or (abs(o2) <= eps and _on_segment(a, b, d, eps))
                or (abs(o3) <= eps and _on_segment(c, d, a, eps))
                or (abs(o4) <= eps and _on_segment(c, d, b, eps))
            ):
                return True
    return False


def _on_segment(a: Point2, b: Point2, p: Point2, eps: float) -> bool:
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _canonical_ccw(points: Sequence[Point2]) -> tuple[Point2, ...]:
    ordered = list(points)
    if _polygon_area(ordered) < 0.0:
        ordered.reverse()
    start = min(range(len(ordered)), key=lambda index: ordered[index])
    return tuple(ordered[start:] + ordered[:start])


def _polygon_area(points: Sequence[Point2]) -> float:
    anchor_x, anchor_y = points[0]
    try:
        return math.fsum(
            (points[index][0] - anchor_x)
            * (points[(index + 1) % len(points)][1] - anchor_y)
            - (points[(index + 1) % len(points)][0] - anchor_x)
            * (points[index][1] - anchor_y)
            for index in range(len(points))
        ) / 2.0
    except (OverflowError, ValueError) as exc:
        raise GeometryInputError("profile_xy_m area is outside the finite numeric range") from exc


def _cross(a: Point2, b: Point2, c: Point2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _positive_finite(value: float, label: str) -> float:
    result = _finite_number(value, label)
    if result <= 0.0:
        raise GeometryInputError(f"{label} must be > 0")
    return result


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryInputError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise GeometryInputError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise GeometryInputError(f"{label} must be a finite number")
    return result


def _ensure_finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise GeometryInputError(f"{label} must remain finite")


def _vector3(value: object, label: str) -> Vertex3:
    if all(hasattr(value, attribute) for attribute in ("x", "y", "z")):
        raw = (getattr(value, "x"), getattr(value, "y"), getattr(value, "z"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 3:
        raw = (value[0], value[1], value[2])
    else:
        raise GeometryInputError(f"{label} must be a 3D point or 3-value tuple")
    return tuple(_finite_number(component, label) for component in raw)  # type: ignore[return-value]


def _quaternion_xyzw(value: object, label: str) -> QuaternionXYWZ:
    if all(hasattr(value, attribute) for attribute in ("x", "y", "z", "w")):
        raw = tuple(getattr(value, attribute) for attribute in ("x", "y", "z", "w"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
        raw = tuple(value)
    else:
        raise GeometryInputError(f"{label} must be an xyzw quaternion")
    quaternion = tuple(_finite_number(component, label) for component in raw)
    norm = math.hypot(*quaternion)
    if not math.isfinite(norm) or norm == 0.0 or abs(norm - 1.0) > 1e-5:
        raise GeometryInputError(f"{label} must be normalized")
    return tuple(component / norm for component in quaternion)  # type: ignore[return-value]


def _pose_components(pose: PoseInput) -> MeshPose:
    if isinstance(pose, Sequence) and not isinstance(pose, (str, bytes)) and len(pose) == 2:
        position, rotation = pose
    else:
        position, rotation = getattr(pose, "position", None), getattr(pose, "rotation", None)
    return MeshPose(
        position_m=_vector3(position, "pose.position"),
        rotation_xyzw=_quaternion_xyzw(rotation, "pose.rotation"),
    )


def _validate_sides(sides: int) -> None:
    if isinstance(sides, bool) or not isinstance(sides, int) or not MIN_WIRE_SIDES <= sides <= MAX_WIRE_SIDES:
        raise GeometryInputError(
            f"sides must be an integer in {MIN_WIRE_SIDES}..{MAX_WIRE_SIDES}"
        )


def _rotation_from_local_z(axis: tuple[float, float, float], length: float) -> QuaternionXYWZ:
    dx, dy, dz = (component / length for component in axis)
    # The shortest rotation axis is +Z cross direction. atan2 remains stable
    # when dz is nearly -1, unlike the usual 1+dot quaternion shortcut.
    cross_norm = math.hypot(dx, dy)
    if cross_norm == 0.0:
        return (1.0, 0.0, 0.0, 0.0) if dz < 0.0 else (0.0, 0.0, 0.0, 1.0)
    half_angle = 0.5 * math.atan2(cross_norm, dz)
    sine = math.sin(half_angle)
    return (-dy / cross_norm * sine, dx / cross_norm * sine,
            0.0, math.cos(half_angle))


def _transform(vertex: Vertex3, pose: MeshPose) -> Vertex3:
    x, y, z = vertex
    qx, qy, qz, qw = pose.rotation_xyzw
    tx = 2.0 * (qy * z - qz * y)
    ty = 2.0 * (qz * x - qx * z)
    tz = 2.0 * (qx * y - qy * x)
    result = (
        pose.position_m[0] + x + qw * tx + qy * tz - qz * ty,
        pose.position_m[1] + y + qw * ty + qz * tx - qx * tz,
        pose.position_m[2] + z + qw * tz + qx * ty - qy * tx,
    )
    for component in result:
        _ensure_finite(component, "posed mesh coordinate")
    return result


def _cylinder_world_bounds(
    start: Vertex3, end: Vertex3, unit_axis: Vertex3, radius: float,
) -> Bounds3:
    """Conservative exact-cylinder AABB from endpoint circle projections."""

    lows: list[float] = []
    highs: list[float] = []
    for index in range(3):
        component = min(1.0, abs(unit_axis[index]))
        radial_extent = radius * math.sqrt(max(0.0, 1.0 - component * component))
        coordinate_scale = max(1.0, abs(start[index]), abs(end[index]), radius)
        roundoff_pad = 4.0 * math.ulp(coordinate_scale)
        low = min(start[index], end[index]) - radial_extent - roundoff_pad
        high = max(start[index], end[index]) + radial_extent + roundoff_pad
        _ensure_finite(low, "wire lower bound")
        _ensure_finite(high, "wire upper bound")
        lows.append(low)
        highs.append(high)
    return (*lows, *highs)


def _bounds(vertices: Sequence[Vertex3]) -> Bounds3:
    if not vertices:
        raise GeometryInputError("mesh must contain vertices")
    return (
        min(vertex[0] for vertex in vertices),
        min(vertex[1] for vertex in vertices),
        min(vertex[2] for vertex in vertices),
        max(vertex[0] for vertex in vertices),
        max(vertex[1] for vertex in vertices),
        max(vertex[2] for vertex in vertices),
    )


def _check_mesh_limits(vertices: Sequence[Vertex3], triangles: Sequence[Triangle]) -> None:
    if not vertices or not triangles:
        raise GeometryInputError("mesh must contain vertices and triangles")
    if len(vertices) > MAX_SECTION_VERTICES * 12:
        raise GeometryInputError("mesh exceeds the bounded vertex limit")
    if len(triangles) > MAX_SECTION_VERTICES * 12:
        raise GeometryInputError("mesh exceeds the bounded triangle limit")
    for triangle in triangles:
        if len(triangle) != 3 or any(index < 0 or index >= len(vertices) for index in triangle):
            raise GeometryInputError("mesh contains an invalid triangle index")


def _validate_triangle_areas(
    vertices: Sequence[Vertex3], triangles: Sequence[Triangle],
) -> None:
    """Refuse triangles that numerically collapse or overflow their area."""

    for triangle in triangles:
        a, b, c = (vertices[index] for index in triangle)
        ab = tuple(b[index] - a[index] for index in range(3))
        ac = tuple(c[index] - a[index] for index in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        twice_area = math.hypot(*cross)
        _ensure_finite(twice_area, "triangle area")
        if twice_area <= 0.0:
            raise GeometryInputError("mesh contains a degenerate triangle")


__all__ = [
    "Bounds3",
    "DEFAULT_WIRE_SIDES",
    "DerivedMesh",
    "GeometryInputError",
    "MAX_SECTION_VERTICES",
    "MAX_WIRE_SIDES",
    "MIN_WIRE_SIDES",
    "MeshPose",
    "PoseInput",
    "PoseLike",
    "PoseTuple",
    "Triangle",
    "Vertex3",
    "extrude_simple_section_m",
    "straight_round_wire_m",
]
