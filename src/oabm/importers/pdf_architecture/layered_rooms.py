"""Conservative room interiors from visible CAD wall and opening layers.

This module only proposes sheet-local source polygons. The importer owns canonical
identity, registration, provenance, and cross-page conflict handling.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .extract import _is_wall_source_layer
from .types import PdfLineObservation, PdfPageObservation


@dataclass(frozen=True, slots=True)
class LayeredRoomRegion:
    anchor: str
    polygon_pt: tuple[tuple[float, float], ...]
    source_wall_ids: tuple[str, ...]
    source_opening_ids: tuple[str, ...]
    closure_count: int


def _is_opening_layer(layer: str) -> bool:
    return layer.rsplit("|", 1)[-1].upper().lstrip("_") in {
        "A-DR.WND", "A-DOOR", "AE-DOOR", "AE-DR.WND",
    }


def has_multiple_wall_regions(page: PdfPageObservation, meters_per_point: float) -> bool:
    """Withhold a shared level/frame when two large drawings are separated on a sheet."""
    level_names: set[str] = set()
    for observation in page.texts:
        text = " ".join(observation.text.upper().split())
        for match in re.finditer(
            r"\b(?:LEVEL|FLOOR)\s*[:#-]?\s*(GROUND|FIRST|SECOND|THIRD|FOURTH|LOWER|UPPER|[0-9]+)\b"
            r"|\b(GROUND|FIRST|SECOND|THIRD|FOURTH|LOWER|UPPER)\s+FLOOR\s+PLAN\b",
            text,
        ):
            level_names.add(match.group(1) or match.group(2))
    if len(level_names) > 1:
        return True

    walls = tuple(
        line for line in page.lines
        if any(_is_wall_source_layer(layer) for layer in line.source_layers)
    )
    if len(walls) < 8:
        return False
    minimum_gap = max(30.0, 1.0 / meters_per_point)
    minimum_side = max(4, math.ceil(len(walls) * 0.2))
    for axis in (0, 1):
        spans = sorted(
            (min(line.start_pt[axis], line.end_pt[axis]),
             max(line.start_pt[axis], line.end_pt[axis]))
            for line in walls
        )
        merged: list[tuple[float, float]] = []
        for low, high in spans:
            if merged and low <= merged[-1][1] + 1.0:
                merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
            else:
                merged.append((low, high))
        for first, second in zip(merged, merged[1:]):
            if second[0] - first[1] < minimum_gap:
                continue
            left_count = sum(high <= first[1] for _, high in spans)
            right_count = sum(low >= second[0] for low, _ in spans)
            if left_count >= minimum_side and right_count >= minimum_side:
                return True
    return False


def _distance_to_segment(point: tuple[float, float], line: PdfLineObservation) -> float:
    ax, ay = line.start_pt
    bx, by = line.end_pt
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2))
    return math.hypot(point[0] - ax - t * dx, point[1] - ay - t * dy)


def _supported_opening_closures(
    walls: tuple[PdfLineObservation, ...],
    openings: tuple[PdfLineObservation, ...],
    meters_per_point: float,
) -> tuple[tuple[str, float, float, float, tuple[str, ...]], ...]:
    """Close only collinear wall-face gaps with opening graphics at both ends."""
    horizontal: dict[int, set[tuple[float, float]]] = defaultdict(set)
    vertical: dict[int, set[tuple[float, float]]] = defaultdict(set)
    for line in walls:
        (ax, ay), (bx, by) = line.start_pt, line.end_pt
        if abs(ay - by) <= 1.5 and abs(ax - bx) > 3:
            horizontal[round((ay + by) / 2)].add((min(ax, bx), max(ax, bx)))
        if abs(ax - bx) <= 1.5 and abs(ay - by) > 3:
            vertical[round((ax + bx) / 2)].add((min(ay, by), max(ay, by)))

    min_gap = max(4.0, 0.15 / meters_per_point)
    max_gap = min(120.0, 1.5 / meters_per_point)
    endpoint_tol = max(3.0, min(12.0, 0.15 / meters_per_point))
    vicinity = max(5.0, min(20.0, 0.25 / meters_per_point))
    result: set[tuple[str, float, float, float, tuple[str, ...]]] = set()
    for axis, groups in (("h", horizontal), ("v", vertical)):
        for fixed, span_set in sorted(groups.items()):
            spans = sorted(span_set)
            for _, left_end in spans:
                for right_start, _ in spans:
                    gap = right_start - left_end
                    if not min_gap <= gap <= max_gap:
                        continue
                    if any(
                        low <= left_end + 1 and high >= right_start - 1
                        for neighbor, neighbor_spans in groups.items()
                        if abs(neighbor - fixed) <= 2
                        for low, high in neighbor_spans
                    ):
                        continue
                    nearby: list[PdfLineObservation] = []
                    for line in openings:
                        (ax, ay), (bx, by) = line.start_pt, line.end_pt
                        if axis == "h":
                            close = not (
                                max(ax, bx) < left_end - 5
                                or min(ax, bx) > right_start + 5
                                or max(ay, by) < fixed - vicinity
                                or min(ay, by) > fixed + vicinity
                            )
                        else:
                            close = not (
                                max(ay, by) < left_end - 5
                                or min(ay, by) > right_start + 5
                                or max(ax, bx) < fixed - vicinity
                                or min(ax, bx) > fixed + vicinity
                            )
                        if close:
                            nearby.append(line)
                    if len(nearby) < 2:
                        continue
                    first = (left_end, fixed) if axis == "h" else (fixed, left_end)
                    second = (right_start, fixed) if axis == "h" else (fixed, right_start)
                    if min(_distance_to_segment(first, line) for line in nearby) > endpoint_tol:
                        continue
                    if min(_distance_to_segment(second, line) for line in nearby) > endpoint_tol:
                        continue
                    midpoint = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
                    evidence = tuple(sorted({
                        min(nearby, key=lambda line: (_distance_to_segment(point, line), line.element_id)).element_id
                        for point in (first, midpoint, second)
                    }))
                    result.add((axis, float(fixed), left_end, right_start, evidence))
    return tuple(sorted(result))


def _flood(
    pixels: object,
    width: int,
    height: int,
    seed: tuple[int, int],
    *,
    limit: int = 500_000,
) -> set[tuple[int, int]] | None:
    x, y = seed
    if not (0 <= x < width and 0 <= y < height) or pixels[x, y] == 0:  # type: ignore[index]
        return None
    queue = deque([seed])
    seen = {seed}
    while queue:
        x, y = queue.popleft()
        if x == 0 or y == 0 or x == width - 1 or y == height - 1:
            return None
        if len(seen) > limit:
            return None
        for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            nx, ny = neighbor
            if neighbor not in seen and pixels[nx, ny] != 0:  # type: ignore[index]
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _outer_polygon(cells: set[tuple[int, int]]) -> tuple[tuple[int, int], ...] | None:
    edges: dict[tuple[int, int], tuple[int, int]] = {}
    for x, y in cells:
        candidate = (
            ((x, y), (x + 1, y)) if (x, y - 1) not in cells else None,
            ((x + 1, y), (x + 1, y + 1)) if (x + 1, y) not in cells else None,
            ((x + 1, y + 1), (x, y + 1)) if (x, y + 1) not in cells else None,
            ((x, y + 1), (x, y)) if (x - 1, y) not in cells else None,
        )
        for edge in candidate:
            if edge is None:
                continue
            start, end = edge
            if start in edges:
                return None  # diagonal touch or another non-manifold boundary
            edges[start] = end
    if not edges:
        return None
    start = min(edges)
    current = start
    polygon: list[tuple[int, int]] = []
    while current in edges:
        polygon.append(current)
        current = edges.pop(current)
        if current == start:
            break
    if current != start or edges:
        return None  # hole or more than one boundary cycle
    corners = []
    for index, point in enumerate(polygon):
        before = polygon[index - 1]
        after = polygon[(index + 1) % len(polygon)]
        if (point[0] - before[0], point[1] - before[1]) != (
            after[0] - point[0], after[1] - point[1]
        ):
            corners.append(point)
    return tuple(corners) if 4 <= len(corners) <= 64 else None


def _line_touches_region(
    line: PdfLineObservation,
    cells: set[tuple[int, int]],
    left: int,
    top: int,
) -> bool:
    (ax, ay), (bx, by) = line.start_pt, line.end_pt
    steps = max(1, math.ceil(math.hypot(bx - ax, by - ay)))
    for index in range(steps + 1):
        t = index / steps
        x = round(ax + (bx - ax) * t - left)
        y = round(top - ay - (by - ay) * t)
        if any((x + dx, y + dy) in cells for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2))):
            return True
    return False


def find_layered_room_regions(
    page: PdfPageObservation,
    meters_per_point: float,
    seeds: tuple[tuple[str, tuple[float, float]], ...],
) -> tuple[LayeredRoomRegion, ...]:
    """Return uniquely labeled, closed interiors; unresolved regions stay absent."""
    walls = tuple(
        line for line in page.lines
        if any(_is_wall_source_layer(layer) for layer in line.source_layers)
    )
    openings = tuple(
        line for line in page.lines
        if any(_is_opening_layer(layer) for layer in line.source_layers)
    )
    if len(walls) < 4 or len(openings) < 2 or not seeds:
        return ()
    if has_multiple_wall_regions(page, meters_per_point):
        return ()
    xs = [value for line in walls for value in (line.start_pt[0], line.end_pt[0])]
    ys = [value for line in walls for value in (line.start_pt[1], line.end_pt[1])]
    left = math.floor(min(xs)) - 3
    right = math.ceil(max(xs)) + 3
    bottom = math.floor(min(ys)) - 3
    top = math.ceil(max(ys)) + 3
    width, height = right - left + 1, top - bottom + 1
    if width * height > 8_000_000:
        return ()
    mask = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(mask)
    for line in walls:
        draw.line(
            (line.start_pt[0] - left, top - line.start_pt[1],
             line.end_pt[0] - left, top - line.end_pt[1]),
            fill=0, width=3,
        )
    closures = _supported_opening_closures(walls, openings, meters_per_point)
    for axis, fixed, low, high, _ in closures:
        points = (
            (low - left, top - fixed, high - left, top - fixed)
            if axis == "h" else
            (fixed - left, top - low, fixed - left, top - high)
        )
        draw.line(points, fill=0, width=3)

    pixels = mask.load()
    components: list[tuple[set[tuple[int, int]], list[str]]] = []
    for anchor, (sx, sy) in sorted(seeds):
        seed = (round(sx - left), round(top - sy))
        if any(seed in cells for cells, _ in components):
            next(anchors for cells, anchors in components if seed in cells).append(anchor)
            continue
        cells = _flood(pixels, width, height, seed)
        if cells is not None:
            components.append((cells, [anchor]))

    regions: list[LayeredRoomRegion] = []
    for cells, anchors in components:
        if len(anchors) != 1:
            continue
        area_m2 = len(cells) * meters_per_point**2
        if not 1.0 <= area_m2 <= 250.0:
            continue
        polygon = _outer_polygon(cells)
        if polygon is None:
            continue
        px = [point[0] for point in polygon]
        py = [point[1] for point in polygon]
        if min(max(px) - min(px), max(py) - min(py)) * meters_per_point < 1.0:
            continue
        source_polygon = tuple((float(x + left), float(top - y)) for x, y in polygon)
        min_x = min(x for x, _ in source_polygon)
        max_x = max(x for x, _ in source_polygon)
        min_y = min(y for _, y in source_polygon)
        max_y = max(y for _, y in source_polygon)
        if (
            min_x <= page.width_pt * 0.05
            and max_x >= page.width_pt * 0.95
            and min_y <= page.height_pt * 0.05
            and max_y >= page.height_pt * 0.95
        ):
            continue
        nearby_walls = tuple(sorted(
            line.element_id for line in walls
            if _line_touches_region(line, cells, left, top)
        ))
        nearby_closures = []
        for axis, fixed, low, high, evidence in closures:
            synthetic = PdfLineObservation(
                element_id="opening-closure",
                start_pt=(low, fixed) if axis == "h" else (fixed, low),
                end_pt=(high, fixed) if axis == "h" else (fixed, high),
            )
            if _line_touches_region(synthetic, cells, left, top):
                nearby_closures.append(evidence)
        regions.append(LayeredRoomRegion(
            anchor=anchors[0],
            polygon_pt=source_polygon,
            source_wall_ids=nearby_walls,
            source_opening_ids=tuple(sorted({item for evidence in nearby_closures for item in evidence})),
            closure_count=len(nearby_closures),
        ))
    return tuple(sorted(regions, key=lambda item: item.anchor))
