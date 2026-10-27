"""Opt-in physical sheet composition, not a canonical model or CAD renderer.

All placed coordinates are millimetres, from the top-left, +Y down. Source
measurements remain metres. Symbol tokens and semantic line styles are retained
for an explicit downstream renderer; no glyph metrics or font layout is guessed.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .generator import _offset_point
from .model import OUTPUT_VERSION, DrawingSet, Point2, _finite


def _number(value: float, label: str, *, positive: bool = False) -> float:
    value = _finite(value, label)
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'non-negative'}")
    return value


def _label(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _mm(value: float) -> float:
    value = _finite(value, "paper coordinate")
    value = round(value, 9)
    return 0.0 if value == 0 else value


@dataclass(frozen=True, slots=True)
class SheetSpec:
    """Explicit physical paper and reserved bottom titleblock, in millimetres."""

    id: str
    width_mm: float
    height_mm: float
    margin_mm: float = 10.0
    titleblock_height_mm: float = 0.0
    title: str = ""
    revision: str = ""
    titleblock_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _label(self.id, "sheet id")
        for key in ("width_mm", "height_mm", "margin_mm", "titleblock_height_mm"):
            object.__setattr__(self, key, _number(getattr(self, key), key, positive=key in {"width_mm", "height_mm"}))
        if 2 * self.margin_mm >= self.width_mm or 2 * self.margin_mm + self.titleblock_height_mm >= self.height_mm:
            raise ValueError("paper must leave positive drawing area")
        if not isinstance(self.title, str) or not isinstance(self.revision, str):
            raise ValueError("title and revision must be strings")
        if not isinstance(self.titleblock_fields, (tuple, list)) or any(
            not isinstance(pair, (tuple, list)) or len(pair) != 2
            for pair in self.titleblock_fields
        ):
            raise ValueError("titleblock fields must be key/value pairs")
        fields = tuple(tuple(pair) for pair in self.titleblock_fields)
        for key, value in fields:
            _label(key, "titleblock field key")
            if not isinstance(value, str):
                raise ValueError("titleblock field values must be strings")
        if len({key for key, _ in fields}) != len(fields):
            raise ValueError("duplicate titleblock field key")
        if fields and not self.titleblock_height_mm:
            raise ValueError("titleblock fields require a reserved titleblock")
        object.__setattr__(self, "titleblock_fields", tuple(sorted(fields)))


@dataclass(frozen=True, slots=True)
class ViewPlacement:
    """Place one view's upper-left bounds at an explicit paper position.

    Scale comes only from DrawingView.scale (1:scale); no fit-to-page scaling.
    Different placement IDs may intentionally repeat a view on the same sheet.
    """

    id: str
    view_id: str
    x_mm: float
    y_mm: float

    def __post_init__(self) -> None:
        _label(self.id, "placement id")
        _label(self.view_id, "view id")
        for key in ("x_mm", "y_mm"):
            object.__setattr__(self, key, _number(getattr(self, key), key))


@dataclass(frozen=True, slots=True)
class ComposedSheet:
    """Immutable JSON snapshot; returned dictionaries are detached copies."""

    _json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


def compose_sheet(drawing_set: DrawingSet, spec: SheetSpec, placements: tuple[ViewPlacement, ...]) -> ComposedSheet:
    """Compose existing derived views without changing model or drawing inputs.

    Reject missing/duplicate identities, unknown source references, nonfinite
    coordinates, overflowing geometry and overlapping view rectangles. Text
    anchors are checked, not text extents; this is not print-readiness approval.
    The digest binds exact DrawingSet serialization, not the canonical model.
    """
    if drawing_set.output_version != OUTPUT_VERSION:
        raise ValueError("unsupported drawing output version")
    placements = tuple(placements)
    if not placements:
        raise ValueError("at least one view placement is required")
    if len({p.id for p in placements}) != len(placements):
        raise ValueError("duplicate placement id")
    views = {v.id: v for v in drawing_set.views}
    if len(views) != len(drawing_set.views):
        raise ValueError("duplicate drawing view id")
    source_ids = {s.canonical_id for s in drawing_set.source_index}
    if len(source_ids) != len(drawing_set.source_index):
        raise ValueError("duplicate canonical source id")
    _label(drawing_set.model_id, "model id")
    for source_id in source_ids:
        _label(source_id, "canonical source id")
    source = drawing_set.to_dict()
    # A complete, lossless source index remains available for every placed item.
    result: dict[str, Any] = {
        "kind": "oabm.physical-sheet", "version": "1.0.0",
        "id": spec.id, "title": spec.title, "revision": spec.revision,
        "model_id": drawing_set.model_id,
        "drawing_set_sha256": hashlib.sha256(drawing_set.to_json(indent=None).encode("utf-8")).hexdigest(),
        "drawing_output_version": drawing_set.output_version,
        "paper": {"width_mm": spec.width_mm, "height_mm": spec.height_mm, "margin_mm": spec.margin_mm,
                  "coordinate_system": "top-left,+x-right,+y-down", "length_unit": "mm"},
        "titleblock": {"height_mm": spec.titleblock_height_mm,
                       "fields": dict(spec.titleblock_fields)},
        "source_index": sorted(source["source_index"], key=lambda s: s["canonical_id"]),
        "views": [],
        "limitations": ["Presentation output only; no canonical writeback.",
                        "Text anchors only; glyph bounds and collisions require renderer QA.",
                        "Symbols and line styles remain semantic tokens, not rendered CAD entities."],
    }
    margin = spec.margin_mm
    right, bottom = spec.width_mm - margin, spec.height_mm - margin - spec.titleblock_height_mm
    result["titleblock"]["bounds_mm"] = None if not spec.titleblock_height_mm else [margin, bottom, right, spec.height_mm - margin]
    rectangles: list[tuple[float, float, float, float]] = []
    source_views = {v["id"]: v for v in source["views"]}
    for placement in sorted(placements, key=lambda p: p.id):
        if placement.view_id not in views:
            raise ValueError(f"unknown view {placement.view_id!r}")
        view = views[placement.view_id]
        _label(view.id, "view id")
        scale = _number(view.scale, "view scale", positive=True)
        factor = 1000.0 / scale
        x, y = placement.x_mm, placement.y_mm
        rect = (x, y, x + view.bounds.width * factor, y + view.bounds.height * factor)
        if not all(math.isfinite(v) for v in rect):
            raise ValueError("nonfinite placed view bounds")
        quantized_rect = tuple(_mm(v) for v in rect)
        if quantized_rect[2] <= quantized_rect[0] or quantized_rect[3] <= quantized_rect[1]:
            raise ValueError("placed view bounds collapse at this scale")
        if rect[0] < margin or rect[1] < margin or rect[2] > right or rect[3] > bottom:
            raise ValueError(f"view {view.id!r} exceeds available drawing area")
        if any(rect[0] < b[2] and b[0] < rect[2] and rect[1] < b[3] and b[1] < rect[3] for b in rectangles):
            raise ValueError("placed view rectangles overlap")
        rectangles.append(rect)

        def paper(point: Point2) -> dict[str, float]:
            px = x + (point.x - view.bounds.min_x) * factor
            py = y + (view.bounds.max_y - point.y) * factor
            # Tolerance is only floating-point noise, not an oversize allowance.
            if px < rect[0] - 1e-8 or px > rect[2] + 1e-8 or py < rect[1] - 1e-8 or py > rect[3] + 1e-8:
                raise ValueError(f"geometry outside placed view {view.id!r}; enlarge its source bounds")
            return {"x": _mm(px), "y": _mm(py)}

        def references(ids: tuple[str, ...]) -> None:
            if not ids or any(identity not in source_ids for identity in ids):
                raise ValueError("unknown or missing canonical source reference")

        primitives = []
        originals = {p["id"]: p for p in source_views[view.id]["primitives"]}
        for primitive in sorted(view.primitives, key=lambda p: p.id):
            references(primitive.source_ids)
            if primitive.kind not in {"line", "polyline", "polygon", "text", "symbol"}:
                raise ValueError(f"unsupported primitive kind {primitive.kind!r}")
            record = dict(originals[primitive.id])
            del record["points"]
            record["points_mm"] = [paper(p) for p in primitive.points]
            primitives.append(record)
        dimensions = []
        dimension_ids: set[str] = set()
        for dimension in sorted(view.dimensions, key=lambda d: d.id):
            _label(dimension.id, "dimension id")
            if dimension.id in dimension_ids:
                raise ValueError("duplicate dimension id")
            dimension_ids.add(dimension.id)
            references(dimension.source_ids)
            start = _offset_point(dimension.start, dimension.end, dimension.offset_m)
            end = _offset_point(dimension.end, dimension.start, -dimension.offset_m)
            a, b = paper(start), paper(end)
            dimensions.append({"id": dimension.id, "source_ids": list(dimension.source_ids),
                               "measured_start_mm": paper(dimension.start), "measured_end_mm": paper(dimension.end),
                               "line_start_mm": a, "line_end_mm": b,
                               "text_anchor_mm": {"x": _mm((a["x"] + b["x"]) / 2), "y": _mm((a["y"] + b["y"]) / 2)},
                               "value_m": dimension.value_m, "text": dimension.text})
        result["views"].append({"placement_id": placement.id, "view_id": view.id, "view_type": view.view_type,
                                "title": view.title, "scale": scale, "bounds_mm": list(quantized_rect),
                                "metadata": source_views[view.id]["metadata"],
                                "primitives": primitives, "dimensions": dimensions})
    return ComposedSheet(json.dumps(result, sort_keys=True, allow_nan=False))


def compose_blank_sheet(spec: SheetSpec) -> ComposedSheet:
    """First-class blank stationery, with no model, views or invented geometry."""
    if not isinstance(spec, SheetSpec):
        raise ValueError("blank sheet requires a SheetSpec")
    margin = spec.margin_mm
    right = spec.width_mm - margin
    bottom = spec.height_mm - margin - spec.titleblock_height_mm
    document = {
        "kind": "oabm.blank-sheet", "version": "1.0.0", "content": "blank",
        "id": spec.id, "title": spec.title, "revision": spec.revision,
        "model_id": None, "drawing_set_sha256": None, "drawing_output_version": None,
        "paper": {"width_mm": spec.width_mm, "height_mm": spec.height_mm,
                  "margin_mm": margin, "coordinate_system": "top-left,+x-right,+y-down", "length_unit": "mm"},
        "titleblock": {"height_mm": spec.titleblock_height_mm, "fields": dict(spec.titleblock_fields),
                       "bounds_mm": None if not spec.titleblock_height_mm else [margin,bottom,right,spec.height_mm-margin]},
        "source_index": [], "views": [],
        "limitations": ["Blank sheet stationery only; no model or design content.",
                        "Supplied titleblock text is not survey, design or approval evidence."]}
    return ComposedSheet(json.dumps(document,sort_keys=True,allow_nan=False))
