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
_COMMON_ROOM_NAMES = {
    "GARAGE": "garage",
    "BEDROOM": "bedroom",
    "PRIMARY BEDROOM": "bedroom",
    "LIVING ROOM": "living-room",
    "DINING ROOM": "dining-room",
    "KITCHEN": "kitchen",
    "OFFICE": "office",
    "HALL": "hall",
    "HALLWAY": "hall",
    "BATH": "bathroom",
    "BATHROOM": "bathroom",
    "RESTROOM": "restroom",
    "CLOSET": "closet",
    "STORAGE": "storage",
    "LOBBY": "lobby",
}


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


@dataclass(frozen=True, slots=True)
class _Shell:
    outer: PdfRectObservation
    inner: PdfRectObservation
    room: _RoomLabel
    thickness_x_m: float
    thickness_y_m: float


@dataclass(frozen=True, slots=True)
class _WallContext:
    wall: Wall
    page_number: int
    room_anchor: str | None
    source_side: str | None


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


def _resolve_transform(
    page: PdfPageObservation,
    scale: _Scale | None,
    options: ImportOptions,
    *,
    allow_page_local_origin: bool,
    ambiguities: list[dict[str, object]],
) -> tuple[_Transform2D | None, _Scale | None]:
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
                return None, scale
        else:
            scale = _Scale(
                transform.meters_per_point,
                hint.confidence,
                "scale resolved by two-point registration",
                None,
            )
        return transform, scale

    if scale is None:
        return None, None
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
        )
    ambiguities.append(
        {
            "page": page.page_number,
            "code": "registration_unresolved",
            "detail": "additional architectural plan page requires RegistrationHint before geometry can share the canonical frame",
        }
    )
    return None, scale


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


def _elevation_from_text(page: PdfPageObservation) -> tuple[float, str] | None:
    for item in page.texts:
        text = _clean_text(item.text)
        upper = text.upper()
        if not ("ELEVATION" in upper or re.search(r"\bEL\.?\s*[:=]", upper)):
            continue
        metric = re.search(r"(-?\d+(?:\.\d+)?)\s*M\b", upper)
        if metric:
            return (float(metric.group(1)), text)
        dim = _find_dimension(text)
        if dim:
            sign = -1.0 if re.search(r"(?:ELEVATION|\bEL\.?)\s*[:=]?\s*-", upper) else 1.0
            return (sign * dim[0], text)
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
        elif boxes or not rooms:
            # A ceiling-height note placed outside all resolved room enclosures
            # is page/level evidence. This preserves title/note-area annotations
            # without promoting notes that are spatially inside a room.
            result.append((observation, height_m, "global", None))
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
            "conflicting_value_m": candidate.value_m,
            "conflicting_page": candidate.page_number,
            "resolution": "current page skipped until explicitly resolved",
        }
    )
    return existing, True


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
            source_text=parsed_elevation[1],
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
                return None

    resolved_name = name if override and override.name else existing.name
    return _LevelInfo(
        anchor=anchor,
        name=resolved_name,
        elevation=elevation,
        height=height,
    )


def _room_labels(page: PdfPageObservation) -> tuple[_RoomLabel, ...]:
    labels: list[_RoomLabel] = []
    explicit = re.compile(r"^(?:ROOM|SPACE)\s*[:#-]?\s*(.+)$", re.IGNORECASE)
    for observation in page.texts:
        text = _clean_text(observation.text)
        upper = text.upper()
        if any(token in upper for token in ("FLOOR PLAN", "SCALE", "LEVEL:", "ELEVATION", "CEILING HEIGHT", "SLAB", "DOOR", "WINDOW")):
            continue
        match = explicit.match(text)
        if match:
            name = _clean_text(match.group(1))
            if name:
                labels.append(_RoomLabel(observation, name, _anchor(name), _COMMON_ROOM_NAMES.get(name.upper()), 0.98))
            continue
        if upper in _COMMON_ROOM_NAMES:
            labels.append(_RoomLabel(observation, text.title() if text.isupper() else text, _anchor(text), _COMMON_ROOM_NAMES[upper], 0.85))
    return tuple(labels)


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

    selected: list[_Shell] = []
    used_pairs: set[tuple[str, str]] = set()
    duplicate_anchors = {room.anchor for room in rooms if sum(item.anchor == room.anchor for item in rooms) > 1}
    for anchor in sorted(duplicate_anchors):
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "duplicate_room_label",
                "detail": f"room label {anchor!r} is not unique on the level; those enclosures are not promoted",
            }
        )

    for room in sorted(rooms, key=lambda item: (item.anchor, item.observation.element_id)):
        if room.anchor in duplicate_anchors:
            continue
        containing = [item for item in candidates if _inside(item[3].bbox_pt, room.observation.center_pt)]
        if not containing:
            continue
        area, _, outer, inner = min(containing, key=lambda item: (item[0], item[1], item[2].element_id, item[3].element_id))
        pair = (outer.element_id, inner.element_id)
        if pair in used_pairs:
            ambiguities.append(
                {
                    "page": page.page_number,
                    "code": "multiple_room_labels_in_enclosure",
                    "detail": "more than one room label resolves to the same wall enclosure",
                    "source_rectangles": list(pair),
                }
            )
            selected = [shell for shell in selected if (shell.outer.element_id, shell.inner.element_id) != pair]
            continue
        used_pairs.add(pair)
        ix0, iy0, ix1, iy1 = inner.bbox_pt
        ox0, oy0, ox1, oy1 = outer.bbox_pt
        selected.append(
            _Shell(
                outer=outer,
                inner=inner,
                room=room,
                thickness_x_m=((ix0 - ox0) + (ox1 - ix1)) * scale.meters_per_point / 2.0,
                thickness_y_m=((iy0 - oy0) + (oy1 - iy1)) * scale.meters_per_point / 2.0,
            )
        )

    labelled_rects = {(shell.outer.element_id, shell.inner.element_id) for shell in selected}
    if candidates and not labelled_rects and not rooms:
        ambiguities.append(
            {
                "page": page.page_number,
                "code": "unlabeled_enclosure",
                "detail": "wall-like nested rectangles were found but no stable room/space label anchors their identity",
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
    base_confidence = min(room.confidence, scale.confidence, transform.confidence)
    footprint = _polygon_from_bbox(shell.inner.bbox_pt, transform, level.elevation_m)
    resolved_height_m = (
        None
        if room_height_blocked
        else (room_height.value_m if room_height is not None else level.height_m)
    )
    height_confidence = (
        0.0
        if room_height_blocked
        else (
            room_height.confidence
            if room_height is not None
            else (level_info.height_confidence or 0.0)
        )
    )
    room_height_provenance: tuple[Provenance, ...] = ()
    if room_height is not None:
        room_height_provenance = _provenance(
            source_id,
            room_height.page_number,
            method=room_height.method,
            confidence=room_height.confidence,
            source_element_id=room_height.source_element_id,
            attributes={
                "field": "height_m",
                "scope": "room",
                "source_text": room_height.source_text,
            },
        )

    space = Space(
        id=stable_id("space", identity),
        name=room.name,
        level_id=level.id,
        footprint=footprint,
        height_m=resolved_height_m,
        usage=room.usage,
        confidence=base_confidence,
        provenance=(
            _provenance(
                source_id,
                page.page_number,
                method="room label contained by a paired wall rectangle enclosure",
                confidence=base_confidence,
                source_element_id=room.observation.element_id,
                attributes={
                    "outer_rect": shell.outer.element_id,
                    "inner_rect": shell.inner.element_id,
                },
            )
            + room_height_provenance
        ),
        attributes={"pdf_architecture": {"identity_anchor": room.anchor}},
    )

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
                        method="wall centerline inferred midway between paired vector boundaries; height from level/ceiling evidence",
                        confidence=wall_confidence,
                        source_element_id=f"{shell.outer.element_id}+{shell.inner.element_id}:{side}",
                        attributes={"room_anchor": room.anchor, "source_side": side},
                    )
                    + room_height_provenance
                ),
                attributes={"pdf_architecture": {"room_anchor": room.anchor, "source_side": side}},
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
                + room_height_provenance
            ),
        )
    return space, tuple(walls), slab, ceiling


def _line_pair_wall_contexts(
    page: PdfPageObservation,
    transform: _Transform2D,
    level: Level,
    level_info: _LevelInfo,
    source_id: str,
    options: ImportOptions,
) -> tuple[_WallContext, ...]:
    if level.height_m is None:
        return ()
    sheet_anchor = _sheet_anchor(page)
    if sheet_anchor is None:
        # MCIDs are page-scoped. Without a stable printed sheet identifier they
        # cannot safely anchor document-global canonical IDs.
        return ()
    tagged = [line for line in page.lines if line.native_id]
    candidates: list[tuple[float, float, str, str, PdfLineObservation, PdfLineObservation, tuple[tuple[float, float], tuple[float, float]]]] = []
    for index, first in enumerate(tagged):
        ax, ay = first.start_pt
        bx, by = first.end_pt
        dx = bx - ax
        dy = by - ay
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            continue
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        a0, a1 = sorted((ax * ux + ay * uy, bx * ux + by * uy))
        first_normal = ((ax + bx) / 2.0) * nx + ((ay + by) / 2.0) * ny
        for second in tagged[index + 1 :]:
            cx, cy = second.start_pt
            ex, ey = second.end_pt
            sdx, sdy = ex - cx, ey - cy
            slength = math.hypot(sdx, sdy)
            if slength <= 1e-9:
                continue
            cross = abs(ux * (sdy / slength) - uy * (sdx / slength))
            if cross > math.sin(math.radians(2.0)):
                continue
            b0, b1 = sorted((cx * ux + cy * uy, ex * ux + ey * uy))
            overlap0 = max(a0, b0)
            overlap1 = min(a1, b1)
            overlap_pt = overlap1 - overlap0
            if overlap_pt <= 0 or overlap_pt * transform.meters_per_point < options.min_space_span_m * 0.5:
                continue
            second_normal = ((cx + ex) / 2.0) * nx + ((cy + ey) / 2.0) * ny
            offset_m = abs(second_normal - first_normal) * transform.meters_per_point
            if not (options.min_wall_thickness_m <= offset_m <= options.max_wall_thickness_m):
                continue
            mean_normal = (first_normal + second_normal) / 2.0
            start_source = (overlap0 * ux + mean_normal * nx, overlap0 * uy + mean_normal * ny)
            end_source = (overlap1 * ux + mean_normal * nx, overlap1 * uy + mean_normal * ny)
            candidates.append(
                (
                    -overlap_pt,
                    offset_m,
                    first.native_id or "",
                    second.native_id or "",
                    first,
                    second,
                    (start_source, end_source),
                )
            )

    used: set[str] = set()
    result: list[_WallContext] = []
    for _, thickness, native_a, native_b, first, second, endpoints in sorted(candidates):
        if native_a in used or native_b in used:
            continue
        used.update((native_a, native_b))
        identity = (
            f"{source_id}|sheet:{sheet_anchor}|level:{level_info.anchor}|"
            f"pdf-native-wall:{min(native_a, native_b)}|{max(native_a, native_b)}"
        )
        confidence = min(transform.confidence, level_info.height_confidence or 0.0, 0.82)
        wall = Wall(
            id=stable_id("wall", identity),
            level_id=level.id,
            centerline=Polyline3D(
                points=(
                    _point3(endpoints[0], transform, level.elevation_m),
                    _point3(endpoints[1], transform, level.elevation_m),
                )
            ),
            thickness_m=thickness,
            height_m=level.height_m,
            confidence=confidence,
            provenance=_provenance(
                source_id,
                page.page_number,
                method="wall centerline inferred between parallel PDF vector elements carrying stable native MCIDs",
                confidence=confidence,
                source_element_id=f"{native_a}+{native_b}",
            ),
            attributes={"pdf_architecture": {"native_boundaries": [native_a, native_b]}},
        )
        result.append(_WallContext(wall, page.page_number, None, None))
    return tuple(result)


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


def _level_entity(source_id: str, info: _LevelInfo) -> Level:
    confidence = min(
        info.elevation_confidence,
        info.height_confidence if info.height_confidence is not None else 1.0,
    )

    if info.height is not None and info.height.page_number == info.elevation.page_number:
        method = (
            info.elevation.method
            if info.elevation.method == info.height.method
            else f"{info.elevation.method}; {info.height.method}"
        )
        provenance = _provenance(
            source_id,
            info.elevation.page_number,
            method=method,
            confidence=confidence,
        )
    else:
        provenance = _provenance(
            source_id,
            info.elevation.page_number,
            method=info.elevation.method,
            confidence=info.elevation.confidence,
        )
        if info.height is not None:
            provenance += _provenance(
                source_id,
                info.height.page_number,
                method=info.height.method,
                confidence=info.height.confidence,
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
        transform, scale = _resolve_transform(
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
        if base_geometry_page is None:
            base_geometry_page = page.page_number

        page_record.update(
            {
                "status": "geometry_imported",
                "scale_meters_per_point": scale.meters_per_point,
                "scale_method": scale.method,
                "scale_source_text": scale.source_text,
                "registration_method": transform.method,
                "registration_rotation_radians": transform.rotation_radians,
                "registration_translation_m": [transform.tx_m, transform.ty_m],
                "level": level.name,
            }
        )

        rooms = _room_labels(page)
        shells = _shell_candidates(page, scale, rooms, options, ambiguities)
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

        tagged_line_walls = _line_pair_wall_contexts(
            page,
            transform,
            level,
            level_info,
            document.source_id,
            options,
        )
        existing_ids = {context.wall.id for context in page_walls}
        page_walls.extend(context for context in tagged_line_walls if context.wall.id not in existing_ids)
        wall_contexts.extend(page_walls)
        openings.extend(
            _make_openings(
                page,
                transform,
                level,
                tuple(page_walls),
                document.source_id,
                options,
                ambiguities,
                used_opening_identity,
            )
        )
        page_record["resolved_room_count"] = sum(
            1 for space in spaces if space.provenance and space.provenance[0].page == page.page_number
        )
        page_record["resolved_wall_count"] = len(page_walls)
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
