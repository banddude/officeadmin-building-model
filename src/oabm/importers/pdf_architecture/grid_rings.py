"""Conservative source-ring observations; no grid-axis or frame inference."""

from __future__ import annotations

from collections import deque
import math
from .types import PdfLineObservation, PdfPageObservation, PdfTextObservation

Point = tuple[float, float]


def _enclosing_ring_centers(
    text: PdfTextObservation, lines: list[PdfLineObservation]
) -> tuple[Point, ...]:
    # Quantization joins PDF endpoint roundoff only, not visible gaps. Drop
    # dangling leaders before examining closed, non-branching components.
    graph: dict[Point, set[Point]] = {}
    families: dict[tuple[Point, Point], set[str]] = {}
    for line in lines:
        if line.filled or line.dashed:
            continue
        a, b = (
            tuple(round(v, 3) for v in point) for point in (line.start_pt, line.end_pt)
        )
        if a == b:
            continue
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
        families.setdefault(tuple(sorted((a, b))), set()).add(line.primitive_family)
    leaves = deque(sorted(p for p, neighbors in graph.items() if len(neighbors) < 2))
    while leaves:
        p = leaves.popleft()
        for neighbor in tuple(graph.pop(p, ())):
            graph[neighbor].discard(p)
            if len(graph[neighbor]) < 2:
                leaves.append(neighbor)
    unseen = set(graph)
    result: list[Point] = []
    while unseen:
        start = min(unseen)
        component = set()
        pending = [start]
        while pending:
            point = pending.pop()
            if point in component:
                continue
            component.add(point)
            pending.extend(graph[point] - component)
        unseen -= component
        if any(len(graph[p]) != 2 for p in component) or len(component) < 4:
            continue
        points = [start]
        previous = None
        current = start
        while True:
            nxt = min(graph[current] - ({previous} if previous is not None else set()))
            if nxt == start:
                break
            points.append(nxt)
            previous, current = current, nxt
        # Four cubic cardinal endpoints describe a ring; four straight edges
        # describe a box. Polygonal circles need enough source vertices.
        if len(points) < 8 and not all(
            "curve" in families[tuple(sorted((a, b)))]
            for a, b in zip(points, points[1:] + points[:1])
        ):
            continue
        a, b, c = points[0], points[len(points) // 3], points[2 * len(points) // 3]
        bx, by = b[0] - a[0], b[1] - a[1]
        cx, cy = c[0] - a[0], c[1] - a[1]
        determinant = 2 * (bx * cy - by * cx)
        if abs(determinant) < 1e-8:
            continue
        bb, cc = bx * bx + by * by, cx * cx + cy * cy
        center = (
            a[0] + (bb * cy - cc * by) / determinant,
            a[1] + (bx * cc - cx * bb) / determinant,
        )
        radius = math.dist(center, a)
        if not 4 <= radius <= 40:
            continue
        if any(
            abs(math.dist(p, center) - radius) > max(0.1, 0.02 * radius) for p in points
        ):
            continue
        angles = [math.atan2(p[1] - center[1], p[0] - center[0]) for p in points]
        turns = [
            (b - a + math.pi) % math.tau - math.pi
            for a, b in zip(angles, angles[1:] + angles[:1])
        ]
        if not (all(t > 0 for t in turns) or all(t < 0 for t in turns)):
            continue
        if abs(abs(sum(turns)) - math.tau) > 1e-5:
            continue
        x0, y0, x1, y1 = text.bbox_pt
        if any(
            math.dist(p, center) > radius + 0.1
            for p in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
        ):
            continue
        result.append(tuple(round(v, 6) for v in center))
    return tuple(sorted(result))


def grid_ring_labels(
    page: PdfPageObservation, scope: tuple[float, float, float, float] | None = None
) -> dict[str, Point]:
    """Short enclosed labels; duplicate labels or competing rings are refused."""
    near: dict[tuple[int, int], list[int]] = {}
    cell = 40.0
    for i, line in enumerate(page.lines):
        for key in {
            (math.floor(p[0] / cell), math.floor(p[1] / cell))
            for p in (line.start_pt, line.end_pt)
        }:
            near.setdefault(key, []).append(i)
    found: dict[str, list[Point]] = {}
    for text in page.texts:
        label = text.text.strip().upper()
        if (
            not 1 <= len(label) <= 3
            or not all(c in "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789." for c in label)
            or label.startswith(".")
        ):
            continue
        center = text.center_pt
        if scope is not None and not (
            scope[0] <= center[0] <= scope[2] and scope[1] <= center[1] <= scope[3]
        ):
            continue
        cx, cy = (math.floor(v / cell) for v in center)
        indexes = sorted(
            {
                i
                for dx in range(-2, 3)
                for dy in range(-2, 3)
                for i in near.get((cx + dx, cy + dy), ())
            }
        )
        lines = [
            page.lines[i]
            for i in indexes
            if all(
                math.dist(center, p) <= 85
                for p in (page.lines[i].start_pt, page.lines[i].end_pt)
            )
        ]
        rings = _enclosing_ring_centers(text, lines)
        if len(rings) != 1:
            continue
        ring = rings[0]
        if scope is not None and not (
            scope[0] <= ring[0] <= scope[2] and scope[1] <= ring[1] <= scope[3]
        ):
            continue
        found.setdefault(label, []).append(ring)
    return {
        label: centers[0]
        for label, centers in sorted(found.items())
        if len(centers) == 1
    }
