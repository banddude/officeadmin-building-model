from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from oabm.model import Box3D, Point3, Polygon3D, Polyline3D, Pose, Quaternion, Vector3

from .model import Bounds2, Point2

_EPS = 1e-9


def dot(a: Vector3, b: Vector3) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def cross(a: Vector3, b: Vector3) -> Vector3:
    return Vector3(
        x=a.y * b.z - a.z * b.y,
        y=a.z * b.x - a.x * b.z,
        z=a.x * b.y - a.y * b.x,
    )


def normalize(v: Vector3) -> Vector3:
    length = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    if length <= _EPS:
        raise ValueError("view axis must be non-zero")
    return Vector3(x=v.x / length, y=v.y / length, z=v.z / length)


def sub_point(a: Point3, b: Point3) -> Vector3:
    return Vector3(x=a.x - b.x, y=a.y - b.y, z=a.z - b.z)


def add_point(a: Point3, v: Vector3) -> Point3:
    return Point3(x=a.x + v.x, y=a.y + v.y, z=a.z + v.z)


def scale(v: Vector3, amount: float) -> Vector3:
    return Vector3(x=v.x * amount, y=v.y * amount, z=v.z * amount)


@dataclass(frozen=True, slots=True)
class ProjectionFrame:
    origin: Point3
    right: Vector3
    up: Vector3
    normal: Vector3

    def __post_init__(self) -> None:
        right = normalize(self.right)
        up = normalize(self.up)
        normal = normalize(self.normal)
        if abs(dot(right, up)) > 1e-6 or abs(dot(right, normal)) > 1e-6 or abs(dot(up, normal)) > 1e-6:
            raise ValueError("projection frame axes must be orthogonal")
        handed = dot(cross(right, up), normal)
        if abs(abs(handed) - 1.0) > 1e-6:
            raise ValueError("projection frame axes must form an orthonormal basis")
        object.__setattr__(self, "right", right)
        object.__setattr__(self, "up", up)
        object.__setattr__(self, "normal", normal)

    def project(self, point: Point3) -> tuple[Point2, float]:
        relative = sub_point(point, self.origin)
        return Point2(x=dot(relative, self.right), y=dot(relative, self.up)), dot(relative, self.normal)


def frame_from_view_direction(*, origin: Point3, direction: Vector3, up: Vector3 = Vector3(x=0, y=0, z=1)) -> ProjectionFrame:
    """Create an elevation/section frame looking along ``direction`` toward the model."""
    normal = normalize(direction)
    up_normalized = normalize(up)
    if abs(dot(normal, up_normalized)) > 1 - 1e-6:
        raise ValueError("view direction cannot be parallel to up")
    right = normalize(cross(up_normalized, normal))
    corrected_up = normalize(cross(normal, right))
    return ProjectionFrame(origin=origin, right=right, up=corrected_up, normal=normal)


def rotate_vector(q: Quaternion, v: Vector3) -> Vector3:
    """Rotate a vector by a normalized quaternion without external geometry deps."""
    # q * v * q^-1, expanded.
    qv = Vector3(x=q.x, y=q.y, z=q.z)
    uv = cross(qv, v)
    uuv = cross(qv, uv)
    return Vector3(
        x=v.x + 2.0 * (q.w * uv.x + uuv.x),
        y=v.y + 2.0 * (q.w * uv.y + uuv.y),
        z=v.z + 2.0 * (q.w * uv.z + uuv.z),
    )


def box_corners(box: Box3D) -> tuple[Point3, ...]:
    return oriented_box_corners(box.pose, box.size.x, box.size.y, box.size.z)


def oriented_box_corners(pose: Pose, size_x: float, size_y: float, size_z: float) -> tuple[Point3, ...]:
    axes = (
        rotate_vector(pose.rotation, Vector3(x=1, y=0, z=0)),
        rotate_vector(pose.rotation, Vector3(x=0, y=1, z=0)),
        rotate_vector(pose.rotation, Vector3(x=0, y=0, z=1)),
    )
    half = (size_x / 2.0, size_y / 2.0, size_z / 2.0)
    corners: list[Point3] = []
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                offset = Vector3(x=0, y=0, z=0)
                for axis, amount in zip(axes, (sx * half[0], sy * half[1], sz * half[2])):
                    offset = Vector3(
                        x=offset.x + axis.x * amount,
                        y=offset.y + axis.y * amount,
                        z=offset.z + axis.z * amount,
                    )
                corners.append(add_point(pose.position, offset))
    return tuple(corners)


def project_points(frame: ProjectionFrame, points: Iterable[Point3]) -> tuple[tuple[Point2, ...], tuple[float, ...]]:
    pairs = tuple(frame.project(point) for point in points)
    return tuple(item[0] for item in pairs), tuple(item[1] for item in pairs)


def geometry_points(geometry: Box3D | Polyline3D | Polygon3D) -> tuple[Point3, ...]:
    if isinstance(geometry, Box3D):
        return box_corners(geometry)
    return geometry.points


def depth_range(frame: ProjectionFrame, points: Iterable[Point3]) -> tuple[float, float]:
    depths = [frame.project(point)[1] for point in points]
    if not depths:
        return (0.0, 0.0)
    return min(depths), max(depths)


def _interpolate_point3(start: Point3, end: Point3, t: float) -> Point3:
    return Point3(
        x=start.x + (end.x - start.x) * t,
        y=start.y + (end.y - start.y) * t,
        z=start.z + (end.z - start.z) * t,
    )


def _point3_close(a: Point3, b: Point3, *, tolerance: float = _EPS) -> bool:
    return (
        abs(a.x - b.x) <= tolerance
        and abs(a.y - b.y) <= tolerance
        and abs(a.z - b.z) <= tolerance
    )


def clip_segment_depth(
    start: Point3,
    end: Point3,
    frame: ProjectionFrame,
    min_depth: float,
    max_depth: float,
) -> tuple[Point3, Point3] | None:
    """Clip a 3D segment to a projection frame's finite depth interval.

    Clipping happens in canonical 3D before 2D projection.  This prevents an
    entity that merely touches a plan/section depth range from contributing
    unrelated out-of-range portions of the same segment.
    """
    if min_depth > max_depth:
        raise ValueError("min_depth must be <= max_depth")

    _, start_depth = frame.project(start)
    _, end_depth = frame.project(end)
    delta = end_depth - start_depth

    if abs(delta) <= _EPS:
        if start_depth < min_depth - _EPS or start_depth > max_depth + _EPS:
            return None
        return start, end

    t_at_min = (min_depth - start_depth) / delta
    t_at_max = (max_depth - start_depth) / delta
    enter = max(0.0, min(t_at_min, t_at_max))
    leave = min(1.0, max(t_at_min, t_at_max))
    if enter > leave + _EPS:
        return None

    # Clamp tiny boundary noise so independently clipped neighboring segments
    # stitch deterministically.
    enter = min(1.0, max(0.0, enter))
    leave = min(1.0, max(0.0, leave))
    return _interpolate_point3(start, end, enter), _interpolate_point3(start, end, leave)


def clip_polyline_depth(
    points: tuple[Point3, ...],
    frame: ProjectionFrame,
    min_depth: float,
    max_depth: float,
) -> tuple[tuple[Point3, ...], ...]:
    """Clip a canonical 3D polyline into contiguous depth-visible fragments."""
    if len(points) < 2:
        return ()
    if min_depth > max_depth:
        raise ValueError("min_depth must be <= max_depth")

    fragments: list[list[Point3]] = []
    current: list[Point3] = []
    for start, end in zip(points, points[1:]):
        clipped = clip_segment_depth(start, end, frame, min_depth, max_depth)
        if clipped is None:
            if len(current) >= 2:
                fragments.append(current)
            current = []
            continue

        a, b = clipped
        if not current:
            current = [a, b]
        elif _point3_close(current[-1], a):
            if not _point3_close(current[-1], b):
                current.append(b)
        else:
            if len(current) >= 2:
                fragments.append(current)
            current = [a, b]

    if len(current) >= 2:
        fragments.append(current)

    return tuple(tuple(fragment) for fragment in fragments)


def clip_segment(start: Point2, end: Point2, bounds: Bounds2) -> tuple[Point2, Point2] | None:
    """Liang-Barsky segment clipping, deterministic at boundaries."""
    dx = end.x - start.x
    dy = end.y - start.y
    p = (-dx, dx, -dy, dy)
    q = (
        start.x - bounds.min_x,
        bounds.max_x - start.x,
        start.y - bounds.min_y,
        bounds.max_y - start.y,
    )
    u1 = 0.0
    u2 = 1.0
    for pi, qi in zip(p, q):
        if abs(pi) <= _EPS:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            if t > u2:
                return None
            u1 = max(u1, t)
        else:
            if t < u1:
                return None
            u2 = min(u2, t)
    return (
        Point2(x=start.x + u1 * dx, y=start.y + u1 * dy),
        Point2(x=start.x + u2 * dx, y=start.y + u2 * dy),
    )


def clip_polyline(points: tuple[Point2, ...], bounds: Bounds2) -> tuple[tuple[Point2, ...], ...]:
    """Clip a polyline into visible contiguous fragments."""
    if len(points) < 2:
        return ()
    fragments: list[list[Point2]] = []
    current: list[Point2] = []
    for start, end in zip(points, points[1:]):
        clipped = clip_segment(start, end, bounds)
        if clipped is None:
            if len(current) >= 2:
                fragments.append(current)
            current = []
            continue
        a, b = clipped
        if not current:
            current = [a, b]
        elif current[-1] == a:
            if current[-1] != b:
                current.append(b)
        else:
            if len(current) >= 2:
                fragments.append(current)
            current = [a, b]
    if len(current) >= 2:
        fragments.append(current)
    return tuple(tuple(fragment) for fragment in fragments)


def clip_polygon(points: tuple[Point2, ...], bounds: Bounds2) -> tuple[Point2, ...]:
    """Sutherland-Hodgman clipping against an axis-aligned rectangle."""
    if len(points) < 3:
        return ()

    def clip_edge(vertices: list[Point2], inside, intersect) -> list[Point2]:
        if not vertices:
            return []
        output: list[Point2] = []
        previous = vertices[-1]
        previous_inside = inside(previous)
        for current in vertices:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current))
            previous = current
            previous_inside = current_inside
        return output

    def vertical(a: Point2, b: Point2, x: float) -> Point2:
        if abs(b.x - a.x) <= _EPS:
            return Point2(x=x, y=a.y)
        t = (x - a.x) / (b.x - a.x)
        return Point2(x=x, y=a.y + t * (b.y - a.y))

    def horizontal(a: Point2, b: Point2, y: float) -> Point2:
        if abs(b.y - a.y) <= _EPS:
            return Point2(x=a.x, y=y)
        t = (y - a.y) / (b.y - a.y)
        return Point2(x=a.x + t * (b.x - a.x), y=y)

    vertices = list(points)
    vertices = clip_edge(vertices, lambda p: p.x >= bounds.min_x - _EPS, lambda a, b: vertical(a, b, bounds.min_x))
    vertices = clip_edge(vertices, lambda p: p.x <= bounds.max_x + _EPS, lambda a, b: vertical(a, b, bounds.max_x))
    vertices = clip_edge(vertices, lambda p: p.y >= bounds.min_y - _EPS, lambda a, b: horizontal(a, b, bounds.min_y))
    vertices = clip_edge(vertices, lambda p: p.y <= bounds.max_y + _EPS, lambda a, b: horizontal(a, b, bounds.max_y))
    # Remove consecutive duplicates introduced when a vertex lies exactly on an edge.
    compact: list[Point2] = []
    for point in vertices:
        if not compact or point != compact[-1]:
            compact.append(point)
    if len(compact) > 1 and compact[0] == compact[-1]:
        compact.pop()
    return tuple(compact) if len(compact) >= 3 else ()


def section_intersection_polyline(
    points: tuple[Point3, ...], frame: ProjectionFrame, *, tolerance: float = 1e-9
) -> tuple[Point2, ...]:
    """Intersect a 3D polyline with the frame's depth=0 section plane."""
    hits: list[Point2] = []
    for a, b in zip(points, points[1:]):
        pa, da = frame.project(a)
        pb, db = frame.project(b)
        if abs(da) <= tolerance:
            hits.append(pa)
        if da * db < -(tolerance * tolerance):
            t = da / (da - db)
            hits.append(Point2(x=pa.x + t * (pb.x - pa.x), y=pa.y + t * (pb.y - pa.y)))
        if abs(db) <= tolerance:
            hits.append(pb)
    unique: list[Point2] = []
    for point in hits:
        if not unique or point != unique[-1]:
            unique.append(point)
    return tuple(unique)

def section_intersection_edges(
    points: tuple[Point3, ...],
    edges: tuple[tuple[int, int], ...],
    frame: ProjectionFrame,
    *,
    tolerance: float = 1e-9,
) -> tuple[Point2, ...]:
    """Intersect explicit solid edges with the frame depth=0 section plane.

    The returned points are de-duplicated and sorted only by their projected
    coordinates; callers can form a convex cut profile from them. Explicit edge
    topology avoids inventing faces or relying on point ordering.
    """
    hits: set[Point2] = set()
    for start_index, end_index in edges:
        a = points[start_index]
        b = points[end_index]
        pa, da = frame.project(a)
        pb, db = frame.project(b)
        if abs(da) <= tolerance:
            hits.add(pa)
        if abs(db) <= tolerance:
            hits.add(pb)
        if da * db < -(tolerance * tolerance):
            t = da / (da - db)
            hits.add(Point2(x=pa.x + t * (pb.x - pa.x), y=pa.y + t * (pb.y - pa.y)))
    return tuple(sorted(hits))

