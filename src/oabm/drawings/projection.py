from __future__ import annotations

import math
from dataclasses import dataclass

from oabm.model import Point3, Vector3

from .model import Bounds2, Point2, ProjectionMetadata

_EPS = 1e-12


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a: tuple[float, float, float], value: float) -> tuple[float, float, float]:
    return (a[0] * value, a[1] * value, a[2] * value)


def _norm(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(v, v))
    if length <= _EPS:
        raise ValueError("projection axis must be non-zero")
    return (v[0] / length, v[1] / length, v[2] / length)


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _p3(point: Point3) -> tuple[float, float, float]:
    return (point.x, point.y, point.z)


@dataclass(frozen=True, slots=True)
class ProjectionFrame:
    origin: tuple[float, float, float]
    horizontal: tuple[float, float, float]
    vertical: tuple[float, float, float]
    depth: tuple[float, float, float]

    @classmethod
    def from_axes(
        cls,
        *,
        origin: Point3,
        horizontal: Vector3,
        vertical: Vector3,
    ) -> ProjectionFrame:
        u = _norm((horizontal.x, horizontal.y, horizontal.z))
        raw_v = (vertical.x, vertical.y, vertical.z)
        projection = _mul(u, _dot(raw_v, u))
        v = _norm(_sub(raw_v, projection))
        w = _norm(_cross(u, v))
        return cls(origin=_p3(origin), horizontal=u, vertical=v, depth=w)

    @classmethod
    def plan(cls, elevation_m: float = 0.0) -> ProjectionFrame:
        return cls(
            origin=(0.0, 0.0, elevation_m),
            horizontal=(1.0, 0.0, 0.0),
            vertical=(0.0, 1.0, 0.0),
            depth=(0.0, 0.0, 1.0),
        )

    @classmethod
    def elevation(cls, direction: str) -> ProjectionFrame:
        key = direction.lower()
        frames = {
            "north": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
            "south": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
            "east": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0)),
            "west": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
        }
        if key not in frames:
            raise ValueError("direction must be north, south, east, or west")
        u, v, w = frames[key]
        return cls(origin=(0.0, 0.0, 0.0), horizontal=u, vertical=v, depth=w)

    @classmethod
    def section(cls, start: Point3, end: Point3) -> ProjectionFrame:
        dx, dy = end.x - start.x, end.y - start.y
        if math.hypot(dx, dy) <= _EPS:
            raise ValueError("section start and end must differ in XY")
        u = _norm((dx, dy, 0.0))
        v = (0.0, 0.0, 1.0)
        w = _norm(_cross(u, v))
        return cls(origin=_p3(start), horizontal=u, vertical=v, depth=w)

    def project(self, point: Point3) -> tuple[Point2, float]:
        relative = _sub(_p3(point), self.origin)
        return (
            Point2(_dot(relative, self.horizontal), _dot(relative, self.vertical)),
            _dot(relative, self.depth),
        )

    def metadata(
        self,
        *,
        depth_min_m: float | None = None,
        depth_max_m: float | None = None,
    ) -> ProjectionMetadata:
        return ProjectionMetadata(
            origin=self.origin,
            horizontal_axis=self.horizontal,
            vertical_axis=self.vertical,
            depth_axis=self.depth,
            depth_min_m=depth_min_m,
            depth_max_m=depth_max_m,
        )


def clip_segment_to_depth(
    a: Point3,
    b: Point3,
    frame: ProjectionFrame,
    depth_min: float | None,
    depth_max: float | None,
) -> tuple[Point3, Point3] | None:
    if depth_min is None and depth_max is None:
        return a, b
    da = frame.project(a)[1]
    db = frame.project(b)[1]
    low = -math.inf if depth_min is None else depth_min
    high = math.inf if depth_max is None else depth_max
    if low > high:
        raise ValueError("depth_min must not exceed depth_max")
    delta = db - da
    if abs(delta) <= _EPS:
        return (a, b) if low - _EPS <= da <= high + _EPS else None
    t0, t1 = 0.0, 1.0
    t_low = (low - da) / delta
    t_high = (high - da) / delta
    enter, leave = min(t_low, t_high), max(t_low, t_high)
    t0 = max(t0, enter)
    t1 = min(t1, leave)
    if t0 > t1 + _EPS:
        return None

    def lerp(t: float) -> Point3:
        return Point3(
            x=a.x + (b.x - a.x) * t,
            y=a.y + (b.y - a.y) * t,
            z=a.z + (b.z - a.z) * t,
        )

    return lerp(max(0.0, t0)), lerp(min(1.0, t1))


def _clip_segment_2d(a: Point2, b: Point2, bounds: Bounds2) -> tuple[Point2, Point2] | None:
    dx, dy = b.x - a.x, b.y - a.y
    p = (-dx, dx, -dy, dy)
    q = (
        a.x - bounds.min_x,
        bounds.max_x - a.x,
        a.y - bounds.min_y,
        bounds.max_y - a.y,
    )
    t0, t1 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) <= _EPS:
            if qi < 0:
                return None
            continue
        ratio = qi / pi
        if pi < 0:
            t0 = max(t0, ratio)
        else:
            t1 = min(t1, ratio)
        if t0 > t1 + _EPS:
            return None
    return (
        Point2(a.x + t0 * dx, a.y + t0 * dy),
        Point2(a.x + t1 * dx, a.y + t1 * dy),
    )


def clip_polyline(points: tuple[Point2, ...], bounds: Bounds2) -> tuple[tuple[Point2, ...], ...]:
    if len(points) < 2:
        return ()
    runs: list[list[Point2]] = []
    current: list[Point2] = []
    for a, b in zip(points, points[1:]):
        clipped = _clip_segment_2d(a, b, bounds)
        if clipped is None:
            if current:
                runs.append(current)
                current = []
            continue
        first, second = clipped
        if not current:
            current = [first, second]
        elif math.hypot(current[-1].x - first.x, current[-1].y - first.y) <= 1e-9:
            if math.hypot(current[-1].x - second.x, current[-1].y - second.y) > 1e-9:
                current.append(second)
        else:
            runs.append(current)
            current = [first, second]
    if current:
        runs.append(current)
    return tuple(tuple(run) for run in runs if len(run) >= 2)


def clip_polygon(points: tuple[Point2, ...], bounds: Bounds2) -> tuple[Point2, ...]:
    if len(points) < 3:
        return ()

    def clip_edge(
        polygon: list[Point2],
        inside,
        intersect,
    ) -> list[Point2]:
        if not polygon:
            return []
        output: list[Point2] = []
        previous = polygon[-1]
        previous_inside = inside(previous)
        for current in polygon:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current))
            previous, previous_inside = current, current_inside
        return output

    polygon = list(points)

    def x_intersect(x: float):
        def fn(a: Point2, b: Point2) -> Point2:
            if abs(b.x - a.x) <= _EPS:
                return Point2(x, a.y)
            t = (x - a.x) / (b.x - a.x)
            return Point2(x, a.y + t * (b.y - a.y))
        return fn

    def y_intersect(y: float):
        def fn(a: Point2, b: Point2) -> Point2:
            if abs(b.y - a.y) <= _EPS:
                return Point2(a.x, y)
            t = (y - a.y) / (b.y - a.y)
            return Point2(a.x + t * (b.x - a.x), y)
        return fn

    polygon = clip_edge(polygon, lambda p: p.x >= bounds.min_x - _EPS, x_intersect(bounds.min_x))
    polygon = clip_edge(polygon, lambda p: p.x <= bounds.max_x + _EPS, x_intersect(bounds.max_x))
    polygon = clip_edge(polygon, lambda p: p.y >= bounds.min_y - _EPS, y_intersect(bounds.min_y))
    polygon = clip_edge(polygon, lambda p: p.y <= bounds.max_y + _EPS, y_intersect(bounds.max_y))
    return tuple(polygon) if len(polygon) >= 3 else ()
