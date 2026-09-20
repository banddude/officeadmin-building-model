"""Deterministic architectural-PDF to canonical-model importer.

The importer is deliberately conservative.  It promotes source observations only
when scale, registration, semantic identity, and the contract-required 3D values
can be supported.  Unresolved facts are retained as explicit diagnostics in the
canonical model's source-specific attributes rather than guessed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median
from typing import Iterable

from oabm.model import (
    BuildingModel,
    Ceiling,
    CoordinateSystem,
    Level,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Provenance,
    Quaternion,
    Size3,
    Slab,
    Space,
    Wall,
    stable_id,
)

from .extract import extract_pdf
from .types import (
    ImportOptions,
    LevelOverride,
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
    RegistrationHint,
    ScaleOverride,
)

_INCH_M = 0.0254
_PT_PER_INCH = 72.0
_VECTOR_AXIS_TOLERANCE_PT = 0.05
_DEFAULT_GEOMETRIC_WALL_HEIGHT_M = 2.7432
_WALL_ID_ROUND_DIGITS = 4
_SHEET_GEOMETRY_REGISTRATION_CONFIDENCE = 0.40
_STRONG_TITLE_BLOCK_RE = re.compile(
    r"^(?:SHEET|DRAWING|PROJECT|TITLE|DATE|DRAWN(?:\s+BY)?|CHECKED(?:\s+BY)?|REVISION|REV|ISSUE)\b",
    re.IGNORECASE,
)
_EXPLICIT_ROOM_LABEL_RE = re.compile(
    r"^(?:ROOM|SPACE)\s*[:#-]?\s*(.+)$",
    re.IGNORECASE,
)
_KEYNOTE_RE = re.compile(
    r"^(?:KEY\s*NOTES?|KEYNOTES?|GENERAL\s+NOTES?|NOTES?)\b",
    re.IGNORECASE,
)
_TITLE_BLOCK_PATTERNS = (
    re.compile(r"\bFLOOR\s+PLAN\b", re.IGNORECASE),
    re.compile(r"^SCALE\b", re.IGNORECASE),
    re.compile(r"^(?:LEVEL|ELEVATION)\s*:", re.IGNORECASE),
    re.compile(
        r"^(?:SHEET|DRAWING|PROJECT|TITLE|DATE|DRAWN(?:\s+BY)?|"
        r"CHECKED(?:\s+BY)?|REVISION|REV|ISSUE)\b",
        re.IGNORECASE,
    ),
)
_ROOM_NUMBER_RE = re.compile(
    r"^(?:\d+[A-Z]?|[A-Z]{1,4}[-.]?\d+[A-Z0-9.-]*)$",
    re.IGNORECASE,
)
_LEADER_TAG_RE = re.compile(
    r"^(?:\d{1,4}[A-Z]?|[A-Z]{1,4}[-.]?\d{0,4})$",
    re.IGNORECASE,
)
_ROOM_LABEL_AMBIGUITY_MARGIN = 0.06


@dataclass(frozen=True, slots=True)
class SheetClassification:
    kind: str
    confidence: float
    architectural_score: int
    electrical_score: int


@dataclass(frozen=True, slots=True)
class _Scale:
    meters_per_point: float
    confidence: float
    method: str
    source_text: str | None


@dataclass(frozen=True, slots=True)
class _Transform2D:
    meters_per_point: float
    rotation_radians: float
    tx_m: float
    ty_m: float
    method: str
    confidence: float

    def apply(self, point_pt: tuple[float, float]) -> tuple[float, float]:
        x = point_pt[0] * self.meters_per_point
        y = point_pt[1] * self.meters_per_point
        c = math.cos(self.rotation_radians)
        s = math.sin(self.rotation_radians)
        return (c * x - s * y + self.tx_m, s * x + c * y + self.ty_m)


@dataclass(frozen=True, slots=True)
class _Measurement:
    value_m: float
    confidence: float
    priority: int
    method: str
    page_number: int
    source_text: str | None = None
    source_element_id: str | None = None


@dataclass(frozen=True, slots=True)
class _LevelInfo:
    anchor: str
    name: str
    elevation: _Measurement
    height: _Measurement | None

    @property
    def elevation_m(self) -> float:
        return self.elevation.value_m

    @property
    def height_m(self) -> float | None:
        return self.height.value_m if self.height is not None else None

    @property
    def elevation_confidence(self) -> float:
        return self.elevation.confidence

    @property
    def height_confidence(self) -> float | None:
        return self.height.confidence if self.height is not None else None


@dataclass(frozen=True, slots=True)
class _RoomLabel:
    observation: PdfTextObservation
    name: str
    anchor: str
    usage: str | None
    confidence: float
    source_observations: tuple[PdfTextObservation, ...] = ()
    room_number_pattern: bool = False
    selection_provenance: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _ShellBoundary:
    bbox_pt: tuple[float, float, float, float]
    source_element_ids: tuple[str, ...]
    source_kind: str

    @property
    def element_id(self) -> str:
        return "+".join(self.source_element_ids)

    @property
    def width_pt(self) -> float:
        return self.bbox_pt[2] - self.bbox_pt[0]

    @property
    def height_pt(self) -> float:
        return self.bbox_pt[3] - self.bbox_pt[1]


@dataclass(frozen=True, slots=True)
class _Shell:
    outer: _ShellBoundary
    inner: _ShellBoundary
    room: _RoomLabel
    thickness_x_m: float
    thickness_y_m: float
    geometry_confidence: float = 1.0
    recognition_method: str = "paired_vector_rectangles"


@dataclass(frozen=True, slots=True)
class _WallContext:
    wall: Wall
    page_number: int
    room_anchor: str | None
    source_side: str | None


@dataclass(frozen=True, slots=True)
class _WallFacePair:
    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    thickness_m: float
    source_element_ids: tuple[str, str]
    geometry_anchor: str


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _anchor(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return token or "unnamed"


def _sheet_anchor(page: PdfPageObservation) -> str | None:
    """Return a stable sheet identifier when one is explicitly printed."""

    pattern = re.compile(r"\b([A-Z]{1,3}\d+(?:[.-]\d+)*)\b", re.IGNORECASE)
    for observation in page.texts:
        match = pattern.search(_clean_text(observation.text))
        if match:
            return _anchor(match.group(1))
    return None


def _number(value: str) -> float:
    value = _clean_text(value)
    if " " in value and "/" in value:
        whole, fraction = value.split(" ", 1)
        return float(whole) + _number(fraction)
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    return float(value)


_NUM = r"(?:\d+(?:\.\d+)?|\d+\s+\d+/\d+|\d+/\d+)"
_DIM_RE = re.compile(
    rf"(?:(?P<feet>{_NUM})\s*['′]\s*(?:-\s*(?P<inches>{_NUM})\s*[\"″])?|(?P<only_inches>{_NUM})\s*[\"″])",
    re.IGNORECASE,
)


def _find_dimension(text: str) -> tuple[float, tuple[int, int]] | None:
    match = _DIM_RE.search(text)
    if not match:
        return None
    feet = _number(match.group("feet")) if match.group("feet") else 0.0
    inches = _number(match.group("inches")) if match.group("inches") else 0.0
    if match.group("only_inches"):
        inches = _number(match.group("only_inches"))
    return ((feet * 12.0 + inches) * _INCH_M, match.span())


def classify_page(page: PdfPageObservation) -> SheetClassification:
    text = "\n".join(item.text.upper() for item in page.texts)
    architectural_score = 0
    electrical_score = 0
    for phrase, weight in (
        ("FLOOR PLAN", 5),
        ("ARCHITECTURAL", 4),
        ("REFLECTED CEILING", 2),
        ("ROOM", 1),
        ("LEVEL", 1),
    ):
        if phrase in text:
            architectural_score += weight
    if re.search(r"(?:^|\s)A[-.]?\d", text):
        architectural_score += 2
    for phrase, weight in (
        ("ELECTRICAL", 6),
        ("POWER PLAN", 5),
        ("LIGHTING PLAN", 5),
        ("PANEL SCHEDULE", 5),
        ("ONE-LINE", 4),
        ("SINGLE LINE", 4),
    ):
        if phrase in text:
            electrical_score += weight

    if electrical_score >= 5 and electrical_score > architectural_score:
        confidence = min(1.0, 0.65 + 0.04 * (electrical_score - architectural_score))
        return SheetClassification("electrical", confidence, architectural_score, electrical_score)
    if architectural_score >= 5:
        confidence = min(1.0, 0.7 + 0.03 * architectural_score)
        return SheetClassification("architectural_plan", confidence, architectural_score, electrical_score)
    if "PLAN" in text and architectural_score > electrical_score:
        return SheetClassification("architectural_plan", 0.6, architectural_score, electrical_score)
    return SheetClassification("other", 0.5, architectural_score, electrical_score)


def _scale_candidates(page: PdfPageObservation) -> list[tuple[float, str]]:
    result: list[tuple[float, str]] = []
    imperial = re.compile(
        rf"(?:SCALE\s*[:=]?\s*)?(?P<paper>{_NUM})\s*[\"″]\s*=\s*"
        rf"(?P<feet>{_NUM})\s*['′]\s*(?:-\s*(?P<inches>{_NUM})\s*[\"″])?",
        re.IGNORECASE,
    )
    metric = re.compile(r"\bSCALE\s*[:=]?\s*1\s*:\s*(?P<ratio>\d+(?:\.\d+)?)\b", re.IGNORECASE)
    for item in page.texts:
        text = _clean_text(item.text)
        for match in imperial.finditer(text):
            paper_inches = _number(match.group("paper"))
            real_inches = _number(match.group("feet")) * 12.0
            if match.group("inches"):
                real_inches += _number(match.group("inches"))
            if paper_inches > 0 and real_inches > 0:
                ratio = real_inches / paper_inches
                result.append((ratio * _INCH_M / _PT_PER_INCH, match.group(0)))
        for match in metric.finditer(text):
            ratio = float(match.group("ratio"))
            if ratio > 0:
                result.append((ratio * _INCH_M / _PT_PER_INCH, match.group(0)))
    return result


def _find_override(page_number: int, overrides: Iterable[ScaleOverride]) -> ScaleOverride | None:
    matches = [item for item in overrides if item.page_number == page_number]
    if len(matches) > 1:
        raise ValueError(f"multiple scale overrides supplied for page {page_number}")
    return matches[0] if matches else None


def _resolve_scale(
    page: PdfPageObservation,
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
) -> _Scale | None:
    override = _find_override(page.page_number, options.scale_overrides)
    if override:
        return _Scale(override.meters_per_point, override.confidence, override.note, None)

    candidates = _scale_candidates(page)
    if not candidates:
        nts = any("NOT TO SCALE" in item.text.upper() or re.search(r"\bNTS\b", item.text.upper()) for item in page.texts)
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "scale_unresolved",
                "detail": "sheet is marked not-to-scale" if nts else "no supported scale annotation found",
            }
        )
        return None

    first = candidates[0][0]
    conflicting = [item for item in candidates[1:] if abs(item[0] - first) / first > 1e-6]
    if conflicting:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "scale_conflict",
                "detail": "multiple distinct scale annotations were found; supply ScaleOverride",
                "source_text": [item[1] for item in candidates],
            }
        )
        return None
    return _Scale(first, 0.98, "parsed printed scale annotation", candidates[0][1])


def _find_registration(page_number: int, hints: Iterable[RegistrationHint]) -> RegistrationHint | None:
    matches = [item for item in hints if item.page_number == page_number]
    if len(matches) > 1:
        raise ValueError(f"multiple registration hints supplied for page {page_number}")
    return matches[0] if matches else None


def _registration_from_hint(hint: RegistrationHint) -> _Transform2D:
    sax = hint.source_b_pt[0] - hint.source_a_pt[0]
    say = hint.source_b_pt[1] - hint.source_a_pt[1]
    max_ = hint.model_b_m[0] - hint.model_a_m[0]
    may = hint.model_b_m[1] - hint.model_a_m[1]
    source_distance = math.hypot(sax, say)
    model_distance = math.hypot(max_, may)
    meters_per_point = model_distance / source_distance
    rotation = math.atan2(may, max_) - math.atan2(say, sax)
    c = math.cos(rotation)
    s = math.sin(rotation)
    ax = hint.source_a_pt[0] * meters_per_point
    ay = hint.source_a_pt[1] * meters_per_point
    rotated_ax = c * ax - s * ay
    rotated_ay = s * ax + c * ay
    return _Transform2D(
        meters_per_point=meters_per_point,
        rotation_radians=rotation,
        tx_m=hint.model_a_m[0] - rotated_ax,
        ty_m=hint.model_a_m[1] - rotated_ay,
        method=hint.note,
        confidence=hint.confidence,
    )


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _title_block_exclusion(
    page: PdfPageObservation,
) -> tuple[tuple[tuple[float, float, float, float], ...], set[str]]:
    strong_centers = [
        item.center_pt
        for item in page.texts
        if _STRONG_TITLE_BLOCK_RE.search(_clean_text(item.text))
    ]
    if not strong_centers:
        return (), set()

    candidate_boxes = [
        item.bbox_pt for item in _ordinary_vector_rect_loops(page)
    ] + [item.bbox_pt for item in page.rects]
    multi_label_boxes = [
        bbox
        for bbox in candidate_boxes
        if sum(_inside(bbox, center) for center in strong_centers) >= 2
    ]
    exclusion_boxes: list[tuple[float, float, float, float]] = []
    if multi_label_boxes:
        exclusion_boxes.append(
            min(multi_label_boxes, key=lambda bbox: (_bbox_area(bbox), bbox))
        )
    else:
        for center in strong_centers:
            containing = [bbox for bbox in candidate_boxes if _inside(bbox, center)]
            if containing:
                exclusion_boxes.append(
                    min(containing, key=lambda bbox: (_bbox_area(bbox), bbox))
                )

    unique_boxes = tuple(sorted(set(exclusion_boxes)))
    excluded_ids = {
        line.element_id
        for line in page.lines
        if any(
            _inside(bbox, line.start_pt) and _inside(bbox, line.end_pt)
            for bbox in unique_boxes
        )
    }
    return unique_boxes, excluded_ids


def _sheet_geometry_registration(
    page: PdfPageObservation,
    scale: _Scale,
    options: ImportOptions,
) -> tuple[_Transform2D, dict[str, object]] | None:
    if not page.lines:
        return None

    title_block_boxes, excluded_ids = _title_block_exclusion(page)
    provisional = _Transform2D(
        meters_per_point=scale.meters_per_point,
        rotation_radians=0.0,
        tx_m=0.0,
        ty_m=0.0,
        method="scale-only provisional geometry frame",
        confidence=_SHEET_GEOMETRY_REGISTRATION_CONFIDENCE,
    )
    pairs = _geometric_wall_face_pairs(
        page,
        provisional,
        options,
        excluded_element_ids=excluded_ids,
    )
    loops = _wall_pair_closed_loops(pairs, provisional, options)

    anchor_basis = "drawing_extents"
    if loops:
        _, polygon, _ = max(
            loops,
            key=lambda item: (
                _polygon_area(item[1]),
                tuple(round(value, 9) for point in item[1] for value in point),
            ),
        )
        xs = [point[0] / scale.meters_per_point for point in polygon]
        ys = [point[1] / scale.meters_per_point for point in polygon]
        source_bbox = (min(xs), min(ys), max(xs), max(ys))
        anchor_basis = "largest_closed_wall_loop_bbox"
    else:
        usable = [line for line in page.lines if line.element_id not in excluded_ids]
        if not usable:
            return None
        xs = [value for line in usable for value in (line.start_pt[0], line.end_pt[0])]
        ys = [value for line in usable for value in (line.start_pt[1], line.end_pt[1])]
        source_bbox = (min(xs), min(ys), max(xs), max(ys))

    x0, y0, _, _ = source_bbox
    method = f"sheet geometry {anchor_basis} lower-left registration fallback"
    confidence = min(scale.confidence, _SHEET_GEOMETRY_REGISTRATION_CONFIDENCE)
    transform = _Transform2D(
        meters_per_point=scale.meters_per_point,
        rotation_radians=0.0,
        tx_m=-x0 * scale.meters_per_point,
        ty_m=-y0 * scale.meters_per_point,
        method=method,
        confidence=confidence,
    )
    metadata: dict[str, object] = {
        "method": method,
        "confidence": confidence,
        "anchor_basis": anchor_basis,
        "source_bbox_pt": list(source_bbox),
        "source_anchor_pt": [x0, y0],
        "title_block_excluded": bool(title_block_boxes),
        "title_block_exclusion_boxes_pt": [list(bbox) for bbox in title_block_boxes],
    }
    return transform, metadata


def _resolve_transform(
    page: PdfPageObservation,
    scale: _Scale | None,
    options: ImportOptions,
    *,
    allow_page_local_origin: bool,
    ambiguities: list[dict[str, object]],
) -> tuple[_Transform2D | None, _Scale | None, dict[str, object] | None]:
    hint = _find_registration(page.page_number, options.registrations)
    if hint:
        transform = _registration_from_hint(hint)
        if scale is not None:
            relative_error = abs(transform.meters_per_point - scale.meters_per_point) / scale.meters_per_point
            if relative_error > options.scale_registration_tolerance:
                ambiguities.append(
                    {
                        "page": page.page_number,
                        "code": "scale_registration_conflict",
                        "detail": "two-point registration scale disagrees with the printed/overridden scale",
                        "printed_meters_per_point": scale.meters_per_point,
                        "registered_meters_per_point": transform.meters_per_point,
                    }
                )
                return None, scale, None
        else:
            scale = _Scale(
                transform.meters_per_point,
                hint.confidence,
                "scale resolved by two-point registration",
                None,
            )
        return transform, scale, None

    if scale is None:
        return None, None, None
    if allow_page_local_origin:
        return (
            _Transform2D(
                meters_per_point=scale.meters_per_point,
                rotation_radians=0.0,
                tx_m=0.0,
                ty_m=0.0,
                method="first plan page defines project-local XY origin",
                confidence=1.0,
            ),
            scale,
            None,
        )

    fallback = _sheet_geometry_registration(page, scale, options)
    if fallback is not None:
        transform, metadata = fallback
        return transform, scale, metadata

    ambiguities.append(
        {
            "page": page.page_number,
            "code": "registration_unresolved",
            "detail": "additional architectural plan page requires RegistrationHint before geometry can share the canonical frame",
        }
    )
    return None, scale, None


def _find_level_override(page_number: int, overrides: Iterable[LevelOverride]) -> LevelOverride | None:
    matches = [item for item in overrides if item.page_number == page_number]
    if len(matches) > 1:
        raise ValueError(f"multiple level overrides supplied for page {page_number}")
    return matches[0] if matches else None


def _level_name_from_text(page: PdfPageObservation) -> str | None:
    explicit = re.compile(r"\bLEVEL\s*[:#-]?\s*([A-Z0-9][A-Z0-9 ._-]{0,30})$", re.IGNORECASE)
    floor_suffix = re.compile(r"\b(GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH)\s+FLOOR\b", re.IGNORECASE)
    for item in page.texts:
        text = _clean_text(item.text)
        match = explicit.search(text)
        if match:
            candidate = _clean_text(match.group(1))
            if candidate.upper() != "PLAN":
                return candidate.title() if candidate.isupper() else candidate
        match = floor_suffix.search(text)
        if match:
            return match.group(0).title()
    return None


def _elevation_from_text(page: PdfPageObservation) -> tuple[float, PdfTextObservation] | None:
    for item in page.texts:
        text = _clean_text(item.text)
        upper = text.upper()
        if not ("ELEVATION" in upper or re.search(r"\bEL\.?\s*[:=]", upper)):
            continue
        metric = re.search(r"(-?\d+(?:\.\d+)?)\s*M\b", upper)
        if metric:
            return (float(metric.group(1)), item)
        dim = _find_dimension(text)
        if dim:
            sign = -1.0 if re.search(r"(?:ELEVATION|\bEL\.?)\s*[:=]?\s*-", upper) else 1.0
            return (sign * dim[0], item)
    return None


def _ceiling_height_notes(page: PdfPageObservation) -> tuple[tuple[PdfTextObservation, float], ...]:
    result: list[tuple[PdfTextObservation, float]] = []
    for item in page.texts:
        upper = item.text.upper()
        if "CEILING HEIGHT" in upper or re.search(r"\bCLG(?:\.|\s)\s*(?:HT|HEIGHT)?\b", upper):
            dim = _find_dimension(item.text)
            if dim and dim[0] > 0:
                result.append((item, dim[0]))
    return tuple(result)


def _is_explicit_global_ceiling_height(text: str) -> bool:
    upper = _clean_text(text).upper()
    return any(
        token in upper
        for token in (
            "LEVEL CEILING HEIGHT",
            "FLOOR CEILING HEIGHT",
            "TYPICAL CEILING HEIGHT",
            "TYPICAL CLG",
            "TYP. CLG",
            "TYP CLG",
        )
    )


def _room_scope_boxes(
    page: PdfPageObservation,
    rooms: tuple[_RoomLabel, ...],
) -> dict[str, tuple[float, float, float, float]]:
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for room in rooms:
        if sum(item.anchor == room.anchor for item in rooms) != 1:
            continue
        containing = [rect for rect in page.rects if _inside(rect.bbox_pt, room.observation.center_pt)]
        if not containing:
            continue
        chosen = min(
            containing,
            key=lambda rect: (
                rect.width_pt * rect.height_pt,
                rect.element_id,
            ),
        )
        boxes[room.anchor] = chosen.bbox_pt
    return boxes


def _ceiling_height_scopes(
    page: PdfPageObservation,
) -> tuple[tuple[PdfTextObservation, float, str, str | None], ...]:
    rooms = _room_labels(page)
    boxes = _room_scope_boxes(page, rooms)
    result: list[tuple[PdfTextObservation, float, str, str | None]] = []
    for observation, height_m in _ceiling_height_notes(page):
        upper = _clean_text(observation.text).upper()
        if _is_explicit_global_ceiling_height(observation.text):
            result.append((observation, height_m, "global", None))
            continue

        named = [room.anchor for room in rooms if room.name.upper() in upper]
        if len(named) == 1:
            result.append((observation, height_m, "room", named[0]))
            continue
        if len(named) > 1:
            result.append((observation, height_m, "unresolved", None))
            continue

        contained = [
            anchor
            for anchor, bbox in boxes.items()
            if _inside(bbox, observation.center_pt)
        ]
        if len(contained) == 1:
            result.append((observation, height_m, "room", contained[0]))
        elif len(contained) > 1:
            result.append((observation, height_m, "unresolved", None))
        elif len(rooms) == 1 and rooms[0].anchor in boxes:
            # With exactly one resolved room, an otherwise-unqualified height
            # note can be scoped to that room. It is never promoted to Level.
            result.append((observation, height_m, "room", rooms[0].anchor))
        else:
            result.append((observation, height_m, "unresolved", None))
    return tuple(result)


def _global_ceiling_height_from_text(
    page: PdfPageObservation,
    ambiguities: list[dict[str, object]],
) -> tuple[_Measurement | None, bool]:
    candidates = [
        (observation, height_m)
        for observation, height_m, scope, _ in _ceiling_height_scopes(page)
        if scope == "global"
    ]
    if not candidates:
        return None, False

    first_value = candidates[0][1]
    if any(
        not math.isclose(height_m, first_value, rel_tol=1e-9, abs_tol=1e-6)
        for _, height_m in candidates[1:]
    ):
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "level_height_conflict",
                "detail": (
                    "multiple distinct page/level ceiling-height annotations were found; "
                    "supply LevelOverride to select the intended level height"
                ),
                "source_text": [observation.text for observation, _ in candidates],
            }
        )
        return None, True

    observation, height_m = candidates[0]
    return (
        _Measurement(
            value_m=height_m,
            confidence=0.9,
            priority=2,
            method="height parsed from explicit ceiling-height annotation",
            page_number=page.page_number,
            source_text=observation.text,
            source_element_id=observation.element_id,
        ),
        False,
    )


def _room_ceiling_height_evidence(
    page: PdfPageObservation,
    shells: tuple[_Shell, ...],
    ambiguities: list[dict[str, object]],
) -> tuple[dict[str, _Measurement], set[str]]:
    shell_anchors = {shell.room.anchor for shell in shells}
    candidates: dict[str, list[tuple[PdfTextObservation, float]]] = {}
    for observation, height_m, scope, room_anchor in _ceiling_height_scopes(page):
        if scope == "unresolved":
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "ceiling_height_scope_unresolved",
                    "detail": (
                        f"ceiling-height annotation {observation.text!r} could not be scoped "
                        "to one room or to the level"
                    ),
                    "source_element_id": observation.element_id,
                }
            )
            continue
        if scope != "room" or room_anchor not in shell_anchors:
            continue
        candidates.setdefault(room_anchor, []).append((observation, height_m))

    result: dict[str, _Measurement] = {}
    blocked: set[str] = set()
    for anchor in sorted(candidates):
        items = candidates[anchor]
        first_value = items[0][1]
        if any(
            not math.isclose(height_m, first_value, rel_tol=1e-9, abs_tol=1e-6)
            for _, height_m in items[1:]
        ):
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "room_ceiling_height_conflict",
                    "detail": (
                        f"room {anchor!r} has multiple distinct ceiling-height annotations; "
                        "room height and 3D wall/ceiling geometry were left unresolved"
                    ),
                    "source_text": [observation.text for observation, _ in items],
                }
            )
            blocked.add(anchor)
            continue
        observation, height_m = items[0]
        result[anchor] = _Measurement(
            value_m=height_m,
            confidence=0.9,
            priority=2,
            method="height parsed from room-scoped ceiling-height annotation",
            page_number=page.page_number,
            source_text=observation.text,
            source_element_id=observation.element_id,
        )
    return result, blocked


def _slab_thickness_from_text(page: PdfPageObservation) -> tuple[float, str] | None:
    for item in page.texts:
        upper = item.text.upper()
        if "SLAB" in upper and ("FLOOR" in upper or "THICK" in upper):
            dim = _find_dimension(item.text)
            if dim and dim[0] > 0:
                return (dim[0], item.text)
    return None


def _reconcile_measurement(
    existing: _Measurement,
    candidate: _Measurement,
    *,
    anchor: str,
    field: str,
    ambiguities: list[dict[str, object]],
) -> tuple[_Measurement, bool]:
    if math.isclose(existing.value_m, candidate.value_m, rel_tol=1e-9, abs_tol=1e-6):
        return (candidate if candidate.priority > existing.priority else existing), False

    if candidate.priority > existing.priority:
        ambiguities.append(
            {
                "page": candidate.page_number,
                "code": f"level_{field}_reconciled",
                "detail": (
                    f"level {anchor!r} {field} was replaced by higher-priority evidence"
                ),
                "previous_value_m": existing.value_m,
                "previous_page": existing.page_number,
                "selected_value_m": candidate.value_m,
                "selected_page": candidate.page_number,
            }
        )
        return candidate, False

    if candidate.priority < existing.priority:
        ambiguities.append(
            {
                "page": candidate.page_number,
                "code": f"level_{field}_conflict",
                "detail": (
                    f"level {anchor!r} has conflicting lower-priority {field} evidence; "
                    "the existing higher-priority value was retained"
                ),
                "selected_value_m": existing.value_m,
                "selected_page": existing.page_number,
                "conflicting_value_m": candidate.value_m,
                "conflicting_page": candidate.page_number,
                "resolution": "kept higher-priority evidence",
                "selected_source_text": existing.source_text,
                "selected_source_element_id": existing.source_element_id,
                "conflicting_source_text": candidate.source_text,
                "conflicting_source_element_id": candidate.source_element_id,
            }
        )
        return existing, False

    ambiguities.append(
        {
            "page": candidate.page_number,
            "code": f"level_{field}_conflict",
            "detail": (
                f"level {anchor!r} has conflicting equally authoritative {field} evidence; "
                "supply LevelOverride to resolve it"
            ),
            "existing_value_m": existing.value_m,
            "existing_page": existing.page_number,
            "existing_source_text": existing.source_text,
            "existing_source_element_id": existing.source_element_id,
            "conflicting_value_m": candidate.value_m,
            "conflicting_page": candidate.page_number,
            "conflicting_source_text": candidate.source_text,
            "conflicting_source_element_id": candidate.source_element_id,
            "resolution": "current page skipped until explicitly resolved",
        }
    )
    return replace(
        existing,
        confidence=min(existing.confidence, candidate.confidence, 0.5),
    ), True


def _resolve_level(
    page: PdfPageObservation,
    options: ImportOptions,
    known_levels: dict[str, _LevelInfo],
    ambiguities: list[dict[str, object]],
) -> _LevelInfo | None:
    override = _find_level_override(page.page_number, options.level_overrides)
    parsed_name = _level_name_from_text(page)
    name = override.name if override and override.name else parsed_name or "Unlabeled Level"
    anchor = _anchor(name)
    existing = known_levels.get(anchor)

    global_height, global_height_conflict = _global_ceiling_height_from_text(page, ambiguities)
    if global_height_conflict:
        return None

    parsed_elevation = _elevation_from_text(page)
    elevation_candidate: _Measurement | None = None
    if override:
        elevation_candidate = _Measurement(
            value_m=override.elevation_m,
            confidence=override.confidence,
            priority=3,
            method=override.note,
            page_number=page.page_number,
        )
    elif parsed_elevation:
        elevation_candidate = _Measurement(
            value_m=parsed_elevation[0],
            confidence=0.95,
            priority=2,
            method="parsed explicit level elevation",
            page_number=page.page_number,
            source_text=parsed_elevation[1].text,
            source_element_id=parsed_elevation[1].element_id,
        )
    elif existing is None:
        if not known_levels:
            elevation_candidate = _Measurement(
                value_m=0.0,
                confidence=0.55,
                priority=1,
                method="sole/first plan level assigned project-local elevation datum 0 m",
                page_number=page.page_number,
            )
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "level_elevation_local_datum",
                    "detail": "no elevation annotation found; this first level defines local Z=0",
                    "level_anchor": anchor,
                }
            )
        else:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "level_elevation_unresolved",
                    "detail": f"level {name!r} has no elevation relative to existing levels; supply LevelOverride",
                }
            )
            return None

    height_candidate: _Measurement | None = None
    if override and override.height_m is not None:
        height_candidate = _Measurement(
            value_m=override.height_m,
            confidence=override.confidence,
            priority=3,
            method=override.note,
            page_number=page.page_number,
        )
    elif global_height is not None:
        height_candidate = global_height
    elif options.default_wall_height_m is not None and (existing is None or existing.height is None):
        height_candidate = _Measurement(
            value_m=options.default_wall_height_m,
            confidence=options.assumed_value_confidence,
            priority=1,
            method="height supplied explicitly by ImportOptions.default_wall_height_m",
            page_number=page.page_number,
        )
    elif page.lines and (existing is None or existing.height is None):
        height_candidate = _Measurement(
            value_m=_DEFAULT_GEOMETRIC_WALL_HEIGHT_M,
            confidence=options.assumed_value_confidence,
            priority=1,
            method=(
                "low-confidence default height used only to materialize geometrically "
                "paired PDF wall faces when no explicit level/ceiling height is available"
            ),
            page_number=page.page_number,
        )
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "level_height_default_assumed",
                "detail": (
                    "no explicit level/ceiling height was found on a vector-geometry page; "
                    "a low-confidence 9 ft default was retained so eligible geometric wall "
                    "pairs can materialize without implying measured height"
                ),
                "level_anchor": anchor,
                "assumed_value_m": _DEFAULT_GEOMETRIC_WALL_HEIGHT_M,
                "confidence": options.assumed_value_confidence,
            }
        )

    if existing is None:
        assert elevation_candidate is not None
        return _LevelInfo(
            anchor=anchor,
            name=name,
            elevation=elevation_candidate,
            height=height_candidate,
        )

    elevation = existing.elevation
    if elevation_candidate is not None:
        elevation, blocked = _reconcile_measurement(
            elevation,
            elevation_candidate,
            anchor=anchor,
            field="elevation",
            ambiguities=ambiguities,
        )
        if blocked:
            known_levels[anchor] = replace(existing, elevation=elevation)
            return None

    height = existing.height
    if height_candidate is not None:
        if height is None:
            height = height_candidate
        else:
            height, blocked = _reconcile_measurement(
                height,
                height_candidate,
                anchor=anchor,
                field="height",
                ambiguities=ambiguities,
            )
            if blocked:
                known_levels[anchor] = replace(existing, elevation=elevation, height=height)
                return None

    resolved_name = name if override and override.name else existing.name
    return _LevelInfo(
        anchor=anchor,
        name=resolved_name,
        elevation=elevation,
        height=height,
    )


def _is_title_block_string(text: str) -> bool:
    return any(pattern.search(text) for pattern in _TITLE_BLOCK_PATTERNS)


def _observation_font_size(observation: PdfTextObservation) -> float:
    if observation.font_size_pt is not None:
        return observation.font_size_pt
    return max(0.1, observation.bbox_pt[3] - observation.bbox_pt[1])


def _page_median_font_size(page: PdfPageObservation) -> float:
    values = [
        _observation_font_size(observation)
        for observation in page.texts
        if _clean_text(observation.text)
    ]
    return float(median(values)) if values else 1.0


def _room_label_sources(room: _RoomLabel) -> tuple[PdfTextObservation, ...]:
    return room.source_observations or (room.observation,)


def _room_label_center_pt(room: _RoomLabel) -> tuple[float, float]:
    observations = _room_label_sources(room)
    return (
        sum(item.center_pt[0] for item in observations) / len(observations),
        sum(item.center_pt[1] for item in observations) / len(observations),
    )


def _room_label_font_size(room: _RoomLabel) -> float:
    return max(_observation_font_size(item) for item in _room_label_sources(room))


def _point_near_bbox(
    point: tuple[float, float],
    bbox: tuple[float, float, float, float],
    tolerance: float,
) -> bool:
    return (
        bbox[0] - tolerance <= point[0] <= bbox[2] + tolerance
        and bbox[1] - tolerance <= point[1] <= bbox[3] + tolerance
    )


def _is_leader_tag(page: PdfPageObservation, observation: PdfTextObservation) -> bool:
    text = _clean_text(observation.text)
    if not _LEADER_TAG_RE.fullmatch(text):
        return False
    text_height = max(1.0, observation.bbox_pt[3] - observation.bbox_pt[1])
    tolerance = max(1.5, text_height * 0.35)
    for line in page.lines:
        near_start = _point_near_bbox(line.start_pt, observation.bbox_pt, tolerance)
        near_end = _point_near_bbox(line.end_pt, observation.bbox_pt, tolerance)
        if near_start == near_end:
            continue
        if math.dist(line.start_pt, line.end_pt) < text_height * 1.5:
            continue
        return True
    return False


def _room_label_candidate(
    page: PdfPageObservation,
    observation: PdfTextObservation,
) -> _RoomLabel | None:
    text = _clean_text(observation.text)
    if not text:
        return None

    explicit = _EXPLICIT_ROOM_LABEL_RE.match(text)
    name = _clean_text(explicit.group(1)) if explicit else text
    if not name:
        return None

    # Position establishes room-label semantics. Lexical and leader rules are
    # exclusions only, so arbitrary project-specific labels stay eligible.
    if _find_dimension(name) is not None:
        return None
    if _KEYNOTE_RE.match(name):
        return None
    if _is_title_block_string(name):
        return None
    if _is_leader_tag(page, observation):
        return None

    return _RoomLabel(
        observation=observation,
        name=name,
        anchor=_anchor(name),
        usage=None,
        confidence=0.98 if explicit else 0.88,
        source_observations=(observation,),
        room_number_pattern=bool(_ROOM_NUMBER_RE.fullmatch(name)),
    )


def _pair_adjacent_room_number_and_name(
    page: PdfPageObservation,
    labels: tuple[_RoomLabel, ...],
) -> tuple[_RoomLabel, ...]:
    if len(labels) < 2:
        return labels

    page_median = _page_median_font_size(page)
    used_ids: set[str] = set()
    combined: list[_RoomLabel] = []
    ordered_numbers = sorted(
        (label for label in labels if label.room_number_pattern),
        key=lambda item: (item.observation.element_id, item.anchor),
    )
    for number in ordered_numbers:
        number_obs = number.observation
        number_size = _room_label_font_size(number)
        candidates: list[tuple[float, float, str, _RoomLabel]] = []
        for name in labels:
            if name is number or name.room_number_pattern:
                continue
            if name.observation.element_id in used_ids:
                continue
            name_obs = name.observation
            name_size = _room_label_font_size(name)
            vertical_gap = abs(name_obs.center_pt[1] - number_obs.center_pt[1])
            if vertical_gap <= max(1.0, 0.35 * max(name_size, number_size)):
                continue
            if vertical_gap > max(18.0, 2.4 * page_median):
                continue
            horizontal_gap = abs(name_obs.center_pt[0] - number_obs.center_pt[0])
            half_span = max(
                name_obs.bbox_pt[2] - name_obs.bbox_pt[0],
                number_obs.bbox_pt[2] - number_obs.bbox_pt[0],
                12.0,
            ) / 2.0
            if horizontal_gap > half_span + page_median:
                continue
            if name_size < page_median * 0.70:
                continue
            candidates.append(
                (vertical_gap, horizontal_gap, name_obs.element_id, name)
            )
        if not candidates:
            continue
        _, _, _, name = min(candidates)
        observations = tuple(
            sorted(
                (name.observation, number.observation),
                key=lambda item: (-item.center_pt[1], item.element_id),
            )
        )
        display_parts = []
        for item in observations:
            candidate = _clean_text(item.text)
            explicit = _EXPLICIT_ROOM_LABEL_RE.match(candidate)
            if explicit:
                candidate = _clean_text(explicit.group(1))
            display_parts.append(candidate)
        combined.append(
            _RoomLabel(
                observation=number.observation,
                name=" ".join(display_parts),
                anchor=number.anchor,
                usage=None,
                confidence=min(0.94, max(number.confidence, name.confidence) + 0.04),
                source_observations=observations,
                room_number_pattern=True,
            )
        )
        used_ids.update(
            (number.observation.element_id, name.observation.element_id)
        )

    result = [
        label
        for label in labels
        if label.observation.element_id not in used_ids
    ]
    result.extend(combined)
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.anchor,
                tuple(obs.element_id for obs in _room_label_sources(item)),
            ),
        )
    )


def _room_labels(page: PdfPageObservation) -> tuple[_RoomLabel, ...]:
    return tuple(
        sorted(
            (
                label
                for observation in page.texts
                if (label := _room_label_candidate(page, observation)) is not None
            ),
            key=lambda item: (item.anchor, item.observation.element_id),
        )
    )


def _polygon_center_and_radius(
    polygon: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], float]:
    center = (
        sum(point[0] for point in polygon) / len(polygon),
        sum(point[1] for point in polygon) / len(polygon),
    )
    radius = max(math.dist(center, point) for point in polygon)
    return center, max(radius, 1e-9)


def _label_candidate_summary(
    room: _RoomLabel,
    ranking: dict[str, object],
) -> dict[str, object]:
    return {
        "text": room.name,
        "source_text_elements": [
            item.element_id for item in _room_label_sources(room)
        ],
        "score": ranking["score"],
        "centrality": ranking["centrality"],
        "font_size_pt": ranking["font_size_pt"],
        "font_size_ratio": ranking["font_size_ratio"],
        "room_number_pattern": room.room_number_pattern,
    }


def _select_room_label(
    page: PdfPageObservation,
    rooms: Iterable[_RoomLabel],
    polygon: tuple[tuple[float, float], ...],
    ambiguities: list[dict[str, object]],
    *,
    transform: _Transform2D | None = None,
    enclosure_attributes: dict[str, object] | None = None,
) -> _RoomLabel | None:
    candidates = _pair_adjacent_room_number_and_name(page, tuple(rooms))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    center, radius = _polygon_center_and_radius(polygon)
    page_median = _page_median_font_size(page)
    ranked: list[tuple[_RoomLabel, dict[str, object]]] = []
    for room in candidates:
        point = _room_label_center_pt(room)
        if transform is not None:
            point = transform.apply(point)
        centrality = max(0.0, 1.0 - math.dist(point, center) / radius)
        font_size = _room_label_font_size(room)
        font_ratio = font_size / page_median if page_median > 0 else 1.0
        font_score = min(1.0, max(0.0, font_ratio / 1.5))
        score = (
            0.60 * centrality
            + 0.25 * font_score
            + 0.15 * (1.0 if room.room_number_pattern else 0.0)
        )
        ranked.append(
            (
                room,
                {
                    "score": round(score, 6),
                    "centrality": round(centrality, 6),
                    "font_size_pt": round(font_size, 6),
                    "page_median_font_size_pt": round(page_median, 6),
                    "font_size_ratio": round(font_ratio, 6),
                },
            )
        )

    ranked.sort(
        key=lambda item: (
            -float(item[1]["score"]),
            -float(item[1]["centrality"]),
            -float(item[1]["font_size_ratio"]),
            -int(item[0].room_number_pattern),
            item[0].anchor,
            tuple(obs.element_id for obs in _room_label_sources(item[0])),
        )
    )
    winner, winner_ranking = ranked[0]
    runner_ups = [
        _label_candidate_summary(room, ranking)
        for room, ranking in ranked[1:]
    ]
    selection_provenance: dict[str, object] = {
        "selection_method": "enclosure_room_label_ranking",
        "score": winner_ranking["score"],
        "centrality": winner_ranking["centrality"],
        "font_size_pt": winner_ranking["font_size_pt"],
        "page_median_font_size_pt": winner_ranking["page_median_font_size_pt"],
        "font_size_ratio": winner_ranking["font_size_ratio"],
        "room_number_pattern": winner.room_number_pattern,
        "source_text_elements": [
            item.element_id for item in _room_label_sources(winner)
        ],
        "runner_ups": runner_ups,
    }
    selected_confidence = winner.confidence
    if ranked[1][1]["score"] is not None:
        margin = float(winner_ranking["score"]) - float(ranked[1][1]["score"])
        if margin <= _ROOM_LABEL_AMBIGUITY_MARGIN + 1e-12:
            selected_confidence = min(selected_confidence, 0.65)
            ambiguity = {
                "page": page.page_number,
                "code": "multiple_room_labels_in_enclosure",
                "detail": (
                    "top two room-label candidates are within the ranking margin; "
                    "the deterministic top candidate was retained with reduced confidence"
                ),
                "ranking_margin": round(margin, 6),
                "ambiguity_margin": _ROOM_LABEL_AMBIGUITY_MARGIN,
                "selected": _label_candidate_summary(winner, winner_ranking),
                "runner_up": _label_candidate_summary(*ranked[1]),
            }
            if enclosure_attributes:
                ambiguity.update(enclosure_attributes)
            ambiguities.append(ambiguity)

    return replace(
        winner,
        confidence=selected_confidence,
        selection_provenance=selection_provenance,
    )

def _inside(bbox: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _shell_candidates(
    page: PdfPageObservation,
    scale: _Scale,
    rooms: tuple[_RoomLabel, ...],
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
) -> tuple[_Shell, ...]:
    candidates: list[tuple[float, float, PdfRectObservation, PdfRectObservation]] = []
    for outer in page.rects:
        for inner in page.rects:
            if outer is inner:
                continue
            ox0, oy0, ox1, oy1 = outer.bbox_pt
            ix0, iy0, ix1, iy1 = inner.bbox_pt
            if not (ox0 < ix0 < ix1 < ox1 and oy0 < iy0 < iy1 < oy1):
                continue
            tx_left = (ix0 - ox0) * scale.meters_per_point
            tx_right = (ox1 - ix1) * scale.meters_per_point
            ty_bottom = (iy0 - oy0) * scale.meters_per_point
            ty_top = (oy1 - iy1) * scale.meters_per_point
            thickness_x = (tx_left + tx_right) / 2.0
            thickness_y = (ty_bottom + ty_top) / 2.0
            if not (options.min_wall_thickness_m <= thickness_x <= options.max_wall_thickness_m):
                continue
            if not (options.min_wall_thickness_m <= thickness_y <= options.max_wall_thickness_m):
                continue
            if abs(tx_left - tx_right) > max(0.03, thickness_x * 0.35):
                continue
            if abs(ty_bottom - ty_top) > max(0.03, thickness_y * 0.35):
                continue
            if inner.width_pt * scale.meters_per_point < options.min_space_span_m:
                continue
            if inner.height_pt * scale.meters_per_point < options.min_space_span_m:
                continue
            area_m2 = inner.width_pt * inner.height_pt * scale.meters_per_point**2
            asymmetry = abs(thickness_x - thickness_y)
            candidates.append((area_m2, asymmetry, outer, inner))

    rooms_by_pair: dict[
        tuple[str, str],
        tuple[PdfRectObservation, PdfRectObservation, list[_RoomLabel]],
    ] = {}
    for room in sorted(rooms, key=lambda item: (item.anchor, item.observation.element_id)):
        containing = [
            item
            for item in candidates
            if _inside(item[3].bbox_pt, _room_label_center_pt(room))
        ]
        if not containing:
            continue
        _, _, outer, inner = min(
            containing,
            key=lambda item: (
                item[0],
                item[1],
                item[2].element_id,
                item[3].element_id,
            ),
        )
        pair = (outer.element_id, inner.element_id)
        if pair not in rooms_by_pair:
            rooms_by_pair[pair] = (outer, inner, [])
        rooms_by_pair[pair][2].append(room)

    selected: list[_Shell] = []
    for pair in sorted(rooms_by_pair):
        outer, inner, contained_rooms = rooms_by_pair[pair]
        ix0, iy0, ix1, iy1 = inner.bbox_pt
        polygon = ((ix0, iy0), (ix1, iy0), (ix1, iy1), (ix0, iy1))
        room = _select_room_label(
            page,
            contained_rooms,
            polygon,
            ambiguities,
            enclosure_attributes={"source_rectangles": list(pair)},
        )
        if room is None:
            continue
        ox0, oy0, ox1, oy1 = outer.bbox_pt
        selected.append(
            _Shell(
                outer=_ShellBoundary(
                    bbox_pt=outer.bbox_pt,
                    source_element_ids=(outer.element_id,),
                    source_kind="rect",
                ),
                inner=_ShellBoundary(
                    bbox_pt=inner.bbox_pt,
                    source_element_ids=(inner.element_id,),
                    source_kind="rect",
                ),
                room=room,
                thickness_x_m=((ix0 - ox0) + (ox1 - ix1)) * scale.meters_per_point / 2.0,
                thickness_y_m=((iy0 - oy0) + (oy1 - iy1)) * scale.meters_per_point / 2.0,
            )
        )

    duplicate_selected_anchors = {
        shell.room.anchor
        for shell in selected
        if sum(item.room.anchor == shell.room.anchor for item in selected) > 1
    }
    for anchor in sorted(duplicate_selected_anchors):
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "duplicate_room_label",
                "detail": (
                    f"selected room label {anchor!r} is not unique on the level; "
                    "those enclosures are not promoted"
                ),
            }
        )
    if duplicate_selected_anchors:
        selected = [
            shell for shell in selected if shell.room.anchor not in duplicate_selected_anchors
        ]

    if candidates and not selected and not rooms:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "unlabeled_enclosure",
                "detail": "wall-like nested rectangles were found but no stable room/space label anchors their identity",
            }
        )
    return tuple(selected)

def _axis_bucket(value: float) -> int:
    return int(round(value / _VECTOR_AXIS_TOLERANCE_PT))


def _ordinary_vector_rect_loops(page: PdfPageObservation) -> tuple[_ShellBoundary, ...]:
    """Resolve exact closed rectangular loops from ordinary untagged line primitives.

    This intentionally recognizes only one conservative vector family. Lines must
    be axis-aligned within a tight PDF-point tolerance and four distinct source
    elements must close the same rectangle. Tagged/native lines stay owned by the
    existing native-MCID wall path.
    """

    horizontal: dict[tuple[int, int], list[tuple[float, float, float, PdfLineObservation]]] = {}
    vertical: dict[tuple[int, int, int], list[tuple[float, float, float, PdfLineObservation]]] = {}

    for line in page.lines:
        if line.native_id:
            continue
        ax, ay = line.start_pt
        bx, by = line.end_pt
        dx = bx - ax
        dy = by - ay
        if abs(dy) <= _VECTOR_AXIS_TOLERANCE_PT and abs(dx) > _VECTOR_AXIS_TOLERANCE_PT:
            x0, x1 = sorted((ax, bx))
            y = (ay + by) / 2.0
            horizontal.setdefault((_axis_bucket(x0), _axis_bucket(x1)), []).append((x0, x1, y, line))
        elif abs(dx) <= _VECTOR_AXIS_TOLERANCE_PT and abs(dy) > _VECTOR_AXIS_TOLERANCE_PT:
            y0, y1 = sorted((ay, by))
            x = (ax + bx) / 2.0
            vertical.setdefault(
                (_axis_bucket(x), _axis_bucket(y0), _axis_bucket(y1)),
                [],
            ).append((x, y0, y1, line))

    by_bbox: dict[
        tuple[int, int, int, int],
        dict[tuple[str, ...], tuple[float, float, float, float]],
    ] = {}
    for _, horizontals in sorted(horizontal.items()):
        ordered = sorted(horizontals, key=lambda item: (item[2], item[3].element_id))
        for index, lower in enumerate(ordered):
            for upper in ordered[index + 1 :]:
                if upper[2] - lower[2] <= _VECTOR_AXIS_TOLERANCE_PT:
                    continue
                x0 = (lower[0] + upper[0]) / 2.0
                x1 = (lower[1] + upper[1]) / 2.0
                y0 = lower[2]
                y1 = upper[2]
                left = vertical.get(
                    (_axis_bucket(x0), _axis_bucket(y0), _axis_bucket(y1)),
                    [],
                )
                right = vertical.get(
                    (_axis_bucket(x1), _axis_bucket(y0), _axis_bucket(y1)),
                    [],
                )
                for left_item in left:
                    for right_item in right:
                        if left_item[3].element_id == right_item[3].element_id:
                            continue
                        if not (
                            abs(left_item[0] - x0) <= _VECTOR_AXIS_TOLERANCE_PT
                            and abs(right_item[0] - x1) <= _VECTOR_AXIS_TOLERANCE_PT
                            and abs(left_item[1] - y0) <= _VECTOR_AXIS_TOLERANCE_PT
                            and abs(left_item[2] - y1) <= _VECTOR_AXIS_TOLERANCE_PT
                            and abs(right_item[1] - y0) <= _VECTOR_AXIS_TOLERANCE_PT
                            and abs(right_item[2] - y1) <= _VECTOR_AXIS_TOLERANCE_PT
                        ):
                            continue
                        bbox = (
                            (lower[0] + upper[0] + 2.0 * left_item[0]) / 4.0,
                            (2.0 * lower[2] + left_item[1] + right_item[1]) / 4.0,
                            (lower[1] + upper[1] + 2.0 * right_item[0]) / 4.0,
                            (2.0 * upper[2] + left_item[2] + right_item[2]) / 4.0,
                        )
                        ids = tuple(
                            sorted(
                                {
                                    lower[3].element_id,
                                    upper[3].element_id,
                                    left_item[3].element_id,
                                    right_item[3].element_id,
                                }
                            )
                        )
                        if len(ids) != 4:
                            continue
                        key = tuple(_axis_bucket(value) for value in bbox)
                        by_bbox.setdefault(key, {})[ids] = bbox

    result: list[_ShellBoundary] = []
    for key in sorted(by_bbox):
        source_sets = by_bbox[key]
        # Multiple distinct four-line proofs for the same loop are not selected
        # by ordering. Leave that boundary unsupported instead.
        if len(source_sets) != 1:
            continue
        ids, bbox = next(iter(source_sets.items()))
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        result.append(
            _ShellBoundary(
                bbox_pt=bbox,
                source_element_ids=ids,
                source_kind="ordinary_vector_line_loop",
            )
        )
    return tuple(result)


def _ordinary_vector_shell_candidates(
    page: PdfPageObservation,
    scale: _Scale,
    rooms: tuple[_RoomLabel, ...],
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
    *,
    excluded_room_anchors: set[str],
) -> tuple[_Shell, ...]:
    loops = _ordinary_vector_rect_loops(page)
    candidates: list[tuple[float, float, _ShellBoundary, _ShellBoundary]] = []
    for outer in loops:
        for inner in loops:
            if outer is inner:
                continue
            ox0, oy0, ox1, oy1 = outer.bbox_pt
            ix0, iy0, ix1, iy1 = inner.bbox_pt
            if not (ox0 < ix0 < ix1 < ox1 and oy0 < iy0 < iy1 < oy1):
                continue
            tx_left = (ix0 - ox0) * scale.meters_per_point
            tx_right = (ox1 - ix1) * scale.meters_per_point
            ty_bottom = (iy0 - oy0) * scale.meters_per_point
            ty_top = (oy1 - iy1) * scale.meters_per_point
            thickness_x = (tx_left + tx_right) / 2.0
            thickness_y = (ty_bottom + ty_top) / 2.0
            if not (options.min_wall_thickness_m <= thickness_x <= options.max_wall_thickness_m):
                continue
            if not (options.min_wall_thickness_m <= thickness_y <= options.max_wall_thickness_m):
                continue
            if abs(tx_left - tx_right) > max(0.03, thickness_x * 0.35):
                continue
            if abs(ty_bottom - ty_top) > max(0.03, thickness_y * 0.35):
                continue
            if inner.width_pt * scale.meters_per_point < options.min_space_span_m:
                continue
            if inner.height_pt * scale.meters_per_point < options.min_space_span_m:
                continue
            area_m2 = inner.width_pt * inner.height_pt * scale.meters_per_point**2
            asymmetry = abs(thickness_x - thickness_y)
            candidates.append((area_m2, asymmetry, outer, inner))

    assignments: dict[
        tuple[str, str],
        tuple[_ShellBoundary, _ShellBoundary, float, str, list[_RoomLabel]],
    ] = {}

    for room in sorted(rooms, key=lambda item: (item.anchor, item.observation.element_id)):
        if room.anchor in excluded_room_anchors:
            continue
        room_center = _room_label_center_pt(room)
        containing = [
            item
            for item in candidates
            if _inside(item[3].bbox_pt, room_center)
        ]
        if containing:
            containing.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                    item[2].element_id,
                    item[3].element_id,
                )
            )
            if len(containing) != 1:
                ambiguities.append(
                    {
                        "page": page.page_number,
                        "code": "ordinary_vector_enclosure_ambiguous",
                        "detail": (
                            f"room {room.anchor!r} is contained by multiple supported ordinary "
                            "vector wall enclosures; none was selected by extraction order"
                        ),
                        "room_anchor": room.anchor,
                        "source_boundaries": [
                            {
                                "outer": list(item[2].source_element_ids),
                                "inner": list(item[3].source_element_ids),
                            }
                            for item in containing
                        ],
                    }
                )
                continue
            _, _, outer, inner = containing[0]
            pair = (outer.element_id, inner.element_id)
            if pair not in assignments:
                assignments[pair] = (
                    outer,
                    inner,
                    0.86,
                    "ordinary_vector_line_loops",
                    [],
                )
            assignments[pair][4].append(room)
            continue

        single_loops = [
            loop
            for loop in loops
            if _inside(loop.bbox_pt, room_center)
            and loop.width_pt * scale.meters_per_point >= options.min_space_span_m
            and loop.height_pt * scale.meters_per_point >= options.min_space_span_m
        ]
        if len(single_loops) == 1:
            boundary = single_loops[0]
            bx0, by0, bx1, by1 = boundary.bbox_pt
            boundary_width = bx1 - bx0
            boundary_height = by1 - by0
            min_offset_pt = options.min_wall_thickness_m / scale.meters_per_point
            max_offset_pt = options.max_wall_thickness_m / scale.meters_per_point
            partial_sides: set[str] = set()
            for line in page.lines:
                if line.native_id:
                    continue
                ax, ay = line.start_pt
                bx, by = line.end_pt
                dx = bx - ax
                dy = by - ay
                if (
                    abs(dy) <= _VECTOR_AXIS_TOLERANCE_PT
                    and abs(dx) >= boundary_width * 0.7
                ):
                    x0, x1 = sorted((ax, bx))
                    y = (ay + by) / 2.0
                    if (
                        x0 >= bx0 - _VECTOR_AXIS_TOLERANCE_PT
                        and x1 <= bx1 + _VECTOR_AXIS_TOLERANCE_PT
                    ):
                        if min_offset_pt <= y - by0 <= max_offset_pt:
                            partial_sides.add("south")
                        if min_offset_pt <= by1 - y <= max_offset_pt:
                            partial_sides.add("north")
                elif (
                    abs(dx) <= _VECTOR_AXIS_TOLERANCE_PT
                    and abs(dy) >= boundary_height * 0.7
                ):
                    y0, y1 = sorted((ay, by))
                    x = (ax + bx) / 2.0
                    if (
                        y0 >= by0 - _VECTOR_AXIS_TOLERANCE_PT
                        and y1 <= by1 + _VECTOR_AXIS_TOLERANCE_PT
                    ):
                        if min_offset_pt <= x - bx0 <= max_offset_pt:
                            partial_sides.add("west")
                        if min_offset_pt <= bx1 - x <= max_offset_pt:
                            partial_sides.add("east")
            if not partial_sides:
                pair = (boundary.element_id, boundary.element_id)
                if pair not in assignments:
                    assignments[pair] = (
                        boundary,
                        boundary,
                        0.68,
                        "ordinary_vector_single_loop_space",
                        [],
                    )
                assignments[pair][4].append(room)
                continue
            single_loops = []

        if single_loops or any(_inside(loop.bbox_pt, room_center) for loop in loops):
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "ordinary_vector_enclosure_unresolved",
                    "detail": (
                        f"ordinary vector boundaries surround room {room.anchor!r} "
                        "but do not prove one unique supported room enclosure"
                    ),
                    "room_anchor": room.anchor,
                }
            )

    selected: list[_Shell] = []
    for pair in sorted(assignments):
        outer, inner, geometry_confidence, recognition_method, contained_rooms = assignments[pair]
        ix0, iy0, ix1, iy1 = inner.bbox_pt
        polygon = ((ix0, iy0), (ix1, iy0), (ix1, iy1), (ix0, iy1))
        enclosure_attributes: dict[str, object]
        if outer is inner:
            enclosure_attributes = {
                "source_boundaries": {"boundary": list(inner.source_element_ids)}
            }
        else:
            enclosure_attributes = {
                "source_boundaries": {
                    "outer": list(outer.source_element_ids),
                    "inner": list(inner.source_element_ids),
                }
            }
        room = _select_room_label(
            page,
            contained_rooms,
            polygon,
            ambiguities,
            enclosure_attributes=enclosure_attributes,
        )
        if room is None:
            continue
        if recognition_method == "ordinary_vector_single_loop_space":
            thickness_x_m = 0.0
            thickness_y_m = 0.0
        else:
            ox0, oy0, ox1, oy1 = outer.bbox_pt
            thickness_x_m = ((ix0 - ox0) + (ox1 - ix1)) * scale.meters_per_point / 2.0
            thickness_y_m = ((iy0 - oy0) + (oy1 - iy1)) * scale.meters_per_point / 2.0
        selected.append(
            _Shell(
                outer=outer,
                inner=inner,
                room=room,
                thickness_x_m=thickness_x_m,
                thickness_y_m=thickness_y_m,
                geometry_confidence=geometry_confidence,
                recognition_method=recognition_method,
            )
        )

    duplicate_selected_anchors = {
        shell.room.anchor
        for shell in selected
        if sum(item.room.anchor == shell.room.anchor for item in selected) > 1
    }
    for anchor in sorted(duplicate_selected_anchors):
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "duplicate_room_label",
                "detail": (
                    f"selected room label {anchor!r} is not unique on the level; "
                    "those enclosures are not promoted"
                ),
            }
        )
    if duplicate_selected_anchors:
        selected = [
            shell for shell in selected if shell.room.anchor not in duplicate_selected_anchors
        ]

    if candidates and not rooms:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "unlabeled_enclosure",
                "detail": (
                    "paired closed ordinary vector wall loops were found but no stable "
                    "room/space label anchors their identity"
                ),
            }
        )
    return tuple(selected)



def _provenance(
    source_id: str,
    page_number: int,
    *,
    method: str,
    confidence: float,
    source_element_id: str | None = None,
    attributes: dict[str, object] | None = None,
) -> tuple[Provenance, ...]:
    return (
        Provenance(
            source_kind="architectural_pdf",
            source_id=source_id,
            source_element_id=source_element_id,
            page=page_number,
            method=method,
            confidence=confidence,
            attributes=attributes or {},
        ),
    )


def _polygon_from_bbox(
    bbox: tuple[float, float, float, float],
    transform: _Transform2D,
    z: float,
) -> Polygon3D:
    x0, y0, x1, y1 = bbox
    points = []
    for source in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        x, y = transform.apply(source)
        points.append(Point3(x=x, y=y, z=z))
    return Polygon3D(points=tuple(points))


def _point3(source: tuple[float, float], transform: _Transform2D, z: float) -> Point3:
    x, y = transform.apply(source)
    return Point3(x=x, y=y, z=z)


def _shell_entities(
    shell: _Shell,
    page: PdfPageObservation,
    transform: _Transform2D,
    scale: _Scale,
    level: Level,
    level_info: _LevelInfo,
    room_height: _Measurement | None,
    room_height_blocked: bool,
    source_id: str,
    slab_thickness: tuple[float, str] | None,
    ambiguities: list[dict[str, object]],
) -> tuple[Space, tuple[_WallContext, ...], Slab | None, Ceiling | None]:
    room = shell.room
    identity = f"{source_id}|level:{level_info.anchor}|room:{room.anchor}"
    base_confidence = min(
        room.confidence,
        scale.confidence,
        transform.confidence,
        shell.geometry_confidence,
    )
    footprint = _polygon_from_bbox(shell.inner.bbox_pt, transform, level.elevation_m)
    level_height = level_info.height
    is_single_loop_space = shell.recognition_method == "ordinary_vector_single_loop_space"
    if is_single_loop_space:
        selected_height = None
        selected_scope = None
    elif room_height_blocked:
        selected_height = (
            level_height
            if level_height is not None and level_height.priority >= 3
            else None
        )
        selected_scope = "level" if selected_height is not None else None
    elif (
        room_height is not None
        and (level_height is None or room_height.priority >= level_height.priority)
    ):
        selected_height = room_height
        selected_scope = "room"
    else:
        selected_height = level_height
        selected_scope = "level" if selected_height is not None else None

    resolved_height_m = selected_height.value_m if selected_height is not None else None
    height_confidence = selected_height.confidence if selected_height is not None else 0.0
    height_provenance: tuple[Provenance, ...] = ()
    if selected_height is not None:
        height_provenance = _provenance(
            source_id,
            selected_height.page_number,
            method=selected_height.method,
            confidence=selected_height.confidence,
            source_element_id=selected_height.source_element_id,
            attributes={
                "field": "height_m",
                "scope": selected_scope,
                "source_text": selected_height.source_text,
            },
        )
    space_confidence = (
        min(base_confidence, height_confidence)
        if resolved_height_m is not None
        else base_confidence
    )

    if shell.recognition_method == "ordinary_vector_line_loops":
        space_method = "room label contained by paired closed ordinary vector wall-face loops"
        space_source_attributes: dict[str, object] = {
            "outer_boundary_elements": list(shell.outer.source_element_ids),
            "inner_boundary_elements": list(shell.inner.source_element_ids),
        }
        space_attributes = {
            "pdf_architecture": {
                "identity_anchor": room.anchor,
                "recognition": shell.recognition_method,
            }
        }
    elif shell.recognition_method == "ordinary_vector_single_loop_space":
        space_method = "room label contained by one unique closed ordinary vector boundary"
        space_source_attributes = {
            "boundary_elements": list(shell.inner.source_element_ids),
        }
        space_attributes = {
            "pdf_architecture": {
                "identity_anchor": room.anchor,
                "recognition": shell.recognition_method,
            }
        }
    else:
        space_method = "room label contained by a paired wall rectangle enclosure"
        space_source_attributes = {
            "outer_rect": shell.outer.element_id,
            "inner_rect": shell.inner.element_id,
        }
        space_attributes = {"pdf_architecture": {"identity_anchor": room.anchor}}

    space_attributes["pdf_architecture"].update(
        {
            "label_confidence": room.confidence,
            "label_method": "text_inside_closed_wall_loop",
            "label_source_element_id": room.observation.element_id,
        }
    )
    if len(_room_label_sources(room)) > 1:
        source_text_elements = [
            item.element_id for item in _room_label_sources(room)
        ]
        space_attributes["pdf_architecture"]["label_source_element_ids"] = (
            source_text_elements
        )
        space_source_attributes["source_text_elements"] = source_text_elements
        space_source_attributes["source_text"] = room.name
    if room.selection_provenance is not None:
        space_source_attributes["label_selection"] = room.selection_provenance

    space = Space(
        id=stable_id("space", identity),
        name=room.name,
        level_id=level.id,
        footprint=footprint,
        height_m=resolved_height_m,
        usage=room.usage,
        confidence=space_confidence,
        provenance=(
            _provenance(
                source_id,
                page.page_number,
                method=space_method,
                confidence=base_confidence,
                source_element_id=room.observation.element_id,
                attributes=space_source_attributes,
            )
            + height_provenance
        ),
        attributes=space_attributes,
    )

    if is_single_loop_space:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "wall_thickness_unresolved",
                "detail": (
                    f"room {room.name!r} has one supported closed boundary but no paired "
                    "wall-face evidence; the 2D space was promoted without inventing walls"
                ),
            }
        )
        return space, (), None, None

    walls: list[_WallContext] = []
    if resolved_height_m is None:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "wall_height_unresolved",
                "detail": (
                    f"room {room.name!r} has wall geometry but no supported room/level "
                    "wall or ceiling height; walls were not promoted"
                ),
            }
        )
    else:
        ox0, oy0, ox1, oy1 = shell.outer.bbox_pt
        ix0, iy0, ix1, iy1 = shell.inner.bbox_pt
        left = (ox0 + ix0) / 2.0
        right = (ox1 + ix1) / 2.0
        bottom = (oy0 + iy0) / 2.0
        top = (oy1 + iy1) / 2.0
        sides = (
            ("south", (left, bottom), (right, bottom), shell.thickness_y_m),
            ("east", (right, bottom), (right, top), shell.thickness_x_m),
            ("north", (right, top), (left, top), shell.thickness_y_m),
            ("west", (left, top), (left, bottom), shell.thickness_x_m),
        )
        wall_confidence = min(base_confidence, 0.95, height_confidence)
        for side, start, end, thickness in sides:
            wall_id = stable_id("wall", f"{identity}|boundary:{side}")
            wall_method = (
                "wall centerline inferred midway between paired closed ordinary vector "
                "wall-face loops; height from level/ceiling evidence"
                if shell.recognition_method == "ordinary_vector_line_loops"
                else "wall centerline inferred midway between paired vector boundaries; height from level/ceiling evidence"
            )
            wall_attributes: dict[str, object] = {
                "room_anchor": room.anchor,
                "source_side": side,
            }
            if shell.recognition_method == "ordinary_vector_line_loops":
                wall_attributes["recognition"] = shell.recognition_method
            wall = Wall(
                id=wall_id,
                level_id=level.id,
                centerline=Polyline3D(
                    points=(
                        _point3(start, transform, level.elevation_m),
                        _point3(end, transform, level.elevation_m),
                    )
                ),
                thickness_m=thickness,
                height_m=resolved_height_m,
                confidence=wall_confidence,
                provenance=(
                    _provenance(
                        source_id,
                        page.page_number,
                        method=wall_method,
                        confidence=wall_confidence,
                        source_element_id=f"{shell.outer.element_id}+{shell.inner.element_id}:{side}",
                        attributes={"room_anchor": room.anchor, "source_side": side},
                    )
                    + height_provenance
                ),
                attributes={"pdf_architecture": wall_attributes},
            )
            walls.append(_WallContext(wall, page.page_number, room.anchor, side))

    slab: Slab | None = None
    if slab_thickness:
        slab_confidence = min(base_confidence, 0.75)
        slab = Slab(
            id=stable_id("slab", f"{identity}|floor"),
            level_id=level.id,
            footprint=_polygon_from_bbox(shell.outer.bbox_pt, transform, level.elevation_m),
            thickness_m=slab_thickness[0],
            confidence=slab_confidence,
            provenance=_provenance(
                source_id,
                page.page_number,
                method="explicit floor slab thickness applied to labelled room wall enclosure footprint",
                confidence=slab_confidence,
                source_element_id=room.observation.element_id,
                attributes={"source_text": slab_thickness[1]},
            ),
        )

    ceiling: Ceiling | None = None
    if resolved_height_m is not None:
        ceiling_confidence = min(base_confidence, height_confidence, 0.85)
        ceiling = Ceiling(
            id=stable_id("ceiling", f"{identity}|ceiling"),
            level_id=level.id,
            footprint=_polygon_from_bbox(
                shell.inner.bbox_pt,
                transform,
                level.elevation_m + resolved_height_m,
            ),
            thickness_m=None,
            confidence=ceiling_confidence,
            provenance=(
                _provenance(
                    source_id,
                    page.page_number,
                    method="ceiling surface inferred at explicit/overridden room height over labelled room footprint",
                    confidence=ceiling_confidence,
                    source_element_id=room.observation.element_id,
                )
                + height_provenance
            ),
        )
    return space, tuple(walls), slab, ceiling


def _canonical_segment(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    return (start, end) if start <= end else (end, start)


def _rounded_wall_anchor(
    start: tuple[float, float],
    end: tuple[float, float],
    transform: _Transform2D,
) -> str:
    first, second = _canonical_segment(transform.apply(start), transform.apply(end))
    digits = _WALL_ID_ROUND_DIGITS
    return (
        f"{round(first[0], digits):.{digits}f},{round(first[1], digits):.{digits}f}|"
        f"{round(second[0], digits):.{digits}f},{round(second[1], digits):.{digits}f}"
    )


def _line_record(
    line: PdfLineObservation,
) -> tuple[float, float, float, float, float]:
    start, end = _canonical_segment(line.start_pt, line.end_pt)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    p0 = start[0] * ux + start[1] * uy
    p1 = end[0] * ux + end[1] * uy
    normal = ((start[0] + end[0]) / 2.0) * nx + ((start[1] + end[1]) / 2.0) * ny
    return ux, uy, p0, p1, normal


def _geometric_wall_face_pairs(
    page: PdfPageObservation,
    transform: _Transform2D,
    options: ImportOptions,
    *,
    excluded_element_ids: set[str] | None = None,
) -> tuple[_WallFacePair, ...]:
    """Pair wall faces by geometry only, never by PDF-native identifiers."""

    duplicate_geometry: set[tuple[float, float, float, float]] = set()
    by_geometry: dict[tuple[float, float, float, float], list[PdfLineObservation]] = {}
    excluded_element_ids = excluded_element_ids or set()
    for line in page.lines:
        if line.element_id in excluded_element_ids:
            continue
        start, end = _canonical_segment(line.start_pt, line.end_pt)
        key = (
            round(start[0], 6),
            round(start[1], 6),
            round(end[0], 6),
            round(end[1], 6),
        )
        by_geometry.setdefault(key, []).append(line)
    duplicate_geometry.update(key for key, values in by_geometry.items() if len(values) != 1)

    minimum_overlap_m = options.min_space_span_m * 0.5
    minimum_overlap_pt = minimum_overlap_m / transform.meters_per_point
    lines = [
        values[0]
        for key, values in sorted(by_geometry.items())
        if key not in duplicate_geometry
        and math.dist(values[0].start_pt, values[0].end_pt) >= minimum_overlap_pt
    ]
    records = [_line_record(line) for line in lines]

    # The audited CAD page contains tens of thousands of line primitives. Build
    # deterministic coarse orientation/spatial buckets so only nearby,
    # sufficiently-overlapping lines reach the exact parallel-pair checks below.
    angle_tolerance = math.radians(2.0)
    orientation_bucket_count = max(1, int(round(math.pi / angle_tolerance)))
    max_offset_pt = options.max_wall_thickness_m / transform.meters_per_point
    normal_bin_size = max(max_offset_pt, 1.0)
    tangent_bin_size = max(minimum_overlap_pt, max_offset_pt * 2.0, 16.0)
    candidate_cells: dict[tuple[int, int, int], set[int]] = {}
    for line_index, line in enumerate(lines):
        start, end = _canonical_segment(line.start_pt, line.end_pt)
        ux, uy, _, _, _ = records[line_index]
        theta = math.atan2(uy, ux) % math.pi
        base_bucket = int(round(theta / angle_tolerance)) % orientation_bucket_count
        for orientation_bucket in {
            (base_bucket - 1) % orientation_bucket_count,
            base_bucket,
            (base_bucket + 1) % orientation_bucket_count,
        }:
            reference_angle = orientation_bucket * angle_tolerance
            tx, ty = math.cos(reference_angle), math.sin(reference_angle)
            nx, ny = -ty, tx
            tangent_values = sorted(
                (
                    start[0] * tx + start[1] * ty,
                    end[0] * tx + end[1] * ty,
                )
            )
            normal_values = sorted(
                (
                    start[0] * nx + start[1] * ny,
                    end[0] * nx + end[1] * ny,
                )
            )
            tangent_start = math.floor(tangent_values[0] / tangent_bin_size)
            tangent_end = math.floor(tangent_values[1] / tangent_bin_size)
            normal_start = math.floor(
                (normal_values[0] - max_offset_pt) / normal_bin_size
            )
            normal_end = math.floor(
                (normal_values[1] + max_offset_pt) / normal_bin_size
            )
            for normal_bucket in range(normal_start, normal_end + 1):
                for tangent_bucket in range(tangent_start, tangent_end + 1):
                    candidate_cells.setdefault(
                        (orientation_bucket, normal_bucket, tangent_bucket),
                        set(),
                    ).add(line_index)

    candidate_pairs: set[tuple[int, int]] = set()
    for cell_lines in candidate_cells.values():
        ordered = sorted(cell_lines)
        for position, first_index in enumerate(ordered):
            for second_index in ordered[position + 1 :]:
                candidate_pairs.add((first_index, second_index))

    candidates: list[
        tuple[
            int,
            int,
            float,
            float,
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ]
    ] = []
    max_cross = math.sin(angle_tolerance)
    for first_index, second_index in sorted(candidate_pairs):
        first = lines[first_index]
        ux, uy, a0, a1, first_normal = records[first_index]
        sux, suy, _, _, _ = records[second_index]
        if abs(ux * suy - uy * sux) > max_cross:
            continue
        nx, ny = -uy, ux
        second = lines[second_index]
        cx, cy = second.start_pt
        ex, ey = second.end_pt
        b0, b1 = sorted((cx * ux + cy * uy, ex * ux + ey * uy))
        overlap0 = max(a0, b0)
        overlap1 = min(a1, b1)
        overlap_pt = overlap1 - overlap0
        overlap_m = overlap_pt * transform.meters_per_point
        if overlap_m < minimum_overlap_m:
            continue
        second_normal = ((cx + ex) / 2.0) * nx + ((cy + ey) / 2.0) * ny
        offset_m = abs(second_normal - first_normal) * transform.meters_per_point
        if not (options.min_wall_thickness_m <= offset_m <= options.max_wall_thickness_m):
            continue
        mean_normal = (first_normal + second_normal) / 2.0
        start_source = (
            overlap0 * ux + mean_normal * nx,
            overlap0 * uy + mean_normal * ny,
        )
        end_source = (
            overlap1 * ux + mean_normal * nx,
            overlap1 * uy + mean_normal * ny,
        )
        score = (
            -round(overlap_m, 6),
            round(offset_m, 6),
        )
        candidates.append(
            (
                first_index,
                second_index,
                overlap_m,
                offset_m,
                start_source,
                end_source,
                score,
            )
        )

    by_line: dict[int, list[int]] = {}
    for candidate_index, candidate in enumerate(candidates):
        by_line.setdefault(candidate[0], []).append(candidate_index)
        by_line.setdefault(candidate[1], []).append(candidate_index)

    unique_best: dict[int, int] = {}
    for line_index, indexes in by_line.items():
        ordered = sorted(indexes, key=lambda index: candidates[index][6])
        if len(ordered) > 1 and candidates[ordered[0]][6] == candidates[ordered[1]][6]:
            continue
        unique_best[line_index] = ordered[0]

    accepted: list[_WallFacePair] = []
    seen_anchors: set[str] = set()
    for candidate_index, candidate in enumerate(candidates):
        first_index, second_index, _, thickness_m, start_source, end_source, _ = candidate
        if unique_best.get(first_index) != candidate_index:
            continue
        if unique_best.get(second_index) != candidate_index:
            continue
        geometry_anchor = _rounded_wall_anchor(start_source, end_source, transform)
        if geometry_anchor in seen_anchors:
            continue
        seen_anchors.add(geometry_anchor)
        accepted.append(
            _WallFacePair(
                start_pt=start_source,
                end_pt=end_source,
                thickness_m=thickness_m,
                source_element_ids=tuple(
                    sorted((lines[first_index].element_id, lines[second_index].element_id))
                ),
                geometry_anchor=geometry_anchor,
            )
        )
    return tuple(sorted(accepted, key=lambda item: item.geometry_anchor))


def _polygon_area(points: tuple[tuple[float, float], ...]) -> float:
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, (*points[1:], points[0]), strict=True)
        )
    ) / 2.0


def _segments_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orientation(
        first: tuple[float, float],
        second: tuple[float, float],
        third: tuple[float, float],
    ) -> float:
        return (
            (second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0])
        )

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    tolerance = 1e-9
    return (
        ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance))
        and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance))
    )


def _simple_closed_polygon(points: tuple[tuple[float, float], ...]) -> bool:
    if len(points) < 3 or _polygon_area(points) <= 1e-6:
        return False
    edge_count = len(points)
    for first_index in range(edge_count):
        a = points[first_index]
        b = points[(first_index + 1) % edge_count]
        for second_index in range(first_index + 1, edge_count):
            if second_index in {
                first_index,
                (first_index + 1) % edge_count,
                (first_index - 1) % edge_count,
            }:
                continue
            if first_index == 0 and second_index == edge_count - 1:
                continue
            c = points[second_index]
            d = points[(second_index + 1) % edge_count]
            if _segments_cross(a, b, c, d):
                return False
    return True


def _wall_pair_closed_loops(
    pairs: tuple[_WallFacePair, ...],
    transform: _Transform2D,
    options: ImportOptions,
) -> tuple[
    tuple[
        tuple[int, ...],
        tuple[tuple[float, float], ...],
        dict[tuple[int, int], tuple[float, float]],
    ],
    ...,
]:
    if not pairs:
        return ()

    endpoints: dict[tuple[int, int], tuple[float, float]] = {}
    directions: dict[int, tuple[float, float]] = {}
    for index, pair in enumerate(pairs):
        first = transform.apply(pair.start_pt)
        second = transform.apply(pair.end_pt)
        endpoints[(index, 0)] = first
        endpoints[(index, 1)] = second
        dx = second[0] - first[0]
        dy = second[1] - first[1]
        length = math.hypot(dx, dy)
        directions[index] = (dx / length, dy / length)

    possible: dict[tuple[int, int], list[tuple[int, int]]] = {}
    endpoint_keys = sorted(endpoints)
    for endpoint_key in endpoint_keys:
        wall_index, _ = endpoint_key
        point = endpoints[endpoint_key]
        matches: list[tuple[int, int]] = []
        for other_key in endpoint_keys:
            other_wall, _ = other_key
            if other_wall == wall_index:
                continue
            other = endpoints[other_key]
            join_tolerance = (
                max(pairs[wall_index].thickness_m, pairs[other_wall].thickness_m) * 1.1
                + 1e-6
            )
            if math.hypot(point[0] - other[0], point[1] - other[1]) > join_tolerance:
                continue
            first_direction = directions[wall_index]
            second_direction = directions[other_wall]
            if abs(
                first_direction[0] * second_direction[1]
                - first_direction[1] * second_direction[0]
            ) < math.sin(math.radians(15.0)):
                continue
            matches.append(other_key)
        possible[endpoint_key] = sorted(matches)

    matched: dict[tuple[int, int], tuple[int, int]] = {}
    for endpoint_key, matches in possible.items():
        if len(matches) != 1:
            continue
        other_key = matches[0]
        if possible.get(other_key) == [endpoint_key]:
            matched[endpoint_key] = other_key

    complete = {
        index
        for index in range(len(pairs))
        if (index, 0) in matched and (index, 1) in matched
    }
    adjacency: dict[int, set[int]] = {index: set() for index in complete}
    for index in sorted(complete):
        for endpoint in (0, 1):
            other_wall = matched[(index, endpoint)][0]
            if other_wall in complete:
                adjacency[index].add(other_wall)

    loops: list[
        tuple[
            tuple[int, ...],
            tuple[tuple[float, float], ...],
            dict[tuple[int, int], tuple[float, float]],
        ]
    ] = []
    unseen = set(complete)
    while unseen:
        seed = min(unseen, key=lambda index: pairs[index].geometry_anchor)
        stack = [seed]
        component: set[int] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current] - component)
        unseen -= component
        if len(component) < 3 or any(len(adjacency[index]) != 2 for index in component):
            continue

        endpoint_vertices: dict[tuple[int, int], tuple[float, float]] = {}
        vertex_adjacency: dict[tuple[float, float], set[tuple[float, float]]] = {}
        for index in component:
            for endpoint in (0, 1):
                key = (index, endpoint)
                other_key = matched[key]
                if other_key[0] not in component:
                    break
                point = endpoints[key]
                other = endpoints[other_key]
                vertex = (
                    round((point[0] + other[0]) / 2.0, 9),
                    round((point[1] + other[1]) / 2.0, 9),
                )
                endpoint_vertices[key] = vertex
            else:
                first_vertex = endpoint_vertices[(index, 0)]
                second_vertex = endpoint_vertices[(index, 1)]
                if first_vertex == second_vertex:
                    break
                vertex_adjacency.setdefault(first_vertex, set()).add(second_vertex)
                vertex_adjacency.setdefault(second_vertex, set()).add(first_vertex)
                continue
            vertex_adjacency = {}
            break
        if not vertex_adjacency or any(len(neighbors) != 2 for neighbors in vertex_adjacency.values()):
            continue

        start_vertex = min(vertex_adjacency)
        first_neighbor = min(vertex_adjacency[start_vertex])
        ordered_vertices = [start_vertex]
        previous = start_vertex
        current = first_neighbor
        while current != start_vertex and len(ordered_vertices) <= len(vertex_adjacency):
            ordered_vertices.append(current)
            neighbors = sorted(vertex_adjacency[current])
            next_vertex = neighbors[0] if neighbors[0] != previous else neighbors[1]
            previous, current = current, next_vertex
        if current != start_vertex or len(ordered_vertices) != len(vertex_adjacency):
            continue
        polygon = tuple(ordered_vertices)
        if not _simple_closed_polygon(polygon):
            continue
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        if (
            max(xs) - min(xs) < options.min_space_span_m
            or max(ys) - min(ys) < options.min_space_span_m
        ):
            continue
        loops.append(
            (
                tuple(sorted(component, key=lambda index: pairs[index].geometry_anchor)),
                polygon,
                endpoint_vertices,
            )
        )

    return tuple(
        sorted(
            loops,
            key=lambda item: tuple(pairs[index].geometry_anchor for index in item[0]),
        )
    )


def _point_in_polygon(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    if len(polygon) < 3:
        return False

    x, y = point
    inside = False
    for first, second in zip(polygon, (*polygon[1:], polygon[0]), strict=True):
        x1, y1 = first
        x2, y2 = second

        dx = x2 - x1
        dy = y2 - y1
        cross = (x - x1) * dy - (y - y1) * dx
        if abs(cross) <= 1e-9:
            dot = (x - x1) * dx + (y - y1) * dy
            length_sq = dx * dx + dy * dy
            if -1e-9 <= dot <= length_sq + 1e-9:
                return True

        if (y1 > y) == (y2 > y):
            continue
        intersection_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
        if intersection_x >= x:
            inside = not inside
    return inside


def _geometric_wall_loop_entities(
    page: PdfPageObservation,
    transform: _Transform2D,
    level: Level,
    level_info: _LevelInfo,
    source_id: str,
    options: ImportOptions,
    rooms: tuple[_RoomLabel, ...],
    ambiguities: list[dict[str, object]],
    *,
    excluded_element_ids: set[str] | None = None,
) -> tuple[tuple[_WallContext, ...], tuple[Space, ...]]:
    if level.height_m is None or level_info.height is None:
        return (), ()
    sheet_anchor = _sheet_anchor(page)
    if sheet_anchor is None:
        return (), ()

    pairs = _geometric_wall_face_pairs(
        page,
        transform,
        options,
        excluded_element_ids=excluded_element_ids,
    )
    loops = _wall_pair_closed_loops(pairs, transform, options)
    if not loops:
        return (), ()

    contexts_by_anchor: dict[str, _WallContext] = {}
    spaces: list[Space] = []
    height_confidence = level_info.height_confidence or options.assumed_value_confidence
    for loop_indexes, polygon_xy, endpoint_vertices in loops:
        wall_ids: list[str] = []
        source_ids: set[str] = set()
        loop_wall_anchors = tuple(pairs[index].geometry_anchor for index in loop_indexes)
        loop_identity = (
            f"{source_id}|sheet:{sheet_anchor}|level:{level_info.anchor}|"
            f"wall-loop:{';'.join(loop_wall_anchors)}"
        )
        for index in loop_indexes:
            pair = pairs[index]
            wall_identity = (
                f"{source_id}|sheet:{sheet_anchor}|level:{level_info.anchor}|"
                f"wall-geometry:{pair.geometry_anchor}"
            )
            wall_id = stable_id("wall", wall_identity)
            wall_ids.append(wall_id)
            source_ids.update(pair.source_element_ids)
            if pair.geometry_anchor in contexts_by_anchor:
                continue
            first_xy = endpoint_vertices[(index, 0)]
            second_xy = endpoint_vertices[(index, 1)]
            wall_confidence = min(transform.confidence, height_confidence, 0.78)
            wall = Wall(
                id=wall_id,
                level_id=level.id,
                centerline=Polyline3D(
                    points=(
                        Point3(x=first_xy[0], y=first_xy[1], z=level.elevation_m),
                        Point3(x=second_xy[0], y=second_xy[1], z=level.elevation_m),
                    )
                ),
                thickness_m=pair.thickness_m,
                height_m=level.height_m,
                confidence=wall_confidence,
                provenance=(
                    _provenance(
                        source_id,
                        page.page_number,
                        method=(
                            "wall centerline inferred from a unique geometric pairing of "
                            "parallel PDF wall faces and joined only as part of a closed loop"
                        ),
                        confidence=wall_confidence,
                        source_element_id="+".join(pair.source_element_ids),
                        attributes={
                            "source_boundaries": list(pair.source_element_ids),
                            "geometry_anchor": pair.geometry_anchor,
                        },
                    )
                    + _level_measurement_provenance(
                        source_id,
                        level_info.height,
                        field="height_m",
                    )
                ),
                attributes={
                    "pdf_architecture": {
                        "recognition": "geometric_parallel_wall_faces",
                        "geometry_anchor": pair.geometry_anchor,
                        "source_boundaries": list(pair.source_element_ids),
                    }
                },
            )
            contexts_by_anchor[pair.geometry_anchor] = _WallContext(
                wall,
                page.page_number,
                None,
                None,
            )

        contained_rooms = sorted(
            (
                room
                for room in rooms
                if _point_in_polygon(
                    transform.apply(_room_label_center_pt(room)),
                    polygon_xy,
                )
            ),
            key=lambda room: (room.observation.element_id, room.anchor),
        )
        room = _select_room_label(
            page,
            contained_rooms,
            polygon_xy,
            ambiguities,
            transform=transform,
            enclosure_attributes={
                "wall_geometry_anchors": list(loop_wall_anchors),
            },
        )

        space_confidence = min(
            transform.confidence,
            height_confidence,
            min(
                contexts_by_anchor[anchor].wall.confidence
                for anchor in loop_wall_anchors
            ),
            room.confidence if room is not None else 1.0,
            0.72,
        )
        space_provenance = _provenance(
            source_id,
            page.page_number,
            method=(
                "space footprint joined from one unambiguous closed loop of "
                "geometrically paired wall centerlines"
            ),
            confidence=space_confidence,
            source_element_id="+".join(sorted(source_ids)),
            attributes={
                "wall_ids": sorted(wall_ids),
                "wall_geometry_anchors": list(loop_wall_anchors),
            },
        )
        space_attributes: dict[str, object] = {
            "recognition": "geometric_parallel_wall_closed_loop",
            "sheet_anchor": sheet_anchor,
            "wall_ids": sorted(wall_ids),
        }
        if room is not None:
            label_provenance_attributes: dict[str, object] = {
                "label_anchor": room.anchor,
                "source_text": room.name,
            }
            if len(_room_label_sources(room)) > 1:
                label_provenance_attributes["source_text_elements"] = [
                    item.element_id for item in _room_label_sources(room)
                ]
            if room.selection_provenance is not None:
                label_provenance_attributes["label_selection"] = room.selection_provenance
            space_provenance += _provenance(
                source_id,
                page.page_number,
                method="room label assigned by text position inside closed wall loop",
                confidence=room.confidence,
                source_element_id=room.observation.element_id,
                attributes=label_provenance_attributes,
            )
            space_attributes.update(
                {
                    "label_anchor": room.anchor,
                    "label_confidence": room.confidence,
                    "label_method": "text_inside_closed_wall_loop",
                    "label_source_element_id": room.observation.element_id,
                }
            )
            if len(_room_label_sources(room)) > 1:
                space_attributes["label_source_element_ids"] = [
                    item.element_id for item in _room_label_sources(room)
                ]

        spaces.append(
            Space(
                id=stable_id("space", loop_identity),
                name=room.name if room is not None else None,
                level_id=level.id,
                footprint=Polygon3D(
                    points=tuple(
                        Point3(x=x, y=y, z=level.elevation_m)
                        for x, y in polygon_xy
                    )
                ),
                height_m=level.height_m,
                usage=room.usage if room is not None else None,
                confidence=space_confidence,
                provenance=(
                    space_provenance
                    + _level_measurement_provenance(
                        source_id,
                        level_info.height,
                        field="height_m",
                    )
                ),
                attributes={"pdf_architecture": space_attributes},
            )
        )

    return (
        tuple(
            sorted(
                contexts_by_anchor.values(),
                key=lambda context: context.wall.id,
            )
        ),
        tuple(sorted(spaces, key=lambda space: space.id)),
    )

def _distance_to_segment(
    point: tuple[float, float],
    start: Point3,
    end: Point3,
) -> tuple[float, tuple[float, float], float]:
    vx = end.x - start.x
    vy = end.y - start.y
    length_sq = vx * vx + vy * vy
    if length_sq <= 1e-12:
        return (math.inf, (start.x, start.y), 0.0)
    t = ((point[0] - start.x) * vx + (point[1] - start.y) * vy) / length_sq
    t = max(0.0, min(1.0, t))
    qx = start.x + t * vx
    qy = start.y + t * vy
    return (math.hypot(point[0] - qx, point[1] - qy), (qx, qy), t)


def _opening_annotations(page: PdfPageObservation) -> list[tuple[PdfTextObservation, str, str | None, float, float, float | None]]:
    result = []
    for observation in page.texts:
        text = _clean_text(observation.text)
        match = re.match(r"^(DOOR|WINDOW)\b(.*)$", text, re.IGNORECASE)
        if not match:
            continue
        kind = match.group(1).lower()
        rest = match.group(2).strip(" :-")
        separator = re.search(r"\s+[X×]\s+", rest, re.IGNORECASE)
        if not separator:
            continue
        left = rest[: separator.start()].strip()
        right = rest[separator.end() :].strip()
        width_match = _find_dimension(left)
        height_match = _find_dimension(right)
        if not width_match or not height_match or width_match[0] <= 0 or height_match[0] <= 0:
            continue
        mark = left[: width_match[1][0]].strip(" :-") or None
        sill_m: float | None = None
        if kind == "window":
            after_height = right[height_match[1][1] :]
            sill_index = after_height.upper().find("SILL")
            if sill_index >= 0:
                sill = _find_dimension(after_height[sill_index + 4 :])
                if sill:
                    sill_m = sill[0]
        result.append((observation, kind, mark, width_match[0], height_match[0], sill_m))
    return result


def _make_openings(
    page: PdfPageObservation,
    transform: _Transform2D,
    level: Level,
    wall_contexts: tuple[_WallContext, ...],
    source_id: str,
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
    used_identity: set[str],
) -> tuple[Opening, ...]:
    result: list[Opening] = []
    for observation, kind, mark, width_m, height_m, sill_m in _opening_annotations(page):
        if kind == "window" and sill_m is None:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "window_vertical_position_unresolved",
                    "detail": f"window annotation {observation.text!r} has no supported sill height; opening was not promoted",
                }
            )
            continue
        source_center = transform.apply(observation.center_pt)
        nearest: tuple[float, tuple[float, float], _WallContext] | None = None
        for context in wall_contexts:
            points = context.wall.centerline.points
            distance, projection, _ = _distance_to_segment(source_center, points[0], points[-1])
            candidate = (distance, projection, context)
            if nearest is None or (candidate[0], context.wall.id) < (nearest[0], nearest[2].wall.id):
                nearest = candidate
        if nearest is None or nearest[0] > options.max_opening_host_distance_m:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "opening_host_unresolved",
                    "detail": f"opening annotation {observation.text!r} could not be associated with a wall",
                }
            )
            continue
        _, projection, context = nearest
        wall = context.wall
        wall_start, wall_end = wall.centerline.points[0], wall.centerline.points[-1]
        wall_length = math.hypot(wall_end.x - wall_start.x, wall_end.y - wall_start.y)
        if width_m > wall_length + 1e-6:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "opening_larger_than_host",
                    "detail": f"opening annotation {observation.text!r} is wider than its resolved host wall",
                }
            )
            continue

        if mark:
            identity_anchor = f"mark:{_anchor(mark)}"
        elif context.room_anchor and context.source_side:
            identity_anchor = f"room:{context.room_anchor}|side:{context.source_side}|kind:{kind}"
        else:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "opening_identity_unresolved",
                    "detail": f"unmarked opening annotation {observation.text!r} lacks a stable semantic host anchor",
                }
            )
            continue
        identity = f"{source_id}|level:{level.id}|opening:{kind}|{identity_anchor}"
        if identity in used_identity:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "opening_identity_collision",
                    "detail": f"more than one opening resolves to stable anchor {identity_anchor!r}; add unique opening marks",
                }
            )
            continue
        used_identity.add(identity)

        theta = math.atan2(wall_end.y - wall_start.y, wall_end.x - wall_start.x)
        rotation = Quaternion(z=math.sin(theta / 2.0), w=math.cos(theta / 2.0))
        bottom_m = 0.0 if kind == "door" else float(sill_m)
        confidence = min(wall.confidence, 0.9 if mark else 0.78)
        result.append(
            Opening(
                id=stable_id("opening", identity),
                name=mark or kind.title(),
                host_id=wall.id,
                opening_type=kind,
                pose=Pose(
                    position=Point3(
                        x=projection[0],
                        y=projection[1],
                        z=level.elevation_m + bottom_m + height_m / 2.0,
                    ),
                    rotation=rotation,
                ),
                size=Size3(x=width_m, y=wall.thickness_m, z=height_m),
                confidence=confidence,
                provenance=_provenance(
                    source_id,
                    page.page_number,
                    method="opening dimensions parsed from annotation and projected to nearest resolved wall",
                    confidence=confidence,
                    source_element_id=observation.element_id,
                    attributes={"source_text": observation.text},
                ),
                attributes={"pdf_architecture": {"mark": mark, "host_distance_m": nearest[0]}},
            )
        )
    return tuple(result)


def _level_measurement_provenance(
    source_id: str,
    measurement: _Measurement,
    *,
    field: str,
) -> tuple[Provenance, ...]:
    attributes: dict[str, object] = {"field": field}
    if measurement.source_text is not None:
        attributes["source_text"] = measurement.source_text
    if field == "height_m":
        attributes["scope"] = "level"
    return _provenance(
        source_id,
        measurement.page_number,
        method=measurement.method,
        confidence=measurement.confidence,
        source_element_id=measurement.source_element_id,
        attributes=attributes,
    )


def _level_entity(source_id: str, info: _LevelInfo) -> Level:
    confidence = min(
        info.elevation_confidence,
        info.height_confidence if info.height_confidence is not None else 1.0,
    )
    provenance = _level_measurement_provenance(
        source_id,
        info.elevation,
        field="elevation_m",
    )
    if info.height is not None:
        provenance += _level_measurement_provenance(
            source_id,
            info.height,
            field="height_m",
        )

    return Level(
        id=stable_id("level", f"{source_id}|level:{info.anchor}"),
        name=info.name,
        elevation_m=info.elevation_m,
        height_m=info.height_m,
        confidence=confidence,
        provenance=provenance,
        attributes={"pdf_architecture": {"identity_anchor": info.anchor}},
    )


def import_observations(
    document: PdfDocumentObservation,
    *,
    options: ImportOptions | None = None,
) -> BuildingModel:
    """Convert extracted PDF observations into the canonical v1 model."""

    options = options or ImportOptions()
    ambiguities: list[dict[str, object]] = []
    page_metadata: list[dict[str, object]] = []
    level_info_by_anchor: dict[str, _LevelInfo] = {}
    level_anchor_by_page: dict[int, str | None] = {}
    ordered_pages = tuple(sorted(document.pages, key=lambda item: item.page_number))

    # Resolve all level evidence before materializing geometry. This lets later
    # explicit/override evidence correctly upgrade an earlier local datum or
    # assumed height without leaving already-emitted geometry at stale Z/height.
    for page in ordered_pages:
        classification = classify_page(page)
        if classification.kind != "architectural_plan":
            continue
        level_info = _resolve_level(page, options, level_info_by_anchor, ambiguities)
        if level_info is None:
            level_anchor_by_page[page.page_number] = None
            continue
        level_info_by_anchor[level_info.anchor] = level_info
        level_anchor_by_page[page.page_number] = level_info.anchor

    levels_by_anchor = {
        anchor: _level_entity(document.source_id, info)
        for anchor, info in level_info_by_anchor.items()
    }

    spaces: list[Space] = []
    wall_contexts: list[_WallContext] = []
    slabs: list[Slab] = []
    ceilings: list[Ceiling] = []
    openings: list[Opening] = []
    used_space_ids: set[str] = set()
    used_opening_identity: set[str] = set()
    base_geometry_page: int | None = None
    registration_fallback_provenance: list[Provenance] = []

    for page in ordered_pages:
        classification = classify_page(page)
        page_record: dict[str, object] = {
            "page": page.page_number,
            "classification": classification.kind,
            "classification_confidence": classification.confidence,
            "architectural_score": classification.architectural_score,
            "electrical_score": classification.electrical_score,
        }
        if classification.kind != "architectural_plan":
            page_record["status"] = "ignored_non_architectural_plan"
            page_metadata.append(page_record)
            continue

        level_anchor = level_anchor_by_page.get(page.page_number)
        if level_anchor is None:
            page_record["status"] = "skipped_unresolved_level"
            page_metadata.append(page_record)
            continue
        level_info = level_info_by_anchor[level_anchor]
        level = levels_by_anchor[level_anchor]

        scale = _resolve_scale(page, options, ambiguities)
        transform, scale, registration_fallback = _resolve_transform(
            page,
            scale,
            options,
            allow_page_local_origin=base_geometry_page is None,
            ambiguities=ambiguities,
        )
        if transform is None or scale is None:
            page_record["status"] = "skipped_unresolved_scale_or_registration"
            page_metadata.append(page_record)
            continue
        page_record.update(
            {
                "scale_meters_per_point": scale.meters_per_point,
                "scale_method": scale.method,
                "scale_source_text": scale.source_text,
                "registration_method": transform.method,
                "registration_rotation_radians": transform.rotation_radians,
                "registration_translation_m": [transform.tx_m, transform.ty_m],
                "level": level.name,
            }
        )
        if registration_fallback is not None:
            page_record["registration_confidence"] = transform.confidence
            page_record["registration_provenance"] = registration_fallback
            registration_fallback_provenance.append(
                Provenance(
                    source_kind="architectural_pdf",
                    source_id=document.source_id,
                    page=page.page_number,
                    method=transform.method,
                    confidence=transform.confidence,
                    attributes=registration_fallback,
                )
            )

        rooms = _room_labels(page)
        rectangle_shells = _shell_candidates(page, scale, rooms, options, ambiguities)
        ordinary_vector_shells = _ordinary_vector_shell_candidates(
            page,
            scale,
            rooms,
            options,
            ambiguities,
            excluded_room_anchors={shell.room.anchor for shell in rectangle_shells},
        )
        shells = (*rectangle_shells, *ordinary_vector_shells)
        room_heights, blocked_room_heights = _room_ceiling_height_evidence(page, shells, ambiguities)
        slab_thickness = _slab_thickness_from_text(page)
        page_walls: list[_WallContext] = []
        for shell in shells:
            prospective_space_id = stable_id(
                "space",
                f"{document.source_id}|level:{level_info.anchor}|room:{shell.room.anchor}",
            )
            if prospective_space_id in used_space_ids:
                ambiguities.append(
                    {
                        "page": page.page_number,
                        "code": "duplicate_room_identity_across_pages",
                        "detail": (
                            f"room {shell.room.name!r} repeats a canonical level/room identity; "
                            "later geometry was not substituted automatically"
                        ),
                    }
                )
                continue
            space, shell_walls, slab, ceiling = _shell_entities(
                shell,
                page,
                transform,
                scale,
                level,
                level_info,
                room_heights.get(shell.room.anchor),
                shell.room.anchor in blocked_room_heights,
                document.source_id,
                slab_thickness,
                ambiguities,
            )
            spaces.append(space)
            used_space_ids.add(space.id)
            page_walls.extend(shell_walls)
            if slab:
                slabs.append(slab)
            if ceiling:
                ceilings.append(ceiling)

        consumed_vector_line_ids = {
            source_element_id
            for shell in shells
            for boundary in (shell.outer, shell.inner)
            if boundary.source_kind == "ordinary_vector_line_loop"
            for source_element_id in boundary.source_element_ids
        }
        blocking_enclosure_codes = {
            "duplicate_room_label",
            "ordinary_vector_enclosure_ambiguous",
        }
        page_has_blocking_enclosure_ambiguity = any(
            item.get("page") == page.page_number
            and item.get("code") in blocking_enclosure_codes
            for item in ambiguities
        )
        if page_has_blocking_enclosure_ambiguity:
            geometric_line_walls, geometric_spaces = (), ()
        else:
            geometric_line_walls, geometric_spaces = _geometric_wall_loop_entities(
                page,
                transform,
                level,
                level_info,
                document.source_id,
                options,
                rooms,
                ambiguities,
                excluded_element_ids=consumed_vector_line_ids,
            )
            resolved_geometric_label_anchors = {
                space.attributes["pdf_architecture"].get("label_anchor")
                for space in geometric_spaces
                if space.attributes["pdf_architecture"].get("label_anchor")
            }
            if resolved_geometric_label_anchors:
                ambiguities[:] = [
                    item
                    for item in ambiguities
                    if not (
                        item.get("page") == page.page_number
                        and item.get("code") == "ordinary_vector_enclosure_unresolved"
                        and item.get("room_anchor") in resolved_geometric_label_anchors
                    )
                ]
        existing_wall_geometry = {
            (
                tuple(
                    sorted(
                        (
                            (round(context.wall.centerline.points[0].x, 6), round(context.wall.centerline.points[0].y, 6)),
                            (round(context.wall.centerline.points[-1].x, 6), round(context.wall.centerline.points[-1].y, 6)),
                        )
                    )
                ),
                round(context.wall.thickness_m, 6),
            )
            for context in page_walls
        }
        for context in geometric_line_walls:
            geometry_key = (
                tuple(
                    sorted(
                        (
                            (round(context.wall.centerline.points[0].x, 6), round(context.wall.centerline.points[0].y, 6)),
                            (round(context.wall.centerline.points[-1].x, 6), round(context.wall.centerline.points[-1].y, 6)),
                        )
                    )
                ),
                round(context.wall.thickness_m, 6),
            )
            if geometry_key not in existing_wall_geometry:
                page_walls.append(context)
                existing_wall_geometry.add(geometry_key)

        existing_space_footprints = {
            tuple(
                sorted(
                    (round(point.x, 6), round(point.y, 6))
                    for point in space.footprint.points
                )
            )
            for space in spaces
            if space.level_id == level.id
        }
        for geometric_space in geometric_spaces:
            footprint_key = tuple(
                sorted(
                    (round(point.x, 6), round(point.y, 6))
                    for point in geometric_space.footprint.points
                )
            )
            if footprint_key not in existing_space_footprints:
                spaces.append(geometric_space)
                used_space_ids.add(geometric_space.id)
                existing_space_footprints.add(footprint_key)

        wall_contexts.extend(page_walls)
        page_openings = _make_openings(
            page,
            transform,
            level,
            tuple(page_walls),
            document.source_id,
            options,
            ambiguities,
            used_opening_identity,
        )
        openings.extend(page_openings)
        page_room_count = sum(
            1 for space in spaces if space.provenance and space.provenance[0].page == page.page_number
        )
        page_slab_count = sum(
            1 for slab in slabs if slab.provenance and slab.provenance[0].page == page.page_number
        )
        page_ceiling_count = sum(
            1 for ceiling in ceilings if ceiling.provenance and ceiling.provenance[0].page == page.page_number
        )
        page_geometry_count = (
            page_room_count
            + len(page_walls)
            + page_slab_count
            + page_ceiling_count
            + len(page_openings)
        )
        if page_geometry_count:
            page_record["status"] = "geometry_imported"
            if base_geometry_page is None:
                base_geometry_page = page.page_number
        else:
            page_record["status"] = "no_supported_geometry_recognized"
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "architectural_geometry_unrecognized",
                    "detail": (
                        "architectural plan resolved level, scale, and registration but "
                        "produced no supported canonical spatial geometry; the page did "
                        "not establish the shared geometry frame"
                    ),
                }
            )
        page_record["resolved_room_count"] = page_room_count
        page_record["resolved_wall_count"] = len(page_walls)
        if ordinary_vector_shells:
            page_record["ordinary_vector_enclosure_count"] = len(ordinary_vector_shells)
        if geometric_spaces:
            page_record["geometric_wall_loop_count"] = len(geometric_spaces)
        page_record["stable_native_line_count"] = sum(1 for item in page.lines if item.native_id)
        page_record["untagged_vector_line_count"] = sum(1 for item in page.lines if not item.native_id)
        page_metadata.append(page_record)

    walls = [context.wall for context in wall_contexts]
    entity_confidences = [
        *(item.confidence for item in levels_by_anchor.values()),
        *(item.confidence for item in spaces),
        *(item.confidence for item in walls),
        *(item.confidence for item in slabs),
        *(item.confidence for item in ceilings),
        *(item.confidence for item in openings),
    ]
    model_confidence = min(entity_confidences) if entity_confidences else 0.5
    model_provenance = (
        Provenance(
            source_kind="architectural_pdf",
            source_id=document.source_id,
            method="deterministic vector/text architectural PDF importer",
            confidence=model_confidence,
            attributes={"content_sha256": document.content_sha256},
        ),
        *registration_fallback_provenance,
    )
    attributes = {
        "pdf_architecture": {
            "content_sha256": document.content_sha256,
            "pages": page_metadata,
            "ambiguities": sorted(
                ambiguities,
                key=lambda item: (int(item.get("page", 0)), str(item.get("code", "")), str(item.get("detail", ""))),
            ),
        }
    }
    return BuildingModel(
        model_id=stable_id("model", f"architectural-pdf:{document.source_id}"),
        name=f"Architectural PDF import {document.source_id}",
        coordinate_system=CoordinateSystem(frame_id="model"),
        levels=tuple(sorted(levels_by_anchor.values(), key=lambda item: item.id)),
        spaces=tuple(sorted(spaces, key=lambda item: item.id)),
        walls=tuple(sorted(walls, key=lambda item: item.id)),
        slabs=tuple(sorted(slabs, key=lambda item: item.id)),
        ceilings=tuple(sorted(ceilings, key=lambda item: item.id)),
        openings=tuple(sorted(openings, key=lambda item: item.id)),
        provenance=model_provenance,
        attributes=attributes,
    )


def import_architectural_pdf(
    path: str | Path,
    *,
    source_id: str | None = None,
    options: ImportOptions | None = None,
) -> BuildingModel:
    """Import an architectural PDF into the canonical model.

    source_id should be a stable logical document identifier when the caller
    expects IDs to survive revised PDF bytes.  When omitted, the file stem is
    used as the logical source identity while a SHA-256 of the bytes is retained
    separately in provenance/attributes for traceability.
    """

    return import_observations(extract_pdf(path, source_id=source_id), options=options)
