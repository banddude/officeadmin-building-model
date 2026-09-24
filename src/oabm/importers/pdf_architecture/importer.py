"""Deterministic architectural-PDF to canonical-model importer.

The importer is deliberately conservative.  It promotes source observations only
when scale, registration, semantic identity, and the contract-required 3D values
can be supported.  Unresolved facts are retained as explicit diagnostics in the
canonical model's source-specific attributes rather than guessed.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median
from typing import Iterable

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
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

from .drawing_regions import (
    DrawingRegionSplit,
    RegionEvidence,
    SourceDrawingRegion,
    signatures_repeat,
    split_drawing_regions,
)
from .extract import _is_wall_source_layer, extract_pdf
from .layered_rooms import (
    LayeredRoomRegion,
    find_layered_room_regions,
    has_multiple_wall_regions,
)
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
    source_element_id: str | None = None


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
class _WallFaceRun:
    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    source_element_ids: tuple[str, ...]
    primitive_families: tuple[str, ...]
    dashed: bool
    filled: bool


@dataclass(frozen=True, slots=True)
class _WallFacePair:
    start_pt: tuple[float, float]
    end_pt: tuple[float, float]
    thickness_m: float
    source_element_ids: tuple[str, ...]
    primitive_families: tuple[str, ...]
    dashed: bool
    junction_supported: bool
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
    drawing_title = _explicit_drawing_title(page)
    if drawing_title and re.search(r"\b(?:DETAILS?|MILLWORK|INTERIOR ELEVATIONS?)\b", drawing_title):
        return SheetClassification("other", 0.95, 0, 0)
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


def _explicit_drawing_title(page: PdfPageObservation) -> str | None:
    """Read a ruled title-block drawing title when the source provides one."""

    markers = [
        item for item in page.texts
        if _clean_text(item.text).upper() == "DRAWING TITLE:"
        and item.center_pt[0] >= 0.70 * page.width_pt
        and item.center_pt[1] <= 0.30 * page.height_pt
    ]
    if len(markers) != 1:
        return None
    marker = markers[0]
    sheet_markers = [
        item for item in page.texts
        if _clean_text(item.text).upper() in {"SHEET NO:", "SHEET NUMBER:"}
        and item.center_pt[0] >= 0.70 * page.width_pt
        and item.center_pt[1] < marker.center_pt[1]
    ]
    lower_y = max(
        (item.center_pt[1] for item in sheet_markers),
        default=marker.center_pt[1] - 90.0,
    )
    title_lines = [
        item for item in page.texts
        if item.element_id != marker.element_id
        and item.center_pt[0] >= 0.70 * page.width_pt
        and lower_y < item.center_pt[1] < marker.center_pt[1]
    ]
    if not title_lines:
        return None
    return " ".join(
        _clean_text(item.text).upper()
        for item in sorted(title_lines, key=lambda value: (-value.center_pt[1], value.center_pt[0]))
    )


def _scale_candidates(page: PdfPageObservation) -> list[tuple[float, str, str]]:
    """Printed scales as (metres per point, matched text, source element id)."""

    result: list[tuple[float, str, str]] = []
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
                result.append((ratio * _INCH_M / _PT_PER_INCH, match.group(0), item.element_id))
        for match in metric.finditer(text):
            ratio = float(match.group("ratio"))
            if ratio > 0:
                result.append((ratio * _INCH_M / _PT_PER_INCH, match.group(0), item.element_id))
    return result


def _resolve_scale(
    page: PdfPageObservation,
    override: ScaleOverride | None,
    ambiguities: list[dict[str, object]],
    *,
    sheet_texts: tuple[PdfTextObservation, ...] = (),
) -> _Scale | None:
    """Resolve one drawing's scale.

    ``sheet_texts`` are sheet-level annotations that belong to no single drawing
    region. They are consulted only when the drawing carries no scale of its
    own, and the inheritance is recorded in the scale method.
    """

    if override:
        return _Scale(override.meters_per_point, override.confidence, override.note, None)

    candidates = _scale_candidates(page)
    inherited = False
    if not candidates and sheet_texts:
        candidates = _scale_candidates(replace(page, texts=sheet_texts))
        inherited = bool(candidates)
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
    if inherited:
        return _Scale(
            first,
            0.9,
            "sheet-level printed scale annotation inherited by drawing region",
            candidates[0][1],
            candidates[0][2],
        )
    return _Scale(first, 0.98, "parsed printed scale annotation", candidates[0][1], candidates[0][2])


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
    hint: RegistrationHint | None,
    allow_page_local_origin: bool,
    allow_sheet_geometry_fallback: bool = True,
    ambiguities: list[dict[str, object]],
) -> tuple[_Transform2D | None, _Scale | None, dict[str, object] | None]:
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

    if allow_sheet_geometry_fallback:
        fallback = _sheet_geometry_registration(page, scale, options)
        if fallback is not None:
            transform, metadata = fallback
            return transform, scale, metadata
        detail = (
            "additional architectural plan page requires RegistrationHint before "
            "geometry can share the canonical frame"
        )
    else:
        detail = (
            "drawing region shares its sheet with other drawings; its own frame needs a "
            "region-scoped RegistrationHint and is not taken from the sheet or page origin"
        )

    ambiguities.append(
        {
            "page": page.page_number,
            "code": "registration_unresolved",
            "detail": detail,
        }
    )
    return None, scale, None


_EXPLICIT_LEVEL_RE = re.compile(
    r"^LEVEL\s*[:#-]\s*([A-Z0-9][A-Z0-9 ._-]{0,30})$",
    re.IGNORECASE,
)
_BARE_LEVEL_RE = re.compile(
    r"^LEVEL\s+((?:GROUND|BASEMENT|\d{1,2}(?:ST|ND|RD|TH)?)(?:\s+FLOOR)?)"
    r"(?:\s+(?:[A-Z/&.]+\s+){0,3}PLAN)?$",
    re.IGNORECASE,
)
# A floor designation counts only as a drawing/level title, never inside a note:
# "SECOND FLOOR", "SECOND FLOOR PLAN", "EXISTING SECOND FLOOR POWER PLAN" or a
# plan title with a short qualifier such as "SECOND FLOOR PLAN - UNIT A",
# ": AREA A" or "(NORTH)". A qualifier needs the word PLAN before it, so
# note-shaped strings such as "THIRD FLOOR, TYP." are not level names, nor
# are "SEE SECOND FLOOR FRAMING FOR BLOCKING" or
# "SECOND FLOOR PLAN - SEE SHEET A5 FOR DETAILS".
_TITLE_QUALIFIER_WORDS = r"[A-Z0-9#.&/'-]{1,12}(?:\s+[A-Z0-9#.&/'-]{1,12}){0,2}"
_FLOOR_TITLE_RE = re.compile(
    r"^(?:(?:EXISTING|PROPOSED|NEW|DEMOLITION|DEMO|PARTIAL|OVERALL|ENLARGED|\(E\)|\(N\))\s+)*"
    r"(GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|LOWER|MAIN|UPPER|BASEMENT|\d{1,2}(?:ST|ND|RD|TH))"
    r"\s+FLOOR(?:\s*[-:]?\s*(?:[A-Z/&.]+\s+){0,4}PLAN"
    rf"(?:\s*[-\u2013\u2014:,]\s*{_TITLE_QUALIFIER_WORDS}|\s*\(\s*{_TITLE_QUALIFIER_WORDS}\s*\))?)?$",
    re.IGNORECASE,
)


def _level_name_candidates(
    page: PdfPageObservation,
) -> tuple[tuple[str, PdfTextObservation], ...]:
    """Every explicit level name on the drawing, with its source text."""

    result: list[tuple[str, PdfTextObservation]] = []
    for item in page.texts:
        text = _clean_text(item.text)
        match = _EXPLICIT_LEVEL_RE.fullmatch(text) or _BARE_LEVEL_RE.fullmatch(text)
        if match:
            candidate = _clean_text(match.group(1))
            if candidate.upper() != "PLAN":
                result.append((candidate.title() if candidate.isupper() else candidate, item))
            continue
        match = _FLOOR_TITLE_RE.fullmatch(text)
        if match:
            designation = match.group(1)
            designation = designation.capitalize() if designation.isalpha() else designation.lower()
            result.append((f"{designation} Floor", item))
    return tuple(result)


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


def _level_name_evidence(
    page: PdfPageObservation,
    override: LevelOverride | None,
) -> tuple[str | None, tuple[PdfTextObservation, ...], tuple[str, ...]]:
    """Return (selected name, supporting texts, distinct parsed names).

    An explicit override name wins. Otherwise the drawing must carry exactly
    one distinct level name; several distinct names leave the name unselected.
    """

    candidates = _level_name_candidates(page)
    distinct = tuple(sorted({_anchor(name): name for name, _ in candidates}.values()))
    if override and override.name:
        return override.name, (), distinct
    if len(distinct) != 1:
        return None, (), distinct
    return distinct[0], tuple(item for _, item in candidates), distinct


def _resolve_level(
    page: PdfPageObservation,
    options: ImportOptions,
    known_levels: dict[str, _LevelInfo],
    ambiguities: list[dict[str, object]],
    *,
    override: LevelOverride | None,
    require_drawing_level_name: bool = False,
) -> _LevelInfo | None:
    parsed_name, _, distinct_names = _level_name_evidence(page, override)
    if parsed_name is None and len(distinct_names) > 1:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "level_ambiguous",
                "detail": (
                    "the drawing names more than one level; supply a LevelOverride "
                    "with a name to select one"
                ),
                "level_names": list(distinct_names),
                "source_element_ids": sorted(
                    item.element_id for _, item in _level_name_candidates(page)
                ),
            }
        )
        return None
    if parsed_name is None and require_drawing_level_name:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "level_unresolved",
                "detail": (
                    "this drawing shares its sheet with other drawings and carries no "
                    "level name of its own; sheet-level text is not assigned to one "
                    "drawing, so supply a region-scoped LevelOverride"
                ),
            }
        )
        return None
    name = parsed_name or "Unlabeled Level"
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


def _is_sheet_frame_enclosure(
    page: PdfPageObservation,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Reject an almost page-sized ruled frame as a room or wall enclosure.

    The source plan may fill a large drawing field, so require both near-page
    coverage and proximity to all four media edges. Ordinary room enclosures
    need not be rectangular, but the current rectangle recognizers do.
    """

    x0, y0, x1, y1 = bbox
    width = page.width_pt
    height = page.height_pt
    return (
        (x1 - x0) >= 0.75 * width
        and (y1 - y0) >= 0.85 * height
        and x0 <= 0.10 * width
        and y0 <= 0.10 * height
        and x1 >= 0.85 * width
        and y1 >= 0.90 * height
    )


def _shell_candidates(
    page: PdfPageObservation,
    scale: _Scale,
    rooms: tuple[_RoomLabel, ...],
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
) -> tuple[_Shell, ...]:
    candidates: list[tuple[float, float, PdfRectObservation, PdfRectObservation]] = []
    rejected_frames = 0
    for outer in page.rects:
        if _is_sheet_frame_enclosure(page, outer.bbox_pt):
            rejected_frames += 1
            continue
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
    if rejected_frames:
        ambiguities.append({
            "page": page.page_number,
            "code": "sheet_frame_enclosure_rejected",
            "detail": "near-page ruled frame is not building geometry",
            "rejected_rectangle_count": rejected_frames,
        })

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
    raw_loops = _ordinary_vector_rect_loops(page)
    loops = tuple(
        loop for loop in raw_loops
        if not _is_sheet_frame_enclosure(page, loop.bbox_pt)
    )
    if len(loops) != len(raw_loops):
        ambiguities.append({
            "page": page.page_number,
            "code": "sheet_frame_enclosure_rejected",
            "detail": "near-page ruled frame is not building geometry",
            "rejected_loop_count": len(raw_loops) - len(loops),
        })
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
            derivation=DERIVATION_OBSERVED,
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


def _layered_region_space(
    region: LayeredRoomRegion,
    room: _RoomLabel,
    page: PdfPageObservation,
    transform: _Transform2D,
    scale: _Scale,
    level: Level,
    level_info: _LevelInfo,
    source_id: str,
) -> Space:
    confidence = min(room.confidence, scale.confidence, transform.confidence, 0.64)
    identity = f"{source_id}|level:{level_info.anchor}|room:{room.anchor}"
    return Space(
        id=stable_id("space", identity),
        name=room.name,
        level_id=level.id,
        footprint=Polygon3D(points=tuple(
            _point3(point, transform, level.elevation_m) for point in region.polygon_pt
        )),
        height_m=None,
        usage=room.usage,
        confidence=confidence,
        provenance=(
            Provenance(
                source_kind="architectural_pdf",
                derivation=DERIVATION_INFERRED,
                source_id=source_id,
                source_element_id=room.observation.element_id,
                page=page.page_number,
                method="room interior bounded by visible CAD walls and supported opening closures",
                confidence=confidence,
                attributes={
                    "source_wall_elements": list(region.source_wall_ids),
                    "source_opening_elements": list(region.source_opening_ids),
                    "opening_closure_count": region.closure_count,
                    "raster_resolution_pt": 1.0,
                    "height_status": "unresolved",
                    "wall_thickness_status": "unresolved",
                },
            ),
            _provenance(
                source_id,
                page.page_number,
                method="room label observed inside closed CAD wall region",
                confidence=room.confidence,
                source_element_id=room.observation.element_id,
            )[0],
        ),
        attributes={"pdf_architecture": {
            "identity_anchor": room.anchor,
            "recognition": "layered_wall_opening_region",
            "geometry_derivation": "inferred_from_observed_vectors",
            "label_source_element_id": room.observation.element_id,
        }},
    )


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
    line: PdfLineObservation | _WallFaceRun,
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


def _gap_histogram(gaps_m: list[float]) -> list[dict[str, object]]:
    edges = (
        0.0,
        2.0 * _INCH_M,
        4.0 * _INCH_M,
        8.0 * _INCH_M,
        12.0 * _INCH_M,
        18.0 * _INCH_M,
        24.0 * _INCH_M,
    )
    result: list[dict[str, object]] = []
    for lower, upper in zip(edges, edges[1:]):
        result.append(
            {
                "min_m": round(lower, 6),
                "max_m": round(upper, 6),
                "count": sum(1 for value in gaps_m if lower <= value < upper),
            }
        )
    result.append(
        {
            "min_m": round(edges[-1], 6),
            "max_m": None,
            "count": sum(1 for value in gaps_m if value >= edges[-1]),
        }
    )
    return result


def _source_point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float]:
    vx = end[0] - start[0]
    vy = end[1] - start[1]
    length_sq = vx * vx + vy * vy
    if length_sq <= 1e-12:
        return (math.inf, 0.0)
    raw_t = (
        (point[0] - start[0]) * vx + (point[1] - start[1]) * vy
    ) / length_sq
    t = max(0.0, min(1.0, raw_t))
    nearest = (start[0] + t * vx, start[1] + t * vy)
    return (math.dist(point, nearest), raw_t)


def _is_dimension_pattern_text(text: str) -> bool:
    cleaned = _clean_text(text)
    if _find_dimension(cleaned) is not None:
        return True
    return bool(
        re.fullmatch(
            r"[+-]?\d{4,5}(?:\.\d+)?(?:\s*MM)?",
            cleaned,
            re.IGNORECASE,
        )
    )


def _dimension_marker_evidence_ids(
    lines: list[PdfLineObservation],
    transform: _Transform2D,
) -> set[str]:
    if not lines:
        return set()

    endpoint_tolerance_pt = (3.0 * _INCH_M) / transform.meters_per_point
    marker_max_length_m = 36.0 * _INCH_M
    marker_max_length_pt = marker_max_length_m / transform.meters_per_point
    cell_size = max(marker_max_length_pt, 1.0)
    marker_cells: dict[tuple[int, int], set[int]] = {}
    lengths_m = [
        math.dist(line.start_pt, line.end_pt) * transform.meters_per_point
        for line in lines
    ]
    for index, line in enumerate(lines):
        if lengths_m[index] > marker_max_length_m:
            continue
        x0 = min(line.start_pt[0], line.end_pt[0]) - endpoint_tolerance_pt
        x1 = max(line.start_pt[0], line.end_pt[0]) + endpoint_tolerance_pt
        y0 = min(line.start_pt[1], line.end_pt[1]) - endpoint_tolerance_pt
        y1 = max(line.start_pt[1], line.end_pt[1]) + endpoint_tolerance_pt
        for cell_x in range(math.floor(x0 / cell_size), math.floor(x1 / cell_size) + 1):
            for cell_y in range(math.floor(y0 / cell_size), math.floor(y1 / cell_size) + 1):
                marker_cells.setdefault((cell_x, cell_y), set()).add(index)

    result: set[str] = set()
    minimum_cross = math.sin(math.radians(20.0))
    for index, line in enumerate(lines):
        line_length_m = lengths_m[index]
        max_marker_m = min(marker_max_length_m, line_length_m * 0.45)
        if max_marker_m < 2.0 * _INCH_M:
            continue
        ux, uy, _, _, _ = _line_record(line)
        matched_marker_ids: set[str] = set()
        endpoint_hits = 0
        for endpoint in (line.start_pt, line.end_pt):
            cell_x = math.floor(endpoint[0] / cell_size)
            cell_y = math.floor(endpoint[1] / cell_size)
            hit = False
            for nearby_x in range(cell_x - 1, cell_x + 2):
                for nearby_y in range(cell_y - 1, cell_y + 2):
                    for other_index in marker_cells.get((nearby_x, nearby_y), ()):
                        if other_index == index or lengths_m[other_index] > max_marker_m:
                            continue
                        other = lines[other_index]
                        oux, ouy, _, _, _ = _line_record(other)
                        if abs(ux * ouy - uy * oux) < minimum_cross:
                            continue
                        distance, _ = _source_point_to_segment_distance(
                            endpoint,
                            other.start_pt,
                            other.end_pt,
                        )
                        if distance <= endpoint_tolerance_pt:
                            hit = True
                            matched_marker_ids.add(other.element_id)
            if hit:
                endpoint_hits += 1
        if endpoint_hits == 2:
            result.add(line.element_id)
            result.update(matched_marker_ids)
    return result


def _dimension_evidence_ids(
    page: PdfPageObservation,
    lines: list[PdfLineObservation],
    transform: _Transform2D,
) -> set[str]:
    result = _dimension_marker_evidence_ids(lines, transform)
    dimension_texts = [
        observation
        for observation in page.texts
        if _is_dimension_pattern_text(observation.text)
    ]
    if not dimension_texts:
        return result

    perpendicular_tolerance_pt = (12.0 * _INCH_M) / transform.meters_per_point
    along_margin_pt = (6.0 * _INCH_M) / transform.meters_per_point
    for line in lines:
        line_length_pt = math.dist(line.start_pt, line.end_pt)
        along_margin_fraction = along_margin_pt / max(line_length_pt, 1e-9)
        for observation in dimension_texts:
            distance, raw_t = _source_point_to_segment_distance(
                observation.center_pt,
                line.start_pt,
                line.end_pt,
            )
            if (
                distance <= perpendicular_tolerance_pt
                and -along_margin_fraction <= raw_t <= 1.0 + along_margin_fraction
            ):
                result.add(line.element_id)
                break
    return result


def _hatch_evidence_ids(
    page: PdfPageObservation,
    lines: list[PdfLineObservation],
    transform: _Transform2D,
) -> set[str]:
    if not lines:
        return set()

    max_hatch_length_m = 72.0 * _INCH_M
    min_hatch_length_m = 1.0 * _INCH_M
    max_hatch_length_pt = max_hatch_length_m / transform.meters_per_point
    min_pitch_pt = (0.5 * _INCH_M) / transform.meters_per_point
    max_pitch_pt = (18.0 * _INCH_M) / transform.meters_per_point
    candidates = [
        (index, line)
        for index, line in enumerate(lines)
        if min_hatch_length_m
        <= math.dist(line.start_pt, line.end_pt) * transform.meters_per_point
        <= max_hatch_length_m
    ]
    result = {line.element_id for _, line in candidates if line.filled}

    page_area = max(page.width_pt * page.height_pt, 1.0)
    filled_rects = [
        rect
        for rect in page.rects
        if rect.filled
        and (rect.width_pt * rect.height_pt) <= page_area * 0.35
    ]
    for _, line in candidates:
        midpoint = (
            (line.start_pt[0] + line.end_pt[0]) / 2.0,
            (line.start_pt[1] + line.end_pt[1]) / 2.0,
        )
        for rect in filled_rects:
            if all(
                _inside(rect.bbox_pt, point)
                for point in (line.start_pt, midpoint, line.end_pt)
            ):
                result.add(line.element_id)
                break

    if len(candidates) < 5:
        return result

    angle_tolerance = math.radians(2.0)
    orientation_bucket_count = max(1, int(round(math.pi / angle_tolerance)))
    tangent_bin_size = max(max_hatch_length_pt, 1.0)
    groups: dict[
        tuple[int, int],
        list[tuple[float, float, float, int]],
    ] = {}
    for index, line in candidates:
        start, end = _canonical_segment(line.start_pt, line.end_pt)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        theta = math.atan2(dy, dx) % math.pi
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
            normal_value = (
                ((start[0] + end[0]) / 2.0) * nx
                + ((start[1] + end[1]) / 2.0) * ny
            )
            first_bin = math.floor(tangent_values[0] / tangent_bin_size)
            last_bin = math.floor(tangent_values[1] / tangent_bin_size)
            for tangent_bin in range(first_bin, last_bin + 1):
                groups.setdefault(
                    (orientation_bucket, tangent_bin),
                    [],
                ).append(
                    (
                        normal_value,
                        tangent_values[0],
                        tangent_values[1],
                        index,
                    )
                )

    for values in groups.values():
        unique_values = {
            item[3]: item
            for item in values
        }
        ordered = sorted(unique_values.values(), key=lambda item: (item[0], item[3]))
        if len(ordered) < 5:
            continue
        for start_index in range(len(ordered) - 4):
            window = ordered[start_index : start_index + 5]
            normals = [item[0] for item in window]
            gaps = [
                second - first
                for first, second in zip(normals, normals[1:])
            ]
            if any(gap <= 0 for gap in gaps):
                continue
            pitch = float(median(gaps))
            if not (min_pitch_pt <= pitch <= max_pitch_pt):
                continue
            if any(abs(gap - pitch) > pitch * 0.25 for gap in gaps):
                continue
            common_overlap = min(item[2] for item in window) - max(
                item[1] for item in window
            )
            shortest_span = min(item[2] - item[1] for item in window)
            if common_overlap < shortest_span * 0.5:
                continue
            result.update(lines[item[3]].element_id for item in window)
    return result


def _wall_pair_has_endpoint_junction(
    start_pt: tuple[float, float],
    end_pt: tuple[float, float],
    first_index: int,
    second_index: int,
    runs: list[_WallFaceRun],
    records: list[tuple[float, float, float, float, float]],
    endpoint_cells: dict[tuple[int, int], set[tuple[int, tuple[float, float]]]],
    junction_tolerance_pt: float,
) -> bool:
    dx = end_pt[0] - start_pt[0]
    dy = end_pt[1] - start_pt[1]
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return False
    ux, uy = dx / length, dy / length
    minimum_cross = math.sin(math.radians(15.0))
    cell_size = max(junction_tolerance_pt, 1.0)
    for endpoint in (start_pt, end_pt):
        cell_x = math.floor(endpoint[0] / cell_size)
        cell_y = math.floor(endpoint[1] / cell_size)
        for nearby_x in range(cell_x - 1, cell_x + 2):
            for nearby_y in range(cell_y - 1, cell_y + 2):
                for run_index, other_endpoint in endpoint_cells.get(
                    (nearby_x, nearby_y),
                    (),
                ):
                    if run_index in {first_index, second_index}:
                        continue
                    oux, ouy, _, _, _ = records[run_index]
                    if abs(ux * ouy - uy * oux) < minimum_cross:
                        continue
                    if math.dist(endpoint, other_endpoint) <= junction_tolerance_pt:
                        return True
    return False


def _collinear_wall_face_runs(
    page: PdfPageObservation,
    transform: _Transform2D,
    *,
    excluded_element_ids: set[str],
    diagnostics: dict[str, object],
) -> tuple[_WallFaceRun, ...]:
    by_geometry: dict[tuple[float, float, float, float], list[PdfLineObservation]] = {}
    input_lines = [
        line
        for line in page.lines
        if line.element_id not in excluded_element_ids
    ]
    dimension_evidence_ids = _dimension_evidence_ids(page, input_lines, transform)
    hatch_evidence_ids = _hatch_evidence_ids(
        page,
        [
            line
            for line in input_lines
            if line.element_id not in dimension_evidence_ids
        ],
        transform,
    )
    eligible = [
        line
        for line in input_lines
        if line.element_id not in dimension_evidence_ids
        and line.element_id not in hatch_evidence_ids
    ]
    for line in eligible:
        start, end = _canonical_segment(line.start_pt, line.end_pt)
        key = (
            round(start[0], 6),
            round(start[1], 6),
            round(end[0], 6),
            round(end[1], 6),
        )
        by_geometry.setdefault(key, []).append(line)

    duplicate_geometry = {
        key for key, values in by_geometry.items() if len(values) != 1
    }
    lines = [
        values[0]
        for key, values in sorted(by_geometry.items())
        if key not in duplicate_geometry
    ]

    family_counts: dict[str, int] = {}
    for line in input_lines:
        family_counts[line.primitive_family] = family_counts.get(line.primitive_family, 0) + 1

    diagnostics.update(
        {
            "input_segment_count": len(input_lines),
            "wall_evidence_segment_count": len(eligible),
            "primitive_family_counts": dict(sorted(family_counts.items())),
            "dashed_input_segment_count": sum(1 for line in input_lines if line.dashed),
            "filled_input_segment_count": sum(1 for line in input_lines if line.filled),
            "dimension_evidence_rejected_count": len(dimension_evidence_ids),
            "hatch_evidence_rejected_count": len(hatch_evidence_ids),
            "duplicate_geometry_segment_count": sum(
                len(by_geometry[key]) for key in duplicate_geometry
            ),
            "style_or_layer_gate_applied": False,
            "style_or_layer_rejected_count": 0,
        }
    )
    if not lines:
        diagnostics.update(
            {
                "unique_segment_count": 0,
                "joined_run_count": 0,
                "collinear_join_component_count": 0,
            }
        )
        return ()

    angle_tolerance = math.radians(2.0)
    max_cross = math.sin(angle_tolerance)
    join_gap_pt = (6.0 * _INCH_M) / transform.meters_per_point
    collinear_offset_pt = (0.25 * _INCH_M) / transform.meters_per_point
    orientation_bucket_count = max(1, int(round(math.pi / angle_tolerance)))
    tangent_bin_size = max(join_gap_pt * 4.0, 32.0)
    normal_bin_size = max(collinear_offset_pt * 2.0, 1.0)
    records = [_line_record(line) for line in lines]

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
            normal_value = (
                ((start[0] + end[0]) / 2.0) * nx
                + ((start[1] + end[1]) / 2.0) * ny
            )
            tangent_start = math.floor(
                (tangent_values[0] - join_gap_pt) / tangent_bin_size
            )
            tangent_end = math.floor(
                (tangent_values[1] + join_gap_pt) / tangent_bin_size
            )
            normal_start = math.floor(
                (normal_value - collinear_offset_pt) / normal_bin_size
            )
            normal_end = math.floor(
                (normal_value + collinear_offset_pt) / normal_bin_size
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

    parents = list(range(len(lines)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if first_root < second_root:
            parents[second_root] = first_root
        else:
            parents[first_root] = second_root

    for first_index, second_index in sorted(candidate_pairs):
        ux, uy, a0, a1, first_normal = records[first_index]
        sux, suy, _, _, _ = records[second_index]
        if abs(ux * suy - uy * sux) > max_cross:
            continue
        nx, ny = -uy, ux
        second = lines[second_index]
        b_values = sorted(
            (
                second.start_pt[0] * ux + second.start_pt[1] * uy,
                second.end_pt[0] * ux + second.end_pt[1] * uy,
            )
        )
        second_normal = (
            ((second.start_pt[0] + second.end_pt[0]) / 2.0) * nx
            + ((second.start_pt[1] + second.end_pt[1]) / 2.0) * ny
        )
        if abs(second_normal - first_normal) > collinear_offset_pt:
            continue
        if b_values[0] > a1:
            gap_pt = b_values[0] - a1
        elif a0 > b_values[1]:
            gap_pt = a0 - b_values[1]
        else:
            gap_pt = 0.0
        if gap_pt <= join_gap_pt:
            union(first_index, second_index)

    components: dict[int, list[int]] = {}
    for index in range(len(lines)):
        components.setdefault(find(index), []).append(index)

    runs: list[_WallFaceRun] = []
    joined_components = 0
    for members in components.values():
        ordered_members = sorted(members)
        if len(ordered_members) > 1:
            joined_components += 1
        reference_index = max(
            ordered_members,
            key=lambda index: (
                math.dist(lines[index].start_pt, lines[index].end_pt),
                -index,
            ),
        )
        ux, uy, _, _, _ = records[reference_index]
        nx, ny = -uy, ux
        tangent_values: list[float] = []
        normal_values: list[float] = []
        source_ids: set[str] = set()
        primitive_families: set[str] = set()
        dashed = False
        filled = False
        for index in ordered_members:
            line = lines[index]
            source_ids.add(line.element_id)
            primitive_families.add(line.primitive_family)
            dashed = dashed or line.dashed
            filled = filled or line.filled
            for point in (line.start_pt, line.end_pt):
                tangent_values.append(point[0] * ux + point[1] * uy)
                normal_values.append(point[0] * nx + point[1] * ny)
        mean_normal = sum(normal_values) / len(normal_values)
        tangent_start = min(tangent_values)
        tangent_end = max(tangent_values)
        start_pt = (
            tangent_start * ux + mean_normal * nx,
            tangent_start * uy + mean_normal * ny,
        )
        end_pt = (
            tangent_end * ux + mean_normal * nx,
            tangent_end * uy + mean_normal * ny,
        )
        start_pt, end_pt = _canonical_segment(start_pt, end_pt)
        runs.append(
            _WallFaceRun(
                start_pt=start_pt,
                end_pt=end_pt,
                source_element_ids=tuple(sorted(source_ids)),
                primitive_families=tuple(sorted(primitive_families)),
                dashed=dashed,
                filled=filled,
            )
        )

    diagnostics.update(
        {
            "unique_segment_count": len(lines),
            "joined_run_count": len(runs),
            "collinear_join_component_count": joined_components,
            "collinear_join_gap_m": round(6.0 * _INCH_M, 6),
            "collinear_offset_tolerance_m": round(0.25 * _INCH_M, 6),
        }
    )
    return tuple(
        sorted(
            runs,
            key=lambda run: (
                _canonical_segment(run.start_pt, run.end_pt),
                run.source_element_ids,
            ),
        )
    )


def _geometric_wall_face_pairs(
    page: PdfPageObservation,
    transform: _Transform2D,
    options: ImportOptions,
    *,
    excluded_element_ids: set[str] | None = None,
    diagnostics: dict[str, object] | None = None,
) -> tuple[_WallFacePair, ...]:
    """Pair wall faces from joined CAD runs, independent of PDF-native IDs."""

    diagnostics = diagnostics if diagnostics is not None else {}
    excluded_element_ids = excluded_element_ids or set()
    runs = _collinear_wall_face_runs(
        page,
        transform,
        excluded_element_ids=excluded_element_ids,
        diagnostics=diagnostics,
    )
    minimum_overlap_m = min(options.min_space_span_m * 0.5, 12.0 * _INCH_M)
    minimum_overlap_pt = minimum_overlap_m / transform.meters_per_point
    eligible_runs = [
        run
        for run in runs
        if math.dist(run.start_pt, run.end_pt) >= minimum_overlap_pt
    ]
    minimum_overlap_ratio = 0.25
    diagnostics["minimum_pair_overlap_m"] = round(minimum_overlap_m, 6)
    diagnostics["minimum_overlap_ratio"] = minimum_overlap_ratio
    diagnostics["minimum_length_rejected_count"] = len(runs) - len(eligible_runs)
    diagnostics["wall_gap_range_m"] = [
        round(options.min_wall_thickness_m, 6),
        round(options.max_wall_thickness_m, 6),
    ]
    if not eligible_runs:
        diagnostics.update(
            {
                "candidate_pair_count": 0,
                "parallel_gap_histogram_m": _gap_histogram([]),
                "accepted_pair_count": 0,
                "rejected": {
                    "parallel": 0,
                    "overlap_ratio": 0,
                    "gap_range": 0,
                    "ambiguous_or_non_mutual": 0,
                },
            }
        )
        return ()

    records = [_line_record(run) for run in eligible_runs]
    angle_tolerance = math.radians(2.0)
    orientation_bucket_count = max(1, int(round(math.pi / angle_tolerance)))
    diagnostic_max_offset_m = max(options.max_wall_thickness_m, 24.0 * _INCH_M)
    diagnostic_max_offset_pt = diagnostic_max_offset_m / transform.meters_per_point
    normal_bin_size = max(diagnostic_max_offset_pt, 1.0)
    tangent_bin_size = max(minimum_overlap_pt, diagnostic_max_offset_pt * 2.0, 16.0)
    candidate_cells: dict[tuple[int, int, int], set[int]] = {}
    for run_index, run in enumerate(eligible_runs):
        start, end = _canonical_segment(run.start_pt, run.end_pt)
        ux, uy, _, _, _ = records[run_index]
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
                (normal_values[0] - diagnostic_max_offset_pt) / normal_bin_size
            )
            normal_end = math.floor(
                (normal_values[1] + diagnostic_max_offset_pt) / normal_bin_size
            )
            for normal_bucket in range(normal_start, normal_end + 1):
                for tangent_bucket in range(tangent_start, tangent_end + 1):
                    candidate_cells.setdefault(
                        (orientation_bucket, normal_bucket, tangent_bucket),
                        set(),
                    ).add(run_index)

    candidate_pairs: set[tuple[int, int]] = set()
    for cell_runs in candidate_cells.values():
        ordered = sorted(cell_runs)
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
    rejected_parallel = 0
    rejected_overlap = 0
    rejected_gap = 0
    gap_samples_m: list[float] = []
    max_cross = math.sin(angle_tolerance)
    for first_index, second_index in sorted(candidate_pairs):
        ux, uy, a0, a1, first_normal = records[first_index]
        sux, suy, _, _, _ = records[second_index]
        if abs(ux * suy - uy * sux) > max_cross:
            rejected_parallel += 1
            continue
        nx, ny = -uy, ux
        second = eligible_runs[second_index]
        cx, cy = second.start_pt
        ex, ey = second.end_pt
        b0, b1 = sorted((cx * ux + cy * uy, ex * ux + ey * uy))
        overlap0 = max(a0, b0)
        overlap1 = min(a1, b1)
        overlap_pt = overlap1 - overlap0
        overlap_m = overlap_pt * transform.meters_per_point
        shorter_span_pt = min(a1 - a0, b1 - b0)
        overlap_ratio = max(0.0, overlap_pt) / shorter_span_pt
        if (
            overlap_m < minimum_overlap_m
            or overlap_ratio < minimum_overlap_ratio
        ):
            rejected_overlap += 1
            continue
        second_normal = ((cx + ex) / 2.0) * nx + ((cy + ey) / 2.0) * ny
        offset_m = abs(second_normal - first_normal) * transform.meters_per_point
        gap_samples_m.append(offset_m)
        gap_tolerance_m = 1e-9
        if (
            offset_m < options.min_wall_thickness_m - gap_tolerance_m
            or offset_m > options.max_wall_thickness_m + gap_tolerance_m
        ):
            rejected_gap += 1
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

    by_run: dict[int, list[int]] = {}
    for candidate_index, candidate in enumerate(candidates):
        by_run.setdefault(candidate[0], []).append(candidate_index)
        by_run.setdefault(candidate[1], []).append(candidate_index)

    unique_best: dict[int, int] = {}
    ambiguous_best_run_count = 0
    for run_index, indexes in by_run.items():
        ordered = sorted(indexes, key=lambda index: candidates[index][6])
        if len(ordered) > 1 and candidates[ordered[0]][6] == candidates[ordered[1]][6]:
            ambiguous_best_run_count += 1
            continue
        unique_best[run_index] = ordered[0]

    junction_tolerance_pt = (12.0 * _INCH_M) / transform.meters_per_point
    junction_cell_size = max(junction_tolerance_pt, 1.0)
    endpoint_cells: dict[
        tuple[int, int],
        set[tuple[int, tuple[float, float]]],
    ] = {}
    for run_index, run in enumerate(eligible_runs):
        for endpoint in (run.start_pt, run.end_pt):
            endpoint_cells.setdefault(
                (
                    math.floor(endpoint[0] / junction_cell_size),
                    math.floor(endpoint[1] / junction_cell_size),
                ),
                set(),
            ).add((run_index, endpoint))

    accepted: list[_WallFacePair] = []
    seen_anchors: set[str] = set()
    accepted_candidate_indexes: set[int] = set()
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
        accepted_candidate_indexes.add(candidate_index)
        first = eligible_runs[first_index]
        second = eligible_runs[second_index]
        accepted.append(
            _WallFacePair(
                start_pt=start_source,
                end_pt=end_source,
                thickness_m=thickness_m,
                source_element_ids=tuple(
                    sorted(set(first.source_element_ids) | set(second.source_element_ids))
                ),
                primitive_families=tuple(
                    sorted(set(first.primitive_families) | set(second.primitive_families))
                ),
                dashed=first.dashed or second.dashed,
                junction_supported=_wall_pair_has_endpoint_junction(
                    start_source,
                    end_source,
                    first_index,
                    second_index,
                    eligible_runs,
                    records,
                    endpoint_cells,
                    junction_tolerance_pt,
                ),
                geometry_anchor=geometry_anchor,
            )
        )

    diagnostics.update(
        {
            "candidate_pair_count": len(candidate_pairs),
            "parallel_gap_histogram_m": _gap_histogram(gap_samples_m),
            "accepted_pair_count": len(accepted),
            "accepted_pair_junction_supported_count": sum(
                1 for pair in accepted if pair.junction_supported
            ),
            "ambiguous_best_run_count": ambiguous_best_run_count,
            "rejected": {
                "parallel": rejected_parallel,
                "overlap_ratio": rejected_overlap,
                "gap_range": rejected_gap,
                "ambiguous_or_non_mutual": len(candidates) - len(accepted_candidate_indexes),
            },
        }
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
    allow_partial_faces: bool = True,
    sheet_anchor: str | None = None,
) -> tuple[
    tuple[_WallContext, ...],
    tuple[Space, ...],
    dict[str, object],
]:
    if level.height_m is None or level_info.height is None:
        return (), (), {}
    sheet_anchor = sheet_anchor or _sheet_anchor(page)
    if sheet_anchor is None:
        return (), (), {}

    diagnostics: dict[str, object] = {}
    explicit_wall_lines = tuple(
        line
        for line in page.lines
        if any(_is_wall_source_layer(layer) for layer in line.source_layers)
    )
    if page.hidden_wall_source_present and len(explicit_wall_lines) < 4:
        diagnostics.update({
            "wall_source_layer_filter": "hidden_wall_layer_unresolved",
            "explicit_wall_source_segment_count": len(explicit_wall_lines),
            "closed_loop_pair_count": 0,
            "partial_pair_count": 0,
        })
        ambiguities.append({
            "page": page.page_number,
            "code": "hidden_wall_layer_unresolved",
            "detail": "hidden wall-layer geometry was excluded; visible wall evidence is insufficient",
        })
        return (), (), diagnostics
    pair_page = replace(page, lines=explicit_wall_lines) if len(explicit_wall_lines) >= 4 else page
    pairs = _geometric_wall_face_pairs(
        pair_page,
        transform,
        options,
        excluded_element_ids=excluded_element_ids,
        diagnostics=diagnostics,
    )
    if pair_page is not page and not pairs and not page.hidden_wall_source_present:
        diagnostics = {}
        pair_page = page
        pairs = _geometric_wall_face_pairs(
            page,
            transform,
            options,
            excluded_element_ids=excluded_element_ids,
            diagnostics=diagnostics,
        )
    diagnostics["wall_source_layer_filter"] = (
        "explicit_wall_layers" if pair_page is not page else "all_source_vectors"
    )
    diagnostics["explicit_wall_source_segment_count"] = len(explicit_wall_lines)
    if not pairs:
        diagnostics["closed_loop_pair_count"] = 0
        diagnostics["partial_pair_count"] = 0
        return (), (), diagnostics

    raw_loops = _wall_pair_closed_loops(pairs, transform, options)
    rejected_frame_pairs: set[int] = set()
    loops = []
    for loop_indexes, polygon, endpoint_vertices in raw_loops:
        source_points = [
            point
            for index in loop_indexes
            for point in (pairs[index].start_pt, pairs[index].end_pt)
        ]
        source_bbox = (
            min(point[0] for point in source_points),
            min(point[1] for point in source_points),
            max(point[0] for point in source_points),
            max(point[1] for point in source_points),
        )
        if _is_sheet_frame_enclosure(page, source_bbox):
            rejected_frame_pairs.update(loop_indexes)
        else:
            loops.append((loop_indexes, polygon, endpoint_vertices))
    if rejected_frame_pairs:
        ambiguities.append({
            "page": page.page_number,
            "code": "sheet_frame_enclosure_rejected",
            "detail": "near-page paired wall-face frame is not building geometry",
            "rejected_wall_pair_count": len(rejected_frame_pairs),
        })
    loop_endpoint_vertices: dict[
        int,
        tuple[tuple[float, float], tuple[float, float]],
    ] = {}
    loop_pair_indexes: set[int] = set()
    for loop_indexes, _, endpoint_vertices in loops:
        for index in loop_indexes:
            loop_pair_indexes.add(index)
            loop_endpoint_vertices[index] = (
                endpoint_vertices[(index, 0)],
                endpoint_vertices[(index, 1)],
            )

    diagnostics["closed_loop_pair_count"] = len(loop_pair_indexes)
    partial_pair_indexes = {
        index for index in range(len(pairs))
        if index not in loop_pair_indexes and index not in rejected_frame_pairs
    }
    partial_min_length_m = 24.0 * _INCH_M
    short_partial_pair_indexes = {
        index
        for index in partial_pair_indexes
        if math.dist(pairs[index].start_pt, pairs[index].end_pt)
        * transform.meters_per_point
        < partial_min_length_m
    }
    no_junction_partial_pair_indexes = {
        index
        for index in partial_pair_indexes
        if index not in short_partial_pair_indexes
        and not pairs[index].junction_supported
    }
    evidence_supported_partial_pair_indexes = (
        partial_pair_indexes
        - short_partial_pair_indexes
        - no_junction_partial_pair_indexes
    )
    allow_partial_pairs = (
        allow_partial_faces
        and int(diagnostics.get("ambiguous_best_run_count", 0)) == 0
    )
    promoted_partial_pair_indexes = (
        evidence_supported_partial_pair_indexes
        if allow_partial_pairs
        else set()
    )
    diagnostics["partial_wall_min_length_m"] = round(partial_min_length_m, 6)
    diagnostics["partial_candidate_pair_count"] = len(partial_pair_indexes)
    diagnostics["partial_short_rejected_count"] = len(short_partial_pair_indexes)
    diagnostics["partial_no_junction_rejected_count"] = len(
        no_junction_partial_pair_indexes
    )
    diagnostics["partial_pair_count"] = len(promoted_partial_pair_indexes)
    diagnostics["suppressed_partial_pair_count"] = (
        len(partial_pair_indexes) - len(promoted_partial_pair_indexes)
    )

    contexts_by_anchor: dict[str, _WallContext] = {}
    spaces: list[Space] = []
    height_confidence = level_info.height_confidence or options.assumed_value_confidence
    source_layers_by_element = {
        line.element_id: line.source_layers for line in pair_page.lines
    }

    for index, pair in enumerate(pairs):
        if (
            index not in loop_pair_indexes
            and index not in promoted_partial_pair_indexes
        ):
            continue
        wall_identity = (
            f"{source_id}|sheet:{sheet_anchor}|level:{level_info.anchor}|"
            f"wall-geometry:{pair.geometry_anchor}"
        )
        wall_id = stable_id("wall", wall_identity)
        source_layer_names = sorted({
            layer
            for element_id in pair.source_element_ids
            for layer in source_layers_by_element.get(element_id, ())
        })
        if index in loop_endpoint_vertices:
            first_xy, second_xy = loop_endpoint_vertices[index]
            wall_confidence = min(transform.confidence, height_confidence, 0.78)
            recognition = "geometric_parallel_wall_faces"
            method = (
                "wall centerline inferred from a unique geometric pairing of "
                "parallel PDF wall faces after joining collinear source segments; "
                "the pair participates in an unambiguous closed loop"
            )
        else:
            first_xy = transform.apply(pair.start_pt)
            second_xy = transform.apply(pair.end_pt)
            wall_confidence = min(transform.confidence, height_confidence, 0.42)
            recognition = "geometric_parallel_wall_face_partial"
            method = (
                "partial wall centerline inferred from a unique geometric pairing "
                "of parallel PDF wall faces after joining collinear source segments; "
                "no closed enclosure was proven"
            )

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
                    method=method,
                    confidence=wall_confidence,
                    source_element_id="+".join(pair.source_element_ids),
                    attributes={
                        "source_boundaries": list(pair.source_element_ids),
                        "geometry_anchor": pair.geometry_anchor,
                        "primitive_families": list(pair.primitive_families),
                        "dashed_source": pair.dashed,
                        "junction_supported": pair.junction_supported,
                        "closed_loop": index in loop_pair_indexes,
                        "source_layers": source_layer_names,
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
                    "recognition": recognition,
                    "geometry_anchor": pair.geometry_anchor,
                    "source_boundaries": list(pair.source_element_ids),
                    "primitive_families": list(pair.primitive_families),
                    "dashed_source": pair.dashed,
                    "junction_supported": pair.junction_supported,
                    "closed_loop": index in loop_pair_indexes,
                    "source_layers": source_layer_names,
                }
            },
        )
        contexts_by_anchor[pair.geometry_anchor] = _WallContext(
            wall,
            page.page_number,
            None,
            None,
        )

    for loop_indexes, polygon_xy, _ in loops:
        wall_ids: list[str] = []
        source_ids: set[str] = set()
        loop_wall_anchors = tuple(pairs[index].geometry_anchor for index in loop_indexes)
        loop_identity = (
            f"{source_id}|sheet:{sheet_anchor}|level:{level_info.anchor}|"
            f"wall-loop:{';'.join(loop_wall_anchors)}"
        )
        for index in loop_indexes:
            pair = pairs[index]
            wall_ids.append(contexts_by_anchor[pair.geometry_anchor].wall.id)
            source_ids.update(pair.source_element_ids)

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
        diagnostics,
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


_REGION_SEPARATION_M = 3.0
_REGION_MIN_SPAN_M = 4.0
_REGION_MIN_WALL_LENGTH_M = 12.0
_REGION_MIN_WALL_LAYER_SEGMENTS = 8
_REGION_MIN_WALL_FACE_SEGMENTS = 3


@dataclass(slots=True)
class _DrawingRegionState:
    """One separately drawn plan on a sheet and what has been resolved for it."""

    region_id: str
    page_number: int
    index: int
    scope: str
    page: PdfPageObservation
    bbox_pt: tuple[float, float, float, float] | None
    scope_bbox_pt: tuple[float, float, float, float] | None
    evidence_kind: str
    evidence_count: int
    signature: frozenset[tuple[int, int, int, int]] = frozenset()
    sheet_texts: tuple[PdfTextObservation, ...] = ()
    reason_codes: set[str] = field(default_factory=set)
    repeated_with: set[str] = field(default_factory=set)
    scale_hint: ScaleOverride | None = None
    level_hint: LevelOverride | None = None
    registration_hint: RegistrationHint | None = None
    level_anchor: str | None = None
    level_name_source_ids: tuple[str, ...] = ()
    scale: _Scale | None = None
    transform: _Transform2D | None = None
    frame_basis: str | None = None
    status: str = "unresolved"
    entity_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _SheetRegions:
    multiple: bool
    regions: list[_DrawingRegionState]
    detection: dict[str, object]


def _region_detection_scale(page: PdfPageObservation, options: ImportOptions) -> float | None:
    """Sheet scale used only to size region separation and wall-thickness gates."""

    overrides = [
        item for item in options.scale_overrides
        if item.page_number == page.page_number and item.region_point_pt is None
    ]
    if len(overrides) == 1:
        return overrides[0].meters_per_point
    values = [value for value, _, _ in _scale_candidates(page)]
    if values and all(abs(value - values[0]) / values[0] <= 1e-6 for value in values):
        return values[0]
    return None


def _is_sheet_border_segment(
    page: PdfPageObservation,
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    """A long segment running along a media edge is sheet framing, not a building."""

    (ax, ay), (bx, by) = start, end
    if abs(ay - by) <= 1.0 and abs(ax - bx) >= 0.5 * page.width_pt:
        y = (ay + by) / 2.0
        return y <= 0.10 * page.height_pt or y >= 0.90 * page.height_pt
    if abs(ax - bx) <= 1.0 and abs(ay - by) >= 0.5 * page.height_pt:
        x = (ax + bx) / 2.0
        return x <= 0.10 * page.width_pt or x >= 0.90 * page.width_pt
    return False


def _nested_rect_wall_evidence(
    page: PdfPageObservation,
    meters_per_point: float,
    options: ImportOptions,
) -> list[RegionEvidence]:
    """Outer edges of rectangle pairs whose four insets are wall-thickness gaps."""

    minimum = options.min_wall_thickness_m / meters_per_point - 1e-9
    maximum = options.max_wall_thickness_m / meters_per_point + 1e-9
    rects = sorted(
        (rect for rect in page.rects if not _is_sheet_frame_enclosure(page, rect.bbox_pt)),
        key=lambda rect: (rect.bbox_pt, rect.element_id),
    )
    starts = [rect.bbox_pt[0] for rect in rects]
    result: list[RegionEvidence] = []
    for outer in rects:
        x0, y0, x1, y1 = outer.bbox_pt
        low = bisect.bisect_left(starts, x0 + minimum)
        high = bisect.bisect_right(starts, x0 + maximum)
        for inner in rects[low:high]:
            ix0, iy0, ix1, iy1 = inner.bbox_pt
            insets = (ix0 - x0, iy0 - y0, x1 - ix1, y1 - iy1)
            if not all(minimum <= inset <= maximum for inset in insets):
                continue
            ids = tuple(sorted((outer.element_id, inner.element_id)))
            corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            result.extend(
                RegionEvidence(corners[index], corners[(index + 1) % 4], ids)
                for index in range(4)
            )
    return result


def _drawing_region_evidence(
    page: PdfPageObservation,
    meters_per_point: float | None,
    options: ImportOptions,
    excluded_line_ids: set[str],
    *,
    use_wall_layers: bool = True,
) -> tuple[str, tuple[RegionEvidence, ...]]:
    """Wall evidence that says where building drawings sit on a sheet."""

    wall_lines = tuple(
        line for line in page.lines
        if any(_is_wall_source_layer(layer) for layer in line.source_layers)
        and line.element_id not in excluded_line_ids
        and not _is_sheet_border_segment(page, line.start_pt, line.end_pt)
    )
    if use_wall_layers and len(wall_lines) >= _REGION_MIN_WALL_LAYER_SEGMENTS:
        return "visible_wall_layer", tuple(
            RegionEvidence(line.start_pt, line.end_pt, (line.element_id,))
            for line in wall_lines
        )
    if meters_per_point is None:
        return "unavailable_without_sheet_scale", ()
    evidence = _nested_rect_wall_evidence(page, meters_per_point, options)
    if page.lines:
        provisional = _Transform2D(
            meters_per_point=meters_per_point,
            rotation_radians=0.0,
            tx_m=0.0,
            ty_m=0.0,
            method="scale-only drawing-region detection",
            confidence=1.0,
        )
        pairs = _geometric_wall_face_pairs(
            page,
            provisional,
            options,
            excluded_element_ids=set(excluded_line_ids),
        )
        # Chords of drawn circles (grid bubbles, keynote tags) can pair up like
        # wall faces; a pair built only from curve segments does not locate a wall.
        evidence.extend(
            RegionEvidence(pair.start_pt, pair.end_pt, pair.source_element_ids)
            for pair in pairs
            if not _is_sheet_border_segment(page, pair.start_pt, pair.end_pt)
            and set(pair.primitive_families) != {"curve"}
        )
    return "paired_wall_faces", tuple(evidence)


def _assign_region_hints(
    page_number: int,
    regions: list[_DrawingRegionState],
    hints: Iterable[ScaleOverride | LevelOverride | RegistrationHint],
    *,
    kind: str,
    code_prefix: str,
    page_scope_applies_to_every_region: bool,
    ambiguities: list[dict[str, object]],
) -> dict[int, ScaleOverride | LevelOverride | RegistrationHint]:
    """Attach explicit caller hints to drawing regions, never by guessing.

    On a single-drawing sheet every hint for the page targets that drawing. On a
    multi-drawing sheet a hint selects its drawing with ``region_point_pt``; a
    page-scoped scale override applies to every drawing, while a page-scoped
    level or registration hint cannot say which drawing it means and is not used.
    """

    page_hints = [item for item in hints if item.page_number == page_number]
    if len(regions) == 1 and regions[0].scope == "sheet":
        if len(page_hints) > 1:
            raise ValueError(f"multiple {kind}s supplied for page {page_number}")
        return {regions[0].index: page_hints[0]} if page_hints else {}

    page_scoped = [item for item in page_hints if item.region_point_pt is None]
    if len(page_scoped) > 1:
        raise ValueError(f"multiple {kind}s supplied for page {page_number}")
    result: dict[int, ScaleOverride | LevelOverride | RegistrationHint] = {}
    for hint in page_hints:
        if hint.region_point_pt is None:
            continue
        containing = [
            region for region in regions
            if region.scope_bbox_pt is not None and _inside(region.scope_bbox_pt, hint.region_point_pt)
        ]
        if len(containing) != 1:
            ambiguities.append(
                {
                    "page": page_number,
                    "code": f"{code_prefix}_region_unresolved",
                    "detail": (
                        f"{kind} region_point_pt lies inside {len(containing)} drawing "
                        "regions; it was not applied"
                    ),
                    "region_point_pt": list(hint.region_point_pt),
                }
            )
            continue
        index = containing[0].index
        if index in result:
            raise ValueError(
                f"multiple {kind}s supplied for page {page_number} drawing region {index}"
            )
        result[index] = hint
    if page_scoped:
        if page_scope_applies_to_every_region:
            for region in regions:
                result.setdefault(region.index, page_scoped[0])
        else:
            ambiguities.append(
                {
                    "page": page_number,
                    "code": f"{code_prefix}_region_unresolved",
                    "detail": (
                        f"page-scoped {kind} on a sheet with {len(regions)} drawing regions "
                        "does not say which drawing it describes; set region_point_pt"
                    ),
                }
            )
    return result


def _split_sheet(
    page: PdfPageObservation,
    meters_per_point: float | None,
    options: ImportOptions,
    *,
    use_wall_layers: bool = True,
) -> tuple[str, tuple[RegionEvidence, ...], DrawingRegionSplit, float]:
    """Wall evidence and its split into separately drawn plans for one sheet."""

    title_boxes, title_line_ids = _title_block_exclusion(page)
    evidence_kind, evidence = _drawing_region_evidence(
        page, meters_per_point, options, title_line_ids, use_wall_layers=use_wall_layers,
    )
    if meters_per_point is not None:
        separation_pt = max(72.0, _REGION_SEPARATION_M / meters_per_point)
        margin_pt = min(separation_pt / 3.0, max(36.0, 1.5 / meters_per_point))
        min_span_pt = _REGION_MIN_SPAN_M / meters_per_point
        min_length_pt = _REGION_MIN_WALL_LENGTH_M / meters_per_point
    else:
        separation_pt, margin_pt, min_span_pt, min_length_pt = 144.0, 36.0, 144.0, 432.0
    split = split_drawing_regions(
        page,
        evidence,
        gap_pt=separation_pt,
        margin_pt=margin_pt,
        text_margin_pt=max(separation_pt, 144.0),
        min_span_pt=min_span_pt,
        min_total_length_pt=min_length_pt,
        min_evidence_count=(
            _REGION_MIN_WALL_LAYER_SEGMENTS
            if evidence_kind == "visible_wall_layer"
            else _REGION_MIN_WALL_FACE_SEGMENTS
        ),
        excluded_text_ids=frozenset(
            text.element_id for text in page.texts
            if any(_inside(box, text.center_pt) for box in title_boxes)
        ),
    )
    return evidence_kind, evidence, split, separation_pt


@dataclass(frozen=True, slots=True)
class SheetWallEvidence:
    """Read-only wall evidence of one sheet, grouped by separately drawn plan.

    Registration consumers (#104) match electrical sheets against this source
    evidence. Coordinates are displayed sheet points, bottom-left origin.
    """

    page_number: int
    meters_per_point: float | None
    evidence_kind: str
    drawings: tuple[tuple[tuple[float, float, float, float], tuple[RegionEvidence, ...]], ...]


def printed_sheet_scale(page: PdfPageObservation) -> tuple[float, str, str] | None:
    """The sheet's printed scale as (metres per point, text, element id), if unambiguous."""

    candidates = _scale_candidates(page)
    if not candidates:
        return None
    first = candidates[0][0]
    if any(abs(value - first) / first > 1e-6 for value, _, _ in candidates[1:]):
        return None
    return candidates[0]


def sheet_wall_evidence(
    page: PdfPageObservation,
    *,
    meters_per_point: float | None = None,
    options: ImportOptions | None = None,
    use_wall_layers: bool = True,
) -> SheetWallEvidence:
    """Group a sheet's wall evidence by drawing, using the #103 region rules.

    ``use_wall_layers=False`` ignores CAD layer names and uses paired wall faces,
    so a layered sheet can be compared with a flattened one on equal terms.
    """

    options = options or ImportOptions()
    if meters_per_point is None:
        meters_per_point = _region_detection_scale(page, options)
    evidence_kind, evidence, split, _ = _split_sheet(
        page, meters_per_point, options, use_wall_layers=use_wall_layers,
    )
    drawings: tuple[tuple[tuple[float, float, float, float], tuple[RegionEvidence, ...]], ...]
    if split.regions:
        drawings = tuple((region.bbox_pt, region.evidence) for region in split.regions)
    elif split.single_region_bbox_pt is not None:
        drawings = ((split.single_region_bbox_pt, split.single_region_evidence),)
    elif evidence:
        drawings = ((
            (
                min(item.bbox_pt[0] for item in evidence),
                min(item.bbox_pt[1] for item in evidence),
                max(item.bbox_pt[2] for item in evidence),
                max(item.bbox_pt[3] for item in evidence),
            ),
            evidence,
        ),)
    else:
        drawings = ()
    return SheetWallEvidence(
        page_number=page.page_number,
        meters_per_point=meters_per_point,
        evidence_kind=evidence_kind,
        drawings=drawings,
    )


def region_wall_evidence(
    page: PdfPageObservation,
    bbox_pt: tuple[float, float, float, float],
    meters_per_point: float,
    *,
    options: ImportOptions | None = None,
    use_wall_layers: bool = True,
) -> tuple[str, tuple[RegionEvidence, ...]]:
    """Wall evidence lying inside one resolved drawing region's source extents."""

    options = options or ImportOptions()
    _, title_line_ids = _title_block_exclusion(page)
    kind, evidence = _drawing_region_evidence(
        page, meters_per_point, options, title_line_ids, use_wall_layers=use_wall_layers,
    )
    x0, y0, x1, y1 = bbox_pt[0] - 1.0, bbox_pt[1] - 1.0, bbox_pt[2] + 1.0, bbox_pt[3] + 1.0
    return kind, tuple(
        item for item in evidence
        if all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in (item.start_pt, item.end_pt))
    )


def _sheet_drawing_regions(
    source_id: str,
    page: PdfPageObservation,
    options: ImportOptions,
    ambiguities: list[dict[str, object]],
) -> _SheetRegions:
    """Find the separately drawn plans on one architectural sheet."""

    meters_per_point = _region_detection_scale(page, options)
    evidence_kind, evidence, split, separation_pt = _split_sheet(page, meters_per_point, options)
    detection: dict[str, object] = {
        "evidence_kind": evidence_kind,
        "evidence_segment_count": len(evidence),
        "evidence_cluster_count": split.cluster_count,
        "drawing_cluster_count": split.qualifying_cluster_count,
        "separation_pt": round(separation_pt, 6),
        "detection_meters_per_point": meters_per_point,
    }

    if not split.regions:
        regions = [
            _DrawingRegionState(
                region_id=stable_id("drawing-region", f"{source_id}|page:{page.page_number}|sheet"),
                page_number=page.page_number,
                index=1,
                scope="sheet",
                page=page,
                bbox_pt=split.single_region_bbox_pt,
                scope_bbox_pt=None,
                evidence_kind=evidence_kind,
                evidence_count=len(evidence),
            )
        ]
    else:
        regions = [
            _DrawingRegionState(
                region_id=stable_id(
                    "drawing-region",
                    f"{source_id}|page:{page.page_number}|bbox:"
                    + ",".join(f"{value:.0f}" for value in item.bbox_pt),
                ),
                page_number=page.page_number,
                index=item.index,
                scope="region",
                page=item.page,
                bbox_pt=item.bbox_pt,
                scope_bbox_pt=item.scope_bbox_pt,
                evidence_kind=evidence_kind,
                evidence_count=item.evidence_count,
                signature=item.signature,
                sheet_texts=split.sheet_texts,
            )
            for item in split.regions
        ]
        if split.overlapping:
            for region in regions:
                region.reason_codes.add("drawing_regions_overlap")
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "drawing_regions_overlap",
                    "detail": (
                        "separated wall drawings have overlapping extents, so source "
                        "annotations cannot be scoped to one drawing"
                    ),
                    "drawing_region_ids": [region.region_id for region in regions],
                }
            )
        for position, first in enumerate(regions):
            for second in regions[position + 1:]:
                if signatures_repeat(first.signature, second.signature):
                    first.repeated_with.add(second.region_id)
                    second.repeated_with.add(first.region_id)

    for hints, kind, prefix, applies, attribute in (
        (options.scale_overrides, "scale override", "scale_override", True, "scale_hint"),
        (options.level_overrides, "level override", "level_override", False, "level_hint"),
        (options.registrations, "registration hint", "registration_hint", False, "registration_hint"),
    ):
        assigned = _assign_region_hints(
            page.page_number,
            regions,
            hints,
            kind=kind,
            code_prefix=prefix,
            page_scope_applies_to_every_region=applies,
            ambiguities=ambiguities,
        )
        for region in regions:
            setattr(region, attribute, assigned.get(region.index))
    return _SheetRegions(multiple=bool(split.regions), regions=regions, detection=detection)


def _reason_codes_since(
    ambiguities: list[dict[str, object]],
    start: int,
) -> set[str]:
    return {str(item["code"]) for item in ambiguities[start:] if "code" in item}


def _tag_region_ambiguities(
    ambiguities: list[dict[str, object]],
    start: int,
    region: _DrawingRegionState,
) -> None:
    if region.scope == "region":
        for item in ambiguities[start:]:
            item.setdefault("drawing_region_id", region.region_id)


def _block_regions_sharing_a_level(
    sheet: _SheetRegions,
    ambiguities: list[dict[str, object]],
) -> None:
    """Two drawings on one sheet may not be promoted onto the same level."""

    if not sheet.multiple:
        return
    by_anchor: dict[str, list[_DrawingRegionState]] = {}
    for region in sheet.regions:
        if region.reason_codes:
            continue
        name, _, _ = _level_name_evidence(region.page, region.level_hint)
        if name is not None:
            by_anchor.setdefault(_anchor(name), []).append(region)
    for anchor, members in sorted(by_anchor.items()):
        if len(members) < 2:
            continue
        for region in members:
            region.reason_codes.add("drawing_regions_share_level")
            if region.repeated_with:
                region.reason_codes.add("drawing_region_geometry_repeated")
            ambiguities.append(
                {
                    "page": region.page_number,
                    "code": "drawing_regions_share_level",
                    "detail": (
                        f"drawing region {region.index} names level {anchor!r}, as does "
                        "another drawing on the same sheet; neither is promoted into a "
                        "shared level and frame"
                    ),
                    "drawing_region_id": region.region_id,
                    "level_anchor": anchor,
                    "competing_region_ids": sorted(
                        other.region_id for other in members if other is not region
                    ),
                    "repeated_geometry_region_ids": sorted(region.repeated_with),
                }
            )


def _region_record(
    region: _DrawingRegionState,
    *,
    frame_id: str,
    levels_by_anchor: dict[str, Level],
    level_info_by_anchor: dict[str, _LevelInfo],
) -> dict[str, object]:
    level_record: dict[str, object] | None = None
    level_confidence: float | None = None
    if region.level_anchor is not None and region.level_anchor in levels_by_anchor:
        level = levels_by_anchor[region.level_anchor]
        info = level_info_by_anchor[region.level_anchor]
        level_confidence = level.confidence
        level_record = {
            "level_id": level.id,
            "name": level.name,
            "elevation_m": level.elevation_m,
            "elevation_method": info.elevation.method,
            "elevation_source_element_id": info.elevation.source_element_id,
            "name_source_element_ids": list(region.level_name_source_ids),
            "name_method": (
                region.level_hint.note
                if region.level_hint is not None and region.level_hint.name
                else "parsed drawing level name" if region.level_name_source_ids
                else "no level name printed; unlabeled level"
            ),
            "confidence": level.confidence,
        }
    scale_record: dict[str, object] | None = None
    if region.scale is not None:
        scale_record = {
            "meters_per_point": region.scale.meters_per_point,
            "method": region.scale.method,
            "source_text": region.scale.source_text,
            "source_element_id": region.scale.source_element_id,
            "confidence": region.scale.confidence,
        }
    frame_record: dict[str, object] | None = None
    if region.status == "resolved" and region.transform is not None:
        frame_record = {
            "frame_id": frame_id,
            "basis": region.frame_basis,
            "method": region.transform.method,
            "confidence": region.transform.confidence,
            "meters_per_point": region.transform.meters_per_point,
            "rotation_radians": region.transform.rotation_radians,
            "translation_m": [region.transform.tx_m, region.transform.ty_m],
        }
    confidences = [
        value for value in (
            level_confidence,
            region.scale.confidence if region.scale else None,
            region.transform.confidence if frame_record else None,
        )
        if value is not None
    ]
    return {
        "region_id": region.region_id,
        "page": region.page_number,
        "index": region.index,
        "scope": region.scope,
        "source_bbox_pt": list(region.bbox_pt) if region.bbox_pt is not None else None,
        "evidence": {
            "kind": region.evidence_kind,
            "segment_count": region.evidence_count,
        },
        "status": region.status,
        "reason_codes": sorted(region.reason_codes),
        "level": level_record,
        "scale": scale_record,
        "frame": frame_record,
        "confidence": min(confidences) if region.status == "resolved" and confidences else None,
        "entity_counts": dict(sorted(region.entity_counts.items())),
        "repeated_geometry_region_ids": sorted(region.repeated_with),
    }


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
    ordered_pages = tuple(sorted(document.pages, key=lambda item: item.page_number))
    classifications = {page.page_number: classify_page(page) for page in ordered_pages}

    # Split every architectural sheet into its separately drawn plans first.
    # A sheet with one drawing stays whole and keeps page-level behavior.
    sheets: dict[int, _SheetRegions] = {}
    for page in ordered_pages:
        if classifications[page.page_number].kind != "architectural_plan":
            continue
        sheets[page.page_number] = _sheet_drawing_regions(
            document.source_id, page, options, ambiguities,
        )

    # Resolve all level evidence before materializing geometry. This lets later
    # explicit/override evidence correctly upgrade an earlier local datum or
    # assumed height without leaving already-emitted geometry at stale Z/height.
    for page in ordered_pages:
        sheet = sheets.get(page.page_number)
        if sheet is None:
            continue
        _block_regions_sharing_a_level(sheet, ambiguities)
        for region in sheet.regions:
            if region.reason_codes:
                continue
            start = len(ambiguities)
            level_info = _resolve_level(
                region.page,
                options,
                level_info_by_anchor,
                ambiguities,
                override=region.level_hint,
                require_drawing_level_name=sheet.multiple,
            )
            _tag_region_ambiguities(ambiguities, start, region)
            if level_info is None:
                region.reason_codes |= _reason_codes_since(ambiguities, start) or {"level_unresolved"}
                continue
            level_info_by_anchor[level_info.anchor] = level_info
            region.level_anchor = level_info.anchor
            _, name_sources, _ = _level_name_evidence(region.page, region.level_hint)
            region.level_name_source_ids = tuple(sorted(item.element_id for item in name_sources))

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
    base_geometry_region: str | None = None
    registration_fallback_provenance: list[Provenance] = []

    def import_region(
        page: PdfPageObservation,
        sheet: _SheetRegions,
        region: _DrawingRegionState,
        record: dict[str, object],
    ) -> None:
        """Materialize one drawing region into the shared canonical lists."""

        nonlocal base_geometry_region
        region_page = region.page
        if region.level_anchor is None:
            record["status"] = "skipped_unresolved_level"
            return
        level_info = level_info_by_anchor[region.level_anchor]
        level = levels_by_anchor[region.level_anchor]

        start = len(ambiguities)
        scale = _resolve_scale(
            region_page,
            region.scale_hint,
            ambiguities,
            sheet_texts=region.sheet_texts,
        )
        transform, scale, registration_fallback = _resolve_transform(
            region_page,
            scale,
            options,
            hint=region.registration_hint,
            allow_page_local_origin=base_geometry_region is None,
            allow_sheet_geometry_fallback=not sheet.multiple,
            ambiguities=ambiguities,
        )
        region.scale = scale
        if transform is None or scale is None:
            _tag_region_ambiguities(ambiguities, start, region)
            region.reason_codes |= _reason_codes_since(ambiguities, start) or {"registration_unresolved"}
            record["status"] = "skipped_unresolved_scale_or_registration"
            return
        region.transform = transform
        region.frame_basis = (
            "explicit_registration" if region.registration_hint is not None
            else "sheet_geometry_fallback" if registration_fallback is not None
            else "project_origin"
        )
        record.update(
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
            record["registration_confidence"] = transform.confidence
            record["registration_provenance"] = registration_fallback
            registration_fallback_provenance.append(
                Provenance(
                    source_kind="architectural_pdf",
                    derivation=DERIVATION_OBSERVED,
                    source_id=document.source_id,
                    page=page.page_number,
                    method=transform.method,
                    confidence=transform.confidence,
                    attributes=registration_fallback,
                )
            )

        counts_before = (len(spaces), len(slabs), len(ceilings))
        rooms = _room_labels(region_page)
        rectangle_shells = _shell_candidates(region_page, scale, rooms, options, ambiguities)
        ordinary_vector_shells = _ordinary_vector_shell_candidates(
            region_page,
            scale,
            rooms,
            options,
            ambiguities,
            excluded_room_anchors={shell.room.anchor for shell in rectangle_shells},
        )
        shells = (*rectangle_shells, *ordinary_vector_shells)
        room_heights, blocked_room_heights = _room_ceiling_height_evidence(
            region_page, shells, ambiguities,
        )
        slab_thickness = _slab_thickness_from_text(region_page)
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
                region_page,
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

        layered_room_count = 0
        if not region_page.hidden_wall_source_present:
            if has_multiple_wall_regions(region_page, scale.meters_per_point):
                ambiguities.append({
                    "page": page.page_number,
                    "code": "multiple_layered_drawing_regions_unresolved",
                    "detail": (
                        "multiple separated wall drawings share this sheet; "
                        "their distinct level and registration frames are not yet established"
                    ),
                })
            unique_rooms = tuple(
                room for room in rooms
                if sum(other.anchor == room.anchor for other in rooms) == 1
                and stable_id(
                    "space",
                    f"{document.source_id}|level:{level_info.anchor}|room:{room.anchor}",
                ) not in used_space_ids
            )
            regions = find_layered_room_regions(
                region_page,
                scale.meters_per_point,
                tuple((room.anchor, _room_label_center_pt(room)) for room in unique_rooms),
            )
            rooms_by_anchor = {room.anchor: room for room in unique_rooms}
            for layered_region in regions:
                room = rooms_by_anchor[layered_region.anchor]
                space = _layered_region_space(
                    layered_region, room, region_page, transform, scale, level, level_info,
                    document.source_id,
                )
                if space.id in used_space_ids:
                    continue
                spaces.append(space)
                used_space_ids.add(space.id)
                layered_room_count += 1
                ambiguities.append({
                    "page": page.page_number,
                    "code": "layered_room_3d_extent_unresolved",
                    "detail": (
                        f"room {room.anchor!r} has an inferred 2D interior from visible "
                        "wall and opening vectors; wall thickness and height remain unresolved"
                    ),
                    "room_anchor": room.anchor,
                })

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
        region_has_blocking_enclosure_ambiguity = any(
            item.get("code") in blocking_enclosure_codes
            for item in ambiguities[start:]
        )
        legacy_single_loop_partial_guard = (
            len(_ordinary_vector_rect_loops(region_page)) == 1
            and any(
                item.get("code") == "ordinary_vector_enclosure_unresolved"
                for item in ambiguities[start:]
            )
        )
        if region_has_blocking_enclosure_ambiguity:
            geometric_line_walls, geometric_spaces, geometric_diagnostics = (), (), {}
        else:
            (
                geometric_line_walls,
                geometric_spaces,
                geometric_diagnostics,
            ) = _geometric_wall_loop_entities(
                region_page,
                transform,
                level,
                level_info,
                document.source_id,
                options,
                rooms,
                ambiguities,
                excluded_element_ids=consumed_vector_line_ids,
                allow_partial_faces=not legacy_single_loop_partial_guard,
                sheet_anchor=_sheet_anchor(page),
            )
            resolved_geometric_label_anchors = {
                space.attributes["pdf_architecture"].get("label_anchor")
                for space in geometric_spaces
                if space.attributes["pdf_architecture"].get("label_anchor")
            }
            if resolved_geometric_label_anchors:
                ambiguities[start:] = [
                    item
                    for item in ambiguities[start:]
                    if not (
                        item.get("code") == "ordinary_vector_enclosure_unresolved"
                        and item.get("room_anchor") in resolved_geometric_label_anchors
                    )
                ]

        def wall_geometry_key(context: _WallContext) -> tuple[object, ...]:
            points = context.wall.centerline.points
            return (
                tuple(
                    sorted(
                        (
                            (round(points[0].x, 6), round(points[0].y, 6)),
                            (round(points[-1].x, 6), round(points[-1].y, 6)),
                        )
                    )
                ),
                round(context.wall.thickness_m, 6),
            )

        existing_wall_geometry = {wall_geometry_key(context) for context in page_walls}
        for context in geometric_line_walls:
            geometry_key = wall_geometry_key(context)
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
            region_page,
            transform,
            level,
            tuple(page_walls),
            document.source_id,
            options,
            ambiguities,
            used_opening_identity,
        )
        openings.extend(page_openings)
        region_room_count = len(spaces) - counts_before[0]
        region_slab_count = len(slabs) - counts_before[1]
        region_ceiling_count = len(ceilings) - counts_before[2]
        region.entity_counts = {
            "spaces": region_room_count,
            "walls": len(page_walls),
            "slabs": region_slab_count,
            "ceilings": region_ceiling_count,
            "openings": len(page_openings),
        }
        if sum(region.entity_counts.values()):
            record["status"] = "geometry_imported"
            region.status = "resolved"
            if base_geometry_region is None:
                base_geometry_region = region.region_id
        else:
            record["status"] = "no_supported_geometry_recognized"
            region.status = "no_supported_geometry"
            region.reason_codes.add("architectural_geometry_unrecognized")
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
        _tag_region_ambiguities(ambiguities, start, region)
        record["resolved_room_count"] = region_room_count
        record["resolved_wall_count"] = len(page_walls)
        if ordinary_vector_shells:
            record["ordinary_vector_enclosure_count"] = len(ordinary_vector_shells)
        if layered_room_count:
            record["layered_wall_room_count"] = layered_room_count
        if geometric_spaces:
            record["geometric_wall_loop_count"] = len(geometric_spaces)
        if geometric_diagnostics:
            record["geometric_wall_pair_diagnostics"] = geometric_diagnostics
            record["geometric_partial_wall_count"] = int(
                geometric_diagnostics.get("partial_pair_count", 0)
            )

    for page in ordered_pages:
        classification = classifications[page.page_number]
        page_record: dict[str, object] = {
            "page": page.page_number,
            "classification": classification.kind,
            "classification_confidence": classification.confidence,
            "architectural_score": classification.architectural_score,
            "electrical_score": classification.electrical_score,
        }
        sheet = sheets.get(page.page_number)
        if sheet is None:
            page_record["status"] = "ignored_non_architectural_plan"
            page_metadata.append(page_record)
            continue

        page_record["drawing_region_ids"] = [region.region_id for region in sheet.regions]
        page_record["drawing_region_detection"] = sheet.detection
        if not sheet.multiple:
            import_region(page, sheet, sheet.regions[0], page_record)
        else:
            region_statuses: list[str] = []
            for region in sheet.regions:
                if region.reason_codes:
                    region_statuses.append("skipped_unresolved_drawing_region")
                    continue
                region_record: dict[str, object] = {}
                import_region(page, sheet, region, region_record)
                region_statuses.append(str(region_record["status"]))
            page_record["status"] = (
                "geometry_imported" if "geometry_imported" in region_statuses
                else "no_supported_geometry_recognized"
                if "no_supported_geometry_recognized" in region_statuses
                else "skipped_unresolved_drawing_regions"
            )
            page_record["resolved_room_count"] = sum(
                region.entity_counts.get("spaces", 0) for region in sheet.regions
            )
            page_record["resolved_wall_count"] = sum(
                region.entity_counts.get("walls", 0) for region in sheet.regions
            )
        if sheet.multiple or page_record["status"] in {
            "geometry_imported",
            "no_supported_geometry_recognized",
        }:
            page_record["stable_native_line_count"] = sum(1 for item in page.lines if item.native_id)
            page_record["untagged_vector_line_count"] = sum(1 for item in page.lines if not item.native_id)
        page_metadata.append(page_record)

    drawing_regions = [
        _region_record(
            region,
            frame_id=CoordinateSystem().frame_id,
            levels_by_anchor=levels_by_anchor,
            level_info_by_anchor=level_info_by_anchor,
        )
        for page_number in sorted(sheets)
        for region in sheets[page_number].regions
    ]
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
            derivation=DERIVATION_OBSERVED,
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
            "drawing_regions": drawing_regions,
            "ambiguities": sorted(
                ambiguities,
                key=lambda item: (
                    int(item.get("page", 0)),
                    str(item.get("code", "")),
                    str(item.get("detail", "")),
                    str(item.get("drawing_region_id", "")),
                ),
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
