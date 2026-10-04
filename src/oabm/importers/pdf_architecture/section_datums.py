"""Bounded printed floor datums, never section geometry or inferred wall heights.

A building section may supply a named floor elevation while remaining excluded
from plan geometry. Split labels must form an unambiguous aligned text stack.
An unnamed finished-floor zero establishes only a vertical datum, never the
identity or observed elevation of an otherwise unnamed plan level.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable

from .types import PdfPageObservation, PdfTextObservation


@dataclass(frozen=True, slots=True)
class SectionFloorDatum:
    level_name: str
    elevation_m: float
    page_number: int
    sources: tuple[PdfTextObservation, ...]
    confidence: float
    dimension: PdfTextObservation


@dataclass(frozen=True, slots=True)
class _Stack:
    name: str | None
    value_m: float
    sources: tuple[PdfTextObservation, ...]
    dimension: PdfTextObservation
    verify_in_field: bool


def _clean(value: str) -> str:
    return " ".join(value.upper().split())


def _name(value: str) -> str | None:
    value = _clean(value)
    if value == "MEZZANINE":
        return "Mezzanine"
    if re.fullmatch(
        r"(?:GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|LOWER|UPPER|MAIN|BASEMENT|\d{1,2}(?:ST|ND|RD|TH)) FLOOR",
        value,
    ):
        return value.title()
    match = re.fullmatch(r"LEVEL\s*[:#-]?\s+([A-Z0-9][A-Z0-9 ._-]{0,30})", value)
    if match and not re.search(
        r"\b(?:CEILING|SOFFIT|PARAPET|ROOF|VOID|SEE|NOTE)\b", match.group(1)
    ):
        return match.group(1).title()
    return None


def _height(item: PdfTextObservation) -> float:
    return item.font_size_pt or max(1.0, item.bbox_pt[3] - item.bbox_pt[1])


def _aligned(a: PdfTextObservation, b: PdfTextObservation) -> bool:
    h = max(_height(a), _height(b))
    overlap = min(a.bbox_pt[2], b.bbox_pt[2]) - max(a.bbox_pt[0], b.bbox_pt[0])
    width = min(a.bbox_pt[2] - a.bbox_pt[0], b.bbox_pt[2] - b.bbox_pt[0])
    edge = min(abs(a.bbox_pt[0] - b.bbox_pt[0]), abs(a.bbox_pt[2] - b.bbox_pt[2]))
    return width > 0 and overlap >= 0.5 * width and edge <= 1.5 * h


def _next_line(
    item: PdfTextObservation, texts: tuple[PdfTextObservation, ...]
) -> PdfTextObservation | None:
    candidates = []
    h = _height(item)
    for other in texts:
        if other.element_id == item.element_id or not _aligned(item, other):
            continue
        gap = item.bbox_pt[1] - other.bbox_pt[3]
        if -0.2 * h <= gap <= 1.2 * h and 0.5 * h <= _height(other) <= 2 * h:
            candidates.append((abs(gap), other.element_id, other))
    candidates.sort(key=lambda row: (row[0], row[1]))
    if not candidates or (
        len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= 0.25 * h
    ):
        return None
    return candidates[0][2]


def _value(
    text: str, parse_dimension: Callable[[str], tuple[float, tuple[int, int]] | None]
) -> tuple[float, bool] | None:
    text = _clean(text)
    vif = bool(re.search(r"\s+(?:\(?VIF\)?|VERIFY IN FIELD)$", text))
    text = re.sub(r"\s+(?:\(?VIF\)?|VERIFY IN FIELD)$", "", text).strip()
    metric = re.fullmatch(r"([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(MM|M)", text)
    if metric:
        value = float(metric.group(1)) * (0.001 if metric.group(2) == "MM" else 1.0)
    else:
        sign = -1 if text.startswith("-") else 1
        unsigned = (text[1:] if text[:1] in ("+", "-") else text).strip()
        try:
            parsed = parse_dimension(unsigned)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
        if parsed is None or parsed[1] != (0, len(unsigned)):
            return None
        value = sign * parsed[0]
    return (value, vif) if math.isfinite(value) else None


def section_floor_datums(
    page: PdfPageObservation,
    parse_dimension: Callable[[str], tuple[float, tuple[int, int]] | None],
) -> tuple[tuple[SectionFloorDatum, ...], tuple[dict[str, object], ...]]:
    # Room interior elevations often use independent local zeros. Only an
    # explicit building-wide section/elevation title enables this narrow lane.
    roles = tuple(
        sorted(
            (
                t
                for t in page.texts
                if t.center_pt[1] <= 0.25 * page.height_pt
                and re.fullmatch(r"BUILDING (?:SECTIONS?|ELEVATIONS?)", _clean(t.text))
            ),
            key=lambda t: t.element_id,
        )
    )
    if not roles:
        return (), ()
    stacks: list[_Stack] = []
    refused: list[dict[str, object]] = []

    def refuse_text(name: str | None, sources: list[PdfTextObservation]) -> None:
        if name is not None:
            refused.append(
                {
                    "page": page.page_number,
                    "code": "section_floor_datum_text_unresolved",
                    "level_name": name,
                    "source_element_ids": sorted(t.element_id for t in sources),
                }
            )

    for label in sorted(page.texts, key=lambda t: t.element_id):
        match = re.fullmatch(
            r"FINISHED FLOOR(?:\s*\([EN]\))?(?:\s*[-:]?\s+(.+))?", _clean(label.text)
        )
        if not match:
            continue
        name = _name(match.group(1)) if match.group(1) else None
        if match.group(1) and name is None:
            continue
        sources = [label]
        following = _next_line(label, page.texts)
        if following is None:
            refuse_text(name, sources)
            continue
        if name is None and (following_name := _name(following.text)) is not None:
            name = following_name
            sources.append(following)
            following = _next_line(following, page.texts)
            if following is None:
                refuse_text(name, sources)
                continue
        value = _value(following.text, parse_dimension)
        if value is None:
            refuse_text(name, sources + [following])
            continue
        sources.append(following)
        qualifier = _next_line(following, page.texts)
        split_vif = qualifier is not None and _clean(qualifier.text) in (
            "VIF",
            "VERIFY IN FIELD",
        )
        if split_vif:
            assert qualifier is not None
            sources.append(qualifier)
        stacks.append(
            _Stack(name, value[0], tuple(sources), following, value[1] or split_vif)
        )

    result: list[SectionFloorDatum] = []
    for stack in stacks:
        if stack.name is None:
            continue
        sources = list(stack.sources)
        verify_in_field = stack.verify_in_field
        if abs(stack.value_m) > 1e-9:
            zeros = []
            for other in stacks:
                if abs(other.value_m) > 1e-9 or not _aligned(
                    stack.dimension, other.dimension
                ):
                    continue
                distance = stack.dimension.center_pt[1] - other.dimension.center_pt[1]
                if (
                    distance * stack.value_m > 0
                    and abs(distance) <= page.height_pt * 0.45
                ):
                    zeros.append((abs(distance), other.dimension.element_id, other))
            zeros.sort(key=lambda row: (row[0], row[1]))
            if not zeros or (
                len(zeros) > 1 and zeros[1][0] - zeros[0][0] <= _height(stack.dimension)
            ):
                refused.append(
                    {
                        "page": page.page_number,
                        "code": "section_datum_reference_unresolved",
                        "level_name": stack.name,
                        "source_element_ids": sorted(t.element_id for t in sources),
                    }
                )
                continue
            sources.extend(zeros[0][2].sources)
            verify_in_field = verify_in_field or zeros[0][2].verify_in_field
        sources.extend(roles)
        unique = {t.element_id: t for t in sources}
        result.append(
            SectionFloorDatum(
                stack.name,
                stack.value_m,
                page.page_number,
                tuple(unique[key] for key in sorted(unique)),
                0.75 if verify_in_field else 0.95,
                stack.dimension,
            )
        )
    return tuple(result), tuple(refused)
