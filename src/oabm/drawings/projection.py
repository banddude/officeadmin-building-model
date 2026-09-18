from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from oabm.model import Point3, Pose, Quaternion, Size3, Vector3

from .types import DrawingError, Point2, Rect2, canonical_number

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class ProjectedPoint:
    point: Point2
    depth: float


@dataclass(frozen=True, slots=True)
class ProjectionFrame:
    origin: Point3
    right: Vector3
    up: Vector3
    forward: Vector3

    def __post_init__(self) -> None:
        for label, vector in (("right", self.right), ("up", self.up), ("forward", self.forward)):
            if abs(vector.magnitude - 1.0) > 1e-6:
                raise DrawingError(f"projection {label} vector must be unit length")
        if abs(_dot(self.right, self.up)) > 1e-6:
            raise DrawingError("projection right and up vectors must be orthogonal")
        if abs(_dot(self.right, self.forward)) > 1e-6 or abs(_dot(self.up, self.forward)) > 1e-6:
            raise DrawingError("projection forward vector must be orthogonal to the view plane")

    def project(self, point: Point3) -> ProjectedPoint:
        delta = Vector3(
            x=point.x - self.origin.x,
            y=point.y - self.origin.y,
            z=point.z - self.origin.z,
        )
        return ProjectedPoint(
            point=Point2(x=_dot(delta, self.right), y=_dot(delta, self.up)),
            depth=canonical_number(_dot(delta, self.forward)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": {"x": self.origin.x, "y": self.origin.y, "z": self.origin.z},
            "right": {"x": self.right.x, "y": self.right.y, "z": self.right.z},
            "up": {"x": self.up.x, "y": self.up.y, "z": self.up.z},
            "forward": {"x": self.forward.x, "y": self.forward.y, "z": self.forward.z},
        }


def plan_frame(level_elevation_m: float) -> ProjectionFrame:
    return ProjectionFrame(
        origin=Point3(x=0, y=0, z=level_elevation_m),
        right=Vector3(x=1, y=0, z=0),
        up=Vector3(x=0, y=1, z=0),
        forward=Vector3(x=0, y=0, z=1),
    )


def elevation_frame(direction: str) -> ProjectionFrame:
    if direction == "south":
        right, forward = Vector3(x=1, y=0, z=0), Vector3(x=0, y=1, z=0)
    elif direction == "north":
        right, forward = Vector3(x=-1, y=0, z=0), Vector3(x=0, y=-1, z=0)
    elif direction == "west":
        right, forward = Vector3(x=0, y=-1, z=0), Vector3(x=1, y=0, z=0)
    elif direction == "east":
        right, forward = Vector3(x=0, y=1, z=0), Vector3(x=-1, y=0, z=0)
    else:
        raise DrawingError(f"unsupported elevation direction {direction!r}")
    return ProjectionFrame(
        origin=Point3(x=0, y=0, z=0),
        right=right,
        up=Vector3(x=0, y=0, z=1),
        forward=forward,
    )


def section_frame(axis: str, offset_m: float, direction: str) -> ProjectionFrame:
    if axis == "x":
        origin = Point3(x=offset_m, y=0, z=0)
        if direction == "positive":
            right, forward = Vector3(x=0, y=-1, z=0), Vector3(x=1, y=0, z=0)
        else:
            right, forward = Vector3(x=0, y=1, z=0), Vector3(x=-1, y=0, z=0)
    elif axis == "y":
        origin = Point3(x=0, y=offset_m, z=0)
        if direction == "positive":
            right, forward = Vector3(x=1, y=0, z=0), Vector3(x=0, y=1, z=0)
        else:
            right, forward = Vector3(x=-1, y=0, z=0), Vector3(x=0, y=-1, z=0)
    else:
        raise DrawingError(f"unsupported section axis {axis!r}")
    return ProjectionFrame(
        origin=origin,
        right=right,
        up=Vector3(x=0, y=0, z=1),
        forward=forward,
    )


def _dot(a: Vector3, b: Vector3) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def interpolate(a: Point3, b: Point3, t: float) -> Point3:
    return Point3(
        x=a.x + (b.x - a.x) * t,
        y=a.y + (b.y - a.y) * t,
        z=a.z + (b.z - a.z) * t,
    )


def clip_segment_by_depth(
    frame: ProjectionFrame,
    a: Point3,
    b: Point3,
    min_depth: float | None,
    max_depth: float | None,
) -> tuple[Point3, Point3] | None:
    pa = frame.project(a)
    pb = frame.project(b)
    d0, d1 = pa.depth, pb.depth
    t0, t1 = 0.0, 1.0
    delta = d1 - d0

    if min_depth is not None:
        if abs(delta) <= _EPS:
            if d0 < min_depth - _EPS:
                return None
        else:
            t = (min_depth - d0) / delta
            if delta > 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    if max_depth is not None:
        if abs(delta) <= _EPS:
            if d0 > max_depth + _EPS:
                return None
        else:
            t = (max_depth - d0) / delta
            if delta > 0:
                t1 = min(t1, t)
            else:
                t0 = max(t0, t)
    t0, t1 = max(0.0, t0), min(1.0, t1)
    if t0 > t1 + _EPS:
        return None
    return interpolate(a, b, t0), interpolate(a, b, t1)


def clip_segment_2d(a: Point2, b: Point2, crop: Rect2 | None) -> tuple[Point2, Point2] | None:
    if crop is None:
        return a, b
    dx, dy = b.x - a.x, b.y - a.y
    p = (-dx, dx, -dy, dy)
    q = (a.x - crop.min_x, crop.max_x - a.x, a.y - crop.min_y, crop.max_y - a.y)
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) <= _EPS:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            u1 = max(u1, t)
        else:
            u2 = min(u2, t)
        if u1 > u2:
            return None
    return (
        Point2(x=a.x + u1 * dx, y=a.y + u1 * dy),
        Point2(x=a.x + u2 * dx, y=a.y + u2 * dy),
    )


def project_polyline_segments(
    frame: ProjectionFrame,
    points: Iterable[Point3],
    *,
    min_depth: float | None = None,
    max_depth: float | None = None,
    crop: Rect2 | None = None,
) -> tuple[tuple[Point2, Point2], ...]:
    items = tuple(points)
    output: list[tuple[Point2, Point2]] = []
    for a, b in zip(items, items[1:]):
        clipped3 = clip_segment_by_depth(frame, a, b, min_depth, max_depth)
        if clipped3 is None:
            continue
        pa, pb = (frame.project(point).point for point in clipped3)
        clipped2 = clip_segment_2d(pa, pb, crop)
        if clipped2 is not None and clipped2[0] != clipped2[1]:
            output.append(clipped2)
    return tuple(output)


def clip_polygon(points: tuple[Point2, ...], crop: Rect2 | None) -> tuple[Point2, ...]:
    if crop is None:
        return points
    result = list(points)
    boundaries = (
        ("left", crop.min_x),
        ("right", crop.max_x),
        ("bottom", crop.min_y),
        ("top", crop.max_y),
    )
    for edge, value in boundaries:
        if not result:
            break
        incoming = result
        result = []
        previous = incoming[-1]
        for current in incoming:
            prev_inside = _inside(previous, edge, value)
            curr_inside = _inside(current, edge, value)
            if curr_inside:
                if not prev_inside:
                    result.append(_intersection(previous, current, edge, value))
                result.append(current)
            elif prev_inside:
                result.append(_intersection(previous, current, edge, value))
            previous = current
    return tuple(_dedupe_adjacent(result))


def _inside(point: Point2, edge: str, value: float) -> bool:
    if edge == "left":
        return point.x >= value - _EPS
    if edge == "right":
        return point.x <= value + _EPS
    if edge == "bottom":
        return point.y >= value - _EPS
    return point.y <= value + _EPS


def _intersection(a: Point2, b: Point2, edge: str, value: float) -> Point2:
    dx, dy = b.x - a.x, b.y - a.y
    if edge in ("left", "right"):
        if abs(dx) <= _EPS:
            return Point2(x=value, y=a.y)
        t = (value - a.x) / dx
        return Point2(x=value, y=a.y + t * dy)
    if abs(dy) <= _EPS:
        return Point2(x=a.x, y=value)
    t = (value - a.y) / dy
    return Point2(x=a.x + t * dx, y=value)


def _dedupe_adjacent(points: Iterable[Point2]) -> list[Point2]:
    output: list[Point2] = []
    for point in points:
        if not output or point != output[-1]:
            output.append(point)
    if len(output) > 1 and output[0] == output[-1]:
        output.pop()
    return output


def quaternion_rotate(rotation: Quaternion, point: Point3) -> Point3:
    # Unit-quaternion rotation using the expanded matrix form.
    x, y, z, w = rotation.x, rotation.y, rotation.z, rotation.w
    m00 = 1 - 2 * (y * y + z * z)
    m01 = 2 * (x * y - z * w)
    m02 = 2 * (x * z + y * w)
    m10 = 2 * (x * y + z * w)
    m11 = 1 - 2 * (x * x + z * z)
    m12 = 2 * (y * z - x * w)
    m20 = 2 * (x * z - y * w)
    m21 = 2 * (y * z + x * w)
    m22 = 1 - 2 * (x * x + y * y)
    return Point3(
        x=m00 * point.x + m01 * point.y + m02 * point.z,
        y=m10 * point.x + m11 * point.y + m12 * point.z,
        z=m20 * point.x + m21 * point.y + m22 * point.z,
    )


def oriented_box_corners(pose: Pose, size: Size3) -> tuple[Point3, ...]:
    hx, hy, hz = size.x / 2, size.y / 2, size.z / 2
    corners: list[Point3] = []
    for z in (-hz, hz):
        for y in (-hy, hy):
            for x in (-hx, hx):
                rotated = quaternion_rotate(pose.rotation, Point3(x=x, y=y, z=z))
                corners.append(
                    Point3(
                        x=rotated.x + pose.position.x,
                        y=rotated.y + pose.position.y,
                        z=rotated.z + pose.position.z,
                    )
                )
    return tuple(corners)


BOX_EDGE_INDICES: tuple[tuple[int, int], ...] = (
    (0, 1), (0, 2), (0, 4),
    (1, 3), (1, 5),
    (2, 3), (2, 6),
    (3, 7),
    (4, 5), (4, 6),
    (5, 7),
    (6, 7),
)


def convex_hull(points: Iterable[Point2]) -> tuple[Point2, ...]:
    unique = sorted(set(points))
    if len(unique) <= 1:
        return tuple(unique)

    def cross(o: Point2, a: Point2, b: Point2) -> float:
        return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)

    lower: list[Point2] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[Point2] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def midpoint(points: Iterable[Point2]) -> Point2:
    values = tuple(points)
    if not values:
        raise DrawingError("cannot compute midpoint of empty point set")
    return Point2(
        x=sum(point.x for point in values) / len(values),
        y=sum(point.y for point in values) / len(values),
    )


def distance_2d(a: Point2, b: Point2) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def polygon_plane_intersections(
    points: tuple[Point3, ...], axis: str, offset_m: float
) -> tuple[Point3, ...]:
    """Intersect a closed polygon boundary with a vertical x/y section plane."""
    hits: list[Point3] = []
    for a, b in zip(points, points[1:] + points[:1]):
        da = (a.x if axis == "x" else a.y) - offset_m
        db = (b.x if axis == "x" else b.y) - offset_m
        if abs(da) <= _EPS and abs(db) <= _EPS:
            hits.extend((a, b))
            continue
        if da * db > _EPS:
            continue
        denom = da - db
        if abs(denom) <= _EPS:
            continue
        t = da / denom
        if -_EPS <= t <= 1 + _EPS:
            hits.append(interpolate(a, b, min(1.0, max(0.0, t))))
    unique: list[Point3] = []
    for hit in hits:
        if not any(math.dist((hit.x, hit.y, hit.z), (other.x, other.y, other.z)) <= 1e-8 for other in unique):
            unique.append(hit)
    horizontal = (lambda p: p.y) if axis == "x" else (lambda p: p.x)
    unique.sort(key=lambda point: (horizontal(point), point.z, point.x, point.y))
    return tuple(unique)


def section_intervals(
    frame: ProjectionFrame, points: tuple[Point3, ...], axis: str, offset_m: float
) -> tuple[tuple[ProjectedPoint, ProjectedPoint], ...]:
    hits = polygon_plane_intersections(points, axis, offset_m)
    if len(hits) < 2:
        return ()
    projected = [frame.project(point) for point in hits]
    projected.sort(key=lambda item: (item.point.x, item.point.y))
    pairs: list[tuple[ProjectedPoint, ProjectedPoint]] = []
    for index in range(0, len(projected) - 1, 2):
        a, b = projected[index], projected[index + 1]
        if a.point != b.point:
            pairs.append((a, b))
    return tuple(pairs)
