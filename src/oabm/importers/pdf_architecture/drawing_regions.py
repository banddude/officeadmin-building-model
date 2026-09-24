"""Split one architectural sheet into separately drawn plan regions.

This module works only in sheet-local PDF points. It groups caller-selected wall
evidence into spatially separated clusters and scopes the page's source
observations to each cluster. The importer decides which evidence to supply and
owns level, scale, registration, identity, and canonical promotion.

A sheet is split only when at least two separated clusters each carry enough
wall evidence to be a building drawing: a minimum segment count, span, and total
wall length, and at least a quarter of the largest cluster's wall length so a
legend swatch or wall-type key cannot pose as a second plan. Otherwise the whole
sheet remains one region, so single-drawing sheets keep their existing
page-level behavior.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import PdfPageObservation, PdfTextObservation

BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class RegionEvidence:
    """One wall-evidence segment in sheet-local PDF points."""

    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    source_element_ids: tuple[str, ...]

    @property
    def bbox_pt(self) -> BBox:
        (ax, ay), (bx, by) = self.start_pt, self.end_pt
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    @property
    def length_pt(self) -> float:
        return math.dist(self.start_pt, self.end_pt)


@dataclass(frozen=True, slots=True)
class DrawingRegionSplit:
    """A sheet's regions; ``regions`` is empty when the sheet stays whole."""

    regions: tuple["SourceDrawingRegion", ...]
    sheet_texts: tuple[PdfTextObservation, ...]
    overlapping: bool
    qualifying_cluster_count: int
    cluster_count: int
    single_region_bbox_pt: BBox | None


@dataclass(frozen=True, slots=True)
class SourceDrawingRegion:
    index: int
    bbox_pt: BBox
    scope_bbox_pt: BBox
    evidence_count: int
    evidence_element_ids: tuple[str, ...]
    page: PdfPageObservation
    signature: frozenset[tuple[int, int, int, int]]


def _bbox_distance(first: BBox, second: BBox) -> float:
    dx = max(0.0, max(first[0], second[0]) - min(first[2], second[2]))
    dy = max(0.0, max(first[1], second[1]) - min(first[3], second[3]))
    return math.hypot(dx, dy)


def _union_bbox(boxes: list[BBox]) -> BBox:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _expand(bbox: BBox, margin: float) -> BBox:
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _contains(bbox: BBox, point: tuple[float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _boxes_overlap(first: BBox, second: BBox) -> bool:
    return not (
        first[2] < second[0]
        or second[2] < first[0]
        or first[3] < second[1]
        or second[3] < first[1]
    )


def cluster_evidence(
    evidence: tuple[RegionEvidence, ...],
    gap_pt: float,
) -> tuple[tuple[int, ...], ...]:
    """Group evidence whose bounding boxes lie within ``gap_pt`` of each other."""

    if not evidence:
        return ()
    parent = list(range(len(evidence)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        a, b = find(first), find(second)
        if a != b:
            parent[max(a, b)] = min(a, b)

    cell = max(gap_pt, 1.0)
    cells: dict[tuple[int, int], list[int]] = {}
    boxes = [item.bbox_pt for item in evidence]
    for index, box in enumerate(boxes):
        x0, y0, x1, y1 = _expand(box, gap_pt / 2.0)
        for cx in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
            for cy in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
                cells.setdefault((cx, cy), []).append(index)
    checked: set[tuple[int, int]] = set()
    for members in cells.values():
        for position, first in enumerate(members):
            for second in members[position + 1:]:
                key = (first, second) if first < second else (second, first)
                if key in checked:
                    continue
                checked.add(key)
                if find(first) != find(second) and _bbox_distance(boxes[first], boxes[second]) <= gap_pt:
                    union(first, second)
    groups: dict[int, list[int]] = {}
    for index in range(len(evidence)):
        groups.setdefault(find(index), []).append(index)
    return tuple(tuple(sorted(group)) for _, group in sorted(groups.items()))


def _signature(
    evidence: tuple[RegionEvidence, ...],
    bbox: BBox,
) -> frozenset[tuple[int, int, int, int]]:
    """Translation-normalized, rounded, direction-free evidence segments."""

    x0, y0 = bbox[0], bbox[1]
    result: set[tuple[int, int, int, int]] = set()
    for item in evidence:
        a = (round(item.start_pt[0] - x0), round(item.start_pt[1] - y0))
        b = (round(item.end_pt[0] - x0), round(item.end_pt[1] - y0))
        first, second = sorted((a, b))
        result.add((first[0], first[1], second[0], second[1]))
    return frozenset(result)


def signatures_repeat(
    first: frozenset[tuple[int, int, int, int]],
    second: frozenset[tuple[int, int, int, int]],
    *,
    minimum_ratio: float = 0.8,
) -> bool:
    """True when two regions' wall evidence is congruent under translation."""

    if not first or not second:
        return False
    shared = len(first & second)
    return shared / max(len(first), len(second)) >= minimum_ratio


def _assign_texts(
    texts: tuple[PdfTextObservation, ...],
    boxes: tuple[BBox, ...],
    *,
    excluded_text_ids: frozenset[str],
    text_margin_pt: float,
) -> tuple[dict[int, list[PdfTextObservation]], list[PdfTextObservation]]:
    """Assign a text to the uniquely nearest region; everything else is sheet-level."""

    assigned: dict[int, list[PdfTextObservation]] = {index: [] for index in range(len(boxes))}
    sheet: list[PdfTextObservation] = []
    for text in texts:
        if text.element_id in excluded_text_ids:
            sheet.append(text)
            continue
        distances = sorted(
            (_bbox_distance(box, (*text.center_pt, *text.center_pt)), index)
            for index, box in enumerate(boxes)
        )
        nearest_distance, nearest = distances[0]
        runner_up = distances[1][0] if len(distances) > 1 else math.inf
        if nearest_distance <= text_margin_pt and runner_up >= 2.0 * nearest_distance + 18.0:
            assigned[nearest].append(text)
        else:
            sheet.append(text)
    return assigned, sheet


def split_drawing_regions(
    page: PdfPageObservation,
    evidence: tuple[RegionEvidence, ...],
    *,
    gap_pt: float,
    margin_pt: float,
    text_margin_pt: float,
    min_span_pt: float,
    min_evidence_count: int,
    min_total_length_pt: float,
    min_relative_length: float = 0.25,
    excluded_text_ids: frozenset[str] = frozenset(),
) -> DrawingRegionSplit:
    """Return separately scoped drawing regions when a sheet holds several plans."""

    clusters = cluster_evidence(evidence, gap_pt)
    measured: list[tuple[BBox, tuple[RegionEvidence, ...], float]] = []
    for members in clusters:
        items = tuple(evidence[index] for index in members)
        measured.append((
            _union_bbox([item.bbox_pt for item in items]),
            items,
            sum(item.length_pt for item in items),
        ))
    largest_length = max((length for _, _, length in measured), default=0.0)
    qualifying: list[tuple[BBox, tuple[RegionEvidence, ...]]] = []
    for bbox, items, length in measured:
        span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if (
            len(items) >= min_evidence_count
            and span >= min_span_pt
            and length >= min_total_length_pt
            and length >= min_relative_length * largest_length
        ):
            qualifying.append((bbox, items))

    if len(qualifying) <= 1:
        return DrawingRegionSplit(
            regions=(),
            sheet_texts=(),
            overlapping=False,
            qualifying_cluster_count=len(qualifying),
            cluster_count=len(clusters),
            single_region_bbox_pt=qualifying[0][0] if qualifying else None,
        )

    qualifying.sort(key=lambda item: (round(item[0][0], 3), -round(item[0][3], 3), item[0]))
    boxes = tuple(bbox for bbox, _ in qualifying)
    expanded = tuple(_expand(bbox, margin_pt) for bbox in boxes)
    overlapping = any(
        _boxes_overlap(expanded[first], expanded[second])
        for first in range(len(expanded))
        for second in range(first + 1, len(expanded))
    )
    assigned_texts, sheet_texts = _assign_texts(
        page.texts,
        boxes,
        excluded_text_ids=excluded_text_ids,
        text_margin_pt=text_margin_pt,
    )

    regions: list[SourceDrawingRegion] = []
    for index, (bbox, items) in enumerate(qualifying):
        scope = expanded[index]
        others = tuple(box for other, box in enumerate(expanded) if other != index)
        lines = tuple(
            line for line in page.lines
            if _contains(scope, line.start_pt)
            and _contains(scope, line.end_pt)
            and not any(
                _contains(box, line.start_pt) or _contains(box, line.end_pt)
                for box in others
            )
        )
        rects = tuple(
            rect for rect in page.rects
            if _contains(scope, (rect.bbox_pt[0], rect.bbox_pt[1]))
            and _contains(scope, (rect.bbox_pt[2], rect.bbox_pt[3]))
            and not any(_boxes_overlap(box, rect.bbox_pt) for box in others)
        )
        sub_page = PdfPageObservation(
            page_number=page.page_number,
            width_pt=page.width_pt,
            height_pt=page.height_pt,
            texts=tuple(assigned_texts[index]),
            lines=lines,
            rects=rects,
            hidden_wall_source_present=page.hidden_wall_source_present,
        )
        regions.append(
            SourceDrawingRegion(
                index=index + 1,
                bbox_pt=bbox,
                scope_bbox_pt=scope,
                evidence_count=len(items),
                evidence_element_ids=tuple(
                    sorted({element for item in items for element in item.source_element_ids})
                ),
                page=sub_page,
                signature=_signature(items, bbox),
            )
        )
    return DrawingRegionSplit(
        regions=tuple(regions),
        sheet_texts=tuple(sheet_texts),
        overlapping=overlapping,
        qualifying_cluster_count=len(qualifying),
        cluster_count=len(clusters),
        single_region_bbox_pt=None,
    )
