"""Dependency-neutral deterministic 2D/3D mesh primitives.

This module contains only bounded reusable math shared by exporters and
assembly geometry. It imports no semantic model, renderer, or optional
geometry backend. Its implementation is factored from the established GLB
export helpers so existing output ordering and bytes stay unchanged.
"""

from __future__ import annotations

import math
from typing import Protocol, TypeAlias

Point2: TypeAlias = tuple[float, float]
Point3Tuple: TypeAlias = tuple[float, float, float]


class Point3Like(Protocol):
    """Structural point input accepted without a model-package dependency."""

    x: float
    y: float
    z: float


Point3Input: TypeAlias = Point3Like | Point3Tuple


#: Plan tolerance for footprint winding tests: square metres when it weighs a
#: turn, metres when it weighs a duplicate vertex. Far below any drawing
#: intent, far above float noise at building scale.
_PLAN_EPSILON = 1e-9


def _plan_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float:
    """Twice the signed area of plan triangle ``a``-``b``-``c`` (CCW: > 0)."""

    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _plan_ring_area(
    plan: list[tuple[float, float]],
    ring: list[int],
) -> float:
    """Twice the signed area of a plan ring (CCW: > 0)."""

    return math.fsum(
        plan[ring[position]][0] * plan[ring[(position + 1) % len(ring)]][1]
        - plan[ring[(position + 1) % len(ring)]][0] * plan[ring[position]][1]
        for position in range(len(ring))
    )


def _plan_is_convex(plan: list[tuple[float, float]]) -> bool:
    """True when every turn has the same sign, collinear vertices ignored."""

    sign = 0
    for index in range(len(plan)):
        turn = _plan_cross(plan[index - 1], plan[index], plan[(index + 1) % len(plan)])
        if abs(turn) <= _PLAN_EPSILON:
            continue
        turn_sign = 1 if turn > 0.0 else -1
        if sign == 0:
            sign = turn_sign
        elif sign != turn_sign:
            return False
    return sign != 0


def _plan_same_point(
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """True when two plan points coincide within ``_PLAN_EPSILON``."""

    return (
        abs(a[0] - b[0]) <= _PLAN_EPSILON and abs(a[1] - b[1]) <= _PLAN_EPSILON
    )


def _plan_point_on_segment(
    a: tuple[float, float],
    b: tuple[float, float],
    point: tuple[float, float],
) -> bool:
    """True when ``point`` touches segment ``a``-``b`` within ``_PLAN_EPSILON``."""

    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length <= _PLAN_EPSILON:
        return False
    if abs(_plan_cross(a, b, point)) / length > _PLAN_EPSILON:
        return False
    along = (point[0] - a[0]) * (b[0] - a[0]) + (point[1] - a[1]) * (b[1] - a[1])
    return -_PLAN_EPSILON * length <= along <= length * length + _PLAN_EPSILON * length


def _plan_is_ear(
    plan: list[tuple[float, float]],
    ring: list[int],
    position: int,
) -> bool:
    """True when ``ring[position]`` is an ear tip of the CCW ring."""

    previous = plan[ring[position - 1]]
    current = plan[ring[position]]
    following = plan[ring[(position + 1) % len(ring)]]
    if _plan_cross(previous, current, following) <= _PLAN_EPSILON:
        return False  # reflex corner, or collinear float noise
    corner = (ring[position - 1], ring[position], ring[(position + 1) % len(ring)])
    for index in ring:
        if index in corner:
            continue
        point = plan[index]
        if any(_plan_same_point(point, other) for other in (previous, current, following)):
            # A seam can put a second ring entry on the very spot of an ear
            # corner; it is the same point of the polygon, so it never blocks.
            continue
        if (
            _plan_cross(previous, current, point) >= -_PLAN_EPSILON
            and _plan_cross(current, following, point) >= -_PLAN_EPSILON
            and _plan_cross(following, previous, point) >= -_PLAN_EPSILON
        ):
            return False
    return True


def _plan_clean_ring(
    plan: list[tuple[float, float]],
    ring: list[int],
) -> list[int] | None:
    """Drop consecutive duplicate and collinear vertices until stable, so
    every candidate ear corner is a well-defined turn."""

    while True:
        if len(ring) < 3:
            return None
        kept: list[int] = []
        changed = False
        for position in range(len(ring)):
            index = ring[position]
            previous = plan[ring[position - 1]]
            current = plan[index]
            following = plan[ring[(position + 1) % len(ring)]]
            duplicate = (
                abs(current[0] - previous[0]) <= _PLAN_EPSILON
                and abs(current[1] - previous[1]) <= _PLAN_EPSILON
            )
            if duplicate or abs(_plan_cross(previous, current, following)) <= _PLAN_EPSILON:
                changed = True
                continue
            kept.append(index)
        if not changed:
            return kept
        ring = kept


def _plan_seam_subrings(
    plan: list[tuple[float, float]],
    ring: list[int],
) -> list[list[int]] | None:
    """Split a self-touching ring at its first seam, or None when it is simple.

    A seam is a vertex that touches another vertex of the same ring, or lies
    on another edge of the same ring, within ``_PLAN_EPSILON``; adjacent
    positions never count because the cleanup already removed those. Both
    sub-rings still index ``plan``, and their caps together cover exactly the
    footprint: an edge touch is split by re-using the touching vertex's own
    index, so no new coordinate is ever invented.
    """

    count = len(ring)
    for first in range(count):
        for second in range(first + 1, count):
            if second - first == 1 or (first == 0 and second == count - 1):
                continue  # adjacent positions share an edge, not a seam
            if _plan_same_point(plan[ring[first]], plan[ring[second]]):
                return [ring[first:second], ring[second:] + ring[:first]]
    for position in range(count):
        point = plan[ring[position]]
        for start in range(count):
            end = (start + 1) % count
            if position in (start, end):
                continue  # an edge never seams with its own endpoints
            if not _plan_point_on_segment(plan[ring[start]], plan[ring[end]], point):
                continue
            split = ring[:end] + [ring[position]] + ring[end:]
            if position < end:
                first, second = position, end
            else:
                first, second = end, position + 1
            return [split[first:second], split[second:] + split[:first]]
    return None


def _plan_clip_ring(
    plan: list[tuple[float, float]],
    ring: list[int],
) -> list[tuple[int, int, int]] | None:
    """Deterministic ear clipping of an already cleaned CCW ring."""

    triangles: list[tuple[int, int, int]] = []
    while len(ring) > 3:
        # Lowest remaining index first, so the clip order is deterministic.
        start = ring.index(min(ring))
        clipped = False
        for step in range(len(ring)):
            position = (start + step) % len(ring)
            if _plan_is_ear(plan, ring, position):
                triangles.append((
                    ring[position - 1],
                    ring[position],
                    ring[(position + 1) % len(ring)],
                ))
                del ring[position]
                clipped = True
                break
        if not clipped:
            return None
    triangles.append((ring[0], ring[1], ring[2]))
    return triangles


def _plan_ring_triangles(
    plan: list[tuple[float, float]],
    ring: list[int],
) -> list[tuple[int, int, int]] | None:
    """Ear-clip one plan ring, splitting it at self-touching seams first.

    Each seam split shrinks the rings, so the recursion is bounded. A
    sub-ring wound against the split's parent is a hole behind a bridge
    seam, not floor: it is never filled, and the whole ring reports
    failure so the caller falls back to the fan. The same happens when a
    sub-ring is degenerate or still cannot be clipped.
    """

    ring = _plan_clean_ring(plan, ring)
    if ring is None:
        return None
    area = _plan_ring_area(plan, ring)
    if abs(area) <= 2.0 * _PLAN_EPSILON:
        return None
    if area < 0.0:
        ring.reverse()
    subrings = _plan_seam_subrings(plan, ring)
    if subrings is not None:
        triangles: list[tuple[int, int, int]] = []
        for subring in subrings:
            sub_area = _plan_ring_area(plan, subring)
            if abs(sub_area) <= 2.0 * _PLAN_EPSILON:
                return None  # a collapsed sliver from the split
            if sub_area < 0.0:
                # The parent ring is CCW here, so a negative sub-ring is
                # wound against it: the split walked around a hole and
                # isolated it. Filling it would cover the hole and count
                # its area twice; fail closed to the disclosed fan.
                return None
            sub_triangles = _plan_ring_triangles(plan, subring)
            if sub_triangles is None:
                return None
            triangles.extend(sub_triangles)
        return triangles
    return _plan_clip_ring(plan, ring)


def _plan_ear_triangles(
    plan: list[tuple[float, float]],
) -> list[tuple[int, int, int]] | None:
    """Deterministic ear-clip triangulation of a plan ring, or None.

    Returns index triangles in CCW order. A keyhole ring — one that touches
    itself at a seam, through a repeated vertex or a vertex lying on another
    edge — is split at the seam into simple sub-rings that are ear-clipped
    independently. A split that isolates a sub-ring wound against its parent
    has found a hole behind a bridge seam; the hole is never filled, and the
    ring reports failure like any other unclippable footprint. Returns None
    when the footprint is degenerate or self-intersecting so badly that no
    ear can be clipped; the caller then falls back to the fan instead of
    raising.
    """

    return _plan_ring_triangles(plan, list(range(len(plan))))


def segment_frame(
    start: Point3Input, end: Point3Input
) -> tuple[float, float, float, float, float, float, float, float, float] | None:
    """Deterministic orthonormal segment frame ``(d, u, v)``, or None.

    Zero-length segments have no direction, so they cannot carry a tube. The
    basis picks the reference cardinal axis least aligned with the segment
    direction, the same rule conduit tubes and wire offsets share.
    """

    start_xyz, end_xyz = _point_xyz(start), _point_xyz(end)
    axis = tuple(end_xyz[index] - start_xyz[index] for index in range(3))
    length = math.sqrt(sum(component * component for component in axis))
    if length <= 1e-12:
        return None
    dx, dy, dz = (component / length for component in axis)
    ref = (1.0, 0.0, 0.0) if abs(dx) < 0.9 else (0.0, 1.0, 0.0)
    ux = ref[1] * dz - ref[2] * dy
    uy = ref[2] * dx - ref[0] * dz
    uz = ref[0] * dy - ref[1] * dx
    u_norm = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux, uy, uz = ux / u_norm, uy / u_norm, uz / u_norm
    vx, vy, vz = dy * uz - dz * uy, dz * ux - dx * uz, dx * uy - dy * ux
    return (dx, dy, dz, ux, uy, uz, vx, vy, vz)


def cylinder_vertices(
    start: Point3Input, end: Point3Input, radius: float, sides: int
) -> list[tuple[float, float, float]]:
    """Cylinder between two world-space points (canonical, +Z up)."""

    start_xyz, end_xyz = _point_xyz(start), _point_xyz(end)
    frame = segment_frame(start_xyz, end_xyz)
    if frame is None:
        return []
    dx, dy, dz, ux, uy, uz, vx, vy, vz = frame

    def ring(point: Point3Tuple) -> list[tuple[float, float, float]]:
        return [
            (
                point[0] + radius * (math.cos(2.0 * math.pi * i / sides) * ux
                                     + math.sin(2.0 * math.pi * i / sides) * vx),
                point[1] + radius * (math.cos(2.0 * math.pi * i / sides) * uy
                                     + math.sin(2.0 * math.pi * i / sides) * vy),
                point[2] + radius * (math.cos(2.0 * math.pi * i / sides) * uz
                                     + math.sin(2.0 * math.pi * i / sides) * vz),
            )
            for i in range(sides)
        ]

    bottom = ring(start_xyz)
    top = ring(end_xyz)
    vertices: list[tuple[float, float, float]] = []
    for index in range(sides):
        nxt = (index + 1) % sides
        vertices += [
            bottom[index], bottom[nxt], top[nxt],
            bottom[index], top[nxt], top[index],
        ]
    for index in range(sides):
        nxt = (index + 1) % sides
        vertices += [bottom[index], bottom[nxt], bottom[0]]
        vertices += [top[index], top[0], top[nxt]]
    return vertices




def indexed_cylinder_mesh(
    radius: float, height: float, sides: int,
) -> tuple[tuple[Point3Tuple, ...], tuple[tuple[int, int, int], ...]]:
    """Indexed, watertight +Z cylinder with outward caps and side faces.

    Unlike :func:`cylinder_vertices`, which preserves the historical GLB
    triangle-soup byte contract, this primitive shares ring vertices across
    faces so strict geometry callers can validate closed-manifold topology.
    Dimensions are finite, positive, and capped before allocating the mesh.
    """

    if (
        isinstance(radius, bool)
        or not isinstance(radius, (int, float))
        or not math.isfinite(radius)
        or radius <= 0.0
    ):
        raise ValueError("radius must be finite and > 0")
    if (
        isinstance(height, bool)
        or not isinstance(height, (int, float))
        or not math.isfinite(height)
        or height <= 0.0
    ):
        raise ValueError("height must be finite and > 0")
    if isinstance(sides, bool) or not isinstance(sides, int) or not 3 <= sides <= 64:
        raise ValueError("sides must be an integer in 3..64")
    ring = tuple(
        (radius * math.cos(2.0 * math.pi * index / sides),
         radius * math.sin(2.0 * math.pi * index / sides))
        for index in range(sides)
    )
    bottom = tuple((x, y, 0.0) for x, y in ring)
    top = tuple((x, y, height) for x, y in ring)
    vertices = bottom + top
    triangles: list[tuple[int, int, int]] = []
    # Bottom cap faces -Z; top cap faces +Z. The convex regular rings need no
    # general triangulator and the fan diagonals are deterministic.
    for index in range(1, sides - 1):
        triangles.append((0, index + 1, index))
        triangles.append((sides, sides + index, sides + index + 1))
    # Each side quad is split with outward winding and shared indexed edges.
    for index in range(sides):
        nxt = (index + 1) % sides
        triangles.append((index, nxt, sides + nxt))
        triangles.append((index, sides + nxt, sides + index))
    return vertices, tuple(triangles)


# Public neutral names. The GLB module re-exports these under its historical
# private names, preserving existing imports while keeping the primitive owner
# below the export layer.
plan_is_convex = _plan_is_convex
plan_ear_triangles = _plan_ear_triangles


def _point_xyz(point: Point3Input) -> Point3Tuple:
    """Read either a numeric tuple or a structural ``x/y/z`` point."""

    if isinstance(point, tuple):
        return point
    return (point.x, point.y, point.z)


__all__ = [
    "Point2",
    "Point3Input",
    "Point3Like",
    "Point3Tuple",
    "cylinder_vertices",
    "indexed_cylinder_mesh",
    "plan_ear_triangles",
    "plan_is_convex",
    "segment_frame",
]
