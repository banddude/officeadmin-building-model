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

from .extract import _is_wall_source_layer, _wall_layer_rect_segments
from .types import PdfLineObservation, PdfPageObservation


@dataclass(frozen=True, slots=True)
class LayeredRoomRegion:
    anchor: str
    polygon_pt: tuple[tuple[float, float], ...]
    source_wall_ids: tuple[str, ...]
    source_opening_ids: tuple[str, ...]
    closure_count: int
    source_glazing_ids: tuple[str, ...] = ()
    source_mullion_ids: tuple[str, ...] = ()
    source_leader_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LayeredRoomSeed:
    """One room identity seed, optionally linked by an observed room-ID leader."""

    anchor: str
    point_pt: tuple[float, float]
    source_leader_ids: tuple[str, ...] = ()


# This is a raster stroke width for filling source loops, not a physical gap
# allowance, measured wall width, or engineering dimension.
_MASK_LINE_WIDTH_PT = 3.0
_GLAZING_BOUNDARY_LAYERS = frozenset({
    "A-GLAZ", "AE-GLAZ", "A-GLAZ-BASE", "AE-GLAZ-BASE",
    "A-GLAZ-EXST", "AE-GLAZ-EXST",
})
_GLAZING_MULLION_LAYERS = frozenset({
    "A-GLAZ-MULL", "AE-GLAZ-MULL",
})


def _source_layer_tokens(line: PdfLineObservation) -> frozenset[str]:
    return frozenset(
        token.upper().lstrip("_")
        for layer in line.source_layers
        for token in layer.split("|")
        if token
    )


def _segment_bbox(line: PdfLineObservation) -> tuple[float, float, float, float]:
    (ax, ay), (bx, by) = line.start_pt, line.end_pt
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _segment_intersects(first: PdfLineObservation, second: PdfLineObservation) -> bool:
    def cross(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    a, b = first.start_pt, first.end_pt
    c, d = second.start_pt, second.end_pt
    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    return (
        ab_c == 0 and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
    ) or (
        ab_d == 0 and min(a[0], b[0]) <= d[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= d[1] <= max(a[1], b[1])
    ) or (
        cd_a == 0 and min(c[0], d[0]) <= a[0] <= max(c[0], d[0])
        and min(c[1], d[1]) <= a[1] <= max(c[1], d[1])
    ) or (
        cd_b == 0 and min(c[0], d[0]) <= b[0] <= max(c[0], d[0])
        and min(c[1], d[1]) <= b[1] <= max(c[1], d[1])
    ) or (ab_c * ab_d < 0 and cd_a * cd_b < 0)


def _segments_source_connected(
    first: PdfLineObservation,
    second: PdfLineObservation,
) -> bool:
    """Whether observed strokes touch within the raster mask stroke width.

    The tolerance is only a topology/raster-connectivity test, not a physical
    gap bridge or a measurement of wall/glazing separation.
    """
    first_box, second_box = _segment_bbox(first), _segment_bbox(second)
    gap_x = max(0.0, first_box[0] - second_box[2], second_box[0] - first_box[2])
    gap_y = max(0.0, first_box[1] - second_box[3], second_box[1] - first_box[3])
    if math.hypot(gap_x, gap_y) > _MASK_LINE_WIDTH_PT:
        return False
    if _segment_intersects(first, second):
        return True
    return min(
        _distance_to_segment(first.start_pt, second),
        _distance_to_segment(first.end_pt, second),
        _distance_to_segment(second.start_pt, first),
        _distance_to_segment(second.end_pt, first),
    ) <= _MASK_LINE_WIDTH_PT


def _connected_glazing_evidence(
    page: PdfPageObservation,
    walls: tuple[PdfLineObservation, ...],
) -> tuple[tuple[PdfLineObservation, ...], tuple[PdfLineObservation, ...]]:
    """Return only glazing/mullion vectors in components touching opaque walls.

    The connectivity tolerance is exactly the existing three-point raster mask
    stroke width. No connector segment is synthesized; only source vectors are
    returned and drawn. Disconnected glazing details therefore cannot close a
    room on their own.
    """
    candidates = tuple(
        line for line in page.lines
        if _source_layer_tokens(line) & (_GLAZING_BOUNDARY_LAYERS | _GLAZING_MULLION_LAYERS)
    )
    if not candidates:
        return (), ()

    connected = {
        index for index, line in enumerate(candidates)
        if any(_segments_source_connected(line, wall) for wall in walls)
    }
    neighbors: dict[int, list[int]] = {index: [] for index in range(len(candidates))}
    for first in range(len(candidates)):
        for second in range(first + 1, len(candidates)):
            if _segments_source_connected(candidates[first], candidates[second]):
                neighbors[first].append(second)
                neighbors[second].append(first)

    pending = list(sorted(connected))
    while pending:
        index = pending.pop()
        for neighbor in neighbors[index]:
            if neighbor not in connected:
                connected.add(neighbor)
                pending.append(neighbor)

    selected = tuple(candidates[index] for index in sorted(connected))
    glazing = tuple(
        line for line in selected
        if _source_layer_tokens(line) & _GLAZING_BOUNDARY_LAYERS
    )
    mullions = tuple(
        line for line in selected
        if _source_layer_tokens(line) & _GLAZING_MULLION_LAYERS
    )
    return glazing, mullions


def _is_opening_layer(layer: str) -> bool:
    return layer.rsplit("|", 1)[-1].upper().lstrip("_") in {
        "A-DR.WND", "A-DOOR", "AE-DOOR", "AE-DR.WND",
    }


def _wall_layer_lines(page: PdfPageObservation) -> tuple[PdfLineObservation, ...]:
    """Wall-layer lines plus the outline segments of wall-layer rectangles."""

    return (
        *(
            line for line in page.lines
            if any(_is_wall_source_layer(layer) for layer in line.source_layers)
        ),
        *_wall_layer_rect_segments(page),
    )


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

    walls = _wall_layer_lines(page)
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


def _wall_face_spans(
    walls: tuple[PdfLineObservation, ...],
) -> dict[str, list[tuple[float, float, float]]]:
    """Straight wall-face spans per axis as ``(fixed, low, high)`` in points."""

    spans: dict[str, list[tuple[float, float, float]]] = {"h": [], "v": []}
    for line in walls:
        (ax, ay), (bx, by) = line.start_pt, line.end_pt
        if abs(ay - by) <= 1.5 and abs(ax - bx) > 3:
            spans["h"].append(((ay + by) / 2.0, min(ax, bx), max(ax, bx)))
        if abs(ax - bx) <= 1.5 and abs(ay - by) > 3:
            spans["v"].append(((ax + bx) / 2.0, min(ay, by), max(ay, by)))
    return spans


def _door_leaf_closures(
    walls: tuple[PdfLineObservation, ...],
    openings: tuple[PdfLineObservation, ...],
    meters_per_point: float,
) -> tuple[tuple[str, float, float, float, tuple[str, ...]], ...]:
    """Close a wall gap jamb-to-jamb only where door-leaf evidence supports it.

    A straight opening-layer line that starts at, or within a small tolerance
    of, one jamb of a 0.6 m to 1.2 m gap and is about the gap width (a door
    leaf) closes that opening. The barrier is drawn across the gap between the
    jambs, never along the leaf. Gaps without that evidence stay open.
    """

    if not openings:
        return ()
    minimum_door_pt = 0.6 / meters_per_point
    maximum_door_pt = 1.2 / meters_per_point
    jamb_tol = max(3.0, min(12.0, 0.15 / meters_per_point))
    width_tol = max(6.0, 0.12 / meters_per_point)
    leaves: list[tuple[PdfLineObservation, float]] = []
    for line in openings:
        length = math.hypot(
            line.end_pt[0] - line.start_pt[0], line.end_pt[1] - line.start_pt[1]
        )
        if minimum_door_pt <= length <= maximum_door_pt:
            leaves.append((line, length))
    if not leaves:
        return ()
    result: dict[tuple[str, float, float, float], set[str]] = {}
    for axis, axis_spans in _wall_face_spans(walls).items():
        ordered = sorted(axis_spans)
        faces: list[list[tuple[float, float, float]]] = []
        for span in ordered:
            if faces and abs(span[0] - faces[-1][-1][0]) <= 2.0:
                faces[-1].append(span)
            else:
                faces.append([span])
        for face in faces:
            face_spans = sorted((low, high, fixed) for fixed, low, high in face)
            for (_, left_end, left_fixed), (right_start, _, right_fixed) in zip(
                face_spans, face_spans[1:]
            ):
                gap = right_start - left_end
                if not minimum_door_pt <= gap <= maximum_door_pt:
                    continue
                # A parallel face covering the gap means solid wall, not an opening.
                if any(
                    other_low <= left_end + 1 and other_high >= right_start - 1
                    for other_low, other_high, _ in face_spans
                ):
                    continue
                fixed = (left_fixed + right_fixed) / 2.0
                first = (left_end, fixed) if axis == "h" else (fixed, left_end)
                second = (right_start, fixed) if axis == "h" else (fixed, right_start)
                for line, length in leaves:
                    if abs(length - gap) > width_tol:
                        continue
                    if (
                        min(math.dist(first, line.start_pt), math.dist(first, line.end_pt))
                        <= jamb_tol
                        or min(math.dist(second, line.start_pt), math.dist(second, line.end_pt))
                        <= jamb_tol
                    ):
                        result.setdefault(
                            (axis, float(fixed), left_end, right_start), set()
                        ).add(line.element_id)
                        break
    return tuple(
        (axis, fixed, low, high, tuple(sorted(evidence)))
        for (axis, fixed, low, high), evidence in sorted(result.items())
    )


def _merge_closures(
    *closure_groups: tuple[tuple[str, float, float, float, tuple[str, ...]], ...],
) -> tuple[tuple[str, float, float, float, tuple[str, ...]], ...]:
    """Union closure groups by geometry so one opening yields one closure."""

    merged: dict[tuple[str, float, float, float], set[str]] = {}
    for closures in closure_groups:
        for axis, fixed, low, high, evidence in closures:
            merged.setdefault((axis, fixed, low, high), set()).update(evidence)
    return tuple(
        (axis, fixed, low, high, tuple(sorted(evidence)))
        for (axis, fixed, low, high), evidence in sorted(merged.items())
    )


def _flood(
    pixels: object,
    width: int,
    height: int,
    seed: tuple[int, int],
    *,
    limit: int = 500_000,
) -> tuple[set[tuple[int, int]], bool] | None:
    x, y = seed
    if not (0 <= x < width and 0 <= y < height) or pixels[x, y] == 0:  # type: ignore[index]
        return None
    queue = deque([seed])
    seen = {seed}
    touches_mask_edge = False
    while queue:
        x, y = queue.popleft()
        if x == 0 or y == 0 or x == width - 1 or y == height - 1:
            touches_mask_edge = True
        if len(seen) > limit:
            return seen, False
        for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            nx, ny = neighbor
            if (
                0 <= nx < width
                and 0 <= ny < height
                and neighbor not in seen
                and pixels[nx, ny] != 0  # type: ignore[index]
            ):
                seen.add(neighbor)
                queue.append(neighbor)
    return seen, not touches_mask_edge


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
    seeds: tuple[LayeredRoomSeed | tuple[str, tuple[float, float]], ...],
) -> tuple[LayeredRoomRegion, ...]:
    """Return uniquely labeled, closed interiors; unresolved regions stay absent."""
    normalized_seeds = tuple(sorted(
        (
            seed if isinstance(seed, LayeredRoomSeed)
            else LayeredRoomSeed(anchor=seed[0], point_pt=seed[1])
            for seed in seeds
        ),
        key=lambda item: (item.anchor, item.point_pt, item.source_leader_ids),
    ))
    walls = _wall_layer_lines(page)
    openings = tuple(
        line for line in page.lines
        if any(_is_opening_layer(layer) for layer in line.source_layers)
    )
    opening_rects = tuple(sorted(
        (
            rect
            for rect in page.rects
            if rect.source_layer is not None and _is_opening_layer(rect.source_layer)
        ),
        key=lambda rect: (rect.bbox_pt, rect.element_id),
    ))
    if len(walls) < 4 or len(openings) + len(opening_rects) < 2 or not normalized_seeds:
        return ()
    if has_multiple_wall_regions(page, meters_per_point):
        return ()
    glazing, mullions = _connected_glazing_evidence(
        page, walls,
    ) if any(seed.source_leader_ids for seed in normalized_seeds) else ((), ())
    closures = _merge_closures(
        _supported_opening_closures(walls, openings, meters_per_point),
        _door_leaf_closures(walls, openings, meters_per_point),
    )

    def flood_components(
        boundary_lines: tuple[PdfLineObservation, ...],
        seed_indexes: tuple[int, ...],
    ) -> tuple[
        list[tuple[set[tuple[int, int]], list[tuple[int, LayeredRoomSeed]], int, int]],
        set[int],
    ]:
        if not boundary_lines or not seed_indexes:
            return [], set()
        xs = [value for line in boundary_lines for value in (line.start_pt[0], line.end_pt[0])]
        ys = [value for line in boundary_lines for value in (line.start_pt[1], line.end_pt[1])]
        left = math.floor(min(xs)) - 3
        right = math.ceil(max(xs)) + 3
        bottom = math.floor(min(ys)) - 3
        top = math.ceil(max(ys)) + 3
        width, height = right - left + 1, top - bottom + 1
        if width * height > 8_000_000:
            return [], set()
        mask = Image.new("L", (width, height), 255)
        draw = ImageDraw.Draw(mask)
        for line in boundary_lines:
            draw.line(
                (line.start_pt[0] - left, top - line.start_pt[1],
                 line.end_pt[0] - left, top - line.end_pt[1]),
                fill=0, width=int(_MASK_LINE_WIDTH_PT),
            )
        for axis, fixed, low, high, _ in closures:
            points = (
                (low - left, top - fixed, high - left, top - fixed)
                if axis == "h" else
                (fixed - left, top - low, fixed - left, top - high)
            )
            draw.line(points, fill=0, width=int(_MASK_LINE_WIDTH_PT))
        # Opening rectangles are source-supported barriers across their own
        # openings, not invented wall segments.
        for rect in opening_rects:
            x0, y0, x1, y1 = rect.bbox_pt
            draw.rectangle(
                (x0 - left, top - y1, x1 - left, top - y0),
                outline=0, width=int(_MASK_LINE_WIDTH_PT),
            )

        pixels = mask.load()
        components: list[
            tuple[set[tuple[int, int]], list[tuple[int, LayeredRoomSeed]], int, int]
        ] = []
        bounded_seed_indexes: set[int] = set()
        unresolved_pixels: set[tuple[int, int]] = set()
        for seed_index in seed_indexes:
            room_seed = normalized_seeds[seed_index]
            sx, sy = room_seed.point_pt
            seed = (round(sx - left), round(top - sy))
            existing = next(
                (component for component in components if seed in component[0]),
                None,
            )
            if existing is not None:
                existing[1].append((seed_index, room_seed))
                bounded_seed_indexes.add(seed_index)
                continue
            if seed in unresolved_pixels:
                continue
            result = _flood(pixels, width, height, seed)
            if result is None:
                continue
            cells, bounded = result
            if not bounded:
                # Cache failed/open components too, so repeated text or leader
                # candidates do not flood the same leaking region repeatedly.
                unresolved_pixels.update(cells)
                continue
            components.append((cells, [(seed_index, room_seed)], left, top))
            bounded_seed_indexes.add(seed_index)
        return components, bounded_seed_indexes

    boundary_lines = (*walls, *glazing, *mullions)
    seed_indexes = tuple(range(len(normalized_seeds)))
    wall_components, wall_bounded_seed_indexes = flood_components(walls, seed_indexes)
    components: list[
        tuple[
            set[tuple[int, int]], list[tuple[int, LayeredRoomSeed]], int, int,
            tuple[PdfLineObservation, ...], tuple[PdfLineObservation, ...],
        ]
    ] = [
        (cells, seeds, left, top, (), ())
        for cells, seeds, left, top in wall_components
    ]

    # Existing opaque-wall interiors retain legacy text-center seeding. Only
    # seeds not already assigned to any bounded wall component are retried on
    # the connected facade mask, and every resulting component must have one
    # explicit room-ID leader seed. Including unresolved text seeds in that
    # second flood keeps them as ambiguity evidence rather than allowing a
    # nearby leader seed to silently claim their component.
    facade_seed_indexes = tuple(
        index for index in seed_indexes if index not in wall_bounded_seed_indexes
    )
    if facade_seed_indexes and (glazing or mullions):
        facade_components, _ = flood_components(boundary_lines, facade_seed_indexes)
        components.extend(
            (cells, seeds, left, top, glazing, mullions)
            for cells, seeds, left, top in facade_components
            if len(seeds) == 1 and seeds[0][1].source_leader_ids
        )

    regions: list[LayeredRoomRegion] = []
    for cells, indexed_seeds, left, top, component_glazing, component_mullions in components:
        room_seeds = [seed for _, seed in indexed_seeds]
        if len(room_seeds) != 1:
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
        nearby_glazing = tuple(sorted(
            line.element_id for line in component_glazing
            if _line_touches_region(line, cells, left, top)
        ))
        nearby_mullions = tuple(sorted(
            line.element_id for line in component_mullions
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
            anchor=room_seeds[0].anchor,
            polygon_pt=source_polygon,
            source_wall_ids=nearby_walls,
            source_opening_ids=tuple(sorted({item for evidence in nearby_closures for item in evidence})),
            closure_count=len(nearby_closures),
            source_glazing_ids=nearby_glazing,
            source_mullion_ids=nearby_mullions,
            source_leader_ids=tuple(sorted(room_seeds[0].source_leader_ids)),
        ))
    return tuple(sorted(regions, key=lambda item: item.anchor))
