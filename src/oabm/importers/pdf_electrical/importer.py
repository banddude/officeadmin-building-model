from __future__ import annotations

import hashlib
from collections import Counter
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from oabm.model import (
    BuildingModel,
    Circuit,
    CoordinateSystem,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Port,
    Pose,
    Provenance,
    Vector3,
    stable_id,
)

from .extract import PdfPageDisplayTransform, page_display_transform

POINT_TO_M = 0.0254 / 72.0


class ElectricalPdfError(ValueError):
    """Raised when an electrical PDF cannot be extracted or recognized safely."""


@dataclass(frozen=True, slots=True)
class PdfTextObservation:
    element_id: str
    page: int
    text: str
    x_pt: float
    y_pt: float
    font_size_pt: float | None = None

    def __post_init__(self) -> None:
        _validate_source_observation(self.element_id, self.page, self.x_pt, self.y_pt)
        if not self.text.strip():
            raise ElectricalPdfError("text observations cannot be empty")


@dataclass(frozen=True, slots=True)
class PdfSymbolObservation:
    element_id: str
    page: int
    name: str
    x_pt: float
    y_pt: float
    source_kind: str = "form-xobject"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_source_observation(self.element_id, self.page, self.x_pt, self.y_pt)
        if not self.name.strip():
            raise ElectricalPdfError("symbol observations require a name")


@dataclass(frozen=True, slots=True)
class PdfVectorPathObservation:
    element_id: str
    page: int
    points_pt: tuple[tuple[float, float], ...]
    closed: bool = False
    source_kind: str = "vector-path"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.element_id:
            raise ElectricalPdfError("vector observations require an element_id")
        if self.page < 1:
            raise ElectricalPdfError("source page is 1-based")
        if len(self.points_pt) < 2:
            raise ElectricalPdfError("vector observations require at least two points")
        if self.closed and len(self.points_pt) < 3:
            raise ElectricalPdfError("closed vector observations require at least three points")
        for index, point in enumerate(self.points_pt):
            if len(point) != 2:
                raise ElectricalPdfError(
                    f"vector point {index} must contain exactly x/y coordinates"
                )
            x_pt, y_pt = point
            if not math.isfinite(float(x_pt)) or not math.isfinite(float(y_pt)):
                raise ElectricalPdfError("vector observation coordinates must be finite")
        for first, second in zip(self.points_pt, self.points_pt[1:]):
            if math.hypot(first[0] - second[0], first[1] - second[1]) <= 1e-9:
                raise ElectricalPdfError(
                    "vector observations cannot contain consecutive duplicate points"
                )


@dataclass(frozen=True, slots=True)
class PdfElectricalDocument:
    source_id: str
    page_count: int
    texts: tuple[PdfTextObservation, ...] = ()
    symbols: tuple[PdfSymbolObservation, ...] = ()
    vectors: tuple[PdfVectorPathObservation, ...] = ()
    page_provenance: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ElectricalPdfError("source_id is required")
        if self.page_count < 1:
            raise ElectricalPdfError("page_count must be >= 1")
        for item in (*self.texts, *self.symbols, *self.vectors):
            if item.page > self.page_count:
                raise ElectricalPdfError(
                    f"{item.element_id} references page {item.page}, but page_count is {self.page_count}"
                )
        for page, provenance in self.page_provenance.items():
            if not isinstance(page, int) or not 1 <= page <= self.page_count:
                raise ElectricalPdfError(
                    f"page provenance references invalid page {page!r}"
                )
            rotation = provenance.get("page_rotation")
            if rotation not in {0, 90, 180, 270}:
                raise ElectricalPdfError(
                    f"page provenance for page {page} has invalid page_rotation {rotation!r}"
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PdfElectricalDocument:
        if not isinstance(data, Mapping):
            raise ElectricalPdfError("electrical PDF fixture must be an object")
        texts = tuple(
            PdfTextObservation(
                element_id=str(item["element_id"]),
                page=int(item["page"]),
                text=str(item["text"]),
                x_pt=float(item["x_pt"]),
                y_pt=float(item["y_pt"]),
                font_size_pt=(
                    None if item.get("font_size_pt") is None else float(item["font_size_pt"])
                ),
            )
            for item in data.get("texts", ())
        )
        symbols = tuple(
            PdfSymbolObservation(
                element_id=str(item["element_id"]),
                page=int(item["page"]),
                name=str(item["name"]),
                x_pt=float(item["x_pt"]),
                y_pt=float(item["y_pt"]),
                source_kind=str(item.get("source_kind", "form-xobject")),
                metadata=dict(item.get("metadata", {})),
            )
            for item in data.get("symbols", ())
        )
        vectors = tuple(
            PdfVectorPathObservation(
                element_id=str(item["element_id"]),
                page=int(item["page"]),
                points_pt=tuple(
                    (float(point[0]), float(point[1]))
                    for point in item["points_pt"]
                ),
                closed=bool(item.get("closed", False)),
                source_kind=str(item.get("source_kind", "vector-path")),
                metadata=dict(item.get("metadata", {})),
            )
            for item in data.get("vectors", ())
        )
        raw_page_provenance = data.get("page_provenance", {})
        if not isinstance(raw_page_provenance, Mapping):
            raise ElectricalPdfError("page_provenance must be an object")
        page_provenance = {
            int(page): dict(provenance)
            for page, provenance in raw_page_provenance.items()
        }
        return cls(
            source_id=str(data["source_id"]),
            page_count=int(data["page_count"]),
            texts=texts,
            symbols=symbols,
            vectors=vectors,
            page_provenance=page_provenance,
        )


@dataclass(frozen=True, slots=True)
class PdfPageTransform:
    """Affine registration from displayed PDF page coordinates into one canonical frame."""

    frame_id: str
    m11_m_per_pt: float = POINT_TO_M
    m12_m_per_pt: float = 0.0
    m21_m_per_pt: float = 0.0
    m22_m_per_pt: float = POINT_TO_M
    tx_m: float = 0.0
    ty_m: float = 0.0
    z_m: float = 0.0

    def __post_init__(self) -> None:
        if not self.frame_id:
            raise ElectricalPdfError("page transform frame_id is required")
        values = (
            self.m11_m_per_pt,
            self.m12_m_per_pt,
            self.m21_m_per_pt,
            self.m22_m_per_pt,
            self.tx_m,
            self.ty_m,
            self.z_m,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ElectricalPdfError("page transform coefficients must be finite")
        determinant = (
            self.m11_m_per_pt * self.m22_m_per_pt
            - self.m12_m_per_pt * self.m21_m_per_pt
        )
        if abs(determinant) <= 1e-15:
            raise ElectricalPdfError("page transform must have a non-zero planar determinant")

    def apply(self, x_pt: float, y_pt: float) -> Point3:
        return Point3(
            x=self.m11_m_per_pt * x_pt + self.m12_m_per_pt * y_pt + self.tx_m,
            y=self.m21_m_per_pt * x_pt + self.m22_m_per_pt * y_pt + self.ty_m,
            z=self.z_m,
        )

    def to_attributes(self) -> dict[str, float | str]:
        return {
            "frame_id": self.frame_id,
            "m11_m_per_pt": self.m11_m_per_pt,
            "m12_m_per_pt": self.m12_m_per_pt,
            "m21_m_per_pt": self.m21_m_per_pt,
            "m22_m_per_pt": self.m22_m_per_pt,
            "tx_m": self.tx_m,
            "ty_m": self.ty_m,
            "z_m": self.z_m,
        }


@dataclass(frozen=True, slots=True)
class ElectricalInstanceHint:
    """Explicit stable identity for one source-page electrical instance.

    Use this only when the source itself does not expose a stable semantic/native
    identifier. Coordinates locate the recognized source instance; ``identity_key``
    is the caller-owned stable semantic anchor used for canonical identity.
    """

    identity_key: str
    page: int
    entity_kind: str
    canonical_type: str
    x_pt: float
    y_pt: float
    tag: str | None = None
    source_element_id: str | None = None
    confidence: float = 1.0
    note: str = "explicit electrical instance hint"

    def __post_init__(self) -> None:
        if not self.identity_key.strip():
            raise ElectricalPdfError("instance hint identity_key is required")
        if self.page < 1:
            raise ElectricalPdfError("instance hint page is 1-based")
        if self.entity_kind not in {"device", "equipment"}:
            raise ElectricalPdfError("instance hint entity_kind must be device or equipment")
        if not self.canonical_type.strip():
            raise ElectricalPdfError("instance hint canonical_type is required")
        if not math.isfinite(float(self.x_pt)) or not math.isfinite(float(self.y_pt)):
            raise ElectricalPdfError("instance hint coordinates must be finite")
        if self.tag is not None and not self.tag.strip():
            raise ElectricalPdfError("instance hint tag cannot be blank")
        if self.source_element_id is not None and not self.source_element_id.strip():
            raise ElectricalPdfError("instance hint source_element_id cannot be blank")
        if not 0.0 <= self.confidence <= 1.0:
            raise ElectricalPdfError("instance hint confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class SymbolRule:
    pattern: str
    entity_kind: str
    canonical_type: str
    confidence: float

    def __post_init__(self) -> None:
        if self.entity_kind not in {"device", "equipment"}:
            raise ElectricalPdfError("symbol rule entity_kind must be device or equipment")
        if not 0.0 <= self.confidence <= 1.0:
            raise ElectricalPdfError("symbol rule confidence must be between 0 and 1")


DEFAULT_SYMBOL_RULES: tuple[SymbolRule, ...] = (
    SymbolRule(r"\b(?:PANEL|PNL)\b", "equipment", "panelboard", 0.98),
    SymbolRule(r"\b(?:SWBD|SWGR|SWITCHBOARD|SWITCHGEAR)\b", "equipment", "switchboard", 0.97),
    SymbolRule(r"\b(?:XFMR|TRANSFORMER)\b", "equipment", "transformer", 0.97),
    SymbolRule(r"\b(?:EVSE|CHARGER)[A-Z0-9]*\b", "device", "evse", 0.98),
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b).*\bDUPLEX\b.*\b(?:ELECTRICAL\s+)?OUTLET\b",
        "device",
        "receptacle_duplex",
        1.0,
    ),
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b).*\b(?:QUADRUPLEX|QUADRUPLE|QUAD)\b.*\b(?:ELECTRICAL\s+)?OUTLET\b",
        "device",
        "receptacle_quad",
        1.0,
    ),
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b).*\b(?:TELEPHONE|TELE\s*/\s*DATA|TELE/DATA|DATA)\b.*\bOUTLET\b",
        "device",
        "data_outlet",
        1.0,
    ),
    SymbolRule(
        r"\bCOMBINATION\b.*\b(?:OUTLET|RECEPTACLE)\b",
        "device",
        "combination_outlet",
        1.0,
    ),
    SymbolRule(
        r"\b(?:ELECTRICAL|POWER)\s+(?:J-?BOX|JUNCTION\s+BOX)\b",
        "device",
        "junction_box_power",
        1.0,
    ),
    SymbolRule(
        r"\b(?:TELE(?:PHONE)?(?:\s*/\s*|\s+AND/OR\s+|\s+AND\s+)?DATA|DATA)\s+(?:J-?BOX|JUNCTION\s+BOX)\b",
        "device",
        "junction_box_data",
        1.0,
    ),
    SymbolRule(
        r"\b(?:CARD\s+READER|ELECTRIC\s+LOCK\s+RELEASE|ACCESS\s+CONTROL)\b",
        "device",
        "access_control_device",
        1.0,
    ),
    SymbolRule(
        r"\b(?:CABLE\s+TV|CATV)\b.*\bOUTLET\b",
        "device",
        "catv_outlet",
        1.0,
    ),
    SymbolRule(r"^(?!.*\bCOMBINATION\b)(?!.*\bDUPLEX\b.*\b(?:ELECTRICAL\s+)?OUTLET\b).*\b(?:GFCI|GFI|RECEPTACLE|RECEPT|DUPLEX|REC)[A-Z0-9]*\b", "device", "receptacle", 0.94),
    SymbolRule(r"^(?!.*\b(?:ELECTRICAL|POWER|DATA|TELE|TELEPHONE)\b.*\b(?:JBOX|J-?BOX|JUNCTION\s+BOX)\b).*\b(?:(?:JBOX|J-?BOX|JB)[A-Z0-9]*|JUNCTION\s+BOX)\b", "device", "junction_box", 0.94),
    SymbolRule(r"\b(?:LUMINAIRE|LIGHT|LTG|FIXTURE)\b", "device", "luminaire", 0.91),
    SymbolRule(r"\b(?:DISCONNECT|DISC)\b", "device", "disconnect", 0.92),
    SymbolRule(r"\b(?:SWITCH|SW)\b", "device", "switch", 0.75),
    # Bare "SW" is intentionally ambiguous without a legend.
    SymbolRule(r"\bSW\b", "equipment", "switchboard", 0.75),
)


@dataclass(slots=True)
class _EntityCandidate:
    key: str
    entity_kind: str
    canonical_type: str
    tag: str | None
    identity_key: str | None
    page: int
    x_pt: float
    y_pt: float
    confidence: float
    primary_method: str
    source_element_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    symbol_names: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    shape_recognition: dict[str, Any] | None = None
    annotation_recognition: dict[str, Any] | None = None

    def merge_source(
        self,
        *,
        element_id: str,
        text: str | None,
        symbol_name: str | None,
        x_pt: float,
        y_pt: float,
        confidence: float,
        provenance: Provenance,
        method: str,
    ) -> None:
        if element_id not in self.source_element_ids:
            self.source_element_ids.append(element_id)
            self.provenance.append(provenance)
        if text and text not in self.texts:
            self.texts.append(text)
        if symbol_name and symbol_name not in self.symbol_names:
            self.symbol_names.append(symbol_name)
        if confidence > self.confidence:
            self.confidence = confidence
            if self.shape_recognition is None or method == "pdf-legend-shape-match":
                self.x_pt = x_pt
                self.y_pt = y_pt
                self.primary_method = method


@dataclass(frozen=True, slots=True)
class _VectorCluster:
    page: int
    vectors: tuple[PdfVectorPathObservation, ...]
    bbox_pt: tuple[float, float, float, float]
    center_pt: tuple[float, float]
    shape_signature: str
    geometry_key: str
    stripped_text_tags: tuple[str, ...] = ()
    cleanup_actions: tuple[str, ...] = ()
    excluded_source_element_ids: tuple[str, ...] = ()

    @property
    def source_element_ids(self) -> tuple[str, ...]:
        return tuple(sorted(vector.element_id for vector in self.vectors))


@dataclass(frozen=True, slots=True)
class _LegendRow:
    cluster: _VectorCluster
    label: PdfTextObservation
    classification: tuple[str, str, float] | None
    classification_candidates: tuple[Mapping[str, Any], ...]
    orientation: int
    horizontal_gap_pt: float
    label_source_element_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _LegendRegion:
    page: int
    method: str
    rows: tuple[_LegendRow, ...]
    heading: PdfTextObservation | None
    confidence: float
    header_element_ids: tuple[str, ...] = ()
    table_bbox_pt: tuple[float, float, float, float] | None = None
    frame_provenance: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class _PageFrameDetection:
    bbox_pt: tuple[float, float, float, float]
    method: str


@dataclass(frozen=True, slots=True)
class _LegendReference:
    page: int
    source_element_id: str
    text: str
    target_alias: str
    legend_page: int


@dataclass(frozen=True, slots=True)
class _LegendEntry:
    entity_kind: str
    canonical_type: str
    confidence: float
    prototype: _VectorCluster
    label: PdfTextObservation
    region: _LegendRegion
    label_source_element_ids: tuple[str, ...] = ()


def _validate_source_observation(element_id: str, page: int, x_pt: float, y_pt: float) -> None:
    if not element_id:
        raise ElectricalPdfError("source element_id is required")
    if page < 1:
        raise ElectricalPdfError("source page is 1-based")
    for label, value in (("x_pt", x_pt), ("y_pt", y_pt)):
        if not math.isfinite(float(value)):
            raise ElectricalPdfError(f"{label} must be finite")


def _clean_pdf_string(value: Any) -> str | None:
    if value is None:
        return None
    try:
        value = value.get_object()
    except AttributeError:
        pass
    text = str(value).strip()
    return text or None


def _transform_text_point(cm: Sequence[float], tm: Sequence[float]) -> tuple[float, float]:
    # PDF text position transformed through the current graphics matrix.
    x = float(cm[0]) * float(tm[4]) + float(cm[2]) * float(tm[5]) + float(cm[4])
    y = float(cm[1]) * float(tm[4]) + float(cm[3]) * float(tm[5]) + float(cm[5])
    return x, y


def _transform_graphics_point(
    cm: Sequence[float],
    x: float,
    y: float,
) -> tuple[float, float]:
    return (
        float(cm[0]) * float(x) + float(cm[2]) * float(y) + float(cm[4]),
        float(cm[1]) * float(x) + float(cm[3]) * float(y) + float(cm[5]),
    )


_BEZIER_FLATTEN_STEPS = 8


def _flatten_cubic_bezier(
    start: tuple[float, float],
    control_1: tuple[float, float],
    control_2: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    """Return deterministic polyline samples for one cubic Bézier segment.

    The source curve controls are retained separately in observation metadata.
    The fixed subdivision count avoids extraction-order or tolerance-dependent
    geometry while preserving the non-linear shape for downstream inspection.
    """

    samples: list[tuple[float, float]] = []
    for index in range(1, _BEZIER_FLATTEN_STEPS + 1):
        t = index / _BEZIER_FLATTEN_STEPS
        inverse = 1.0 - t
        samples.append(
            (
                inverse**3 * start[0]
                + 3.0 * inverse**2 * t * control_1[0]
                + 3.0 * inverse * t**2 * control_2[0]
                + t**3 * end[0],
                inverse**3 * start[1]
                + 3.0 * inverse**2 * t * control_1[1]
                + 3.0 * inverse * t**2 * control_2[1]
                + t**3 * end[1],
            )
        )
    return tuple(samples)


def _make_page_visitors(
    *,
    page_number: int,
    form_names: frozenset[str],
    display_transform: PdfPageDisplayTransform,
    texts: list[PdfTextObservation],
    symbols: list[PdfSymbolObservation],
    vectors: list[PdfVectorPathObservation],
):
    text_counter = 0
    operator_counter = 0
    vector_counter = 0
    current_points: list[tuple[float, float]] = []
    current_curve_commands: list[dict[str, Any]] = []
    current_closed = False
    current_supported = True
    pending_subpaths: list[
        tuple[
            tuple[tuple[float, float], ...],
            bool,
            bool,
            tuple[dict[str, Any], ...],
        ]
    ] = []

    def displayed_graphics_point(
        cm: Sequence[float],
        x: float,
        y: float,
    ) -> tuple[float, float]:
        raw_x, raw_y = _transform_graphics_point(cm, x, y)
        return display_transform.apply(raw_x, raw_y)

    def visitor_text(
        text: str,
        cm: Sequence[float],
        tm: Sequence[float],
        font_dict: Any,
        font_size: float,
    ) -> None:
        nonlocal text_counter
        cleaned = " ".join(text.split())
        if not cleaned:
            return
        text_counter += 1
        raw_x_pt, raw_y_pt = _transform_text_point(cm, tm)
        x_pt, y_pt = display_transform.apply(raw_x_pt, raw_y_pt)
        texts.append(
            PdfTextObservation(
                element_id=f"p{page_number}:text:{text_counter:04d}",
                page=page_number,
                text=cleaned,
                x_pt=x_pt,
                y_pt=y_pt,
                font_size_pt=float(font_size) if font_size is not None else None,
            )
        )

    def finish_current() -> None:
        nonlocal current_points, current_curve_commands, current_closed, current_supported
        if len(current_points) >= 2:
            pending_subpaths.append(
                (
                    tuple(current_points),
                    current_closed,
                    current_supported,
                    tuple(current_curve_commands),
                )
            )
        current_points = []
        current_curve_commands = []
        current_closed = False
        current_supported = True

    def clear_paths() -> None:
        nonlocal pending_subpaths
        finish_current()
        pending_subpaths = []

    def emit_paths(operator: bytes, *, close_subpaths: bool = False) -> None:
        nonlocal pending_subpaths, vector_counter
        finish_current()
        paint_operator = operator.decode("ascii", errors="replace")
        for points, closed, supported, curve_commands in pending_subpaths:
            if not supported:
                continue
            normalized_points: list[tuple[float, float]] = []
            for point in points:
                if (
                    normalized_points
                    and math.hypot(
                        normalized_points[-1][0] - point[0],
                        normalized_points[-1][1] - point[1],
                    )
                    <= 1e-9
                ):
                    continue
                normalized_points.append(point)
            if len(normalized_points) < 2 or (closed and len(normalized_points) < 3):
                continue
            vector_counter += 1
            metadata: dict[str, Any] = {"paint_operator": paint_operator}
            if curve_commands:
                metadata.update(
                    {
                        "geometry_kind": "bezier-flattened",
                        "curve_flatten_steps": _BEZIER_FLATTEN_STEPS,
                        "curve_commands": [dict(command) for command in curve_commands],
                    }
                )
            vectors.append(
                PdfVectorPathObservation(
                    element_id=f"p{page_number}:vector:{vector_counter:05d}",
                    page=page_number,
                    points_pt=tuple(normalized_points),
                    closed=closed or close_subpaths,
                    source_kind="pdf-vector-path",
                    metadata=metadata,
                )
            )
        pending_subpaths = []

    def visitor_operand_before(
        operator: bytes,
        operands: Sequence[Any],
        cm: Sequence[float],
        tm: Sequence[float],
    ) -> None:
        nonlocal operator_counter, current_points, current_curve_commands, current_closed, current_supported
        operator_counter += 1

        if operator == b"Do" and operands:
            name = str(operands[0])
            if not form_names or name in form_names:
                symbols.append(
                    PdfSymbolObservation(
                        element_id=f"p{page_number}:xobject:{operator_counter:05d}",
                        page=page_number,
                        name=name,
                        x_pt=display_transform.apply(
                            float(cm[4]), float(cm[5])
                        )[0],
                        y_pt=display_transform.apply(
                            float(cm[4]), float(cm[5])
                        )[1],
                        source_kind="form-xobject",
                    )
                )
            return

        if operator == b"m" and len(operands) >= 2:
            finish_current()
            current_points = [
                displayed_graphics_point(cm, float(operands[0]), float(operands[1]))
            ]
            return

        if operator == b"l" and len(operands) >= 2:
            if current_points:
                current_points.append(
                    displayed_graphics_point(
                        cm,
                        float(operands[0]),
                        float(operands[1]),
                    )
                )
            return

        if operator == b"re" and len(operands) >= 4:
            finish_current()
            x, y, width, height = (float(value) for value in operands[:4])
            pending_subpaths.append(
                (
                    (
                        displayed_graphics_point(cm, x, y),
                        displayed_graphics_point(cm, x + width, y),
                        displayed_graphics_point(cm, x + width, y + height),
                        displayed_graphics_point(cm, x, y + height),
                    ),
                    True,
                    True,
                    (),
                )
            )
            return

        if operator == b"h":
            current_closed = True
            return

        if operator in {b"c", b"v", b"y"}:
            if not current_points:
                current_supported = False
                return

            if operator == b"c":
                if len(operands) < 6:
                    current_supported = False
                    return
                control_1 = displayed_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                control_2 = displayed_graphics_point(
                    cm, float(operands[2]), float(operands[3])
                )
                end = displayed_graphics_point(
                    cm, float(operands[4]), float(operands[5])
                )
            elif operator == b"v":
                if len(operands) < 4:
                    current_supported = False
                    return
                control_1 = current_points[-1]
                control_2 = displayed_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                end = displayed_graphics_point(
                    cm, float(operands[2]), float(operands[3])
                )
            else:
                if len(operands) < 4:
                    current_supported = False
                    return
                control_1 = displayed_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                end = displayed_graphics_point(
                    cm, float(operands[2]), float(operands[3])
                )
                control_2 = end

            current_curve_commands.append(
                {
                    "operator": operator.decode("ascii"),
                    "control_points_pt": [
                        [control_1[0], control_1[1]],
                        [control_2[0], control_2[1]],
                    ],
                    "end_pt": [end[0], end[1]],
                }
            )
            current_points.extend(
                _flatten_cubic_bezier(
                    current_points[-1],
                    control_1,
                    control_2,
                    end,
                )
            )
            return

        if operator in {b"s", b"b", b"b*"}:
            current_closed = True
            emit_paths(operator)
            return

        if operator in {b"S", b"B", b"B*"}:
            emit_paths(operator)
            return

        if operator in {b"f", b"F", b"f*"}:
            emit_paths(operator, close_subpaths=True)
            return

        if operator == b"n":
            clear_paths()

    return visitor_text, visitor_operand_before


def extract_pdf(path: str | Path, *, source_id: str | None = None) -> PdfElectricalDocument:
    """Extract deterministic observations in each page's displayed orientation.

    pypdf visitor callbacks expose raw PDF user-space coordinates and do not
    apply the page /Rotate entry. Extraction normalizes text, symbols, vector
    geometry, and annotation positions into displayed, bottom-origin page space
    before downstream electrical recognition sees them.
    """

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise ElectricalPdfError("pypdf is required for PDF extraction") from exc

    pdf_path = Path(path)
    payload = pdf_path.read_bytes()
    resolved_source_id = source_id or f"sha256:{hashlib.sha256(payload).hexdigest()}"

    try:
        reader = PdfReader(pdf_path)
    except Exception as exc:
        raise ElectricalPdfError(f"failed to read PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            raise ElectricalPdfError("encrypted PDF could not be opened") from exc
        if not unlocked:
            raise ElectricalPdfError("encrypted PDF requires a password")

    texts: list[PdfTextObservation] = []
    symbols: list[PdfSymbolObservation] = []
    vectors: list[PdfVectorPathObservation] = []
    page_provenance: dict[int, dict[str, float | int | str]] = {}

    for page_number, page in enumerate(reader.pages, start=1):
        try:
            display_transform = page_display_transform(page)
        except ValueError as exc:
            raise ElectricalPdfError(
                f"failed to normalize page {page_number} display orientation: {exc}"
            ) from exc
        page_provenance[page_number] = display_transform.provenance_attributes()

        resources = page.get("/Resources")
        if resources is not None:
            try:
                resources = resources.get_object()
            except AttributeError:
                pass
        form_names: set[str] = set()
        if resources:
            xobjects = resources.get("/XObject")
            if xobjects is not None:
                try:
                    xobjects = xobjects.get_object()
                except AttributeError:
                    pass
                if isinstance(xobjects, Mapping):
                    for raw_name, raw_object in xobjects.items():
                        try:
                            xobject = raw_object.get_object()
                        except AttributeError:
                            xobject = raw_object
                        if str(xobject.get("/Subtype")) == "/Form":
                            form_names.add(str(raw_name))

        visitor_text, visitor_operand_before = _make_page_visitors(
            page_number=page_number,
            form_names=frozenset(form_names),
            display_transform=display_transform,
            texts=texts,
            symbols=symbols,
            vectors=vectors,
        )

        temporary_font_resource = False
        try:
            # pypdf 6.19 short-circuits extract_text before visitor callbacks
            # when /Resources is an empty dictionary. Geometry-only CAD pages can
            # validly have no resources, so temporarily add an empty /Font entry
            # solely to force content-stream traversal for the operator visitor.
            if isinstance(resources, dict) and not resources:
                from pypdf.generic import DictionaryObject, NameObject

                resources[NameObject("/Font")] = DictionaryObject()
                temporary_font_resource = True

            page.extract_text(
                visitor_text=visitor_text,
                visitor_operand_before=visitor_operand_before,
            )
        except Exception as exc:
            raise ElectricalPdfError(
                f"failed to extract page {page_number}: {exc}"
            ) from exc
        finally:
            if temporary_font_resource:
                resources.pop(NameObject("/Font"), None)

        annotations = page.get("/Annots") or ()
        for annotation_index, annotation_ref in enumerate(annotations, start=1):
            try:
                annotation = annotation_ref.get_object()
            except AttributeError:
                annotation = annotation_ref
            rect = annotation.get("/Rect")
            if not rect or len(rect) < 4:
                continue
            raw_x_pt = (float(rect[0]) + float(rect[2])) / 2.0
            raw_y_pt = (float(rect[1]) + float(rect[3])) / 2.0
            x_pt, y_pt = display_transform.apply(raw_x_pt, raw_y_pt)
            subtype = _clean_pdf_string(annotation.get("/Subtype")) or "/Unknown"
            subject = _clean_pdf_string(annotation.get("/Subj"))
            contents = _clean_pdf_string(annotation.get("/Contents"))
            native_id = _clean_pdf_string(annotation.get("/NM"))
            element_root = f"p{page_number}:annotation:{annotation_index:04d}"

            if contents:
                texts.append(
                    PdfTextObservation(
                        element_id=f"{element_root}:text",
                        page=page_number,
                        text=contents,
                        x_pt=x_pt,
                        y_pt=y_pt,
                    )
                )

            if subtype in {"/Stamp", "/Square", "/Circle"}:
                symbols.append(
                    PdfSymbolObservation(
                        element_id=element_root,
                        page=page_number,
                        name=subject or native_id or subtype,
                        x_pt=x_pt,
                        y_pt=y_pt,
                        source_kind=f"annotation:{subtype.lstrip('/').lower()}",
                        metadata={
                            key: value
                            for key, value in {
                                "subject": subject,
                                "contents": contents,
                                "native_id": native_id,
                            }.items()
                            if value is not None
                        },
                    )
                )

    return PdfElectricalDocument(
        source_id=resolved_source_id,
        page_count=len(reader.pages),
        texts=tuple(sorted(texts, key=lambda item: (item.page, item.element_id))),
        symbols=tuple(sorted(symbols, key=lambda item: (item.page, item.element_id))),
        vectors=tuple(sorted(vectors, key=lambda item: (item.page, item.element_id))),
        page_provenance=page_provenance,
    )


_PANEL_RE = re.compile(r"\b(?:PANEL|PNL)\s+(?P<tag>[A-Z][A-Z0-9_.-]*)\b", re.IGNORECASE)
_EQUIPMENT_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:SWBD|SWGR|SWITCHBOARD|SWITCHGEAR)\s+(?P<tag>[A-Z][A-Z0-9_.-]*)\b", re.IGNORECASE),
        "switchboard",
    ),
    (
        re.compile(r"\b(?:XFMR|TRANSFORMER)\s+(?P<tag>[A-Z][A-Z0-9_.-]*)\b", re.IGNORECASE),
        "transformer",
    ),
)
_INSTANCE_SUFFIX_RE = r"(?:[0-9]+|[-_.][A-Z0-9]+)?"
_DEVICE_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            rf"\b(?P<tag>EVSE{_INSTANCE_SUFFIX_RE})\b",
            re.IGNORECASE,
        ),
        "evse",
    ),
    (
        re.compile(
            rf"\b(?P<tag>(?:GFCI|GFI|RECEPT|REC){_INSTANCE_SUFFIX_RE})\b",
            re.IGNORECASE,
        ),
        "receptacle",
    ),
    (
        re.compile(
            rf"\b(?P<tag>(?:JBOX|J-?BOX|JB){_INSTANCE_SUFFIX_RE})\b",
            re.IGNORECASE,
        ),
        "junction_box",
    ),
    (
        re.compile(
            rf"\b(?P<tag>(?:LIGHT|LTG|LUM){_INSTANCE_SUFFIX_RE})\b",
            re.IGNORECASE,
        ),
        "luminaire",
    ),
    (
        re.compile(
            rf"\b(?P<tag>(?:DISC|DISCONNECT){_INSTANCE_SUFFIX_RE})\b",
            re.IGNORECASE,
        ),
        "disconnect",
    ),
)
_GENERIC_DEVICE_TAGS: frozenset[str] = frozenset(
    {
        "EVSE",
        "GFCI",
        "GFI",
        "RECEPT",
        "REC",
        "JBOX",
        "J-BOX",
        "JB",
        "LIGHT",
        "LTG",
        "LUM",
        "DISC",
        "DISCONNECT",
    }
)

_CIRCUIT_RE = re.compile(r"\b(?:CKT|CIRCUIT)\s*#?\s*(?P<number>[A-Z0-9.-]+)\b", re.IGNORECASE)
_HOMERUN_TAG_RE = re.compile(r"(?<![A-Z0-9_.-])(?P<panel>[A-Z][A-Z0-9_.]*?)-(?P<circuits>\d+(?:\s*,\s*\d+)*)\b", re.IGNORECASE)
_TRAILING_CIRCUIT_LIST_RE = re.compile(r"\s*,")
_PANEL_SCHEDULE_HEADING_RE = re.compile(r"\bPANEL\s+(?P<panel>[A-Z][A-Z0-9_.-]*)\s+SCHEDULE\b", re.IGNORECASE)
_PANEL_SCHEDULE_ROW_RE = re.compile(r"^\s*(?P<circuit>\d+)\s+\S", re.IGNORECASE)
_POLES_RE = re.compile(r"\b(?P<poles>[1234])\s*P\b", re.IGNORECASE)
_PHASE_RE = re.compile(r"\b(?P<phase>[123])\s*PH\b", re.IGNORECASE)
_VOLTAGE_RE = re.compile(
    r"\b(?P<v1>\d+(?:\.\d+)?)(?:\s*/\s*(?P<v2>\d+(?:\.\d+)?))?\s*V\b",
    re.IGNORECASE,
)
_MOUNT_IN_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:\"|IN(?:CH(?:ES)?)?)\s*(?:A\.?F\.?F\.?|AFF)\b",
    re.IGNORECASE,
)
_MOUNT_FT_RE = re.compile(
    r"(?P<feet>\d+(?:\.\d+)?)\s*'\s*(?:-\s*(?P<inches>\d+(?:\.\d+)?)\s*\")?\s*(?:A\.?F\.?F\.?|AFF)\b",
    re.IGNORECASE,
)


def _normalize_tag(value: str) -> str:
    return value.strip().strip(".,:;()[]{}").upper()


def _parse_explicit_circuit_tag(
    text: str,
    *,
    recognized_panels: set[str],
    schedule_circuits: Mapping[str, set[int]],
) -> tuple[str | None, tuple[int, ...], str | None]:
    match = _HOMERUN_TAG_RE.search(text)
    if match is None:
        return None, (), "no_panel_token"
    panel_tag = _normalize_tag(match.group("panel"))
    # The circuit group stops at the last numeric list item, so a comma still
    # following the match means the annotation carries a list item we could not
    # parse. ``LP-1,X`` must fail closed rather than silently resolve as
    # ``LP-1``; #72 forbids resolving a circuit the annotation does not state.
    if _TRAILING_CIRCUIT_LIST_RE.match(text[match.end("circuits") :]):
        return panel_tag, (), "unparseable_circuit_list"
    try:
        numbers = tuple(int(item.strip()) for item in match.group("circuits").split(","))
    except ValueError:
        return panel_tag, (), "unparseable_circuit_list"
    if not numbers:
        return panel_tag, (), "unparseable_circuit_list"
    if panel_tag not in recognized_panels:
        return panel_tag, numbers, "panel_id_not_recognized"
    scheduled = schedule_circuits.get(panel_tag)
    if scheduled is not None and any(number not in scheduled for number in numbers):
        return panel_tag, numbers, "circuit_outside_panel_schedule"
    return panel_tag, numbers, None


def _is_explicit_circuit_annotation(
    text: str,
    *,
    recognized_panels: Iterable[str],
) -> bool:
    """Tell a panel/circuit annotation from an ordinary device identity tag.

    ``EVSE-1`` and ``REC-1`` parse the same shape as ``LP-1``, so a homerun
    branch run picks up the device labels sitting beside it and every
    annotation looks ambiguous. A text is an annotation when it names a
    recognized panel, or when it is annotation-like rather than a device tag
    we already recognize, which keeps an unknown panel id diagnosable.
    """
    match = _HOMERUN_TAG_RE.search(text)
    if match is None:
        return False
    if _CIRCUIT_RE.search(text):
        # Legacy explicit CKT/CIRCUIT callouts belong to the circuit-text and
        # topology paths; a load tag embedded in one is not a panel tag.
        return False
    if _normalize_tag(match.group("panel")) in recognized_panels:
        # A recognized panelboard by that name is real corroborating evidence.
        # Whether this particular text is an annotation or a device's own label
        # is decided per resolution site by the self-reference rule, not here:
        # rejecting every device-shaped text outright would make a genuine
        # `REC-1` annotation on a sheet with a panel named `REC` unresolvable.
        return True
    return not _text_entity_hits(text)


def _panel_schedule_circuits(
    texts: Sequence[PdfTextObservation],
    vectors: Sequence[PdfVectorPathObservation],
    *,
    recognized_panels: set[str],
) -> tuple[dict[str, set[int]], set[str]]:
    schedules: dict[str, set[int]] = {}
    consumed_ids: set[str] = set()
    headings: list[tuple[PdfTextObservation, str]] = []
    for heading in texts:
        match = _PANEL_SCHEDULE_HEADING_RE.search(heading.text)
        if match is None:
            continue
        panel_tag = _normalize_tag(match.group("panel"))
        if panel_tag not in recognized_panels:
            continue
        consumed_ids.add(heading.element_id)
        headings.append((heading, panel_tag))
        # A visible schedule heading is evidence of a schedule even when no
        # row can be parsed. An empty parsed set must reject circuit claims,
        # not silently behave like an absent schedule.
        schedules.setdefault(panel_tag, set())

    # A closed row cell inside a separate schedule frame is source evidence
    # of table ownership. Numbering, position, or a boxed detail note alone
    # cannot establish that ownership.
    row_cells: list[tuple[PdfVectorPathObservation, float, float, float, float]] = []
    for vector in vectors:
        if not vector.closed or len(vector.points_pt) != 4:
            continue
        xs = {point[0] for point in vector.points_pt}
        ys = {point[1] for point in vector.points_pt}
        if len(xs) != 2 or len(ys) != 2:
            continue
        if set(vector.points_pt) != {(x, y) for x in xs for y in ys}:
            continue
        row_cells.append((vector, min(xs), min(ys), max(xs), max(ys)))

    heading_ids = {heading.element_id for heading, _tag in headings}
    claimed_cells: dict[str, list[tuple[PdfTextObservation, int, str]]] = {}
    for row in texts:
        if row.element_id in heading_ids:
            continue
        row_match = _PANEL_SCHEDULE_ROW_RE.match(row.text)
        if row_match is None:
            continue
        candidates = [
            (
                heading.element_id,
                panel_tag,
                cell.element_id,
            )
            for heading, panel_tag in headings
            for cell, left, bottom, right, top in row_cells
            if row.page == heading.page == cell.page
            and row.y_pt < heading.y_pt
            and top < heading.y_pt
            and left < heading.x_pt < right
            and left < row.x_pt < right
            and bottom < row.y_pt < top
            and any(
                frame.page == row.page
                and frame.element_id != cell.element_id
                and frame_left == left
                and frame_right == right
                and frame_bottom < bottom < top < frame_top
                and frame_left < heading.x_pt < frame_right
                and frame_bottom < heading.y_pt < frame_top
                for frame, frame_left, frame_bottom, frame_right, frame_top in row_cells
            )
        ]
        if len(candidates) != 1:
            # Overlapping cells or multiple candidate headings cannot prove
            # one owning table, even if one happens to be geometrically closer.
            continue
        claimed_cells.setdefault(candidates[0][2], []).append(
            (row, int(row_match.group("circuit")), candidates[0][1])
        )

    for rows in claimed_cells.values():
        if len(rows) != 1:
            # Two numbered texts in one box are not an unambiguous row.
            continue
        row, circuit, panel_tag = rows[0]
        schedules[panel_tag].add(circuit)
        consumed_ids.add(row.element_id)
    return schedules, consumed_ids


def _homerun_arrowhead_apex(
    vector: PdfVectorPathObservation,
) -> tuple[float, float] | None:
    if len(vector.points_pt) != 3:
        return None
    first, apex, last = vector.points_pt
    left = (first[0] - apex[0], first[1] - apex[1])
    right = (last[0] - apex[0], last[1] - apex[1])
    cross = left[0] * right[1] - left[1] * right[0]
    dot = left[0] * right[0] + left[1] * right[1]
    # An open, nondegenerate V with an acute tip is an arrowhead regardless of
    # its absolute size on the sheet. Length cutoffs rejected lawful arrows.
    return apex if not vector.closed and cross != 0.0 and dot > 0.0 else None


def _component_has_homerun_arrowhead(
    component: Sequence[PdfVectorPathObservation],
    arrowheads: Sequence[PdfVectorPathObservation],
    *,
    tolerance_pt: float,
) -> bool:
    return any(
        any(
            _point_path_distance_pt(point, branch) <= tolerance_pt
            for point in arrowhead.points_pt
            for branch in component
        )
        for arrowhead in arrowheads
        if _homerun_arrowhead_apex(arrowhead) is not None
    )


def _semantic_text(symbol: PdfSymbolObservation) -> str:
    parts = [symbol.name]
    for key in ("subject", "contents", "native_id"):
        value = symbol.metadata.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).replace("/", " ").replace("_", " ").replace("-", " ").upper()


def _classify_semantic_text(
    semantic: str,
    rules: Sequence[SymbolRule],
    *,
    ambiguity_margin: float,
) -> tuple[tuple[str, str, float] | None, list[dict[str, Any]]]:
    matches: dict[tuple[str, str], float] = {}
    for rule in rules:
        if re.search(rule.pattern, semantic, re.IGNORECASE):
            key = (rule.entity_kind, rule.canonical_type)
            matches[key] = max(matches.get(key, 0.0), rule.confidence)

    ranked = sorted(
        (
            {"entity_kind": kind, "canonical_type": canonical_type, "confidence": confidence}
            for (kind, canonical_type), confidence in matches.items()
        ),
        key=lambda item: (-item["confidence"], item["entity_kind"], item["canonical_type"]),
    )
    if not ranked:
        return None, []
    if len(ranked) > 1 and ranked[0]["confidence"] - ranked[1]["confidence"] <= ambiguity_margin:
        return None, ranked
    top = ranked[0]
    return (
        str(top["entity_kind"]),
        str(top["canonical_type"]),
        float(top["confidence"]),
    ), ranked


def _classify_symbol(
    symbol: PdfSymbolObservation,
    rules: Sequence[SymbolRule],
    *,
    ambiguity_margin: float,
) -> tuple[tuple[str, str, float] | None, list[dict[str, Any]]]:
    return _classify_semantic_text(
        _semantic_text(symbol),
        rules,
        ambiguity_margin=ambiguity_margin,
    )


def _text_entity_hits(text: str) -> list[tuple[str, str, str, float]]:
    if re.match(r"\s*(?:NOTE|KEYNOTE|GENERAL\s+NOTE)\b", text, re.IGNORECASE):
        return []
    if _CIRCUIT_RE.search(text):
        # A circuit callout can name equipment and loads without locating them.
        # Keep those names as circuit evidence, but require independent spatial
        # recognition before materializing canonical equipment or devices.
        return []
    hits: list[tuple[str, str, str, float]] = []
    panel = _PANEL_RE.search(text)
    if panel:
        hits.append(("equipment", "panelboard", _normalize_tag(panel.group("tag")), 0.97))
    for pattern, canonical_type in _EQUIPMENT_TEXT_RULES:
        for match in pattern.finditer(text):
            hits.append(("equipment", canonical_type, _normalize_tag(match.group("tag")), 0.95))
    for pattern, canonical_type in _DEVICE_TEXT_RULES:
        for match in pattern.finditer(text):
            hits.append(("device", canonical_type, _normalize_tag(match.group("tag")), 0.95))
    dedup: dict[tuple[str, str, str], float] = {}
    for kind, canonical_type, tag, confidence in hits:
        key = (kind, canonical_type, tag)
        dedup[key] = max(dedup.get(key, 0.0), confidence)
    return [
        (kind, canonical_type, tag, confidence)
        for (kind, canonical_type, tag), confidence in sorted(dedup.items())
    ]


def _distance_pt(a_x: float, a_y: float, b_x: float, b_y: float) -> float:
    return math.hypot(a_x - b_x, a_y - b_y)


def _vector_contains_bezier(observation: PdfVectorPathObservation) -> bool:
    return bool(observation.metadata.get("curve_commands"))


def _vector_segments(
    observation: PdfVectorPathObservation,
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    segments = list(zip(observation.points_pt, observation.points_pt[1:]))
    if observation.closed:
        segments.append((observation.points_pt[-1], observation.points_pt[0]))
    return tuple(segments)


def _point_segment_distance_pt(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return _distance_pt(point[0], point[1], first[0], first[1])
    t = max(
        0.0,
        min(
            1.0,
            ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
            / length_squared,
        ),
    )
    projected = (first[0] + t * dx, first[1] + t * dy)
    return _distance_pt(point[0], point[1], projected[0], projected[1])


def _point_path_distance_pt(
    point: tuple[float, float],
    observation: PdfVectorPathObservation,
) -> float:
    return min(
        _point_segment_distance_pt(point, first, second)
        for first, second in _vector_segments(observation)
    )


def _paths_touch(
    first: PdfVectorPathObservation,
    second: PdfVectorPathObservation,
    *,
    tolerance_pt: float,
) -> bool:
    if first.page != second.page:
        return False
    first_endpoints = (first.points_pt[0], first.points_pt[-1])
    second_endpoints = (second.points_pt[0], second.points_pt[-1])
    return any(
        _point_path_distance_pt(endpoint, second) <= tolerance_pt
        for endpoint in first_endpoints
    ) or any(
        _point_path_distance_pt(endpoint, first) <= tolerance_pt
        for endpoint in second_endpoints
    )


def _paths_cross_or_touch(
    first: PdfVectorPathObservation,
    second: PdfVectorPathObservation,
    *,
    tolerance_pt: float,
) -> bool:
    """Include mid-segment crossings, which endpoint-only contact misses."""
    if first.page != second.page:
        return False

    def side(
        start: tuple[float, float],
        end: tuple[float, float],
        point: tuple[float, float],
    ) -> float:
        return (
            (end[0] - start[0]) * (point[1] - start[1])
            - (end[1] - start[1]) * (point[0] - start[0])
        )

    for a, b in _vector_segments(first):
        for c, d in _vector_segments(second):
            if (
                max(a[0], b[0]) + tolerance_pt < min(c[0], d[0])
                or max(c[0], d[0]) + tolerance_pt < min(a[0], b[0])
                or max(a[1], b[1]) + tolerance_pt < min(c[1], d[1])
                or max(c[1], d[1]) + tolerance_pt < min(a[1], b[1])
            ):
                continue
            if any(
                _point_segment_distance_pt(point, start, end) <= tolerance_pt
                for point, start, end in ((a, c, d), (b, c, d), (c, a, b), (d, a, b))
            ):
                return True

            if (
                side(a, b, c) * side(a, b, d) < 0
                and side(c, d, a) * side(c, d, b) < 0
            ):
                return True
    return False


def _simple_rectangle_marker(
    observation: PdfVectorPathObservation,
) -> tuple[float, float, float] | None:
    if _vector_contains_bezier(observation):
        return None
    if not observation.closed or len(observation.points_pt) != 4:
        return None
    points = observation.points_pt
    edges = [
        (points[(index + 1) % 4][0] - points[index][0],
         points[(index + 1) % 4][1] - points[index][1])
        for index in range(4)
    ]
    lengths = [math.hypot(dx, dy) for dx, dy in edges]
    if min(lengths) < 2.0 or max(lengths) > 72.0:
        return None
    if abs(lengths[0] - lengths[2]) > 0.5 or abs(lengths[1] - lengths[3]) > 0.5:
        return None
    for first, second in zip(edges, edges[1:] + edges[:1]):
        dot = first[0] * second[0] + first[1] * second[1]
        if abs(dot) > 1e-3 * math.hypot(*first) * math.hypot(*second):
            return None
    center_x = sum(point[0] for point in points) / 4.0
    center_y = sum(point[1] for point in points) / 4.0
    half_diagonal = 0.5 * math.hypot(lengths[0], lengths[1])
    return center_x, center_y, half_diagonal


def _merge_provenance(
    *groups: Iterable[Provenance],
) -> tuple[Provenance, ...]:
    unique: dict[tuple[str, str, str | None, int | None, str | None], Provenance] = {}
    for group in groups:
        for item in group:
            key = (
                item.source_kind,
                item.source_id,
                item.source_element_id,
                item.page,
                item.method,
            )
            unique.setdefault(key, item)
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.page or 0,
                item.source_element_id or "",
                item.method or "",
                item.source_kind,
            ),
        )
    )



_GLYPH_PATH_MAX_EXTENT_PT = 54.0
_GLYPH_CLUSTER_GAP_PT = 2.5
_LEGEND_LABEL_HORIZONTAL_DISTANCE_PT = 120.0
_LEGEND_ROW_VERTICAL_TOLERANCE_PT = 12.0
_LEGEND_TITLE_REGION_RADIUS_PT = 320.0
_LEGEND_LEADER_ENDPOINT_RADIUS_PT = 54.0
_LEGEND_TABLE_COLUMN_TOLERANCE_PT = 24.0
_LEGEND_TABLE_LABEL_COLUMN_TOLERANCE_PT = 48.0
_LEGEND_TABLE_ROW_GAP_PT = 84.0
_LEGEND_TABLE_MIN_ROWS = 3
_LEGEND_MAX_LABEL_CHARS = 48
_LEGEND_MAX_LABEL_WORDS = 6
_LEGEND_SECTION_HEADING_MAX_CHARS = 80
_LEGEND_SECTION_HEADING_MAX_WORDS = 8
_LEGEND_HEADER_ROW_VERTICAL_TOLERANCE_PT = 10.0
_LEGEND_RULE_AXIS_TOLERANCE_PT = 1.5
_LEGEND_RULE_EDGE_TOLERANCE_PT = 12.0
_LEGEND_TABLE_HEADER_MAX_HEIGHT_PT = 42.0
_LEGEND_TABLE_MAX_ROW_HEIGHT_PT = 60.0
_PAGE_FRAME_MIN_MEDIA_AREA_RATIO = 0.85
_PAGE_FRAME_MIN_DIMENSION_PT = 300.0
_NOTES_COLUMN_START_FRACTION = 0.68
_NOTES_TITLE_BAND_MAX_FRACTION = 0.12
_FIELD_STATUS_RADIUS_PT = 28.0
_FIELD_STATUS_AMBIGUITY_PT = 2.0
_GLYPH_MATCH_ABSOLUTE_FLOOR = 0.45
_GLYPH_MATCH_SCORE_MIN = 0.55
_GLYPH_MATCH_MARGIN_MIN = 0.12
_GLYPH_MATCH_STRONG_SCORE = 0.75
_GLYPH_MATCH_NEAR_TIE_MARGIN = _GLYPH_MATCH_MARGIN_MIN
_GLYPH_RESAMPLE_STEP = 0.04
_GLYPH_CHAMFER_DISTANCE_SCALE = 0.30
_ANNOTATION_CODE_MAX_CHARS = 16
_UNREGISTERED_PAGE_TILE_OFFSET_M = 100.0
_LEGEND_TITLE_WORDS = frozenset({"LEGEND", "SYMBOL", "SYMBOLS"})
_LEGEND_REJECTED_HEADING_WORDS = frozenset(
    {
        "KEYNOTE",
        "KEYNOTES",
        "SCHEDULE",
        "PANEL",
        "NOTES",
        "ABBREVIATIONS",
        "DETAIL",
    }
)
_LEGEND_SHEET_ID_RE = re.compile(
    r"\bE(?:-\d{1,4}|\d{1,3}(?:\.\d{1,3})?)\b",
    re.IGNORECASE,
)
_LEGEND_REFERENCE_PREFIX_RE = re.compile(
    r"^\s*(?:SEE|REFER(?:\s+TO)?|REFERENCE)\b",
    re.IGNORECASE,
)
_LEGEND_REFERENCE_CUE_RE = re.compile(
    r"\b(?:SEE|REFER(?:\s+TO)?|REFERENCE|LEGEND\s+(?:ON|AT|SHEET))\b",
    re.IGNORECASE,
)
_FIELD_STATUS_RE = re.compile(r"^\s*(?P<status>[ENR])\s*$", re.IGNORECASE)
_FIELD_HEIGHT_TAG_RE = re.compile(
    r'^\s*\+\s*\d+(?:\.\d+)?\s*(?:"|IN(?:CH(?:ES)?)?)?\s*$',
    re.IGNORECASE,
)
_FIELD_CIRCUIT_COUNT_RE = re.compile(r"^\s*#?\s*\d+\s*$")
_FIELD_STATUS_MEANINGS: Mapping[str, str] = {
    "E": "existing_to_remain",
    "N": "new",
    "R": "existing_to_be_removed",
}


def _vector_bbox(
    observation: PdfVectorPathObservation,
) -> tuple[float, float, float, float]:
    xs = [point[0] for point in observation.points_pt]
    ys = [point[1] for point in observation.points_pt]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _bbox_gap_pt(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def _paint_family(observation: PdfVectorPathObservation) -> str:
    operator = str(observation.metadata.get("paint_operator") or "")
    if operator in {"f", "F", "f*"}:
        return "fill"
    if operator in {"b", "b*", "B", "B*"}:
        return "fill-stroke"
    return "stroke"


def _canonical_point_sequence(
    points: tuple[tuple[float, float], ...],
    *,
    closed: bool,
) -> tuple[tuple[float, float], ...]:
    if not points:
        return ()
    if not closed:
        reversed_points = tuple(reversed(points))
        return min(points, reversed_points)

    candidates: list[tuple[tuple[float, float], ...]] = []
    for source in (points, tuple(reversed(points))):
        for offset in range(len(source)):
            candidates.append(source[offset:] + source[:offset])
    return min(candidates)


def _cluster_shape_signature(
    vectors: Sequence[PdfVectorPathObservation],
    bbox: tuple[float, float, float, float],
) -> str:
    center_x = (bbox[0] + bbox[2]) / 2.0
    center_y = (bbox[1] + bbox[3]) / 2.0
    scale = max(bbox[2] - bbox[0], bbox[3] - bbox[1], 1e-9)
    variants: list[tuple[Any, ...]] = []

    # One reflection plus quarter turns spans mirroring across either axis.
    for mirrored in (False, True):
        for rotation in range(4):
            records: list[tuple[Any, ...]] = []
            for vector in vectors:
                normalized: list[tuple[float, float]] = []
                for x_pt, y_pt in vector.points_pt:
                    x = (x_pt - center_x) / scale
                    y = (y_pt - center_y) / scale
                    if mirrored:
                        x = -x
                    if rotation == 1:
                        x, y = -y, x
                    elif rotation == 2:
                        x, y = -x, -y
                    elif rotation == 3:
                        x, y = y, -x
                    normalized.append((round(x, 4), round(y, 4)))
                points = _canonical_point_sequence(
                    tuple(normalized),
                    closed=vector.closed,
                )
                records.append(
                    (
                        _paint_family(vector),
                        vector.closed,
                        _vector_contains_bezier(vector),
                        points,
                    )
                )
            variants.append(tuple(sorted(records)))

    canonical = min(variants)
    return hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()[:24]


def _cluster_geometry_key(
    vectors: Sequence[PdfVectorPathObservation],
) -> str:
    records: list[tuple[Any, ...]] = []
    for vector in vectors:
        rounded = tuple(
            (round(point[0], 3), round(point[1], 3))
            for point in vector.points_pt
        )
        records.append(
            (
                _paint_family(vector),
                vector.closed,
                _vector_contains_bezier(vector),
                _canonical_point_sequence(rounded, closed=vector.closed),
            )
        )
    canonical = tuple(sorted(records))
    return hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()[:24]


def _transform_normalized_point(
    point: tuple[float, float],
    *,
    center_x: float,
    center_y: float,
    scale: float,
    mirrored: bool,
    rotation: int,
) -> tuple[float, float]:
    x = (point[0] - center_x) / scale
    y = (point[1] - center_y) / scale
    if mirrored:
        x = -x
    if rotation == 1:
        x, y = -y, x
    elif rotation == 2:
        x, y = -x, -y
    elif rotation == 3:
        x, y = y, -x
    return x, y


def _resampled_point_cloud(
    cluster: _VectorCluster,
    *,
    mirrored: bool = False,
    rotation: int = 0,
) -> tuple[tuple[float, float], ...]:
    """Normalize a glyph and sample strokes at a fixed geometric density."""

    vectors = cluster.vectors
    bbox = cluster.bbox_pt
    center_x = (bbox[0] + bbox[2]) / 2.0
    center_y = (bbox[1] + bbox[3]) / 2.0
    scale = max(bbox[2] - bbox[0], bbox[3] - bbox[1], 1e-9)
    cloud: list[tuple[float, float]] = []

    for vector in vectors:
        transformed = [
            _transform_normalized_point(
                point,
                center_x=center_x,
                center_y=center_y,
                scale=scale,
                mirrored=mirrored,
                rotation=rotation,
            )
            for point in vector.points_pt
        ]
        segments = list(zip(transformed, transformed[1:]))
        if vector.closed and len(transformed) > 2:
            segments.append((transformed[-1], transformed[0]))
        for first, second in segments:
            length = _distance_pt(first[0], first[1], second[0], second[1])
            sample_count = max(1, math.ceil(length / _GLYPH_RESAMPLE_STEP))
            for index in range(sample_count):
                fraction = index / sample_count
                cloud.append(
                    (
                        first[0] + (second[0] - first[0]) * fraction,
                        first[1] + (second[1] - first[1]) * fraction,
                    )
                )
        if transformed:
            cloud.append(transformed[-1])

    # Coordinate rounding collapses duplicate points caused by stroke splitting
    # without making the score sensitive to PDF operator boundaries.
    return tuple(sorted({(round(x, 5), round(y, 5)) for x, y in cloud}))


def _point_cloud_distance(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> float:
    if not first or not second:
        return 1.0

    def directed(
        source: Sequence[tuple[float, float]],
        target: Sequence[tuple[float, float]],
    ) -> tuple[float, float]:
        nearest = [
            min(
                math.hypot(point[0] - other[0], point[1] - other[1])
                for other in target
            )
            for point in source
        ]
        nearest.sort()
        return sum(nearest) / len(nearest), nearest[-1]

    first_mean, first_hausdorff = directed(first, second)
    second_mean, second_hausdorff = directed(second, first)
    chamfer = (first_mean + second_mean) / 2.0
    hausdorff = max(first_hausdorff, second_hausdorff)
    return 0.55 * chamfer + 0.45 * hausdorff


def _cluster_match_score(
    cluster: _VectorCluster,
    prototype: _VectorCluster,
) -> float:
    if cluster.shape_signature == prototype.shape_signature:
        return 1.0

    prototype_cloud = _resampled_point_cloud(prototype)
    distance = min(
        _point_cloud_distance(
            _resampled_point_cloud(
                cluster,
                mirrored=mirrored,
                rotation=rotation,
            ),
            prototype_cloud,
        )
        for mirrored in (False, True)
        for rotation in range(4)
    )
    return round(
        max(0.0, min(1.0, 1.0 - distance / _GLYPH_CHAMFER_DISTANCE_SCALE)),
        6,
    )


def _comparison_stroke_counts(cluster: _VectorCluster) -> tuple[int, int]:
    vectors = cluster.vectors
    segment_count = sum(
        max(1, len(vector.points_pt) - 1 + int(vector.closed))
        for vector in vectors
    )
    return len(vectors), segment_count


def _prototype_diagnostic(entry: _LegendEntry) -> dict[str, Any]:
    vector_count, segment_count = _comparison_stroke_counts(entry.prototype)
    return {
        "entity_kind": entry.entity_kind,
        "canonical_type": entry.canonical_type,
        "legend_page": entry.label.page,
        "legend_label": entry.label.text,
        "prototype_geometry_key": entry.prototype.geometry_key,
        "stroke_count": vector_count,
        "segment_count": segment_count,
    }


def _cluster_bbox_size_pt(cluster: _VectorCluster) -> dict[str, float]:
    return {
        "width": round(cluster.bbox_pt[2] - cluster.bbox_pt[0], 6),
        "height": round(cluster.bbox_pt[3] - cluster.bbox_pt[1], 6),
    }


def _cluster_contains_nonmodifier_text(
    cluster: _VectorCluster,
    texts: Sequence[PdfTextObservation],
) -> bool:
    for observation in texts:
        if observation.page != cluster.page:
            continue
        if (
            _field_modifier_text(observation.text) is not None
            or _STRIPPABLE_FIELD_TEXT_RE.fullmatch(" ".join(observation.text.split()))
        ):
            continue
        if (
            cluster.bbox_pt[0] <= observation.x_pt <= cluster.bbox_pt[2]
            and cluster.bbox_pt[1] <= observation.y_pt <= cluster.bbox_pt[3]
        ):
            return True
    return False


def _cluster_scale_ratio(
    cluster: _VectorCluster,
    prototype: _VectorCluster,
) -> float:
    candidate_bbox = cluster.bbox_pt
    prototype_bbox = prototype.bbox_pt
    candidate_extent = max(
        candidate_bbox[2] - candidate_bbox[0],
        candidate_bbox[3] - candidate_bbox[1],
        1e-9,
    )
    prototype_extent = max(
        prototype_bbox[2] - prototype_bbox[0],
        prototype_bbox[3] - prototype_bbox[1],
        1e-9,
    )
    return candidate_extent / prototype_extent


def _unresolved_match_reason(
    cluster: _VectorCluster,
    prototype: _LegendEntry | None,
    texts: Sequence[PdfTextObservation],
) -> str:
    if _cluster_contains_nonmodifier_text(cluster, texts):
        return "contains text"
    if prototype is not None:
        scale_ratio = _cluster_scale_ratio(cluster, prototype.prototype)
        if scale_ratio > 2.5:
            return "cluster too large"
        if scale_ratio < 0.4:
            return "cluster too small"
    return "below threshold"


def _match_cluster_to_legend_entries(
    cluster: _VectorCluster,
    entries: Sequence[_LegendEntry],
    *,
    texts: Sequence[PdfTextObservation] = (),
) -> tuple[_LegendEntry | None, dict[str, Any]]:
    candidate_strokes = _comparison_stroke_counts(cluster)
    base_diagnostics: dict[str, Any] = {
        "nearest_type": None,
        "nearest_score": None,
        "second_type": None,
        "second_score": None,
        "threshold": _GLYPH_MATCH_SCORE_MIN,
        "absolute_floor": _GLYPH_MATCH_ABSOLUTE_FLOOR,
        "margin_threshold": _GLYPH_MATCH_MARGIN_MIN,
        "strong_score_threshold": _GLYPH_MATCH_STRONG_SCORE,
        "margin": None,
        "confidence": {"score": None, "margin": None},
        "reason": None,
        "bbox_size_pt": _cluster_bbox_size_pt(cluster),
        "stroke_count": candidate_strokes[0],
        "segment_count": candidate_strokes[1],
        "stripped_text_tags": list(cluster.stripped_text_tags),
        "cleanup_actions": list(cluster.cleanup_actions),
    }
    if not entries:
        return None, {
            **base_diagnostics,
            "reason": _unresolved_match_reason(cluster, None, texts),
            "nearest_prototype": None,
            "score": None,
            "second_best": None,
            "non_unique_reason": "no classified legend prototypes are available",
        }

    ranked: list[
        tuple[
            float,
            tuple[int, int],
            str,
            str,
            _LegendEntry,
        ]
    ] = []
    for entry in entries:
        prototype_strokes = _comparison_stroke_counts(entry.prototype)
        stroke_delta = (
            abs(candidate_strokes[0] - prototype_strokes[0]),
            abs(candidate_strokes[1] - prototype_strokes[1]),
        )
        ranked.append(
            (
                _cluster_match_score(cluster, entry.prototype),
                stroke_delta,
                entry.canonical_type,
                entry.label.element_id,
                entry,
            )
        )
    ranked.sort(
        key=lambda item: (
            -item[0],
            item[1],
            item[2],
            item[3],
            item[4].prototype.geometry_key,
        )
    )

    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = round(
        best[0] - (second[0] if second is not None else 0.0),
        6,
    )
    diagnostics: dict[str, Any] = {
        **base_diagnostics,
        "nearest_type": best[4].canonical_type,
        "nearest_score": best[0],
        "second_type": second[4].canonical_type if second is not None else None,
        "second_score": second[0] if second is not None else None,
        "margin": margin,
        "confidence": {"score": best[0], "margin": margin},
        "nearest_prototype": _prototype_diagnostic(best[4]),
        "score": best[0],
        "second_best": (
            {
                "prototype": _prototype_diagnostic(second[4]),
                "score": second[0],
            }
            if second is not None
            else None
        ),
        "non_unique_reason": None,
    }

    if best[0] < _GLYPH_MATCH_ABSOLUTE_FLOOR:
        diagnostics["reason"] = _unresolved_match_reason(
            cluster,
            best[4],
            texts,
        )
        diagnostics["non_unique_reason"] = (
            "nearest prototype score is below the absolute match floor"
        )
        return None, diagnostics

    if best[0] >= _GLYPH_MATCH_STRONG_SCORE:
        return best[4], diagnostics

    if best[0] >= _GLYPH_MATCH_SCORE_MIN and margin >= _GLYPH_MATCH_MARGIN_MIN:
        return best[4], diagnostics

    if best[0] < _GLYPH_MATCH_SCORE_MIN:
        diagnostics["reason"] = _unresolved_match_reason(
            cluster,
            best[4],
            texts,
        )
        diagnostics["non_unique_reason"] = (
            "nearest prototype score is below the margin-rule match minimum"
        )
        return None, diagnostics

    near = [
        item
        for item in ranked
        if best[0] - item[0] < _GLYPH_MATCH_MARGIN_MIN
        and item[0] >= _GLYPH_MATCH_ABSOLUTE_FLOOR
    ]
    competing_types = {
        (item[4].entity_kind, item[4].canonical_type)
        for item in near
    }
    if len(competing_types) <= 1:
        return best[4], diagnostics

    best_stroke_delta = min(item[1] for item in near)
    stroke_winners = [
        item for item in near if item[1] == best_stroke_delta
    ]
    winner_types = {
        (item[4].entity_kind, item[4].canonical_type)
        for item in stroke_winners
    }
    if len(stroke_winners) == 1 or len(winner_types) == 1:
        winner = sorted(
            stroke_winners,
            key=lambda item: (
                -item[0],
                item[2],
                item[3],
                item[4].prototype.geometry_key,
            ),
        )[0]
        if winner[0] >= _GLYPH_MATCH_SCORE_MIN:
            diagnostics["tie_breaker"] = "differentiating-stroke-count"
            diagnostics["selected_type"] = winner[4].canonical_type
            diagnostics["selected_score"] = winner[0]
            return winner[4], diagnostics

    diagnostics["reason"] = "tie within margin"
    diagnostics["non_unique_reason"] = (
        "near-tied legend prototypes remain non-unique after differentiating "
        "stroke-count comparison"
    )
    return None, diagnostics


_STRIPPABLE_FIELD_TEXT_RE = re.compile(r"""^[\sENR0-9+#"'.,:/-]+$""", re.IGNORECASE)


def _cluster_extent_pt(cluster: _VectorCluster) -> float:
    return max(
        cluster.bbox_pt[2] - cluster.bbox_pt[0],
        cluster.bbox_pt[3] - cluster.bbox_pt[1],
    )


def _make_vector_cluster(
    vectors: Sequence[PdfVectorPathObservation],
    *,
    stripped_text_tags: Sequence[str] = (),
    cleanup_actions: Sequence[str] = (),
    excluded_source_element_ids: Sequence[str] = (),
) -> _VectorCluster:
    ordered = tuple(
        sorted(
            vectors,
            key=lambda vector: (
                _vector_bbox(vector),
                _cluster_geometry_key((vector,)),
                vector.element_id,
            ),
        )
    )
    bbox = _vector_bbox(ordered[0])
    for vector in ordered[1:]:
        bbox = _bbox_union(bbox, _vector_bbox(vector))
    return _VectorCluster(
        page=ordered[0].page,
        vectors=ordered,
        bbox_pt=bbox,
        center_pt=((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
        shape_signature=_cluster_shape_signature(ordered, bbox),
        geometry_key=_cluster_geometry_key(ordered),
        stripped_text_tags=tuple(sorted(set(stripped_text_tags))),
        cleanup_actions=tuple(dict.fromkeys(cleanup_actions)),
        excluded_source_element_ids=tuple(sorted(set(excluded_source_element_ids))),
    )


def _strippable_text_tags(
    cluster: _VectorCluster,
    texts: Sequence[PdfTextObservation],
) -> tuple[PdfTextObservation, ...]:
    matched: list[PdfTextObservation] = []
    for observation in texts:
        if observation.page != cluster.page:
            continue
        cleaned = " ".join(observation.text.split())
        if not cleaned or not _STRIPPABLE_FIELD_TEXT_RE.fullmatch(cleaned):
            continue
        if len(cleaned) > 12:
            continue
        pad = max(1.0, float(observation.font_size_pt or 0.0) * 0.25)
        if (
            cluster.bbox_pt[0] - pad <= observation.x_pt <= cluster.bbox_pt[2] + pad
            and cluster.bbox_pt[1] - pad <= observation.y_pt <= cluster.bbox_pt[3] + pad
        ):
            matched.append(observation)
    return tuple(
        sorted(
            matched,
            key=lambda item: (item.element_id, item.text),
        )
    )


def _is_leader_vector(
    vector: PdfVectorPathObservation,
    *,
    glyph_extent_pt: float,
) -> bool:
    if (
        vector.closed
        or _vector_contains_bezier(vector)
        or _paint_family(vector) != "stroke"
    ):
        return False
    bbox = _vector_bbox(vector)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    extent = max(width, height)
    thickness = min(width, height)
    length = sum(
        _distance_pt(first[0], first[1], second[0], second[1])
        for first, second in _vector_segments(vector)
    )
    return (
        extent >= 1.60 * glyph_extent_pt
        and thickness <= max(1.0, 0.15 * glyph_extent_pt)
        and length >= 1.60 * glyph_extent_pt
    )


def _strip_cluster_for_matching(
    cluster: _VectorCluster,
    *,
    texts: Sequence[PdfTextObservation],
    glyph_extent_pt: float,
) -> _VectorCluster:
    tags = _strippable_text_tags(cluster, texts)
    excluded: set[str] = set(cluster.excluded_source_element_ids)
    actions = list(cluster.cleanup_actions)

    if _cluster_extent_pt(cluster) > 1.50 * glyph_extent_pt:
        leaders = {
            vector.element_id
            for vector in cluster.vectors
            if _is_leader_vector(vector, glyph_extent_pt=glyph_extent_pt)
        }
        if leaders and len(leaders) < len(cluster.vectors):
            excluded.update(leaders)
            actions.append("removed-leader-lines")

    if tags:
        text_vector_ids: set[str] = set()
        for observation in tags:
            radius = max(3.5, float(observation.font_size_pt or 5.0))
            max_extent = max(3.0, 0.55 * glyph_extent_pt)
            for vector in cluster.vectors:
                if vector.element_id in excluded:
                    continue
                bbox = _vector_bbox(vector)
                extent = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
                center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
                if (
                    extent <= max_extent
                    and _distance_pt(
                        center[0],
                        center[1],
                        observation.x_pt,
                        observation.y_pt,
                    )
                    <= radius
                ):
                    text_vector_ids.add(vector.element_id)
        if text_vector_ids and len(excluded | text_vector_ids) < len(cluster.vectors):
            excluded.update(text_vector_ids)
            actions.append("stripped-text-glyphs")
        actions.append("recorded-stripped-text-tags")

    kept = [
        vector for vector in cluster.vectors
        if vector.element_id not in excluded
    ]
    if not kept:
        kept = list(cluster.vectors)
        excluded.clear()
    return _make_vector_cluster(
        kept,
        stripped_text_tags=(
            *cluster.stripped_text_tags,
            *(item.text for item in tags),
        ),
        cleanup_actions=actions,
        excluded_source_element_ids=excluded,
    )


def _split_connected_components(
    cluster: _VectorCluster,
    *,
    glyph_extent_pt: float,
    gap_pt: float = 0.75,
) -> tuple[_VectorCluster, ...]:
    if len(cluster.vectors) <= 1:
        return (cluster,)
    parent = list(range(len(cluster.vectors)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if first_root < second_root:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    bboxes = [_vector_bbox(vector) for vector in cluster.vectors]
    for first in range(len(cluster.vectors)):
        for second in range(first + 1, len(cluster.vectors)):
            if _bbox_gap_pt(bboxes[first], bboxes[second]) <= gap_pt:
                union(first, second)

    raw_groups: dict[int, list[PdfVectorPathObservation]] = {}
    for index, vector in enumerate(cluster.vectors):
        raw_groups.setdefault(find(index), []).append(vector)
    if len(raw_groups) <= 1:
        return (cluster,)

    components = [
        _make_vector_cluster(group)
        for _root, group in sorted(raw_groups.items())
    ]
    anchors = [
        component
        for component in components
        if _cluster_extent_pt(component) >= 0.60 * glyph_extent_pt
    ]
    if not anchors:
        return (cluster,)

    grouped: dict[str, list[PdfVectorPathObservation]] = {
        anchor.geometry_key: list(anchor.vectors)
        for anchor in anchors
    }
    unassigned: list[_VectorCluster] = []
    for component in components:
        if component in anchors:
            continue
        candidates: list[tuple[float, _VectorCluster]] = []
        for anchor in anchors:
            pad = 0.15 * glyph_extent_pt
            center_x, center_y = component.center_pt
            if (
                anchor.bbox_pt[0] - pad <= center_x <= anchor.bbox_pt[2] + pad
                and anchor.bbox_pt[1] - pad <= center_y <= anchor.bbox_pt[3] + pad
            ):
                candidates.append(
                    (
                        _distance_pt(
                            center_x,
                            center_y,
                            anchor.center_pt[0],
                            anchor.center_pt[1],
                        ),
                        anchor,
                    )
                )
        if candidates:
            _distance, anchor = min(
                candidates,
                key=lambda item: (item[0], item[1].geometry_key),
            )
            grouped[anchor.geometry_key].extend(component.vectors)
        else:
            unassigned.append(component)

    output = [
        _make_vector_cluster(
            vectors,
            stripped_text_tags=cluster.stripped_text_tags,
            cleanup_actions=(
                *cluster.cleanup_actions,
                "split-oversized-connected-components",
            ),
            excluded_source_element_ids=cluster.excluded_source_element_ids,
        )
        for _key, vectors in sorted(grouped.items())
    ]
    output.extend(
        _make_vector_cluster(
            component.vectors,
            stripped_text_tags=cluster.stripped_text_tags,
            cleanup_actions=(
                *cluster.cleanup_actions,
                "split-oversized-connected-components",
            ),
            excluded_source_element_ids=cluster.excluded_source_element_ids,
        )
        for component in unassigned
    )
    if len(output) <= 1:
        return (cluster,)
    return tuple(
        sorted(
            output,
            key=lambda item: (item.bbox_pt, item.geometry_key),
        )
    )


def _merge_undersized_clusters(
    clusters: Sequence[_VectorCluster],
    *,
    prototype_extents: Sequence[float],
) -> tuple[_VectorCluster, ...]:
    if not clusters or not prototype_extents:
        return tuple(clusters)
    smallest_prototype = min(prototype_extents)
    glyph_width = max(prototype_extents)
    undersized_limit = 0.40 * smallest_prototype
    remaining = list(clusters)
    result: list[_VectorCluster] = []

    while remaining:
        cluster = remaining.pop(0)
        if _cluster_extent_pt(cluster) >= undersized_limit or not remaining:
            result.append(cluster)
            continue
        candidates = [
            (index, other)
            for index, other in enumerate(remaining)
            if other.page == cluster.page
            and _bbox_gap_pt(cluster.bbox_pt, other.bbox_pt) <= glyph_width
        ]
        if not candidates:
            result.append(cluster)
            continue
        index, neighbor = min(
            candidates,
            key=lambda item: (
                _cluster_extent_pt(item[1]) >= undersized_limit,
                _bbox_gap_pt(cluster.bbox_pt, item[1].bbox_pt),
                _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    item[1].center_pt[0],
                    item[1].center_pt[1],
                ),
                item[1].geometry_key,
            ),
        )
        remaining.pop(index)
        result.append(
            _make_vector_cluster(
                (*cluster.vectors, *neighbor.vectors),
                stripped_text_tags=(
                    *cluster.stripped_text_tags,
                    *neighbor.stripped_text_tags,
                ),
                cleanup_actions=(
                    *cluster.cleanup_actions,
                    *neighbor.cleanup_actions,
                    "merged-undersized-neighbor",
                ),
                excluded_source_element_ids=(
                    *cluster.excluded_source_element_ids,
                    *neighbor.excluded_source_element_ids,
                ),
            )
        )

    return tuple(
        sorted(
            result,
            key=lambda item: (item.page, item.bbox_pt, item.geometry_key),
        )
    )


def _prepare_field_clusters(
    clusters: Sequence[_VectorCluster],
    *,
    prototype_geometry_keys: set[tuple[int, str]],
    legend_entries_by_page: Mapping[int, Sequence[_LegendEntry]],
    references_by_page: Mapping[int, Sequence[_LegendReference]],
    texts: Sequence[PdfTextObservation],
) -> tuple[_VectorCluster, ...]:
    entries_for_page: dict[int, tuple[_LegendEntry, ...]] = {}
    for cluster in clusters:
        page_entries = list(legend_entries_by_page.get(cluster.page, ()))
        for reference in references_by_page.get(cluster.page, ()):
            page_entries.extend(
                legend_entries_by_page.get(reference.legend_page, ())
            )
        entries_for_page[cluster.page] = tuple(
            sorted(
                {
                    (
                        entry.canonical_type,
                        entry.label.element_id,
                        entry.prototype.geometry_key,
                    ): entry
                    for entry in page_entries
                }.values(),
                key=lambda entry: (
                    entry.canonical_type,
                    entry.label.element_id,
                    entry.prototype.geometry_key,
                ),
            )
        )

    prepared: list[_VectorCluster] = []
    extents_by_page: dict[int, tuple[float, ...]] = {}
    for page, entries in entries_for_page.items():
        extents_by_page[page] = tuple(
            _cluster_extent_pt(entry.prototype)
            for entry in entries
        )

    for cluster in clusters:
        if (cluster.page, cluster.geometry_key) in prototype_geometry_keys:
            continue
        extents = extents_by_page.get(cluster.page, ())
        if not extents:
            prepared.append(cluster)
            continue
        glyph_extent = max(extents)
        cleaned = _strip_cluster_for_matching(
            cluster,
            texts=texts,
            glyph_extent_pt=glyph_extent,
        )
        if _cluster_extent_pt(cleaned) > 2.5 * glyph_extent:
            prepared.extend(
                _split_connected_components(
                    cleaned,
                    glyph_extent_pt=glyph_extent,
                )
            )
        else:
            prepared.append(cleaned)

    merged: list[_VectorCluster] = []
    for page in sorted({cluster.page for cluster in prepared}):
        page_clusters = [
            cluster for cluster in prepared
            if cluster.page == page
        ]
        merged.extend(
            _merge_undersized_clusters(
                page_clusters,
                prototype_extents=extents_by_page.get(page, ()),
            )
        )
    return tuple(
        sorted(
            merged,
            key=lambda item: (item.page, item.bbox_pt, item.geometry_key),
        )
    )


def _cluster_small_vector_glyphs(
    vectors: Sequence[PdfVectorPathObservation],
) -> tuple[_VectorCluster, ...]:
    small: list[
        tuple[
            PdfVectorPathObservation,
            tuple[float, float, float, float],
        ]
    ] = []
    for vector in vectors:
        bbox = _vector_bbox(vector)
        if (
            bbox[2] - bbox[0] <= _GLYPH_PATH_MAX_EXTENT_PT
            and bbox[3] - bbox[1] <= _GLYPH_PATH_MAX_EXTENT_PT
        ):
            small.append((vector, bbox))

    small.sort(
        key=lambda item: (
            item[0].page,
            round(item[1][0], 6),
            round(item[1][1], 6),
            round(item[1][2], 6),
            round(item[1][3], 6),
            _cluster_geometry_key((item[0],)),
        )
    )
    parent = list(range(len(small)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)
        if first_root == second_root:
            return
        if first_root < second_root:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    cell_size = _GLYPH_PATH_MAX_EXTENT_PT + _GLYPH_CLUSTER_GAP_PT
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for second_index, (second, second_bbox) in enumerate(small):
        min_cell_x = math.floor(
            (second_bbox[0] - _GLYPH_CLUSTER_GAP_PT) / cell_size
        )
        max_cell_x = math.floor(
            (second_bbox[2] + _GLYPH_CLUSTER_GAP_PT) / cell_size
        )
        min_cell_y = math.floor(
            (second_bbox[1] - _GLYPH_CLUSTER_GAP_PT) / cell_size
        )
        max_cell_y = math.floor(
            (second_bbox[3] + _GLYPH_CLUSTER_GAP_PT) / cell_size
        )
        nearby_indexes: set[int] = set()
        for cell_x in range(min_cell_x, max_cell_x + 1):
            for cell_y in range(min_cell_y, max_cell_y + 1):
                nearby_indexes.update(
                    buckets.get((second.page, cell_x, cell_y), ())
                )

        for first_index in sorted(nearby_indexes):
            first, first_bbox = small[first_index]
            combined = _bbox_union(first_bbox, second_bbox)
            if (
                combined[2] - combined[0] > _GLYPH_PATH_MAX_EXTENT_PT
                or combined[3] - combined[1] > _GLYPH_PATH_MAX_EXTENT_PT
            ):
                continue
            if _bbox_gap_pt(first_bbox, second_bbox) <= _GLYPH_CLUSTER_GAP_PT:
                union(first_index, second_index)

        own_min_cell_x = math.floor(second_bbox[0] / cell_size)
        own_max_cell_x = math.floor(second_bbox[2] / cell_size)
        own_min_cell_y = math.floor(second_bbox[1] / cell_size)
        own_max_cell_y = math.floor(second_bbox[3] / cell_size)
        for cell_x in range(own_min_cell_x, own_max_cell_x + 1):
            for cell_y in range(own_min_cell_y, own_max_cell_y + 1):
                buckets.setdefault(
                    (second.page, cell_x, cell_y),
                    [],
                ).append(second_index)

    grouped: dict[int, list[tuple[PdfVectorPathObservation, tuple[float, float, float, float]]]] = {}
    for index, item in enumerate(small):
        grouped.setdefault(find(index), []).append(item)

    clusters: list[_VectorCluster] = []
    for group in grouped.values():
        vectors_in_group = tuple(
            sorted(
                (item[0] for item in group),
                key=lambda vector: (
                    _vector_bbox(vector),
                    _cluster_geometry_key((vector,)),
                    vector.element_id,
                ),
            )
        )
        bbox = group[0][1]
        for _vector, vector_bbox in group[1:]:
            bbox = _bbox_union(bbox, vector_bbox)
        if (
            bbox[2] - bbox[0] > _GLYPH_PATH_MAX_EXTENT_PT
            or bbox[3] - bbox[1] > _GLYPH_PATH_MAX_EXTENT_PT
        ):
            continue
        clusters.append(_make_vector_cluster(vectors_in_group))

    return tuple(
        sorted(
            clusters,
            key=lambda cluster: (
                cluster.page,
                cluster.bbox_pt,
                cluster.geometry_key,
            ),
        )
    )


def _is_glyph_cluster(cluster: _VectorCluster) -> bool:
    return bool(
        len(cluster.vectors) > 1
        or any(
            vector.closed
            or _vector_contains_bezier(vector)
            or _paint_family(vector) != "stroke"
            for vector in cluster.vectors
        )
    )


def _normalize_legend_alias(value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9.\-]+", " ", value.upper())
    return " ".join(cleaned.split())


def _field_modifier_text(value: str) -> tuple[str, str] | None:
    cleaned = " ".join(value.split())
    status_match = _FIELD_STATUS_RE.fullmatch(cleaned)
    if status_match:
        status = status_match.group("status").upper()
        return "status", status
    if _FIELD_HEIGHT_TAG_RE.fullmatch(cleaned):
        return "height", cleaned
    if _FIELD_CIRCUIT_COUNT_RE.fullmatch(cleaned):
        return "circuit_count", cleaned
    return None


def _field_status_for_point(
    *,
    page: int,
    x_pt: float,
    y_pt: float,
    texts: Sequence[PdfTextObservation],
) -> tuple[PdfTextObservation, str, str] | None:
    candidates: list[tuple[float, str, PdfTextObservation]] = []
    for observation in texts:
        if observation.page != page:
            continue
        modifier = _field_modifier_text(observation.text)
        if modifier is None or modifier[0] != "status":
            continue
        distance = _distance_pt(
            x_pt,
            y_pt,
            observation.x_pt,
            observation.y_pt,
        )
        if distance > _FIELD_STATUS_RADIUS_PT:
            continue
        candidates.append((distance, modifier[1], observation))
    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[2].element_id))
    nearest_distance = candidates[0][0]
    nearest = [
        item
        for item in candidates
        if item[0] <= nearest_distance + _FIELD_STATUS_AMBIGUITY_PT
    ]
    if len({item[1] for item in nearest}) != 1:
        return None
    _distance, status, observation = nearest[0]
    return observation, status, _FIELD_STATUS_MEANINGS[status]


def _field_status_for_cluster(
    cluster: _VectorCluster,
    texts: Sequence[PdfTextObservation],
) -> tuple[PdfTextObservation, str, str] | None:
    return _field_status_for_point(
        page=cluster.page,
        x_pt=cluster.center_pt[0],
        y_pt=cluster.center_pt[1],
        texts=texts,
    )


def _annotation_code(
    symbol: PdfSymbolObservation,
) -> tuple[str, str] | None:
    if symbol.source_kind != "annotation:square":
        return None
    raw = " ".join(str(symbol.metadata.get("contents") or "").split())
    normalized = re.sub(r"[^A-Z0-9]+", " ", raw.upper()).strip()
    if (
        not raw
        or not normalized
        or len(normalized) > _ANNOTATION_CODE_MAX_CHARS
        or len(normalized.split()) > 3
    ):
        return None
    return raw, normalized


def _annotation_code_legend_match(
    symbol: PdfSymbolObservation,
    entries: Sequence[_LegendEntry],
) -> tuple[_LegendEntry | None, dict[str, Any]] | None:
    code = _annotation_code(symbol)
    if code is None:
        return None
    raw_code, normalized_code = code

    by_type: dict[str, list[_LegendEntry]] = {}
    for entry in entries:
        by_type.setdefault(entry.canonical_type, []).append(entry)

    alias_targets: Mapping[str, tuple[str, ...]] = {
        "CR": ("access_control_device",),
        "TV": ("catv_outlet",),
        "CATV": ("catv_outlet",),
        "J": ("junction_box_power", "junction_box_data"),
        "JB": ("junction_box_power", "junction_box_data"),
        "D": ("data_outlet",),
        "DATA": ("data_outlet",),
    }
    for canonical_type in alias_targets.get(normalized_code, ()):
        matching = by_type.get(canonical_type, ())
        if matching:
            entry = sorted(
                matching,
                key=lambda item: (
                    -item.confidence,
                    item.label.element_id,
                    item.prototype.geometry_key,
                ),
            )[0]
            return entry, {
                "annotation_code": raw_code,
                "normalized_code": normalized_code,
                "match_kind": "legend-abbreviation",
                "canonical_type": entry.canonical_type,
                "legend_page": entry.label.page,
                "legend_row_label": entry.label.text,
                "classification_candidates": [
                    {
                        "canonical_type": canonical_type,
                        "legend_row_label": candidate.label.text,
                    }
                    for canonical_type in alias_targets[normalized_code]
                    for candidate in by_type.get(canonical_type, ())
                ],
            }

    verbatim: list[_LegendEntry] = []
    code_pattern = re.compile(
        rf"(?<![A-Z0-9]){re.escape(normalized_code)}(?![A-Z0-9])"
    )
    for entry in entries:
        normalized_label = re.sub(
            r"[^A-Z0-9]+",
            " ",
            entry.label.text.upper(),
        ).strip()
        if code_pattern.search(normalized_label):
            verbatim.append(entry)

    classifications = {
        (entry.entity_kind, entry.canonical_type)
        for entry in verbatim
    }
    if len(classifications) == 1:
        entry = sorted(
            verbatim,
            key=lambda item: (
                -item.confidence,
                item.label.element_id,
                item.prototype.geometry_key,
            ),
        )[0]
        return entry, {
            "annotation_code": raw_code,
            "normalized_code": normalized_code,
            "match_kind": "legend-verbatim-code",
            "canonical_type": entry.canonical_type,
            "legend_page": entry.label.page,
            "legend_row_label": entry.label.text,
            "classification_candidates": [
                {
                    "canonical_type": candidate.canonical_type,
                    "legend_row_label": candidate.label.text,
                }
                for candidate in sorted(
                    verbatim,
                    key=lambda item: (
                        item.canonical_type,
                        item.label.element_id,
                    ),
                )
            ],
        }

    return None, {
        "annotation_code": raw_code,
        "normalized_code": normalized_code,
        "match_kind": None,
        "canonical_type": None,
        "classification_candidates": [
            {
                "canonical_type": candidate.canonical_type,
                "legend_row_label": candidate.label.text,
            }
            for candidate in sorted(
                verbatim,
                key=lambda item: (
                    item.canonical_type,
                    item.label.element_id,
                ),
            )
        ],
        "reason": (
            "annotation code does not uniquely match a classified legend row"
        ),
    }


def _legend_heading_words(
    observation: PdfTextObservation,
) -> tuple[str, ...]:
    return tuple(_normalize_legend_alias(observation.text).split())


def _heading_has_rejected_legend_context(
    observation: PdfTextObservation,
) -> bool:
    normalized = _normalize_legend_alias(observation.text)
    return any(
        rejected in normalized
        for rejected in _LEGEND_REJECTED_HEADING_WORDS
    )


def _is_legend_heading(observation: PdfTextObservation) -> bool:
    text = " ".join(observation.text.split())
    if _LEGEND_REFERENCE_PREFIX_RE.search(text):
        return False
    words = _legend_heading_words(observation)
    if not words or len(words) > _LEGEND_SECTION_HEADING_MAX_WORDS:
        return False
    if _heading_has_rejected_legend_context(observation):
        return False
    return words[-1] in _LEGEND_TITLE_WORDS


def _is_short_legend_label(observation: PdfTextObservation) -> bool:
    text = " ".join(observation.text.split())
    if _field_modifier_text(text) is not None:
        return False
    if not text or len(text) > _LEGEND_MAX_LABEL_CHARS:
        return False
    if len(text.split()) > _LEGEND_MAX_LABEL_WORDS:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if observation.font_size_pt is not None and observation.font_size_pt > 18.0:
        return False
    if _is_legend_heading(observation):
        return False
    if _LEGEND_REFERENCE_CUE_RE.search(text):
        return False
    if _LEGEND_SHEET_ID_RE.fullmatch(text.strip()):
        return False
    return True


def _looks_like_section_heading(observation: PdfTextObservation) -> bool:
    text = " ".join(observation.text.split())
    if _field_modifier_text(text) is not None:
        return False
    if not text or len(text) > _LEGEND_SECTION_HEADING_MAX_CHARS:
        return False
    if _LEGEND_REFERENCE_PREFIX_RE.search(text):
        return False
    if _LEGEND_SHEET_ID_RE.fullmatch(text.strip()):
        return False
    words = _legend_heading_words(observation)
    if not words or len(words) > _LEGEND_SECTION_HEADING_MAX_WORDS:
        return False
    return bool(re.search(r"[A-Za-z]", text))


def _legend_group_bounds(
    rows: Sequence[_LegendRow],
) -> tuple[float, float, float, float]:
    return (
        min(min(row.cluster.bbox_pt[0], row.label.x_pt) for row in rows),
        min(min(row.cluster.bbox_pt[1], row.label.y_pt) for row in rows),
        max(max(row.cluster.bbox_pt[2], row.label.x_pt) for row in rows),
        max(max(row.cluster.bbox_pt[3], row.label.y_pt) for row in rows),
    )


def _heading_distance_to_group(
    heading: PdfTextObservation,
    rows: Sequence[_LegendRow],
    vectors: Sequence[PdfVectorPathObservation],
    *,
    allow_beside: bool,
) -> float | None:
    min_x, min_y, max_x, max_y = _legend_group_bounds(rows)
    connected = _leader_connects_heading_to_rows(heading, rows, vectors)
    above = heading.y_pt >= max_y + _LEGEND_ROW_VERTICAL_TOLERANCE_PT
    beside = (
        allow_beside
        and min_y - _LEGEND_ROW_VERTICAL_TOLERANCE_PT
        <= heading.y_pt
        <= max_y + _LEGEND_ROW_VERTICAL_TOLERANCE_PT
        and (heading.x_pt <= min_x or heading.x_pt >= max_x)
    )
    if not above and not beside:
        return None

    dx = max(min_x - heading.x_pt, heading.x_pt - max_x, 0.0)
    dy = max(min_y - heading.y_pt, heading.y_pt - max_y, 0.0)
    distance = math.hypot(dx, dy)
    if not connected and distance > _LEGEND_TITLE_REGION_RADIUS_PT:
        return None
    return distance


def _nearest_section_heading(
    rows: Sequence[_LegendRow],
    texts: Sequence[PdfTextObservation],
    vectors: Sequence[PdfVectorPathObservation],
    *,
    allow_beside: bool,
) -> PdfTextObservation | None:
    if not rows:
        return None
    page = rows[0].cluster.page
    row_label_ids = {row.label.element_id for row in rows}
    candidates: list[tuple[float, float, str, PdfTextObservation]] = []
    for observation in texts:
        if observation.page != page or observation.element_id in row_label_ids:
            continue
        if not _looks_like_section_heading(observation):
            continue
        distance = _heading_distance_to_group(
            observation,
            rows,
            vectors,
            allow_beside=allow_beside,
        )
        if distance is None:
            continue
        candidates.append(
            (
                distance,
                -(observation.font_size_pt or 0.0),
                observation.element_id,
                observation,
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[:3])[3]


def _dense_legend_group_is_valid(
    rows: Sequence[_LegendRow],
    texts: Sequence[PdfTextObservation],
) -> bool:
    if len(rows) < _LEGEND_TABLE_MIN_ROWS:
        return False
    if not all(
        _is_glyph_cluster(row.cluster) and _is_short_legend_label(row.label)
        for row in rows
    ):
        return False

    page = rows[0].cluster.page
    label_xs = [row.label.x_pt for row in rows]
    min_y = min(min(row.cluster.center_pt[1], row.label.y_pt) for row in rows)
    max_y = max(max(row.cluster.center_pt[1], row.label.y_pt) for row in rows)
    nearby_text = [
        observation
        for observation in texts
        if observation.page == page
        and min_y - _LEGEND_ROW_VERTICAL_TOLERANCE_PT
        <= observation.y_pt
        <= max_y + _LEGEND_ROW_VERTICAL_TOLERANCE_PT
        and min(label_xs) - _LEGEND_TABLE_LABEL_COLUMN_TOLERANCE_PT
        <= observation.x_pt
        <= max(label_xs) + _LEGEND_LABEL_HORIZONTAL_DISTANCE_PT
    ]
    if not nearby_text:
        return False

    numeric_rows = sum(
        not re.search(r"[A-Za-z]", " ".join(observation.text.split()))
        for observation in nearby_text
    )
    if numeric_rows * 2 >= len(nearby_text):
        return False

    paired_label_ids = {row.label.element_id for row in rows}
    if len(paired_label_ids) * 2 <= len(nearby_text):
        return False
    return True


def _heading_is_explicitly_referenced_from_other_page(
    heading: PdfTextObservation,
    texts: Sequence[PdfTextObservation],
) -> bool:
    words = _legend_heading_words(heading)
    if not words or words[-1] != "LEGEND":
        return False

    aliases = {_normalize_legend_alias(heading.text)}
    for observation in texts:
        if observation.page != heading.page:
            continue
        normalized = _normalize_legend_alias(observation.text)
        sheet_match = re.fullmatch(
            r"(?:SHEET\s+)?(E(?:-\d{1,4}|\d{1,3}(?:\.\d{1,3})?))",
            normalized,
            re.IGNORECASE,
        )
        if sheet_match:
            aliases.add(sheet_match.group(1).upper())

    for observation in texts:
        if observation.page == heading.page:
            continue
        if not _LEGEND_REFERENCE_CUE_RE.search(observation.text):
            continue
        normalized = _normalize_legend_alias(observation.text)
        if any(
            alias
            and re.search(
                rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])",
                normalized,
            )
            for alias in aliases
        ):
            return True
    return False


def _legend_row_candidates(
    clusters: Sequence[_VectorCluster],
    texts: Sequence[PdfTextObservation],
    rules: Sequence[SymbolRule],
    *,
    ambiguity_margin: float,
) -> tuple[_LegendRow, ...]:
    possible: list[
        tuple[
            float,
            float,
            str,
            str,
            _VectorCluster,
            PdfTextObservation,
            tuple[str, str, float] | None,
            tuple[Mapping[str, Any], ...],
        ]
    ] = []
    for label in texts:
        if not _is_short_legend_label(label):
            continue
        classification, ranked = _classify_semantic_text(
            label.text,
            rules,
            ambiguity_margin=ambiguity_margin,
        )
        for cluster in clusters:
            if cluster.page != label.page:
                continue
            vertical_delta = abs(label.y_pt - cluster.center_pt[1])
            if vertical_delta > _LEGEND_ROW_VERTICAL_TOLERANCE_PT:
                continue
            horizontal_gap = label.x_pt - cluster.bbox_pt[2]
            if not 0.0 <= horizontal_gap <= _LEGEND_LABEL_HORIZONTAL_DISTANCE_PT:
                continue
            possible.append(
                (
                    vertical_delta,
                    horizontal_gap,
                    label.element_id,
                    cluster.geometry_key,
                    cluster,
                    label,
                    classification,
                    tuple(ranked),
                )
            )

    possible.sort(key=lambda item: item[:4])
    used_labels: set[str] = set()
    used_clusters: set[tuple[int, str]] = set()
    rows: list[_LegendRow] = []
    for (
        _vertical_delta,
        horizontal_gap,
        _label_id,
        _geometry_key,
        cluster,
        label,
        classification,
        ranked,
    ) in possible:
        cluster_key = (cluster.page, cluster.geometry_key)
        if label.element_id in used_labels or cluster_key in used_clusters:
            continue
        used_labels.add(label.element_id)
        used_clusters.add(cluster_key)
        rows.append(
            _LegendRow(
                cluster=cluster,
                label=label,
                classification=classification,
                classification_candidates=ranked,
                orientation=1,
                horizontal_gap_pt=horizontal_gap,
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.cluster.page,
                -row.cluster.center_pt[1],
                row.cluster.center_pt[0],
                row.label.element_id,
            ),
        )
    )


def _aligned_legend_row_groups(
    rows: Sequence[_LegendRow],
    *,
    require_adjacent_rows: bool,
) -> tuple[tuple[_LegendRow, ...], ...]:
    rows = tuple(rows)
    if not rows:
        return ()
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)
        if first_root == second_root:
            return
        if first_root < second_root:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    for first_index, first in enumerate(rows):
        for second_index in range(first_index + 1, len(rows)):
            second = rows[second_index]
            if first.cluster.page != second.cluster.page:
                continue
            if first.orientation != second.orientation:
                continue
            if (
                abs(first.cluster.center_pt[0] - second.cluster.center_pt[0])
                > _LEGEND_TABLE_COLUMN_TOLERANCE_PT
            ):
                continue
            if (
                abs(first.label.x_pt - second.label.x_pt)
                > _LEGEND_TABLE_LABEL_COLUMN_TOLERANCE_PT
            ):
                continue
            if require_adjacent_rows and (
                abs(first.cluster.center_pt[1] - second.cluster.center_pt[1])
                > _LEGEND_TABLE_ROW_GAP_PT
            ):
                continue
            union(first_index, second_index)

    grouped: dict[int, list[_LegendRow]] = {}
    for index, row in enumerate(rows):
        grouped.setdefault(find(index), []).append(row)
    return tuple(
        tuple(
            sorted(
                group,
                key=lambda row: (
                    -row.cluster.center_pt[1],
                    row.cluster.center_pt[0],
                    row.label.element_id,
                ),
            )
        )
        for _root, group in sorted(grouped.items())
    )


def _legend_group_density(rows: Sequence[_LegendRow]) -> float:
    ys = [row.cluster.center_pt[1] for row in rows]
    span = max(ys) - min(ys) if len(ys) > 1 else 1.0
    return len(rows) / max(span, 1.0)


def _leader_connects_heading_to_rows(
    heading: PdfTextObservation,
    rows: Sequence[_LegendRow],
    vectors: Sequence[PdfVectorPathObservation],
) -> bool:
    def distance_to_rows(point: tuple[float, float]) -> float:
        return min(
            min(
                _distance_pt(point[0], point[1], row.label.x_pt, row.label.y_pt),
                _distance_pt(
                    point[0],
                    point[1],
                    row.cluster.center_pt[0],
                    row.cluster.center_pt[1],
                ),
            )
            for row in rows
        )

    for vector in vectors:
        if vector.page != heading.page or vector.closed or len(vector.points_pt) < 2:
            continue
        endpoints = (vector.points_pt[0], vector.points_pt[-1])
        for heading_end, legend_end in (endpoints, tuple(reversed(endpoints))):
            if (
                _distance_pt(
                    heading.x_pt,
                    heading.y_pt,
                    heading_end[0],
                    heading_end[1],
                )
                <= _LEGEND_LEADER_ENDPOINT_RADIUS_PT
                and distance_to_rows(legend_end) <= _LEGEND_LEADER_ENDPOINT_RADIUS_PT
            ):
                return True
    return False



def _rule_segments(
    vectors: Sequence[PdfVectorPathObservation],
    *,
    page: int,
) -> tuple[
    tuple[tuple[float, float, float], ...],
    tuple[tuple[float, float, float], ...],
]:
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    for vector in vectors:
        if vector.page != page:
            continue
        points = list(vector.points_pt)
        pairs = list(zip(points, points[1:]))
        if vector.closed:
            pairs.append((points[-1], points[0]))
        for first, second in pairs:
            dx = second[0] - first[0]
            dy = second[1] - first[1]
            if abs(dy) <= _LEGEND_RULE_AXIS_TOLERANCE_PT and abs(dx) >= 24.0:
                horizontal.append(
                    (
                        min(first[0], second[0]),
                        (first[1] + second[1]) / 2.0,
                        max(first[0], second[0]),
                    )
                )
            if abs(dx) <= _LEGEND_RULE_AXIS_TOLERANCE_PT and abs(dy) >= 24.0:
                vertical.append(
                    (
                        (first[0] + second[0]) / 2.0,
                        min(first[1], second[1]),
                        max(first[1], second[1]),
                    )
                )
    return tuple(horizontal), tuple(vertical)


def _displayed_media_box_bbox(
    document: PdfElectricalDocument,
    *,
    page: int,
) -> tuple[float, float, float, float] | None:
    provenance = document.page_provenance.get(page)
    if not provenance:
        return None
    try:
        width = float(provenance["displayed_page_width_pt"])
        height = float(provenance["displayed_page_height_pt"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0.0
        or height <= 0.0
    ):
        return None
    return (0.0, 0.0, width, height)


def _axis_aligned_closed_rectangle_bbox(
    vector: PdfVectorPathObservation,
) -> tuple[float, float, float, float] | None:
    if not vector.closed or len(vector.points_pt) < 4:
        return None
    bbox = _vector_bbox(vector)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width <= _LEGEND_RULE_AXIS_TOLERANCE_PT or height <= _LEGEND_RULE_AXIS_TOLERANCE_PT:
        return None

    points = tuple(vector.points_pt)
    pairs = list(zip(points, points[1:]))
    pairs.append((points[-1], points[0]))
    if any(
        abs(second[0] - first[0]) > _LEGEND_RULE_AXIS_TOLERANCE_PT
        and abs(second[1] - first[1]) > _LEGEND_RULE_AXIS_TOLERANCE_PT
        for first, second in pairs
    ):
        return None

    corners = (
        (bbox[0], bbox[1]),
        (bbox[2], bbox[1]),
        (bbox[2], bbox[3]),
        (bbox[0], bbox[3]),
    )
    if not all(
        any(
            _distance_pt(corner[0], corner[1], point[0], point[1])
            <= _LEGEND_RULE_EDGE_TOLERANCE_PT
            for point in points
        )
        for corner in corners
    ):
        return None
    return bbox


def _four_long_rule_rectangles(
    horizontal_rules: Sequence[tuple[float, float, float]],
    vertical_rules: Sequence[tuple[float, float, float]],
    *,
    media_box_pt: tuple[float, float, float, float] | None,
) -> tuple[tuple[float, float, float, float], ...]:
    if media_box_pt is not None:
        media_width = media_box_pt[2] - media_box_pt[0]
        media_height = media_box_pt[3] - media_box_pt[1]
        min_horizontal_length = _PAGE_FRAME_MIN_MEDIA_AREA_RATIO * media_width
        min_vertical_length = _PAGE_FRAME_MIN_MEDIA_AREA_RATIO * media_height
    else:
        min_horizontal_length = _PAGE_FRAME_MIN_DIMENSION_PT
        min_vertical_length = _PAGE_FRAME_MIN_DIMENSION_PT

    horizontals = [
        rule
        for rule in horizontal_rules
        if rule[2] - rule[0] >= min_horizontal_length
    ]
    verticals = [
        rule
        for rule in vertical_rules
        if rule[2] - rule[1] >= min_vertical_length
    ]

    rectangles: set[tuple[float, float, float, float]] = set()
    for first_index, first in enumerate(horizontals):
        for second in horizontals[first_index + 1 :]:
            bottom, top = (
                (first, second)
                if first[1] <= second[1]
                else (second, first)
            )
            if top[1] - bottom[1] < min_vertical_length:
                continue
            if (
                abs(bottom[0] - top[0]) > _LEGEND_RULE_EDGE_TOLERANCE_PT
                or abs(bottom[2] - top[2]) > _LEGEND_RULE_EDGE_TOLERANCE_PT
            ):
                continue

            left_x = (bottom[0] + top[0]) / 2.0
            right_x = (bottom[2] + top[2]) / 2.0
            if right_x - left_x < min_horizontal_length:
                continue
            left_found = any(
                abs(rule[0] - left_x) <= _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[1] <= bottom[1] + _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[2] >= top[1] - _LEGEND_RULE_EDGE_TOLERANCE_PT
                for rule in verticals
            )
            right_found = any(
                abs(rule[0] - right_x) <= _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[1] <= bottom[1] + _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[2] >= top[1] - _LEGEND_RULE_EDGE_TOLERANCE_PT
                for rule in verticals
            )
            if left_found and right_found:
                rectangles.add(
                    (
                        round(left_x, 6),
                        round(bottom[1], 6),
                        round(right_x, 6),
                        round(top[1], 6),
                    )
                )
    return tuple(sorted(rectangles))


def _page_frame_detection(
    vectors: Sequence[PdfVectorPathObservation],
    *,
    page: int,
    media_box_pt: tuple[float, float, float, float] | None,
) -> _PageFrameDetection | None:
    candidates: list[
        tuple[
            float,
            int,
            tuple[float, float, float, float],
            str,
        ]
    ] = []
    media_area = (
        (media_box_pt[2] - media_box_pt[0])
        * (media_box_pt[3] - media_box_pt[1])
        if media_box_pt is not None
        else None
    )

    def consider(
        bbox: tuple[float, float, float, float],
        *,
        method: str,
        method_priority: int,
    ) -> None:
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if (
            width < _PAGE_FRAME_MIN_DIMENSION_PT
            or height < _PAGE_FRAME_MIN_DIMENSION_PT
        ):
            return
        area = width * height
        if media_box_pt is not None:
            assert media_area is not None
            if area / media_area < _PAGE_FRAME_MIN_MEDIA_AREA_RATIO:
                return
            if (
                bbox[0] < media_box_pt[0] - _LEGEND_RULE_EDGE_TOLERANCE_PT
                or bbox[1] < media_box_pt[1] - _LEGEND_RULE_EDGE_TOLERANCE_PT
                or bbox[2] > media_box_pt[2] + _LEGEND_RULE_EDGE_TOLERANCE_PT
                or bbox[3] > media_box_pt[3] + _LEGEND_RULE_EDGE_TOLERANCE_PT
            ):
                return
        candidates.append((area, method_priority, bbox, method))

    for vector in vectors:
        if vector.page != page:
            continue
        bbox = _axis_aligned_closed_rectangle_bbox(vector)
        if bbox is not None:
            consider(
                bbox,
                method="closed-rectangle",
                method_priority=1,
            )

    horizontal_rules, vertical_rules = _rule_segments(vectors, page=page)
    for bbox in _four_long_rule_rectangles(
        horizontal_rules,
        vertical_rules,
        media_box_pt=media_box_pt,
    ):
        consider(
            bbox,
            method="four-long-rules",
            method_priority=0,
        )

    if candidates:
        _area, _priority, bbox, method = max(
            candidates,
            key=lambda item: (item[0], item[1], item[2]),
        )
        return _PageFrameDetection(bbox_pt=bbox, method=method)
    if media_box_pt is not None:
        return _PageFrameDetection(
            bbox_pt=media_box_pt,
            method="media-box-fallback",
        )
    return None


def _page_frame_bbox(
    vectors: Sequence[PdfVectorPathObservation],
    *,
    page: int,
    media_box_pt: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float] | None:
    detection = _page_frame_detection(
        vectors,
        page=page,
        media_box_pt=media_box_pt,
    )
    return detection.bbox_pt if detection is not None else None


def _bbox_contains_point(
    bbox: tuple[float, float, float, float],
    *,
    x_pt: float,
    y_pt: float,
) -> bool:
    return (
        bbox[0] - _LEGEND_RULE_EDGE_TOLERANCE_PT
        <= x_pt
        <= bbox[2] + _LEGEND_RULE_EDGE_TOLERANCE_PT
        and bbox[1] - _LEGEND_RULE_EDGE_TOLERANCE_PT
        <= y_pt
        <= bbox[3] + _LEGEND_RULE_EDGE_TOLERANCE_PT
    )


def _notes_column_title_band_top(
    frame: tuple[float, float, float, float],
    horizontal_rules: Sequence[tuple[float, float, float]],
    *,
    column_left: float,
    column_right: float,
) -> float:
    frame_height = frame[3] - frame[1]
    cap = frame[1] + _NOTES_TITLE_BAND_MAX_FRACTION * frame_height
    ruled_levels = [
        rule[1]
        for rule in horizontal_rules
        if frame[1] + _LEGEND_RULE_AXIS_TOLERANCE_PT < rule[1] <= cap
        and rule[0] <= column_left + _LEGEND_RULE_EDGE_TOLERANCE_PT
        and rule[2] >= column_right - _LEGEND_RULE_EDGE_TOLERANCE_PT
    ]
    if ruled_levels:
        return min(cap, max(ruled_levels))
    return cap


def _symbol_function_legend_regions(
    *,
    document: PdfElectricalDocument,
    texts: Sequence[PdfTextObservation],
    clusters: Sequence[_VectorCluster],
    vectors: Sequence[PdfVectorPathObservation],
    rules: Sequence[SymbolRule],
    ambiguity_margin: float,
) -> tuple[_LegendRegion, ...]:
    symbol_headers = [
        observation
        for observation in texts
        if _normalize_legend_alias(observation.text) == "SYMBOL"
    ]
    function_headers = [
        observation
        for observation in texts
        if _normalize_legend_alias(observation.text) == "FUNCTION"
    ]
    regions: list[_LegendRegion] = []

    for symbol_header in symbol_headers:
        for function_header in function_headers:
            if function_header.page != symbol_header.page:
                continue
            if function_header.x_pt <= symbol_header.x_pt:
                continue
            if (
                abs(function_header.y_pt - symbol_header.y_pt)
                > _LEGEND_HEADER_ROW_VERTICAL_TOLERANCE_PT
            ):
                continue

            page = symbol_header.page
            horizontal_rules, vertical_rules = _rule_segments(vectors, page=page)
            spanning_rules = [
                rule
                for rule in horizontal_rules
                if rule[0] <= symbol_header.x_pt + _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[2] >= function_header.x_pt + 18.0
            ]
            above = [rule for rule in spanning_rules if rule[1] > symbol_header.y_pt]
            below = [rule for rule in spanning_rules if rule[1] < symbol_header.y_pt]
            if not above or not below:
                continue
            top_rule = min(above, key=lambda rule: (rule[1] - symbol_header.y_pt, rule))
            header_bottom_rule = min(
                below,
                key=lambda rule: (symbol_header.y_pt - rule[1], rule),
            )
            if (
                top_rule[1] - header_bottom_rule[1]
                > _LEGEND_TABLE_HEADER_MAX_HEIGHT_PT
            ):
                continue

            table_left = min(top_rule[0], header_bottom_rule[0])
            table_right = max(top_rule[2], header_bottom_rule[2])
            divider_candidates = [
                rule
                for rule in vertical_rules
                if symbol_header.x_pt + 8.0
                <= rule[0]
                <= function_header.x_pt + 2.0
                and rule[1] <= header_bottom_rule[1] + _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[2] >= top_rule[1] - _LEGEND_RULE_EDGE_TOLERANCE_PT
            ]
            if not divider_candidates:
                continue
            divider = min(
                divider_candidates,
                key=lambda rule: (
                    abs(rule[0] - function_header.x_pt),
                    -rule[2] + rule[1],
                    rule[0],
                ),
            )
            split_x = divider[0]

            media_box = _displayed_media_box_bbox(document, page=page)
            frame_detection = _page_frame_detection(
                vectors,
                page=page,
                media_box_pt=media_box,
            )
            frame_provenance: tuple[Mapping[str, Any], ...] = ()
            if frame_detection is not None:
                frame = frame_detection.bbox_pt
                headers_outside_frame = [
                    header
                    for header in (symbol_header, function_header)
                    if not _bbox_contains_point(
                        frame,
                        x_pt=header.x_pt,
                        y_pt=header.y_pt,
                    )
                ]
                if headers_outside_frame and media_box is not None:
                    detected_frame = frame
                    frame = media_box
                    frame_provenance = (
                        {
                            "method": "media-box-rederivation",
                            "reason": "candidate legend header outside detected page frame",
                            "source_element_id": headers_outside_frame[0].element_id,
                            "header_element_ids": [
                                header.element_id
                                for header in headers_outside_frame
                            ],
                            "detected_frame_method": frame_detection.method,
                            "detected_frame_bbox_pt": list(detected_frame),
                            "rederived_frame_bbox_pt": list(media_box),
                        },
                    )

                frame_width = frame[2] - frame[0]
                in_right_notes_column = (
                    symbol_header.x_pt
                    >= frame[0] + _NOTES_COLUMN_START_FRACTION * frame_width
                )
                title_band_top = _notes_column_title_band_top(
                    frame,
                    horizontal_rules,
                    column_left=table_left,
                    column_right=table_right,
                )
                in_bottom_title_band = symbol_header.y_pt <= title_band_top
                if in_right_notes_column and in_bottom_title_band:
                    continue

            matching_rules = [
                rule
                for rule in spanning_rules
                if abs(rule[0] - table_left) <= _LEGEND_RULE_EDGE_TOLERANCE_PT
                and abs(rule[2] - table_right) <= _LEGEND_RULE_EDGE_TOLERANCE_PT
                and rule[1] <= header_bottom_rule[1] + _LEGEND_RULE_AXIS_TOLERANCE_PT
                and rule[1] >= divider[1] - _LEGEND_RULE_AXIS_TOLERANCE_PT
            ]
            y_levels = sorted(
                {round(rule[1], 3) for rule in matching_rules},
                reverse=True,
            )
            if not y_levels or abs(y_levels[0] - header_bottom_rule[1]) > 2.0:
                y_levels.insert(0, round(header_bottom_rule[1], 3))

            table_rows: list[_LegendRow] = []
            used_clusters: set[tuple[int, str]] = set()
            for upper_y, lower_y in zip(y_levels, y_levels[1:]):
                if upper_y - lower_y > _LEGEND_TABLE_MAX_ROW_HEIGHT_PT:
                    continue
                row_clusters = [
                    cluster
                    for cluster in clusters
                    if cluster.page == page
                    and (page, cluster.geometry_key) not in used_clusters
                    and table_left - 1.0 <= cluster.center_pt[0] < split_x - 1.0
                    and lower_y + 1.0
                    <= cluster.center_pt[1]
                    <= upper_y - 1.0
                ]
                if len(row_clusters) != 1:
                    continue
                label_parts = [
                    observation
                    for observation in texts
                    if observation.page == page
                    and observation.element_id
                    not in {symbol_header.element_id, function_header.element_id}
                    and split_x + 1.0 <= observation.x_pt <= table_right + 2.0
                    and lower_y + 1.0 <= observation.y_pt <= upper_y - 1.0
                    and _field_modifier_text(observation.text) is None
                ]
                if not label_parts:
                    continue
                label_parts.sort(
                    key=lambda observation: (
                        -observation.y_pt,
                        observation.x_pt,
                        observation.element_id,
                    )
                )
                combined_text = " ".join(
                    " ".join(observation.text.split())
                    for observation in label_parts
                )
                combined_label = replace(
                    label_parts[0],
                    text=combined_text,
                    x_pt=min(observation.x_pt for observation in label_parts),
                    y_pt=sum(observation.y_pt for observation in label_parts)
                    / len(label_parts),
                    font_size_pt=max(
                        (
                            observation.font_size_pt or 0.0
                            for observation in label_parts
                        ),
                        default=None,
                    ),
                )
                classification, ranked = _classify_semantic_text(
                    combined_label.text,
                    rules,
                    ambiguity_margin=ambiguity_margin,
                )
                cluster = row_clusters[0]
                used_clusters.add((page, cluster.geometry_key))
                table_rows.append(
                    _LegendRow(
                        cluster=cluster,
                        label=combined_label,
                        classification=classification,
                        classification_candidates=tuple(ranked),
                        orientation=1,
                        horizontal_gap_pt=max(
                            0.0,
                            combined_label.x_pt - cluster.bbox_pt[2],
                        ),
                        label_source_element_ids=tuple(
                            observation.element_id
                            for observation in label_parts
                        ),
                    )
                )

            if len(table_rows) < _LEGEND_TABLE_MIN_ROWS:
                continue

            legend_headings = [
                observation
                for observation in texts
                if observation.page == page
                and _normalize_legend_alias(observation.text) == "LEGEND"
                and table_left <= observation.x_pt <= table_right
                and top_rule[1] <= observation.y_pt <= top_rule[1] + 48.0
            ]
            heading = (
                min(
                    legend_headings,
                    key=lambda observation: (
                        observation.y_pt - top_rule[1],
                        observation.element_id,
                    ),
                )
                if legend_headings
                else None
            )
            regions.append(
                _LegendRegion(
                    page=page,
                    method="symbol-function-table",
                    rows=tuple(
                        sorted(
                            table_rows,
                            key=lambda row: (
                                -row.cluster.center_pt[1],
                                row.cluster.center_pt[0],
                                row.label.element_id,
                            ),
                        )
                    ),
                    heading=heading,
                    confidence=0.995,
                    header_element_ids=(
                        symbol_header.element_id,
                        function_header.element_id,
                    ),
                    table_bbox_pt=(
                        table_left,
                        min(y_levels),
                        table_right,
                        top_rule[1],
                    ),
                    frame_provenance=frame_provenance,
                )
            )

    best_by_page: dict[int, _LegendRegion] = {}
    for region in regions:
        existing = best_by_page.get(region.page)
        if existing is None or (
            len(region.rows),
            sum(row.classification is not None for row in region.rows),
            region.table_bbox_pt or (),
            region.header_element_ids,
        ) > (
            len(existing.rows),
            sum(row.classification is not None for row in existing.rows),
            existing.table_bbox_pt or (),
            existing.header_element_ids,
        ):
            best_by_page[region.page] = region
    return tuple(best_by_page[page] for page in sorted(best_by_page))


def _detect_legend_regions(
    *,
    texts: Sequence[PdfTextObservation],
    rows: Sequence[_LegendRow],
    clusters: Sequence[_VectorCluster],
    vectors: Sequence[PdfVectorPathObservation],
    preferred_regions: Sequence[_LegendRegion] = (),
) -> tuple[_LegendRegion, ...]:
    regions_by_page: dict[int, _LegendRegion] = {
        region.page: region for region in preferred_regions
    }
    rows_by_page: dict[int, list[_LegendRow]] = {}
    for row in rows:
        rows_by_page.setdefault(row.cluster.page, []).append(row)

    page_groups_by_page: dict[int, tuple[tuple[_LegendRow, ...], ...]] = {}
    for page, page_rows in sorted(rows_by_page.items()):
        page_groups = _aligned_legend_row_groups(
            page_rows,
            require_adjacent_rows=False,
        )
        page_groups_by_page[page] = page_groups
        if page in regions_by_page:
            continue
        candidates: list[_LegendRegion] = []
        for group in page_groups:
            heading = _nearest_section_heading(
                group,
                texts,
                vectors,
                allow_beside=True,
            )
            if heading is None or not _is_legend_heading(heading):
                continue
            candidates.append(
                _LegendRegion(
                    page=page,
                    method="title-match",
                    rows=group,
                    heading=heading,
                    confidence=0.98,
                )
            )
        if candidates:
            regions_by_page[page] = max(
                candidates,
                key=lambda region: (
                    len(region.rows),
                    sum(row.classification is not None for row in region.rows),
                    (region.heading.font_size_pt or 0.0) if region.heading else 0.0,
                    region.heading.element_id if region.heading else "",
                ),
            )

    signature_counts: dict[tuple[int, str], int] = {}
    for cluster in clusters:
        key = (cluster.page, cluster.shape_signature)
        signature_counts[key] = signature_counts.get(key, 0) + 1

    fallback_rows = [
        row
        for row in rows
        if row.cluster.page not in regions_by_page
        and signature_counts.get((row.cluster.page, row.cluster.shape_signature), 0) >= 2
    ]
    fallback_groups = _aligned_legend_row_groups(
        fallback_rows,
        require_adjacent_rows=True,
    )
    groups_by_page: dict[int, list[tuple[_LegendRow, ...]]] = {}
    for group in fallback_groups:
        if not _dense_legend_group_is_valid(group, texts):
            continue
        nearest_heading = _nearest_section_heading(
            group,
            texts,
            vectors,
            allow_beside=False,
        )
        if (
            nearest_heading is not None
            and _heading_has_rejected_legend_context(nearest_heading)
        ):
            continue
        groups_by_page.setdefault(group[0].cluster.page, []).append(group)

    for page, groups in sorted(groups_by_page.items()):
        group = max(
            groups,
            key=lambda candidate: (
                _legend_group_density(candidate),
                len(candidate),
                sum(row.classification is not None for row in candidate),
                -min(row.cluster.center_pt[0] for row in candidate),
            ),
        )
        regions_by_page[page] = _LegendRegion(
            page=page,
            method="dense-table-cluster",
            rows=group,
            heading=None,
            confidence=0.86,
        )

    # A separate legend sheet may legitimately be titled something broad such
    # as "GENERAL NOTES AND LEGEND". That title is intentionally rejected by
    # both local title matching and the dense fallback. It can still become a
    # legend source only when another sheet explicitly references this page by
    # its sheet ID or exact legend title, preserving the no-silent-inheritance
    # rule.
    for page, page_groups in sorted(page_groups_by_page.items()):
        if page in regions_by_page:
            continue
        candidates: list[_LegendRegion] = []
        for group in page_groups:
            if not _dense_legend_group_is_valid(group, texts):
                continue
            heading = _nearest_section_heading(
                group,
                texts,
                vectors,
                allow_beside=True,
            )
            if heading is None:
                continue
            if not _heading_is_explicitly_referenced_from_other_page(
                heading,
                texts,
            ):
                continue
            candidates.append(
                _LegendRegion(
                    page=page,
                    method="explicit-reference-legend-sheet",
                    rows=group,
                    heading=heading,
                    confidence=0.94,
                )
            )
        if candidates:
            regions_by_page[page] = max(
                candidates,
                key=lambda region: (
                    len(region.rows),
                    sum(row.classification is not None for row in region.rows),
                    _legend_group_density(region.rows),
                    region.heading.element_id if region.heading else "",
                ),
            )

    return tuple(regions_by_page[page] for page in sorted(regions_by_page))

def _legend_page_aliases(
    regions: Sequence[_LegendRegion],
    texts: Sequence[PdfTextObservation],
) -> dict[int, tuple[str, ...]]:
    aliases: dict[int, set[str]] = {region.page: set() for region in regions}
    regions_by_page = {region.page: region for region in regions}
    for page, region in regions_by_page.items():
        if region.heading is not None:
            heading_alias = _normalize_legend_alias(region.heading.text)
            if heading_alias not in _LEGEND_TITLE_WORDS:
                aliases[page].add(heading_alias)
    for observation in texts:
        if observation.page not in aliases:
            continue
        normalized = _normalize_legend_alias(observation.text)
        sheet_match = re.fullmatch(
            r"(?:SHEET\s+)?(E(?:-\d{1,4}|\d{1,3}(?:\.\d{1,3})?))",
            normalized,
            re.IGNORECASE,
        )
        if sheet_match:
            aliases[observation.page].add(sheet_match.group(1).upper())
    return {
        page: tuple(sorted(value for value in page_aliases if value))
        for page, page_aliases in sorted(aliases.items())
    }


def _resolve_explicit_legend_references(
    *,
    texts: Sequence[PdfTextObservation],
    aliases_by_page: Mapping[int, Sequence[str]],
    active_legend_pages: set[int],
) -> tuple[tuple[_LegendReference, ...], list[dict[str, Any]]]:
    alias_to_pages: dict[str, set[int]] = {}
    for page, aliases in aliases_by_page.items():
        if page not in active_legend_pages:
            continue
        for alias in aliases:
            alias_to_pages.setdefault(alias, set()).add(page)

    references: list[_LegendReference] = []
    unresolved: list[dict[str, Any]] = []
    for observation in texts:
        if not _LEGEND_REFERENCE_CUE_RE.search(observation.text):
            continue
        normalized = _normalize_legend_alias(observation.text)
        matched_aliases = [
            alias
            for alias in alias_to_pages
            if alias
            and re.search(
                rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])",
                normalized,
            )
        ]
        if not matched_aliases:
            continue
        target_pages = {
            page
            for alias in matched_aliases
            for page in alias_to_pages[alias]
            if page != observation.page
        }
        if len(target_pages) != 1:
            if target_pages:
                unresolved.append(
                    {
                        "kind": "legend_reference",
                        "page": observation.page,
                        "source_element_id": observation.element_id,
                        "text": observation.text,
                        "status": "unresolved_legend_reference",
                        "reason": "explicit legend reference matches multiple legend pages",
                        "candidate_legend_pages": sorted(target_pages),
                    }
                )
            continue
        legend_page = next(iter(target_pages))
        matching_targets = sorted(
            alias
            for alias in matched_aliases
            if legend_page in alias_to_pages[alias]
        )
        references.append(
            _LegendReference(
                page=observation.page,
                source_element_id=observation.element_id,
                text=observation.text,
                target_alias=matching_targets[0],
                legend_page=legend_page,
            )
        )
    return (
        tuple(
            sorted(
                references,
                key=lambda reference: (
                    reference.page,
                    reference.legend_page,
                    reference.source_element_id,
                    reference.target_alias,
                ),
            )
        ),
        unresolved,
    )


def _resolve_page_transforms(
    document: PdfElectricalDocument,
    page_transforms: Mapping[int, PdfPageTransform] | None,
) -> tuple[dict[int, PdfPageTransform], bool]:
    if page_transforms is None:
        if document.page_count == 1:
            frame_id = stable_id(
                "frame",
                f"pdf-electrical:{document.source_id}:page-local:1",
            )
            return {1: PdfPageTransform(frame_id=frame_id)}, False

        frame_id = stable_id(
            "frame",
            f"pdf-electrical:{document.source_id}:page-local-best-effort",
        )
        return {
            page: PdfPageTransform(
                frame_id=frame_id,
                tx_m=(page - 1) * _UNREGISTERED_PAGE_TILE_OFFSET_M,
            )
            for page in range(1, document.page_count + 1)
        }, False

    transforms = dict(page_transforms)
    expected_pages = set(range(1, document.page_count + 1))
    actual_pages = set(transforms)
    if actual_pages != expected_pages:
        missing = sorted(expected_pages - actual_pages)
        extra = sorted(actual_pages - expected_pages)
        details = []
        if missing:
            details.append(f"missing pages {missing}")
        if extra:
            details.append(f"unknown pages {extra}")
        raise ElectricalPdfError(
            "page_transforms must cover every PDF page exactly: " + ", ".join(details)
        )
    if not all(isinstance(transform, PdfPageTransform) for transform in transforms.values()):
        raise ElectricalPdfError("page_transforms values must be PdfPageTransform instances")
    frame_ids = {transform.frame_id for transform in transforms.values()}
    if len(frame_ids) != 1:
        raise ElectricalPdfError(
            "all page transforms must target the same canonical coordinate frame"
        )
    return transforms, True


def _provenance(
    document: PdfElectricalDocument,
    *,
    element_id: str,
    page: int,
    method: str,
    confidence: float,
    source_kind: str = "pdf-electrical",
    attributes: Mapping[str, Any] | None = None,
) -> Provenance:
    return Provenance(
        source_kind=source_kind,
        source_id=document.source_id,
        source_element_id=element_id,
        page=page,
        method=method,
        confidence=confidence,
        attributes=dict(attributes or {}),
    )



def _recognize_legend_shapes(
    document: PdfElectricalDocument,
    *,
    texts: Sequence[PdfTextObservation],
    vectors: Sequence[PdfVectorPathObservation],
    rules: Sequence[SymbolRule],
    ambiguity_margin: float,
) -> tuple[
    dict[str, _EntityCandidate],
    set[str],
    set[str],
    set[str],
    list[dict[str, Any]],
    dict[str, Any],
    dict[int, tuple[_LegendEntry, ...]],
]:
    clusters = tuple(
        cluster
        for cluster in _cluster_small_vector_glyphs(vectors)
        if _is_glyph_cluster(cluster)
    )
    glyph_vector_ids = {
        element_id
        for cluster in clusters
        for element_id in cluster.source_element_ids
    }
    rows = _legend_row_candidates(
        clusters,
        texts,
        rules,
        ambiguity_margin=ambiguity_margin,
    )
    preferred_regions = _symbol_function_legend_regions(
        document=document,
        texts=texts,
        clusters=clusters,
        vectors=vectors,
        rules=rules,
        ambiguity_margin=ambiguity_margin,
    )
    regions = _detect_legend_regions(
        texts=texts,
        rows=rows,
        clusters=clusters,
        vectors=vectors,
        preferred_regions=preferred_regions,
    )
    legend_text_ids = {
        region.heading.element_id
        for region in regions
        if region.heading is not None
    }
    for region in regions:
        legend_text_ids.update(region.header_element_ids)
        for row in region.rows:
            legend_text_ids.update(
                row.label_source_element_ids or (row.label.element_id,)
            )
    prototype_geometry_keys: set[tuple[int, str]] = set()
    entries_by_signature: dict[tuple[int, str], list[_LegendEntry]] = {}
    classified_entries_by_page: dict[int, list[_LegendEntry]] = {}
    unresolved: list[dict[str, Any]] = []
    legend_pages = {region.page for region in regions}

    for region in regions:
        for row in region.rows:
            prototype = row.cluster
            label = row.label
            prototype_key = (prototype.page, prototype.geometry_key)
            legend_text_ids.add(label.element_id)
            if prototype_key in prototype_geometry_keys:
                unresolved.append(
                    {
                        "kind": "legend_label",
                        "page": label.page,
                        "source_element_id": label.element_id,
                        "text": label.text,
                        "classification_candidates": list(
                            row.classification_candidates
                        ),
                        "status": "unresolved_legend_geometry",
                        "reason": "legend glyph cluster is claimed by more than one label",
                        "candidate_cluster_geometry_keys": [prototype.geometry_key],
                    }
                )
                continue
            prototype_geometry_keys.add(prototype_key)
            if row.classification is None:
                unresolved.append(
                    {
                        "kind": "legend_label",
                        "page": label.page,
                        "source_element_id": label.element_id,
                        "text": label.text,
                        "classification_candidates": list(
                            row.classification_candidates
                        ),
                        "status": "unresolved_legend_classification",
                        "reason": (
                            "detected legend row has no unique canonical type; "
                            "prototype is retained as legend evidence only"
                        ),
                        "candidate_cluster_geometry_keys": [prototype.geometry_key],
                    }
                )
                continue

            entity_kind, canonical_type, confidence = row.classification
            entry = _LegendEntry(
                entity_kind=entity_kind,
                canonical_type=canonical_type,
                confidence=confidence,
                prototype=prototype,
                label=label,
                region=region,
                label_source_element_ids=(
                    row.label_source_element_ids or (label.element_id,)
                ),
            )
            entries_by_signature.setdefault(
                (prototype.page, prototype.shape_signature), []
            ).append(entry)
            classified_entries_by_page.setdefault(prototype.page, []).append(entry)

    legend_by_signature: dict[tuple[int, str], _LegendEntry] = {}
    for (page, signature), entries in sorted(entries_by_signature.items()):
        classifications = {
            (entry.entity_kind, entry.canonical_type)
            for entry in entries
        }
        if len(classifications) != 1:
            for entry in entries:
                unresolved.append(
                    {
                        "kind": "legend_glyph",
                        "page": entry.prototype.page,
                        "source_element_id": entry.prototype.source_element_ids[0],
                        "source_element_ids": list(entry.prototype.source_element_ids),
                        "shape_signature": signature,
                        "legend_label_element_id": entry.label.element_id,
                        "legend_label": entry.label.text,
                        "status": "unresolved_legend_classification",
                        "reason": "the same legend glyph shape maps to conflicting types",
                        "classification_candidates": [
                            {
                                "entity_kind": kind,
                                "canonical_type": canonical_type,
                            }
                            for kind, canonical_type in sorted(classifications)
                        ],
                    }
                )
            continue
        legend_by_signature[(page, signature)] = sorted(
            entries,
            key=lambda entry: (
                -entry.confidence,
                entry.label.element_id,
                entry.prototype.geometry_key,
            ),
        )[0]

    legend_entries_by_page: dict[int, list[_LegendEntry]] = {}
    for (page, _signature), entry in sorted(legend_by_signature.items()):
        legend_entries_by_page.setdefault(page, []).append(entry)

    active_legend_pages = {
        page for page, _signature in legend_by_signature
    }
    aliases_by_page = _legend_page_aliases(regions, texts)
    references, unresolved_references = _resolve_explicit_legend_references(
        texts=texts,
        aliases_by_page=aliases_by_page,
        active_legend_pages=active_legend_pages,
    )
    unresolved.extend(unresolved_references)
    legend_text_ids.update(reference.source_element_id for reference in references)
    references_by_page: dict[int, list[_LegendReference]] = {}
    for reference in references:
        references_by_page.setdefault(reference.page, []).append(reference)

    field_clusters = _prepare_field_clusters(
        clusters,
        prototype_geometry_keys=prototype_geometry_keys,
        legend_entries_by_page=legend_entries_by_page,
        references_by_page=references_by_page,
        texts=texts,
    )

    legend_recognition = {
        "regions": [
            {
                "page": region.page,
                "method": region.method,
                "confidence": region.confidence,
                "heading_element_id": (
                    region.heading.element_id if region.heading is not None else None
                ),
                "heading_text": (
                    region.heading.text if region.heading is not None else None
                ),
                "row_count": len(region.rows),
                "classified_row_count": sum(
                    row.classification is not None for row in region.rows
                ),
                "sheet_aliases": list(aliases_by_page.get(region.page, ())),
                **(
                    {
                        "header_element_ids": list(region.header_element_ids),
                        "table_bbox_pt": list(region.table_bbox_pt),
                    }
                    if region.header_element_ids
                    and region.table_bbox_pt is not None
                    else {}
                ),
                **(
                    {
                        "frame_provenance": [
                            dict(item)
                            for item in region.frame_provenance
                        ]
                    }
                    if region.frame_provenance
                    else {}
                ),
            }
            for region in regions
        ],
        "frame_rederivations": [
            {
                "page": region.page,
                **dict(item),
            }
            for region in regions
            for item in region.frame_provenance
        ],
        "explicit_cross_sheet_references": [
            {
                "page": reference.page,
                "source_element_id": reference.source_element_id,
                "text": reference.text,
                "target_alias": reference.target_alias,
                "legend_page": reference.legend_page,
            }
            for reference in references
        ],
    }

    candidates: dict[str, _EntityCandidate] = {}
    matched_vector_ids: set[str] = {
        element_id
        for cluster in clusters
        if (cluster.page, cluster.geometry_key) in prototype_geometry_keys
        for element_id in cluster.source_element_ids
    }

    for cluster in field_clusters:
        legend_entry, match_diagnostics = _match_cluster_to_legend_entries(
            cluster,
            legend_entries_by_page.get(cluster.page, ()),
            texts=texts,
        )
        reference: _LegendReference | None = None
        if legend_entry is None:
            remote_matches: list[
                tuple[_LegendReference, _LegendEntry, dict[str, Any]]
            ] = []
            remote_diagnostics: list[
                tuple[_LegendReference, dict[str, Any]]
            ] = []
            for candidate_reference in references_by_page.get(cluster.page, ()):
                remote_entry, diagnostics = _match_cluster_to_legend_entries(
                    cluster,
                    legend_entries_by_page.get(
                        candidate_reference.legend_page,
                        (),
                    ),
                    texts=texts,
                )
                remote_diagnostics.append((candidate_reference, diagnostics))
                if remote_entry is not None:
                    remote_matches.append(
                        (candidate_reference, remote_entry, diagnostics)
                    )
            if remote_matches:
                remote_classifications = {
                    (entry.entity_kind, entry.canonical_type)
                    for _reference, entry, _diagnostics in remote_matches
                }
                if len(remote_classifications) != 1:
                    conflict_match_diagnostics = dict(
                        sorted(
                            remote_matches,
                            key=lambda item: (
                                -float(item[2].get("nearest_score") or -1.0),
                                item[0].legend_page,
                                item[0].source_element_id,
                            ),
                        )[0][2]
                    )
                    conflict_match_diagnostics["reason"] = "tie within margin"
                    unresolved.append(
                        {
                            "kind": "vector_cluster",
                            "page": cluster.page,
                            "source_element_id": cluster.source_element_ids[0],
                            "source_element_ids": list(cluster.source_element_ids),
                            "position_pt": {
                                "x": cluster.center_pt[0],
                                "y": cluster.center_pt[1],
                            },
                            "bbox_pt": list(cluster.bbox_pt),
                            "shape_signature": cluster.shape_signature,
                            "match_diagnostics": conflict_match_diagnostics,
                            "status": "unresolved_classification",
                            "reason": (
                                "explicitly referenced legend sheets map the same glyph "
                                "shape to conflicting types"
                            ),
                            "classification_candidates": [
                                {
                                    "entity_kind": kind,
                                    "canonical_type": canonical_type,
                                }
                                for kind, canonical_type in sorted(
                                    remote_classifications
                                )
                            ],
                            "recognition_provenance": {
                                "method": "explicit-cross-sheet-legend-geometry-match",
                                "legend_scope": "explicit-cross-sheet-reference",
                                "page": cluster.page,
                                "referenced_legend_pages": sorted(
                                    {
                                        item.legend_page
                                        for item, _entry, _diagnostics in remote_matches
                                    }
                                ),
                                "match_diagnostics": [
                                    {
                                        "legend_page": item.legend_page,
                                        **diagnostics,
                                    }
                                    for item, _entry, diagnostics in remote_matches
                                ],
                            },
                        }
                    )
                    continue
                reference, legend_entry, match_diagnostics = sorted(
                    remote_matches,
                    key=lambda item: (
                        -float(item[2].get("score") or 0.0),
                        -item[1].confidence,
                        item[0].legend_page,
                        item[0].source_element_id,
                        item[1].label.element_id,
                    ),
                )[0]
            elif remote_diagnostics:
                best_remote = max(
                    remote_diagnostics,
                    key=lambda item: (
                        float(item[1].get("score") or -1.0),
                        -item[0].legend_page,
                        item[0].source_element_id,
                    ),
                )
                if (
                    match_diagnostics.get("score") is None
                    or float(best_remote[1].get("score") or -1.0)
                    > float(match_diagnostics.get("score") or -1.0)
                ):
                    match_diagnostics = dict(best_remote[1])
                    match_diagnostics["legend_page"] = best_remote[0].legend_page

        if legend_entry is None:
            page_references = references_by_page.get(cluster.page, ())
            if page_references:
                reason = (
                    "explicitly referenced legend sheet has no unique matching "
                    "type for glyph"
                )
                recognition_provenance: dict[str, Any] = {
                    "method": "explicit-cross-sheet-legend-geometry-match",
                    "legend_scope": "explicit-cross-sheet-reference",
                    "page": cluster.page,
                    "referenced_legend_pages": sorted(
                        {item.legend_page for item in page_references}
                    ),
                    "reference_element_ids": sorted(
                        {item.source_element_id for item in page_references}
                    ),
                    "match_diagnostics": match_diagnostics,
                }
            else:
                reason = (
                    "page has no recognized legend; glyph remains unresolved and "
                    "cross-page legend inheritance is disabled"
                    if cluster.page not in legend_pages
                    else "glyph cluster has no unique matching type in the sheet legend"
                )
                recognition_provenance = {
                    "method": "sheet-local-legend-geometry-match",
                    "legend_scope": "same-page-only",
                    "page": cluster.page,
                    "page_has_recognized_legend": cluster.page in legend_pages,
                }
                if cluster.page in legend_pages:
                    recognition_provenance["match_diagnostics"] = match_diagnostics
            unresolved.append(
                {
                    "kind": "vector_cluster",
                    "page": cluster.page,
                    "source_element_id": cluster.source_element_ids[0],
                    "source_element_ids": list(cluster.source_element_ids),
                    "position_pt": {
                        "x": cluster.center_pt[0],
                        "y": cluster.center_pt[1],
                    },
                    "bbox_pt": list(cluster.bbox_pt),
                    "shape_signature": cluster.shape_signature,
                    "match_diagnostics": match_diagnostics,
                    "status": "unresolved_classification",
                    "reason": reason,
                    "recognition_provenance": recognition_provenance,
                }
            )
            continue

        legend_scope = (
            "explicit-cross-sheet-reference"
            if reference is not None
            else "same-page-only"
        )
        match_score = float(match_diagnostics.get("score") or 0.0)
        confidence = min(
            match_score,
            legend_entry.confidence,
            0.88 if reference is not None else 1.0,
        )
        prototype = legend_entry.prototype
        label = legend_entry.label
        region = legend_entry.region
        key = f"p{cluster.page}:shape:{cluster.geometry_key}"
        shape_recognition: dict[str, Any] = {
            "method": (
                "explicit-cross-sheet-legend-geometry-match"
                if reference is not None
                else "sheet-legend-geometry-match"
            ),
            "legend_scope": legend_scope,
            "legend_detection_method": region.method,
            "legend_detection_confidence": region.confidence,
            "shape_signature": cluster.shape_signature,
            "source_geometry_key": cluster.geometry_key,
            "legend_label_element_id": label.element_id,
            "legend_page": label.page,
            "legend_label": label.text,
            "legend_row_label": label.text,
            "canonical_type": legend_entry.canonical_type,
            "match_diagnostics": match_diagnostics,
            "legend_source_element_ids": list(prototype.source_element_ids),
            "source_element_ids": list(cluster.source_element_ids),
            "confidence": {
                "score": match_diagnostics.get("score"),
                "margin": match_diagnostics.get("margin"),
            },
        }
        if cluster.cleanup_actions:
            shape_recognition["cluster_cleanup"] = list(cluster.cleanup_actions)
        if cluster.excluded_source_element_ids:
            shape_recognition["excluded_source_element_ids"] = list(
                cluster.excluded_source_element_ids
            )
        if cluster.stripped_text_tags:
            shape_recognition["stripped_text_tags"] = list(
                cluster.stripped_text_tags
            )
            shape_recognition["tags"] = list(cluster.stripped_text_tags)
        if region.heading is not None:
            shape_recognition["legend_header_element_id"] = region.heading.element_id
            shape_recognition["legend_header_text"] = region.heading.text
        if reference is not None:
            shape_recognition.update(
                {
                    "legend_reference_element_id": reference.source_element_id,
                    "legend_reference_text": reference.text,
                    "legend_reference_target": reference.target_alias,
                }
            )
        label_source_element_ids = (
            legend_entry.label_source_element_ids or (label.element_id,)
        )
        if len(label_source_element_ids) > 1:
            shape_recognition["legend_label_element_ids"] = list(
                label_source_element_ids
            )

        status_evidence = _field_status_for_cluster(cluster, texts)
        if status_evidence is not None:
            status_observation, status_code, status_meaning = status_evidence
            shape_recognition.update(
                {
                    "status": status_code,
                    "status_meaning": status_meaning,
                    "status_source_element_id": status_observation.element_id,
                }
            )

        candidate = _EntityCandidate(
            key=key,
            entity_kind=legend_entry.entity_kind,
            canonical_type=legend_entry.canonical_type,
            tag=None,
            identity_key=f"geometry:p{cluster.page}:{cluster.geometry_key}",
            page=cluster.page,
            x_pt=cluster.center_pt[0],
            y_pt=cluster.center_pt[1],
            confidence=confidence,
            primary_method="pdf-legend-shape-match",
            shape_recognition=shape_recognition,
        )
        for vector in cluster.vectors:
            candidate.merge_source(
                element_id=vector.element_id,
                text=None,
                symbol_name=None,
                x_pt=cluster.center_pt[0],
                y_pt=cluster.center_pt[1],
                confidence=confidence,
                provenance=_provenance(
                    document,
                    element_id=vector.element_id,
                    page=cluster.page,
                    method="pdf-legend-shape-match",
                    confidence=confidence,
                    source_kind=vector.source_kind,
                    attributes={
                        "shape_signature": cluster.shape_signature,
                        "source_geometry_key": cluster.geometry_key,
                        "legend_scope": legend_scope,
                        "legend_page": label.page,
                        "legend_label_element_id": label.element_id,
                        "legend_label": label.text,
                        "legend_row_label": label.text,
                        "canonical_type": legend_entry.canonical_type,
                        "match_diagnostics": match_diagnostics,
                        "legend_detection_method": region.method,
                        "legend_source_element_ids": list(
                            prototype.source_element_ids
                        ),
                    },
                ),
                method="pdf-legend-shape-match",
            )
        for label_element_id in label_source_element_ids:
            if label_element_id not in candidate.source_element_ids:
                candidate.source_element_ids.append(label_element_id)
        if status_evidence is not None:
            status_observation, status_code, status_meaning = status_evidence
            if status_observation.element_id not in candidate.source_element_ids:
                candidate.source_element_ids.append(status_observation.element_id)
            candidate.provenance.append(
                _provenance(
                    document,
                    element_id=status_observation.element_id,
                    page=status_observation.page,
                    method="pdf-field-status-tag",
                    confidence=0.92,
                    attributes={
                        "source_text": status_observation.text,
                        "status": status_code,
                        "status_meaning": status_meaning,
                        "source_geometry_key": cluster.geometry_key,
                    },
                )
            )
        label_attributes: dict[str, Any] = {
            "source_text": label.text,
            "legend_row_label": label.text,
            "canonical_type": legend_entry.canonical_type,
            "shape_signature": cluster.shape_signature,
            "legend_scope": legend_scope,
            "legend_detection_method": region.method,
            "match_diagnostics": match_diagnostics,
        }
        if region.heading is not None:
            label_attributes["legend_header_element_id"] = region.heading.element_id
        candidate.provenance.append(
            _provenance(
                document,
                element_id=label.element_id,
                page=label.page,
                method="pdf-sheet-legend-type-label",
                confidence=legend_entry.confidence,
                attributes=label_attributes,
            )
        )
        if reference is not None:
            if reference.source_element_id not in candidate.source_element_ids:
                candidate.source_element_ids.append(reference.source_element_id)
            candidate.provenance.append(
                _provenance(
                    document,
                    element_id=reference.source_element_id,
                    page=reference.page,
                    method="pdf-explicit-legend-sheet-reference",
                    confidence=0.95,
                    attributes={
                        "source_text": reference.text,
                        "reference_target": reference.target_alias,
                        "legend_page": reference.legend_page,
                        "shape_signature": cluster.shape_signature,
                    },
                )
            )
        candidates[key] = candidate
        matched_vector_ids.update(cluster.source_element_ids)

    return (
        candidates,
        matched_vector_ids,
        glyph_vector_ids,
        legend_text_ids,
        unresolved,
        legend_recognition,
        {
            page: tuple(
                sorted(
                    entries,
                    key=lambda entry: (
                        entry.canonical_type,
                        entry.label.element_id,
                        entry.prototype.geometry_key,
                    ),
                )
            )
            for page, entries in sorted(classified_entries_by_page.items())
        },
    )

def _extract_voltage(texts: Iterable[str]) -> tuple[float | None, str | None]:
    best_values: tuple[float, ...] = ()
    raw_system: str | None = None
    for text in texts:
        match = _VOLTAGE_RE.search(text)
        if not match:
            continue
        values = [float(match.group("v1"))]
        if match.group("v2"):
            values.append(float(match.group("v2")))
        candidate = tuple(values)
        if (
            max(candidate) > max(best_values, default=0.0)
            or (
                max(candidate) == max(best_values, default=0.0)
                and len(candidate) > len(best_values)
            )
        ):
            best_values = candidate
            raw_system = match.group(0).replace(" ", "").upper()
    if not best_values:
        return None, None
    phase: str | None = None
    for text in texts:
        phase_match = _PHASE_RE.search(text)
        if phase_match:
            phase = phase_match.group("phase")
            break
    system = raw_system + (f"-{phase}ph" if phase else "")
    return max(best_values), system


def _mounting_heights_m(texts: Iterable[str]) -> list[float]:
    values: set[float] = set()
    for text in texts:
        foot_matches = tuple(_MOUNT_FT_RE.finditer(text))
        for match in foot_matches:
            feet = float(match.group("feet"))
            inches = float(match.group("inches") or 0.0)
            values.add(round((feet * 12.0 + inches) * 0.0254, 6))
        for match in _MOUNT_IN_RE.finditer(text):
            if any(
                match.start() < foot_match.end() and foot_match.start() < match.end()
                for foot_match in foot_matches
            ):
                continue
            values.add(round(float(match.group("value")) * 0.0254, 6))
    return sorted(values)


def _host_hints(texts: Iterable[str]) -> list[str]:
    joined = " ".join(texts).upper()
    hints: set[str] = set()
    if re.search(r"\bWALL(?:\s+MTD|\s+MOUNTED)?\b", joined):
        hints.add("wall")
    if re.search(r"\bCEILING(?:\s+MTD|\s+MOUNTED)?\b", joined):
        hints.add("ceiling")
    if re.search(r"\bFLOOR(?:\s+MTD|\s+MOUNTED)?\b", joined):
        hints.add("floor")
    if re.search(r"\bPOLE(?:\s+MTD|\s+MOUNTED)?\b", joined):
        hints.add("pole")
    return sorted(hints)


def _looks_like_note(text: str) -> bool:
    upper = text.upper()
    return bool(
        re.match(r"\s*(?:NOTE|KEYNOTE|GENERAL\s+NOTE)\b", upper)
        or " AFF" in upper
        or "MOUNT" in upper
        or " MTD" in upper
    )


class ElectricalPdfImporter:
    """Deterministic electrical plan recognition into the canonical v1 model."""

    def __init__(
        self,
        *,
        symbol_rules: Sequence[SymbolRule] = DEFAULT_SYMBOL_RULES,
        instance_hints: Sequence[ElectricalInstanceHint] = (),
        symbol_label_radius_pt: float = 96.0,
        annotation_radius_pt: float = 144.0,
        instance_hint_source_radius_pt: float = 18.0,
        ambiguity_margin: float = 0.08,
        vector_symbol_radius_pt: float = 18.0,
        topology_snap_radius_pt: float = 4.0,
        topology_endpoint_radius_pt: float = 18.0,
        topology_annotation_radius_pt: float = 60.0,
    ) -> None:
        self.symbol_rules = tuple(symbol_rules)
        self.instance_hints = tuple(instance_hints)
        seen_hint_ids: set[str] = set()
        for hint in self.instance_hints:
            if hint.identity_key in seen_hint_ids:
                raise ElectricalPdfError(
                    f"duplicate instance hint identity_key {hint.identity_key!r}"
                )
            seen_hint_ids.add(hint.identity_key)
        self.symbol_label_radius_pt = float(symbol_label_radius_pt)
        self.annotation_radius_pt = float(annotation_radius_pt)
        self.instance_hint_source_radius_pt = float(instance_hint_source_radius_pt)
        if (
            not math.isfinite(self.instance_hint_source_radius_pt)
            or self.instance_hint_source_radius_pt < 0.0
        ):
            raise ElectricalPdfError(
                "instance_hint_source_radius_pt must be finite and non-negative"
            )
        self.ambiguity_margin = float(ambiguity_margin)
        self.vector_symbol_radius_pt = float(vector_symbol_radius_pt)
        self.topology_snap_radius_pt = float(topology_snap_radius_pt)
        self.topology_endpoint_radius_pt = float(topology_endpoint_radius_pt)
        self.topology_annotation_radius_pt = float(topology_annotation_radius_pt)

    def _instance_hint_source_rejection(
        self,
        hint: ElectricalInstanceHint,
        observation: PdfTextObservation | PdfSymbolObservation,
        *,
        texts: Sequence[PdfTextObservation],
    ) -> str | None:
        if (
            _distance_pt(
                hint.x_pt,
                hint.y_pt,
                observation.x_pt,
                observation.y_pt,
            )
            > self.instance_hint_source_radius_pt
        ):
            return "claimed source position does not agree with instance hint"

        if isinstance(observation, PdfTextObservation):
            hits = _text_entity_hits(observation.text)
            classifications = {
                (entity_kind, canonical_type)
                for entity_kind, canonical_type, _tag, _confidence in hits
            }
            if len(classifications) != 1:
                return (
                    "claimed text source does not identify exactly one supported "
                    "electrical classification"
                )
            source_kind, source_type = next(iter(classifications))
            if (source_kind, source_type) != (
                hint.entity_kind,
                hint.canonical_type,
            ):
                return "claimed source semantics do not match instance hint"

            matching_tags = {
                tag
                for entity_kind, canonical_type, tag, _confidence in hits
                if (entity_kind, canonical_type) == (source_kind, source_type)
            }
            if (
                source_kind != "device"
                or not matching_tags
                or any(tag not in _GENERIC_DEVICE_TAGS for tag in matching_tags)
            ):
                return (
                    "claimed source already has stable semantic identity and does "
                    "not need an instance hint"
                )
            return None

        classification, _ranked = _classify_symbol(
            observation,
            self.symbol_rules,
            ambiguity_margin=self.ambiguity_margin,
        )
        if classification is None:
            return (
                "claimed symbol source is ambiguous or unsupported for instance "
                "hint disambiguation"
            )
        source_kind, source_type, _confidence = classification
        if (source_kind, source_type) != (
            hint.entity_kind,
            hint.canonical_type,
        ):
            return "claimed source semantics do not match instance hint"

        native_id = str(observation.metadata.get("native_id") or "").strip()
        if native_id:
            return (
                "claimed source already has stable native identity and does not "
                "need an instance hint"
            )

        for text_observation in texts:
            if text_observation.page != observation.page:
                continue
            if (
                _distance_pt(
                    text_observation.x_pt,
                    text_observation.y_pt,
                    observation.x_pt,
                    observation.y_pt,
                )
                > self.symbol_label_radius_pt
            ):
                continue
            for entity_kind, canonical_type, tag, _confidence in _text_entity_hits(
                text_observation.text
            ):
                if (entity_kind, canonical_type) != (source_kind, source_type):
                    continue
                if entity_kind == "device" and tag in _GENERIC_DEVICE_TAGS:
                    continue
                return (
                    "claimed symbol source already has nearby stable semantic "
                    "identity and does not need an instance hint"
                )
        return None

    def import_pdf(
        self,
        path: str | Path,
        *,
        source_id: str | None = None,
        page_transforms: Mapping[int, PdfPageTransform] | None = None,
    ) -> BuildingModel:
        return self.import_document(
            extract_pdf(path, source_id=source_id),
            page_transforms=page_transforms,
        )

    def import_document(
        self,
        document: PdfElectricalDocument,
        *,
        page_transforms: Mapping[int, PdfPageTransform] | None = None,
    ) -> BuildingModel:
        transforms, has_explicit_registration = _resolve_page_transforms(
            document,
            page_transforms,
        )
        frame_id = next(iter(transforms.values())).frame_id
        if has_explicit_registration:
            spatial_status = "registered-to-canonical-frame"
        elif document.page_count > 1:
            spatial_status = "multi-page-local-best-effort-unregistered"
        else:
            spatial_status = "single-page-local-unregistered"
        texts = tuple(sorted(document.texts, key=lambda item: (item.page, item.element_id)))
        symbols = tuple(sorted(document.symbols, key=lambda item: (item.page, item.element_id)))
        vectors = tuple(sorted(document.vectors, key=lambda item: (item.page, item.element_id)))
        annotation_code_text_ids = {
            f"{symbol.element_id}:text"
            for symbol in symbols
            if _annotation_code(symbol) is not None
        }
        legend_texts = tuple(
            observation
            for observation in texts
            if observation.element_id not in annotation_code_text_ids
        )
        (
            shape_candidates,
            shape_matched_vector_ids,
            glyph_vector_ids,
            legend_text_ids,
            unresolved_shape_rows,
            legend_recognition,
            annotation_legend_entries,
        ) = _recognize_legend_shapes(
            document,
            texts=legend_texts,
            vectors=vectors,
            rules=self.symbol_rules,
            ambiguity_margin=self.ambiguity_margin,
        )
        candidates: dict[str, _EntityCandidate] = dict(shape_candidates)
        unresolved_observations: list[dict[str, Any]] = []
        source_by_key: dict[
            tuple[int, str],
            list[PdfTextObservation | PdfSymbolObservation],
        ] = {}
        for observation in (*texts, *symbols):
            source_by_key.setdefault(
                (observation.page, observation.element_id),
                [],
            ).append(observation)

        claims_by_source: dict[tuple[int, str], list[ElectricalInstanceHint]] = {}
        for hint in self.instance_hints:
            if hint.page > document.page_count:
                raise ElectricalPdfError(
                    f"instance hint {hint.identity_key!r} references page {hint.page}, "
                    f"but page_count is {document.page_count}"
                )
            if hint.source_element_id is not None:
                claims_by_source.setdefault(
                    (hint.page, hint.source_element_id),
                    [],
                ).append(hint)

        claimed_source_ids: set[tuple[int, str]] = set()

        def reject_instance_hint(
            hint: ElectricalInstanceHint,
            reason: str,
            *,
            extra: Mapping[str, Any] | None = None,
        ) -> None:
            row: dict[str, Any] = {
                "kind": "instance_hint",
                "page": hint.page,
                "source_element_id": (
                    hint.source_element_id
                    or f"instance-hint:{hint.identity_key}"
                ),
                "hint_identity_key": hint.identity_key,
                "hint_classification": {
                    "entity_kind": hint.entity_kind,
                    "canonical_type": hint.canonical_type,
                },
                "position_pt": {"x": hint.x_pt, "y": hint.y_pt},
                "status": "rejected_instance_hint",
                "reason": reason,
            }
            if extra:
                row.update(extra)
            unresolved_observations.append(row)

        for hint in sorted(self.instance_hints, key=lambda item: item.identity_key):
            source_observation: PdfTextObservation | PdfSymbolObservation | None = None
            if hint.source_element_id is not None:
                lookup = (hint.page, hint.source_element_id)
                source_observations = source_by_key.get(lookup, ())
                if not source_observations:
                    raise ElectricalPdfError(
                        f"instance hint {hint.identity_key!r} references unknown source element "
                        f"{hint.source_element_id!r} on page {hint.page}"
                    )
                if len(source_observations) != 1:
                    reject_instance_hint(
                        hint,
                        "claimed source element id does not identify one unique observation",
                    )
                    continue

                claimants = claims_by_source[lookup]
                if len(claimants) != 1:
                    reject_instance_hint(
                        hint,
                        "claimed source element is claimed by multiple instance hints",
                        extra={
                            "claiming_hint_identity_keys": sorted(
                                claimant.identity_key for claimant in claimants
                            )
                        },
                    )
                    continue

                source_observation = source_observations[0]
                rejection = self._instance_hint_source_rejection(
                    hint,
                    source_observation,
                    texts=texts,
                )
                if rejection is not None:
                    reject_instance_hint(hint, rejection)
                    continue
                claimed_source_ids.add(lookup)

            key = f"hint:{hint.identity_key}"
            candidate = _EntityCandidate(
                key=key,
                entity_kind=hint.entity_kind,
                canonical_type=hint.canonical_type,
                tag=(hint.tag.strip() if hint.tag is not None else None),
                identity_key=f"hint:{hint.identity_key}",
                page=hint.page,
                x_pt=float(hint.x_pt),
                y_pt=float(hint.y_pt),
                confidence=hint.confidence,
                primary_method="explicit-instance-hint",
            )
            source_element_id = hint.source_element_id or f"instance-hint:{hint.identity_key}"
            source_text = (
                source_observation.text
                if isinstance(source_observation, PdfTextObservation)
                else None
            )
            candidate.merge_source(
                element_id=source_element_id,
                text=source_text,
                symbol_name=(
                    source_observation.name
                    if isinstance(source_observation, PdfSymbolObservation)
                    else None
                ),
                x_pt=float(hint.x_pt),
                y_pt=float(hint.y_pt),
                confidence=hint.confidence,
                provenance=_provenance(
                    document,
                    element_id=source_element_id,
                    page=hint.page,
                    method=hint.note,
                    confidence=hint.confidence,
                    source_kind="caller-instance-hint",
                    attributes={
                        "stable_identity_key": hint.identity_key,
                        "entity_kind": hint.entity_kind,
                        "canonical_type": hint.canonical_type,
                    },
                ),
                method="explicit-instance-hint",
            )
            candidates[key] = candidate

        # Shape recognition is primary when the sheet carries its own legend.
        # Text remains independent or reinforcing evidence, never a prerequisite
        # for a legend-matched drawn device.
        for observation in texts:
            if (
                observation.element_id in legend_text_ids
                or observation.element_id in annotation_code_text_ids
            ):
                continue
            if (observation.page, observation.element_id) in claimed_source_ids:
                continue
            for kind, canonical_type, tag, confidence in _text_entity_hits(observation.text):
                shape_compatible = [
                    candidate
                    for candidate in candidates.values()
                    if candidate.shape_recognition is not None
                    and candidate.page == observation.page
                    and candidate.entity_kind == kind
                    and candidate.canonical_type == canonical_type
                    and _distance_pt(
                        candidate.x_pt,
                        candidate.y_pt,
                        observation.x_pt,
                        observation.y_pt,
                    )
                    <= self.symbol_label_radius_pt
                ]
                shape_compatible.sort(
                    key=lambda item: (
                        _distance_pt(
                            item.x_pt,
                            item.y_pt,
                            observation.x_pt,
                            observation.y_pt,
                        ),
                        item.key,
                    )
                )
                if len(shape_compatible) == 1:
                    candidate = shape_compatible[0]
                    if not (kind == "device" and tag in _GENERIC_DEVICE_TAGS):
                        if candidate.tag is None:
                            candidate.tag = tag
                            candidate.identity_key = (
                                f"tag:{kind}:{canonical_type}:{tag}"
                            )
                        elif candidate.tag != tag:
                            unresolved_observations.append(
                                {
                                    "kind": "text_shape_binding",
                                    "page": observation.page,
                                    "source_element_id": observation.element_id,
                                    "position_pt": {
                                        "x": observation.x_pt,
                                        "y": observation.y_pt,
                                    },
                                    "status": "unresolved_identity",
                                    "reason": (
                                        "nearby legend-matched glyph already carries "
                                        "a different stable semantic tag"
                                    ),
                                    "existing_tag": candidate.tag,
                                    "candidate_tag": tag,
                                }
                            )
                            continue
                    candidate.merge_source(
                        element_id=observation.element_id,
                        text=observation.text,
                        symbol_name=None,
                        x_pt=observation.x_pt,
                        y_pt=observation.y_pt,
                        confidence=confidence,
                        provenance=_provenance(
                            document,
                            element_id=observation.element_id,
                            page=observation.page,
                            method="pdf-text-pattern",
                            confidence=confidence,
                            attributes={"source_text": observation.text},
                        ),
                        method="pdf-text-pattern",
                    )
                    continue

                if kind == "device" and tag in _GENERIC_DEVICE_TAGS:
                    unresolved_observations.append(
                        {
                            "kind": "text",
                            "page": observation.page,
                            "source_element_id": observation.element_id,
                            "position_pt": {"x": observation.x_pt, "y": observation.y_pt},
                            "recognized_classification": {
                                "entity_kind": kind,
                                "canonical_type": canonical_type,
                                "confidence": confidence,
                            },
                            "status": "unresolved_identity",
                            "reason": (
                                "generic device class label has no stable instance identity"
                            ),
                        }
                    )
                    continue
                key = f"p{observation.page}:{kind}:{tag}"
                source = _provenance(
                    document,
                    element_id=observation.element_id,
                    page=observation.page,
                    method="pdf-text-pattern",
                    confidence=confidence,
                    attributes={"source_text": observation.text},
                )
                candidate = candidates.get(key)
                if candidate is None:
                    candidate = _EntityCandidate(
                        key=key,
                        entity_kind=kind,
                        canonical_type=canonical_type,
                        tag=tag,
                        identity_key=f"tag:{kind}:{canonical_type}:{tag}",
                        page=observation.page,
                        x_pt=observation.x_pt,
                        y_pt=observation.y_pt,
                        confidence=confidence,
                        primary_method="pdf-text-pattern",
                    )
                    candidates[key] = candidate
                candidate.merge_source(
                    element_id=observation.element_id,
                    text=observation.text,
                    symbol_name=None,
                    x_pt=observation.x_pt,
                    y_pt=observation.y_pt,
                    confidence=confidence,
                    provenance=source,
                    method="pdf-text-pattern",
                )

        for symbol in symbols:
            if (symbol.page, symbol.element_id) in claimed_source_ids:
                continue

            annotation_match = _annotation_code_legend_match(
                symbol,
                annotation_legend_entries.get(symbol.page, ()),
            )
            if annotation_match is not None:
                legend_entry, annotation_recognition = annotation_match
                if legend_entry is None:
                    unresolved_observations.append(
                        {
                            "kind": "symbol",
                            "page": symbol.page,
                            "source_element_id": symbol.element_id,
                            "name": symbol.name,
                            "source_kind": symbol.source_kind,
                            "position_pt": {"x": symbol.x_pt, "y": symbol.y_pt},
                            "annotation_code": annotation_recognition[
                                "annotation_code"
                            ],
                            "annotation_code_recognition": annotation_recognition,
                            "metadata": dict(symbol.metadata),
                            "status": "unresolved_classification",
                            "reason": annotation_recognition["reason"],
                        }
                    )
                    continue

                kind = legend_entry.entity_kind
                canonical_type = legend_entry.canonical_type
                confidence = min(0.96, legend_entry.confidence)
                compatible = [
                    candidate
                    for candidate in candidates.values()
                    if candidate.page == symbol.page
                    and candidate.entity_kind == kind
                    and candidate.canonical_type == canonical_type
                ]
                compatible.sort(
                    key=lambda item: (
                        _distance_pt(
                            item.x_pt,
                            item.y_pt,
                            symbol.x_pt,
                            symbol.y_pt,
                        ),
                        item.key,
                    )
                )
                candidate = (
                    compatible[0]
                    if compatible
                    and _distance_pt(
                        compatible[0].x_pt,
                        compatible[0].y_pt,
                        symbol.x_pt,
                        symbol.y_pt,
                    )
                    <= self.symbol_label_radius_pt
                    else None
                )
                if candidate is None:
                    candidate = _EntityCandidate(
                        key=(
                            f"p{symbol.page}:annotation-code:"
                            f"{symbol.element_id}"
                        ),
                        entity_kind=kind,
                        canonical_type=canonical_type,
                        tag=None,
                        identity_key=(
                            f"annotation:{symbol.source_kind}:{symbol.element_id}"
                        ),
                        page=symbol.page,
                        x_pt=symbol.x_pt,
                        y_pt=symbol.y_pt,
                        confidence=confidence,
                        primary_method="annotation-code",
                    )
                    candidates[candidate.key] = candidate

                status_evidence = _field_status_for_point(
                    page=symbol.page,
                    x_pt=symbol.x_pt,
                    y_pt=symbol.y_pt,
                    texts=texts,
                )
                annotation_recognition = dict(annotation_recognition)
                annotation_recognition["method"] = "annotation-code"
                if status_evidence is not None:
                    status_observation, status_code, status_meaning = status_evidence
                    annotation_recognition.update(
                        {
                            "status": status_code,
                            "status_meaning": status_meaning,
                            "status_source_element_id": (
                                status_observation.element_id
                            ),
                        }
                    )
                candidate.annotation_recognition = annotation_recognition
                candidate.merge_source(
                    element_id=symbol.element_id,
                    text=annotation_recognition["annotation_code"],
                    symbol_name=symbol.name,
                    x_pt=symbol.x_pt,
                    y_pt=symbol.y_pt,
                    confidence=confidence,
                    provenance=_provenance(
                        document,
                        element_id=symbol.element_id,
                        page=symbol.page,
                        method="annotation-code",
                        confidence=confidence,
                        source_kind=symbol.source_kind,
                        attributes={
                            **annotation_recognition,
                            **(
                                {"metadata": dict(symbol.metadata)}
                                if symbol.metadata
                                else {}
                            ),
                        },
                    ),
                    method="annotation-code",
                )
                for label_element_id in (
                    legend_entry.label_source_element_ids
                    or (legend_entry.label.element_id,)
                ):
                    if label_element_id not in candidate.source_element_ids:
                        candidate.source_element_ids.append(label_element_id)
                candidate.provenance.append(
                    _provenance(
                        document,
                        element_id=legend_entry.label.element_id,
                        page=legend_entry.label.page,
                        method="pdf-sheet-legend-type-label",
                        confidence=legend_entry.confidence,
                        attributes={
                            "source_text": legend_entry.label.text,
                            "legend_row_label": legend_entry.label.text,
                            "canonical_type": canonical_type,
                            "annotation_code": annotation_recognition[
                                "annotation_code"
                            ],
                        },
                    )
                )
                if status_evidence is not None:
                    status_observation, status_code, status_meaning = status_evidence
                    if (
                        status_observation.element_id
                        not in candidate.source_element_ids
                    ):
                        candidate.source_element_ids.append(
                            status_observation.element_id
                        )
                    candidate.provenance.append(
                        _provenance(
                            document,
                            element_id=status_observation.element_id,
                            page=status_observation.page,
                            method="pdf-field-status-tag",
                            confidence=0.92,
                            attributes={
                                "source_text": status_observation.text,
                                "status": status_code,
                                "status_meaning": status_meaning,
                                "annotation_code": annotation_recognition[
                                    "annotation_code"
                                ],
                            },
                        )
                    )
                continue

            classification, ranked = _classify_symbol(
                symbol,
                self.symbol_rules,
                ambiguity_margin=self.ambiguity_margin,
            )
            if classification is None:
                unresolved_observations.append(
                    {
                        "kind": "symbol",
                        "page": symbol.page,
                        "source_element_id": symbol.element_id,
                        "name": symbol.name,
                        "source_kind": symbol.source_kind,
                        "position_pt": {"x": symbol.x_pt, "y": symbol.y_pt},
                        "classification_candidates": ranked,
                        "metadata": dict(symbol.metadata),
                    }
                )
                continue

            kind, canonical_type, confidence = classification
            compatible = [
                candidate
                for candidate in candidates.values()
                if candidate.page == symbol.page
                and candidate.entity_kind == kind
                and candidate.canonical_type == canonical_type
            ]
            compatible.sort(
                key=lambda item: (
                    _distance_pt(item.x_pt, item.y_pt, symbol.x_pt, symbol.y_pt),
                    item.key,
                )
            )
            candidate = (
                compatible[0]
                if compatible
                and _distance_pt(
                    compatible[0].x_pt,
                    compatible[0].y_pt,
                    symbol.x_pt,
                    symbol.y_pt,
                )
                <= self.symbol_label_radius_pt
                else None
            )

            if candidate is None:
                native_id = str(symbol.metadata.get("native_id") or "").strip()
                if not native_id:
                    unresolved_observations.append(
                        {
                            "kind": "symbol",
                            "page": symbol.page,
                            "source_element_id": symbol.element_id,
                            "name": symbol.name,
                            "source_kind": symbol.source_kind,
                            "position_pt": {"x": symbol.x_pt, "y": symbol.y_pt},
                            "classification_candidates": ranked,
                            "recognized_classification": {
                                "entity_kind": kind,
                                "canonical_type": canonical_type,
                                "confidence": confidence,
                            },
                            "metadata": dict(symbol.metadata),
                            "status": "unresolved_identity",
                            "reason": (
                                "recognized symbol has no stable semantic tag or native identifier"
                            ),
                        }
                    )
                    continue
                key = f"p{symbol.page}:{kind}:native:{native_id}"
                candidate = _EntityCandidate(
                    key=key,
                    entity_kind=kind,
                    canonical_type=canonical_type,
                    tag=None,
                    identity_key=f"native:{symbol.source_kind}:{native_id}",
                    page=symbol.page,
                    x_pt=symbol.x_pt,
                    y_pt=symbol.y_pt,
                    confidence=confidence,
                    primary_method="pdf-symbol-catalog",
                )
                candidates[key] = candidate

            source = _provenance(
                document,
                element_id=symbol.element_id,
                page=symbol.page,
                method="pdf-symbol-catalog",
                confidence=confidence,
                source_kind=symbol.source_kind,
                attributes={
                    "symbol_name": symbol.name,
                    **({"metadata": dict(symbol.metadata)} if symbol.metadata else {}),
                },
            )
            candidate.merge_source(
                element_id=symbol.element_id,
                text=None,
                symbol_name=symbol.name,
                x_pt=symbol.x_pt,
                y_pt=symbol.y_pt,
                confidence=confidence,
                provenance=source,
                method="pdf-symbol-catalog",
            )

            # A form symbol centroid is useful for text-led recognition, but a
            # legend-matched geometry centroid remains the primary source position.
            if candidate.shape_recognition is None:
                candidate.x_pt = symbol.x_pt
                candidate.y_pt = symbol.y_pt

        vector_symbol_ids: set[str] = set(shape_matched_vector_ids)
        for vector in vectors:
            marker = _simple_rectangle_marker(vector)
            if marker is None:
                continue
            center_x, center_y, half_diagonal = marker
            nearby = [
                candidate
                for candidate in candidates.values()
                if candidate.page == vector.page
                and _distance_pt(candidate.x_pt, candidate.y_pt, center_x, center_y)
                <= half_diagonal + self.vector_symbol_radius_pt
            ]
            nearby.sort(
                key=lambda item: (
                    _distance_pt(item.x_pt, item.y_pt, center_x, center_y),
                    item.key,
                )
            )
            if len(nearby) != 1:
                continue
            candidate = nearby[0]
            vector_symbol_ids.add(vector.element_id)
            vector_provenance = _provenance(
                document,
                element_id=vector.element_id,
                page=vector.page,
                method="pdf-vector-symbol-outline",
                confidence=0.84,
                source_kind=vector.source_kind,
                attributes={
                    "shape": "rectangle",
                    "points_pt": [list(point) for point in vector.points_pt],
                    **(
                        {"metadata": dict(vector.metadata)}
                        if vector.metadata
                        else {}
                    ),
                },
            )
            candidate.merge_source(
                element_id=vector.element_id,
                text=None,
                symbol_name=None,
                x_pt=center_x,
                y_pt=center_y,
                confidence=0.84,
                provenance=vector_provenance,
                method="pdf-vector-symbol-outline",
            )

        for row in unresolved_shape_rows:
            source_ids = set(row.get("source_element_ids", ()))
            if (
                row.get("kind") == "vector_cluster"
                and source_ids
                and source_ids.issubset(vector_symbol_ids)
            ):
                continue
            unresolved_observations.append(row)

        # Attach nearby note/mounting text without pretending it is a host reference.
        attached_note_ids: set[str] = set()
        for candidate in candidates.values():
            for observation in texts:
                if observation.page != candidate.page:
                    continue
                if observation.element_id in candidate.source_element_ids:
                    continue
                if not _looks_like_note(observation.text):
                    continue
                if (
                    _distance_pt(
                        candidate.x_pt,
                        candidate.y_pt,
                        observation.x_pt,
                        observation.y_pt,
                    )
                    <= self.annotation_radius_pt
                ):
                    candidate.texts.append(observation.text)
                    candidate.source_element_ids.append(observation.element_id)
                    candidate.provenance.append(
                        _provenance(
                            document,
                            element_id=observation.element_id,
                            page=observation.page,
                            method="pdf-nearby-annotation",
                            confidence=0.80,
                            attributes={"source_text": observation.text},
                        )
                    )
                    attached_note_ids.add(observation.element_id)

        equipment: list[ElectricalEquipment] = []
        devices: list[ElectricalDevice] = []
        entity_by_page_tag: dict[tuple[int, str], ElectricalEquipment | ElectricalDevice] = {}
        entity_source_positions: dict[str, tuple[int, float, float]] = {}
        # Which text observation supplied each entity's own identity. A text
        # that labels a device must never be read as that same device's circuit
        # annotation; that self-reference is how an ordinary `EVSE-1` label on
        # a sheet whose panel is also named `EVSE` invented a circuit.
        entity_identity_text_owner: dict[str, str] = {}
        identity_owners: dict[str, str] = {}

        for candidate in sorted(candidates.values(), key=lambda item: item.key):
            if candidate.identity_key is None:
                raise ElectricalPdfError(
                    f"{candidate.key} lacks a stable semantic or native identity key"
                )
            prior_owner = identity_owners.setdefault(candidate.identity_key, candidate.key)
            if prior_owner != candidate.key:
                raise ElectricalPdfError(
                    "the same stable semantic identity was recognized at multiple source "
                    f"locations ({prior_owner}, {candidate.key}); reconcile that ambiguity "
                    "before canonicalizing"
                )
            id_kind = "equipment" if candidate.entity_kind == "equipment" else "device"
            entity_id = stable_id(
                id_kind,
                f"pdf-electrical:{document.source_id}:{candidate.identity_key}",
            )
            all_texts = tuple(dict.fromkeys(candidate.texts))
            mounting = _mounting_heights_m(all_texts)
            hosts = _host_hints(all_texts)
            rated_voltage_v, system = _extract_voltage(all_texts)

            transform = transforms[candidate.page]
            lane_attributes: dict[str, Any] = {
                "spatial_status": spatial_status,
                "source_page": candidate.page,
                "source_position_pt": {"x": candidate.x_pt, "y": candidate.y_pt},
                "source_element_ids": sorted(set(candidate.source_element_ids)),
                "stable_identity_key": candidate.identity_key,
                "page_transform": transform.to_attributes(),
                "pose_interpretation": (
                    "source-page position transformed into the canonical frame"
                    if has_explicit_registration
                    else (
                        "page-local PDF position placed in a deterministic separated "
                        "document-local tile; no cross-page or building registration asserted"
                        if document.page_count > 1
                        else (
                            "single-page PDF-local position in metres; no building "
                            "registration asserted"
                        )
                    )
                ),
            }
            if candidate.tag:
                lane_attributes["tag"] = candidate.tag
            if candidate.symbol_names:
                lane_attributes["symbol_names"] = sorted(set(candidate.symbol_names))
            if candidate.shape_recognition is not None:
                lane_attributes["shape_recognition"] = dict(candidate.shape_recognition)
                if candidate.shape_recognition.get("tags"):
                    lane_attributes["tags"] = list(candidate.shape_recognition["tags"])
                if "legend_row_label" in candidate.shape_recognition:
                    lane_attributes["legend_row_label"] = candidate.shape_recognition[
                        "legend_row_label"
                    ]
                    lane_attributes["device_type_label"] = candidate.shape_recognition[
                        "legend_row_label"
                    ]
                if "status" in candidate.shape_recognition:
                    lane_attributes["status"] = candidate.shape_recognition["status"]
                    lane_attributes["status_meaning"] = candidate.shape_recognition[
                        "status_meaning"
                    ]
            if candidate.annotation_recognition is not None:
                lane_attributes["annotation_recognition"] = dict(
                    candidate.annotation_recognition
                )
                lane_attributes["annotation_code"] = candidate.annotation_recognition[
                    "annotation_code"
                ]
                lane_attributes.setdefault(
                    "legend_row_label",
                    candidate.annotation_recognition.get("legend_row_label"),
                )
                lane_attributes.setdefault(
                    "device_type_label",
                    candidate.annotation_recognition.get("legend_row_label"),
                )
                if "status" in candidate.annotation_recognition:
                    lane_attributes.setdefault(
                        "status",
                        candidate.annotation_recognition["status"],
                    )
                    lane_attributes.setdefault(
                        "status_meaning",
                        candidate.annotation_recognition["status_meaning"],
                    )
            if mounting:
                if len(mounting) == 1:
                    lane_attributes["mounting_height_m"] = mounting[0]
                else:
                    lane_attributes["mounting_height_candidates_m"] = mounting
                    lane_attributes["mounting_height_status"] = "ambiguous"
            if hosts:
                if len(hosts) == 1:
                    lane_attributes["host_hint"] = hosts[0]
                else:
                    lane_attributes["host_candidates"] = hosts
                    lane_attributes["host_status"] = "ambiguous"
            note_texts = sorted({text for text in all_texts if _looks_like_note(text)})
            if note_texts:
                lane_attributes["annotations"] = note_texts

            common = {
                "id": entity_id,
                "name": candidate.tag,
                "pose": Pose(position=transform.apply(candidate.x_pt, candidate.y_pt)),
                "level_id": None,
                "space_id": None,
                "host_id": None,
                "system": system,
                "rated_voltage_v": rated_voltage_v,
                "confidence": min(1.0, candidate.confidence),
                "provenance": tuple(
                    sorted(
                        candidate.provenance,
                        key=lambda item: (
                            item.page or 0,
                            item.source_element_id or "",
                            item.method or "",
                        ),
                    )
                ),
                "attributes": {"pdf_electrical": lane_attributes},
            }
            if candidate.entity_kind == "equipment":
                entity = ElectricalEquipment(
                    equipment_type=candidate.canonical_type,
                    **common,
                )
                equipment.append(entity)
            else:
                entity = ElectricalDevice(
                    device_type=candidate.canonical_type,
                    **common,
                )
                devices.append(entity)

            entity_source_positions[entity.id] = (
                candidate.page,
                candidate.x_pt,
                candidate.y_pt,
            )
            for source_element_id in candidate.source_element_ids:
                entity_identity_text_owner.setdefault(source_element_id, entity.id)
            if candidate.tag:
                entity_by_page_tag[(candidate.page, candidate.tag)] = entity

        ports_by_owner_role: dict[tuple[str, str], Port] = {}
        circuit_evidence: dict[str, dict[str, Any]] = {}
        unresolved_circuits: list[dict[str, Any]] = []
        unresolved_topology: list[dict[str, Any]] = []
        topology_resolved_callout_ids: set[str] = set()
        topology_conflicted_callout_ids: set[str] = set()

        def port_for(
            entity: ElectricalEquipment | ElectricalDevice,
            role: str,
            provenance: Provenance,
        ) -> Port:
            key = (entity.id, role)
            existing = ports_by_owner_role.get(key)
            if existing is not None:
                merged_provenance = _merge_provenance(
                    existing.provenance,
                    (provenance,),
                )
                lane_attributes = dict(existing.attributes["pdf_electrical"])
                lane_attributes["evidence_methods"] = sorted(
                    {
                        item.method
                        for item in merged_provenance
                        if item.method is not None
                    }
                )
                merged = replace(
                    existing,
                    confidence=max(existing.confidence, provenance.confidence),
                    provenance=merged_provenance,
                    attributes={"pdf_electrical": lane_attributes},
                )
                ports_by_owner_role[key] = merged
                return merged
            port = Port(
                id=stable_id("port", f"pdf-electrical:{document.source_id}:{entity.id}:{role}"),
                owner_id=entity.id,
                domain="power",
                role=role,
                pose=entity.pose,
                direction=Vector3(x=0.0, y=0.0, z=1.0),
                confidence=provenance.confidence,
                provenance=(provenance,),
                attributes={
                    "pdf_electrical": {
                        "inferred_for_circuit_semantics": True,
                        "spatial_status": entity.attributes["pdf_electrical"]["spatial_status"],
                        "evidence_methods": (
                            [provenance.method]
                            if provenance.method is not None
                            else []
                        ),
                    }
                },
            )
            ports_by_owner_role[key] = port
            return port

        def evidence_bucket(
            *,
            circuit_id: str,
            name: str,
            source_port_id: str,
            circuit_number: str | None,
        ) -> dict[str, Any]:
            bucket = circuit_evidence.setdefault(
                circuit_id,
                {
                    "name": name,
                    "source_port_id": source_port_id,
                    "load_port_ids": set(),
                    "circuit_number": circuit_number,
                    "voltage_v": set(),
                    "poles": set(),
                    "phase": set(),
                    "confidence": set(),
                    "evidence_methods": set(),
                    "provenance": [],
                    "port_provenance": {},
                    "evidence": [],
                },
            )
            if bucket["source_port_id"] != source_port_id:
                raise ElectricalPdfError(
                    f"circuit {circuit_number or circuit_id} resolved to more than one source port"
                )
            if bucket["circuit_number"] != circuit_number:
                raise ElectricalPdfError(
                    f"circuit {circuit_id} resolved to conflicting circuit numbers"
                )
            return bucket

        def record_port_provenance(
            bucket: dict[str, Any],
            port_ids: Iterable[str],
            provenances: Iterable[Provenance],
        ) -> None:
            provenance_items = tuple(provenances)
            for port_id in port_ids:
                bucket["port_provenance"].setdefault(port_id, []).extend(
                    provenance_items
                )

        def mark_topology_text_conflict(
            topology_row: dict[str, Any],
            nearby_callouts: Iterable[PdfTextObservation],
        ) -> None:
            callout_ids = sorted(
                {observation.element_id for observation in nearby_callouts}
            )
            topology_row["circuit_callout_ids"] = callout_ids
            topology_conflicted_callout_ids.update(callout_ids)

        recognized_panels = {
            entity.name: entity
            for entity in equipment
            if entity.equipment_type == "panelboard" and entity.name
        }
        schedule_circuits, schedule_text_ids = _panel_schedule_circuits(
            texts,
            vectors,
            recognized_panels=set(recognized_panels),
        )

        def record_explicit_circuit(
            *,
            observation: PdfTextObservation,
            panel_tag: str,
            numbers: Sequence[int],
            load_entities: Sequence[ElectricalDevice],
            method: str,
            confidence: float,
            branch_vector_ids: Sequence[str] = (),
        ) -> None:
            source_entity = recognized_panels[panel_tag]
            shared_raceway_group = (
                stable_id(
                    "circuit-group",
                    (
                        f"pdf-electrical:{document.source_id}:"
                        f"{observation.element_id}:{panel_tag}:"
                        + ",".join(str(number) for number in numbers)
                    ),
                )
                if len(numbers) > 1
                else None
            )
            provenance = _provenance(
                document,
                element_id=observation.element_id,
                page=observation.page,
                method=method,
                confidence=confidence,
                attributes={
                    "source_text": observation.text,
                    "panel_tag": panel_tag,
                    "circuit_numbers": list(numbers),
                    "schedule_validated": panel_tag in schedule_circuits,
                    **(
                        {"branch_vector_element_ids": list(branch_vector_ids)}
                        if branch_vector_ids
                        else {}
                    ),
                },
            )
            for number in numbers:
                circuit_number = str(number)
                number_provenance = replace(
                    provenance,
                    attributes={
                        **dict(provenance.attributes),
                        "circuit_number": circuit_number,
                        **(
                            {"shared_raceway_group": shared_raceway_group}
                            if shared_raceway_group is not None
                            else {}
                        ),
                    },
                )
                source_port = port_for(
                    source_entity,
                    f"source:circuit:{circuit_number}",
                    number_provenance,
                )
                load_ports = tuple(
                    port_for(
                        entity,
                        f"sink:circuit:{circuit_number}",
                        number_provenance,
                    )
                    for entity in sorted(load_entities, key=lambda item: item.id)
                )
                circuit_id = stable_id(
                    "circuit",
                    (
                        f"pdf-electrical:{document.source_id}:"
                        f"{source_entity.id}:{circuit_number}"
                    ),
                )
                bucket = evidence_bucket(
                    circuit_id=circuit_id,
                    name=f"{panel_tag} {circuit_number}",
                    source_port_id=source_port.id,
                    circuit_number=circuit_number,
                )
                bucket["load_port_ids"].update(port.id for port in load_ports)
                record_port_provenance(
                    bucket,
                    (source_port.id, *(port.id for port in load_ports)),
                    (number_provenance,),
                )
                bucket["confidence"].add(confidence)
                bucket["evidence_methods"].add(method)
                bucket["provenance"].append(number_provenance)
                bucket["evidence"].append(
                    {
                        "page": observation.page,
                        "source_element_id": observation.element_id,
                        "source_text": observation.text,
                        "method": method,
                        "panel_tag": panel_tag,
                        "circuit_number": circuit_number,
                        "circuit_numbers": list(numbers),
                        "load_ids": [entity.id for entity in load_entities],
                        "schedule_validated": panel_tag in schedule_circuits,
                        "confidence": confidence,
                        "status": "resolved",
                        **(
                            {"shared_raceway_group": shared_raceway_group}
                            if shared_raceway_group is not None
                            else {}
                        ),
                    }
                )

        recognized_panel_tag_re = re.compile(
            r"(?<![A-Z0-9_.-])(?:"
            + "|".join(
                re.escape(panel)
                for panel in sorted(recognized_panels, key=lambda item: (-len(item), item))
            )
            + r")-\d+(?:\s*,\s*\d+)*\b",
            re.IGNORECASE,
        ) if recognized_panels else None
        explicit_circuit_tag_texts = [
            observation
            for observation in texts
            if observation.element_id not in schedule_text_ids
            and recognized_panel_tag_re is not None
            and recognized_panel_tag_re.search(observation.text)
        ]
        homerun_arrowheads = [
            vector
            for vector in vectors
            if _homerun_arrowhead_apex(vector) is not None
        ]
        homerun_arrowhead_ids = {vector.element_id for vector in homerun_arrowheads}

        # Branch-run candidates are every plausible wiring vector, NOT only
        # those near an already-recognized panel tag. Seeding from annotations
        # meant an arrowed leader whose annotation does not parse was never
        # assembled at all, so it could not be diagnosed -- which made
        # `no_panel_token` unreachable for exactly the case #72 names.
        circuit_vectors = [
            vector
            for vector in vectors
            if vector.element_id not in homerun_arrowhead_ids
            and not vector.closed
            and not _vector_contains_bezier(vector)
            and vector.element_id not in vector_symbol_ids
            and vector.element_id not in glyph_vector_ids
            and any(first != second for first, second in _vector_segments(vector))
        ]

        # Spatial index so neighbour lookup is local instead of all-pairs. Cells
        # are keyed by page, and each vector is registered along its segments at
        # cell-sized steps so a long run is found from anywhere it passes, not
        # only at its vertices.
        snap_tolerance_pt = self.topology_snap_radius_pt
        cell_pt = max(snap_tolerance_pt * 4.0, 8.0)

        def circuit_cells(vector: PdfVectorPathObservation) -> set[tuple[int, int]]:
            cells: set[tuple[int, int]] = set()
            for first, second in _vector_segments(vector) or ():
                span = _distance_pt(first[0], first[1], second[0], second[1])
                steps = int(span / cell_pt) + 1
                for step in range(steps + 1):
                    ratio = step / steps
                    cells.add(
                        (
                            int((first[0] + (second[0] - first[0]) * ratio) // cell_pt),
                            int((first[1] + (second[1] - first[1]) * ratio) // cell_pt),
                        )
                    )
            for point in vector.points_pt:
                cells.add((int(point[0] // cell_pt), int(point[1] // cell_pt)))
            return cells

        assigned: dict[int, int] = {}
        circuit_vector_cells: list[set[tuple[int, int]]] = []
        circuit_grid: dict[tuple[int, int, int], set[int]] = {}
        for index, vector in enumerate(circuit_vectors):
            cells = circuit_cells(vector)
            circuit_vector_cells.append(cells)
            for cell_x, cell_y in cells:
                circuit_grid.setdefault((vector.page, cell_x, cell_y), set()).add(index)

        def claim_circuit_vector(index: int, component_key: int) -> None:
            """Take a vector out of the index as it joins a component.

            A claimed vector can never be claimed again, so leaving it in the
            grid only makes every later neighbour query re-walk it. Removing it
            is what keeps a dense cluster from costing one full scan per member.
            """
            assigned[index] = component_key
            vector = circuit_vectors[index]
            for cell_x, cell_y in circuit_vector_cells[index]:
                bucket = circuit_grid.get((vector.page, cell_x, cell_y))
                if bucket is not None:
                    bucket.discard(index)

        def circuit_neighbors_of_path(
            path: PdfVectorPathObservation,
            grid: dict[tuple[int, int, int], list[int]],
            cells: set[tuple[int, int]],
        ) -> set[int]:
            found: set[int] = set()
            for cell_x, cell_y in cells:
                for offset_x in (-1, 0, 1):
                    for offset_y in (-1, 0, 1):
                        found.update(
                            grid.get((path.page, cell_x + offset_x, cell_y + offset_y), ())
                        )
            return found

        def circuit_neighbors(index: int) -> set[int]:
            vector = circuit_vectors[index]
            found: set[int] = set()
            for cell_x, cell_y in circuit_vector_cells[index]:
                for offset_x in (-1, 0, 1):
                    for offset_y in (-1, 0, 1):
                        found.update(
                            circuit_grid.get(
                                (vector.page, cell_x + offset_x, cell_y + offset_y),
                                (),
                            )
                        )
            found.discard(index)
            return found

        # Closed paths are not branch members, but an unclaimed outline that
        # touches a branch is positive evidence that it entered architecture.
        # Keep recognized symbol outlines out of this index: a branch may
        # legitimately terminate at its device's own closed mark.
        unclaimed_closed = [
            vector for vector in vectors
            if vector.closed
            and vector.element_id not in vector_symbol_ids
        ]
        closed_grid: dict[tuple[int, int, int], list[int]] = {}
        for index, vector in enumerate(unclaimed_closed):
            for cell_x, cell_y in circuit_cells(vector):
                closed_grid.setdefault((vector.page, cell_x, cell_y), []).append(index)

        def touching_unclaimed_closed(
            component: Sequence[PdfVectorPathObservation],
        ) -> PdfVectorPathObservation | None:
            for branch in component:
                for index in sorted(
                    circuit_neighbors_of_path(branch, closed_grid, circuit_cells(branch)),
                    key=lambda item: unclaimed_closed[item].element_id,
                ):
                    closed = unclaimed_closed[index]
                    if _paths_cross_or_touch(branch, closed, tolerance_pt=snap_tolerance_pt):
                        return closed
            return None

        # Assemble components by growing outward from each homerun arrowhead.
        # The arrowhead is the signature of a homerun, so this both bounds the
        # work by the (small) arrowhead count and finds arrowed runs whose
        # annotation is absent or unparseable.
        def component_encloses_area(
            component: Sequence[PdfVectorPathObservation],
        ) -> PdfVectorPathObservation | None:
            """Return a vector proving this component encloses space.

            Branch wiring distributes radially: it tees, but it never loops
            back on itself, so it spans no area. Architectural linework --
            wall grids, partitions, hatching -- exists to enclose space, so it
            does.

            The graph is built on CONTACT POINTS, not on vectors. A tee drawn
            as three segments leaving one point is ordinary wiring, yet each of
            those segments touches the other two, so a vector-level graph sees
            a triangle and calls it a loop. With points as nodes that tee is a
            single node of degree three: still a tree, still lawful.

            Points are clustered by the same snap relation the component
            builder used, through a grid of neighbouring cells rather than by
            rounding each coordinate independently. Independent rounding made
            the verdict depend on where identical geometry happened to fall
            against the bin boundaries, so one run with a drafting gap could
            split into two nodes and read as a two-edge loop.

            Both the adjacency scan and the clustering are spatially local, and
            the search returns on the first cycle it sees rather than after a
            complete pass.

            Known gap, tracked in #76: architecture drawn as a simple chain is
            also a tree, so this does not catch it. Connectivity alone cannot
            separate a chain of wall segments from a chain of conduit; that
            needs evidence this lane does not currently receive.
            """
            tolerance = self.topology_snap_radius_pt
            cell = max(tolerance * 2.0, 1.0)

            def cells_around(point: tuple[float, float]):
                base_x, base_y = int(point[0] // cell), int(point[1] // cell)
                for offset_x in (-1, 0, 1):
                    for offset_y in (-1, 0, 1):
                        yield (base_x + offset_x, base_y + offset_y)

            # Local index over this component's own members.
            member_grid: dict[tuple[int, int], list[int]] = {}
            member_cells: list[set[tuple[int, int]]] = []
            for index, vector in enumerate(component):
                own = circuit_cells(vector)
                member_cells.append(own)
                for key in own:
                    member_grid.setdefault(key, []).append(index)

            # Contact points per member, found only against local candidates.
            def midpoint(vector: PdfVectorPathObservation) -> tuple[float, float]:
                points = vector.points_pt
                middle = points[len(points) // 2]
                return (float(middle[0]), float(middle[1]))

            # Each member contributes its own midpoint as a node. Two strokes
            # painted over each other share that midpoint and collapse to one
            # edge, while two genuinely different paths between the same pair
            # of ends keep distinct midpoints and still form a real loop.
            contacts: dict[int, list[tuple[float, float]]] = {
                index: [vector.points_pt[0], midpoint(vector), vector.points_pt[-1]]
                for index, vector in enumerate(component)
            }
            for index, vector in enumerate(component):
                candidates: set[int] = set()
                for cell_x, cell_y in member_cells[index]:
                    for offset_x in (-1, 0, 1):
                        for offset_y in (-1, 0, 1):
                            candidates.update(
                                member_grid.get((cell_x + offset_x, cell_y + offset_y), ())
                            )
                for other_index in candidates:
                    if other_index <= index:
                        continue
                    other = component[other_index]
                    if not _paths_touch(vector, other, tolerance_pt=tolerance):
                        continue
                    for point in (other.points_pt[0], other.points_pt[-1]):
                        if _point_path_distance_pt(point, vector) <= tolerance:
                            contacts[index].append(point)
                            contacts[other_index].append(point)
                    for point in (vector.points_pt[0], vector.points_pt[-1]):
                        if _point_path_distance_pt(point, other) <= tolerance:
                            contacts[index].append(point)
                            contacts[other_index].append(point)

            # Cluster points by proximity, again only against local candidates.
            points: list[tuple[float, float]] = [
                point for values in contacts.values() for point in values
            ]
            point_grid: dict[tuple[int, int], list[int]] = {}
            for index, point in enumerate(points):
                point_grid.setdefault(
                    (int(point[0] // cell), int(point[1] // cell)), []
                ).append(index)

            point_parent = list(range(len(points)))

            def point_find(index: int) -> int:
                while point_parent[index] != index:
                    point_parent[index] = point_parent[point_parent[index]]
                    index = point_parent[index]
                return index

            for index, point in enumerate(points):
                for key in cells_around(point):
                    for other_index in point_grid.get(key, ()):
                        if other_index <= index:
                            continue
                        other = points[other_index]
                        if (
                            _distance_pt(point[0], point[1], other[0], other[1])
                            <= tolerance
                        ):
                            left, right = point_find(index), point_find(other_index)
                            if left != right:
                                point_parent[max(left, right)] = min(left, right)

            node_of: dict[tuple[float, float], int] = {}
            for index, point in enumerate(points):
                node_of.setdefault(point, point_find(index))

            parent: dict[int, int] = {}

            def find(key: int) -> int:
                parent.setdefault(key, key)
                while parent[key] != key:
                    parent[key] = parent[parent[key]]
                    key = parent[key]
                return key

            # A cycle needs DISTINCT edges. Two vectors drawing the same edge
            # are parallel edges in a multigraph, and parallel edges enclose no
            # area, so they must not read as a loop. Overprinted and duplicated
            # strokes are ordinary in exported CAD.
            drawn: set[tuple[int, int]] = set()
            for index, vector in enumerate(component):
                start_point = vector.points_pt[0]
                seen: dict[int, tuple[float, float]] = {}
                for point in contacts[index]:
                    seen.setdefault(node_of[point], point)
                ordered = sorted(
                    seen.items(),
                    key=lambda item: _distance_pt(
                        start_point[0], start_point[1], item[1][0], item[1][1]
                    ),
                )
                # Each span between consecutive contacts is one edge.
                for (left, _lp), (right, _rp) in zip(ordered, ordered[1:]):
                    if left == right:
                        continue
                    edge = (min(left, right), max(left, right))
                    if edge in drawn:
                        continue
                    drawn.add(edge)
                    left_root, right_root = find(left), find(right)
                    if left_root == right_root:
                        return vector
                    parent[left_root] = right_root
            return None

        circuit_components: dict[int, list[PdfVectorPathObservation]] = {}
        for arrow_position, arrowhead in enumerate(homerun_arrowheads):
            seeds = [
                index
                for index in circuit_neighbors_of_path(
                    arrowhead,
                    circuit_grid,
                    circuit_cells(arrowhead),
                )
                if circuit_vectors[index].page == arrowhead.page
                and index not in assigned
                and _paths_touch(
                    arrowhead,
                    circuit_vectors[index],
                    tolerance_pt=snap_tolerance_pt,
                )
            ]
            if not seeds:
                continue
            component_key = arrow_position
            # Claim membership at ENQUEUE, not at dequeue. Marking on dequeue
            # left every queued-but-unclaimed vector visible to each subsequent
            # pop, so a densely touching cluster re-compared the same pairs
            # once per member and the scan stayed quadratic. Claiming on entry
            # means each vector is compared once.
            members: list[int] = []
            queue: list[int] = []
            for seed in seeds:
                if seed in assigned:
                    continue
                claim_circuit_vector(seed, component_key)
                members.append(seed)
                queue.append(seed)
            while queue:
                index = queue.pop()
                for neighbor in circuit_neighbors(index):
                    if neighbor in assigned:
                        continue
                    if not _paths_touch(
                        circuit_vectors[index],
                        circuit_vectors[neighbor],
                        tolerance_pt=snap_tolerance_pt,
                    ):
                        continue
                    claim_circuit_vector(neighbor, component_key)
                    members.append(neighbor)
                    queue.append(neighbor)
            if members:
                circuit_components[component_key] = [
                    circuit_vectors[index] for index in sorted(members)
                ]

        consumed_explicit_tag_ids: set[str] = set()
        homerun_components = [
            component
            for component in sorted(
                circuit_components.values(),
                key=lambda items: (
                    items[0].page,
                    tuple(sorted(item.element_id for item in items)),
                ),
            )
        ]

        # One annotation cannot circuit two disconnected branch runs. Count how
        # many components each annotation is in range of first, so an
        # annotation claimed by more than one fails closed as ambiguous instead
        # of silently attaching every run it happens to sit near.
        annotation_claim_counts: Counter[str] = Counter()
        for component in homerun_components:
            for observation in texts:
                if (
                    observation.page != component[0].page
                    or observation.element_id in schedule_text_ids
                    or not _is_explicit_circuit_annotation(
                        observation.text,
                        recognized_panels=recognized_panels,
                    )
                ):
                    continue
                if min(
                    _point_path_distance_pt(
                        (observation.x_pt, observation.y_pt),
                        vector,
                    )
                    for vector in component
                ) <= self.topology_annotation_radius_pt:
                    annotation_claim_counts[observation.element_id] += 1

        for component in homerun_components:
            page = component[0].page
            load_entities = [
                entity
                for entity in devices
                if entity_source_positions[entity.id][0] == page
                and min(
                    _point_path_distance_pt(
                        (
                            entity_source_positions[entity.id][1],
                            entity_source_positions[entity.id][2],
                        ),
                        vector,
                    )
                    for vector in component
                ) <= self.topology_endpoint_radius_pt
            ]
            if not load_entities:
                # An arrowed leader touching no recognized device is a
                # dimension or annotation leader, not a homerun. #72 names this
                # case directly, and it is the accurate code for it.
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": sorted(
                            item.element_id for item in component
                        )[0],
                        "status": "unresolved",
                        "reason_code": "arrow_not_associated_to_device",
                        "reason": (
                            "homerun arrow is not associated with a recognized device"
                        ),
                    }
                )
                continue
            enclosing = touching_unclaimed_closed(component) or component_encloses_area(component)
            if enclosing is not None:
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": sorted(
                            item.element_id for item in component
                        )[0],
                        "status": "unresolved",
                        "reason_code": "branch_run_not_isolated",
                        "reason": (
                            "arrowed leader reaches geometry that encloses "
                            "space, which branch wiring does not do"
                        ),
                        "cycle_element_id": enclosing.element_id,
                        "component_vector_count": len(component),
                    }
                )
                continue
            nearby_annotations = [
                observation
                for observation in texts
                if observation.page == page
                and observation.element_id not in schedule_text_ids
                and min(
                    _point_path_distance_pt(
                        (observation.x_pt, observation.y_pt),
                        vector,
                    )
                    for vector in component
                ) <= self.topology_annotation_radius_pt
            ]
            parsed = [
                (
                    observation,
                    *_parse_explicit_circuit_tag(
                        observation.text,
                        recognized_panels=set(recognized_panels),
                        schedule_circuits=schedule_circuits,
                    ),
                )
                for observation in nearby_annotations
                if _is_explicit_circuit_annotation(
                    observation.text,
                    recognized_panels=recognized_panels,
                )
            ]
            if not parsed:
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": sorted(
                            item.element_id for item in component
                        )[0],
                        "source_element_ids": sorted(
                            item.element_id for item in component
                        ),
                        "status": "unresolved",
                        "reason_code": "no_panel_token",
                        "reason": (
                            "arrowed leader has no explicit panel/circuit annotation"
                        ),
                    }
                )
                continue
            if len(parsed) != 1:
                for observation, panel_tag, numbers, reason_code in parsed:
                    # Consume every competing annotation. Without this the
                    # direct-device-tag pass below re-reads the same text and
                    # resolves it anyway, which is the opposite of failing
                    # closed on an ambiguous association.
                    consumed_explicit_tag_ids.add(observation.element_id)
                    unresolved_circuits.append(
                        {
                            "kind": "homerun",
                            "page": page,
                            "source_element_id": observation.element_id,
                            "source_text": observation.text,
                            "panel_tag": panel_tag,
                            "circuit_numbers": list(numbers),
                            "status": "unresolved",
                            "reason_code": (
                                reason_code or "conflicting_homerun_annotations"
                            ),
                            "reason": (
                                "homerun does not have one unambiguous circuit annotation"
                            ),
                        }
                    )
                continue
            observation, panel_tag, numbers, reason_code = parsed[0]
            consumed_explicit_tag_ids.add(observation.element_id)
            if annotation_claim_counts.get(observation.element_id, 0) > 1:
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": observation.element_id,
                        "source_text": observation.text,
                        "panel_tag": panel_tag,
                        "circuit_numbers": list(numbers),
                        "status": "unresolved",
                        "reason_code": "ambiguous_homerun_association",
                        "reason": (
                            "one circuit annotation is in range of more than one "
                            "disconnected branch run"
                        ),
                    }
                )
                continue
            if reason_code is not None or panel_tag is None:
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": observation.element_id,
                        "source_text": observation.text,
                        "panel_tag": panel_tag,
                        "circuit_numbers": list(numbers),
                        "status": "unresolved",
                        "reason_code": reason_code,
                        "reason": "homerun annotation failed closed",
                    }
                )
                continue
            # A text used as the circuit annotation is not itself a load on the
            # circuit it names. This is what keeps an ordinary `EVSE-1` device
            # label from circuiting the device it labels, while still letting a
            # genuine `REC-1` annotation resolve on a sheet whose panel is also
            # named `REC`.
            annotation_owner_id = entity_identity_text_owner.get(observation.element_id)
            if annotation_owner_id is not None:
                load_entities = [
                    entity for entity in load_entities if entity.id != annotation_owner_id
                ]
            if not load_entities:
                unresolved_circuits.append(
                    {
                        "kind": "homerun",
                        "page": page,
                        "source_element_id": observation.element_id,
                        "source_text": observation.text,
                        "panel_tag": panel_tag,
                        "circuit_numbers": list(numbers),
                        "status": "unresolved",
                        "reason_code": "arrow_not_associated_to_device",
                        "reason": (
                            "homerun arrow is not associated with a recognized device"
                        ),
                    }
                )
                continue
            record_explicit_circuit(
                observation=observation,
                panel_tag=panel_tag,
                numbers=numbers,
                load_entities=load_entities,
                method="pdf-homerun-annotation",
                confidence=0.96,
                branch_vector_ids=sorted(
                    item.element_id for item in component
                ),
            )

        for observation in texts:
            if (
                observation.element_id in consumed_explicit_tag_ids
                or observation.element_id in schedule_text_ids
                or not _is_explicit_circuit_annotation(
                    observation.text,
                    recognized_panels=recognized_panels,
                )
            ):
                continue
            panel_tag, numbers, reason_code = _parse_explicit_circuit_tag(
                observation.text,
                recognized_panels=set(recognized_panels),
                schedule_circuits=schedule_circuits,
            )
            # A direct tag has no leader to trace. Use the same configurable
            # local annotation association radius as the homerun pass, rather
            # than a second hard-coded cutoff with different boundary behavior.
            nearby_devices = [
                entity
                for entity in devices
                if entity_source_positions[entity.id][0] == observation.page
                and _distance_pt(
                    observation.x_pt,
                    observation.y_pt,
                    entity_source_positions[entity.id][1],
                    entity_source_positions[entity.id][2],
                ) <= self.topology_annotation_radius_pt
            ]
            if len(nearby_devices) == 1 and entity_identity_text_owner.get(
                observation.element_id
            ) == nearby_devices[0].id:
                # This text IS that device's identity label. Reading it as the
                # device's own circuit assignment is self-referential and
                # states no circuit; #72 forbids inventing one.
                continue
            if reason_code is not None or panel_tag is None or len(nearby_devices) != 1:
                unresolved_circuits.append(
                    {
                        "kind": "device_circuit_tag",
                        "page": observation.page,
                        "source_element_id": observation.element_id,
                        "source_text": observation.text,
                        "panel_tag": panel_tag,
                        "circuit_numbers": list(numbers),
                        "status": "unresolved",
                        "reason_code": (
                            reason_code
                            or (
                                "arrow_not_associated_to_device"
                                if not nearby_devices
                                else "ambiguous_device_association"
                            )
                        ),
                        "reason": "direct circuit tag failed closed",
                    }
                )
                continue
            record_explicit_circuit(
                observation=observation,
                panel_tag=panel_tag,
                numbers=numbers,
                load_entities=nearby_devices,
                method="pdf-device-circuit-tag",
                confidence=0.94,
            )

        for observation in texts:
            circuit_match = _CIRCUIT_RE.search(observation.text)
            if not circuit_match:
                continue
            circuit_number = circuit_match.group("number").upper()
            panel_match = _PANEL_RE.search(observation.text)
            panel_tag = _normalize_tag(panel_match.group("tag")) if panel_match else None
            source_entity = (
                entity_by_page_tag.get((observation.page, panel_tag))
                if panel_tag
                else None
            )
            load_entities: list[ElectricalDevice] = []
            for (page, tag), entity in sorted(entity_by_page_tag.items()):
                if page != observation.page or not isinstance(entity, ElectricalDevice):
                    continue
                if re.search(
                    rf"(?<![A-Z0-9_.-]){re.escape(tag)}(?![A-Z0-9_.-])",
                    observation.text,
                    re.IGNORECASE,
                ):
                    load_entities.append(entity)

            evidence = {
                "page": observation.page,
                "source_element_id": observation.element_id,
                "source_text": observation.text,
                "method": "pdf-circuit-text-link",
                "circuit_number": circuit_number,
                "panel_tag": panel_tag,
                "load_ids": [entity.id for entity in load_entities],
            }
            if not isinstance(source_entity, ElectricalEquipment) or not load_entities:
                evidence["status"] = "unresolved"
                evidence["missing"] = [
                    label
                    for label, missing in (
                        ("source_panel", not isinstance(source_entity, ElectricalEquipment)),
                        ("load", not load_entities),
                    )
                    if missing
                ]
                unresolved_circuits.append(evidence)
                continue

            voltage_v, _ = _extract_voltage((observation.text,))
            poles_match = _POLES_RE.search(observation.text)
            phase_match = _PHASE_RE.search(observation.text)
            poles = int(poles_match.group("poles")) if poles_match else None
            phase = f"{phase_match.group('phase')}ph" if phase_match else None
            circuit_confidence = 0.92
            source_provenance = _provenance(
                document,
                element_id=observation.element_id,
                page=observation.page,
                method="pdf-circuit-text-link",
                confidence=circuit_confidence,
                attributes={"source_text": observation.text},
            )
            source_port = port_for(source_entity, "source", source_provenance)
            load_ports = tuple(
                port_for(entity, "sink", source_provenance)
                for entity in sorted(load_entities, key=lambda item: item.id)
            )
            circuit_id = stable_id(
                "circuit",
                f"pdf-electrical:{document.source_id}:{source_entity.id}:{circuit_number}",
            )
            bucket = evidence_bucket(
                circuit_id=circuit_id,
                name=f"{panel_tag} {circuit_number}",
                source_port_id=source_port.id,
                circuit_number=circuit_number,
            )
            bucket["load_port_ids"].update(port.id for port in load_ports)
            record_port_provenance(
                bucket,
                (source_port.id, *(port.id for port in load_ports)),
                (source_provenance,),
            )
            if voltage_v is not None:
                bucket["voltage_v"].add(voltage_v)
            if poles is not None:
                bucket["poles"].add(poles)
            if phase is not None:
                bucket["phase"].add(phase)
            bucket["confidence"].add(circuit_confidence)
            bucket["evidence_methods"].add("pdf-circuit-text-link")
            bucket["provenance"].append(source_provenance)
            evidence.update(
                {
                    "voltage_v": voltage_v,
                    "poles": poles,
                    "phase": phase,
                    "status": "resolved",
                }
            )
            bucket["evidence"].append(evidence)

        topology_vectors = [
            vector
            for vector in vectors
            if not vector.closed
            and not _vector_contains_bezier(vector)
            and vector.element_id not in vector_symbol_ids
            and vector.element_id not in glyph_vector_ids
            and sum(
                _distance_pt(first[0], first[1], second[0], second[1])
                for first, second in _vector_segments(vector)
            )
            >= 12.0
        ]
        parent = list(range(len(topology_vectors)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first_index: int, second_index: int) -> None:
            first_root = find(first_index)
            second_root = find(second_index)
            if first_root != second_root:
                if first_root < second_root:
                    parent[second_root] = first_root
                else:
                    parent[first_root] = second_root

        for first_index, first_vector in enumerate(topology_vectors):
            for second_index in range(first_index + 1, len(topology_vectors)):
                second_vector = topology_vectors[second_index]
                if _paths_touch(
                    first_vector,
                    second_vector,
                    tolerance_pt=self.topology_snap_radius_pt,
                ):
                    union(first_index, second_index)

        topology_components: dict[int, list[PdfVectorPathObservation]] = {}
        for index, vector in enumerate(topology_vectors):
            topology_components.setdefault(find(index), []).append(vector)

        all_entities: tuple[ElectricalEquipment | ElectricalDevice, ...] = tuple(
            sorted((*equipment, *devices), key=lambda item: item.id)
        )
        supported_sources = {"panelboard", "switchboard"}

        for component in sorted(
            topology_components.values(),
            key=lambda items: (
                items[0].page,
                tuple(sorted(item.element_id for item in items)),
            ),
        ):
            page = component[0].page
            element_ids = sorted(item.element_id for item in component)
            attached: dict[str, ElectricalEquipment | ElectricalDevice] = {}
            ambiguous_endpoints: list[dict[str, Any]] = []

            for vector in component:
                for endpoint in (vector.points_pt[0], vector.points_pt[-1]):
                    nearby: list[
                        tuple[float, ElectricalEquipment | ElectricalDevice]
                    ] = []
                    for entity in all_entities:
                        source_page, x_pt, y_pt = entity_source_positions[entity.id]
                        if source_page != page:
                            continue
                        distance = _distance_pt(
                            endpoint[0],
                            endpoint[1],
                            x_pt,
                            y_pt,
                        )
                        if distance <= self.topology_endpoint_radius_pt:
                            nearby.append((distance, entity))
                    nearby.sort(key=lambda item: (item[0], item[1].id))
                    if len(nearby) > 1:
                        ambiguous_endpoints.append(
                            {
                                "point_pt": {"x": endpoint[0], "y": endpoint[1]},
                                "candidate_entity_ids": [
                                    entity.id for _, entity in nearby
                                ],
                            }
                        )
                    elif nearby:
                        attached[nearby[0][1].id] = nearby[0][1]

            if not attached:
                continue

            topology_row: dict[str, Any] = {
                "page": page,
                "source_element_ids": element_ids,
                "attached_entity_ids": sorted(attached),
            }
            if ambiguous_endpoints:
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "ambiguous_endpoint_attachment",
                        "ambiguous_endpoints": ambiguous_endpoints,
                    }
                )
                unresolved_topology.append(topology_row)
                continue

            if len(attached) < 2:
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "incomplete_topology",
                    }
                )
                unresolved_topology.append(topology_row)
                continue

            attached_equipment = [
                entity
                for entity in attached.values()
                if isinstance(entity, ElectricalEquipment)
            ]
            source_entities = [
                entity
                for entity in attached_equipment
                if entity.equipment_type in supported_sources
            ]
            load_entities = [
                entity
                for entity in attached.values()
                if isinstance(entity, ElectricalDevice)
            ]
            if (
                len(source_entities) != 1
                or len(attached_equipment) != 1
                or not load_entities
            ):
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "source_or_load_ambiguous",
                        "source_candidate_ids": sorted(
                            entity.id for entity in source_entities
                        ),
                        "equipment_ids": sorted(
                            entity.id for entity in attached_equipment
                        ),
                        "load_candidate_ids": sorted(
                            entity.id for entity in load_entities
                        ),
                    }
                )
                unresolved_topology.append(topology_row)
                continue

            source_entity = source_entities[0]
            nearby_callouts = [
                observation
                for observation in texts
                if observation.page == page
                and _CIRCUIT_RE.search(observation.text)
                and min(
                    _point_path_distance_pt(
                        (observation.x_pt, observation.y_pt),
                        vector,
                    )
                    for vector in component
                )
                <= self.topology_annotation_radius_pt
            ]
            nearby_callouts.sort(key=lambda item: item.element_id)
            circuit_numbers = {
                match.group("number").upper()
                for observation in nearby_callouts
                if (match := _CIRCUIT_RE.search(observation.text))
            }
            if len(circuit_numbers) > 1:
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "conflicting_circuit_callouts",
                        "circuit_numbers": sorted(circuit_numbers),
                    }
                )
                mark_topology_text_conflict(topology_row, nearby_callouts)
                unresolved_topology.append(topology_row)
                continue

            panel_tags = {
                _normalize_tag(match.group("tag"))
                for observation in nearby_callouts
                if (match := _PANEL_RE.search(observation.text))
            }
            if panel_tags and (
                source_entity.name is None
                or any(tag != source_entity.name for tag in panel_tags)
            ):
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "panel_callout_conflict",
                        "panel_tags": sorted(panel_tags),
                        "source_entity_id": source_entity.id,
                    }
                )
                mark_topology_text_conflict(topology_row, nearby_callouts)
                unresolved_topology.append(topology_row)
                continue

            attached_load_ids = {entity.id for entity in load_entities}
            contradictory_load_ids: set[str] = set()
            for observation in nearby_callouts:
                for (tag_page, tag), entity in entity_by_page_tag.items():
                    if (
                        tag_page == page
                        and isinstance(entity, ElectricalDevice)
                        and re.search(
                            rf"(?<![A-Z0-9_.-]){re.escape(tag)}(?![A-Z0-9_.-])",
                            observation.text,
                            re.IGNORECASE,
                        )
                        and entity.id not in attached_load_ids
                    ):
                        contradictory_load_ids.add(entity.id)
            if contradictory_load_ids:
                topology_row.update(
                    {
                        "status": "unresolved",
                        "reason": "load_callout_conflict",
                        "contradictory_load_ids": sorted(contradictory_load_ids),
                    }
                )
                mark_topology_text_conflict(topology_row, nearby_callouts)
                unresolved_topology.append(topology_row)
                continue

            circuit_number = (
                next(iter(circuit_numbers))
                if circuit_numbers
                else None
            )
            topology_confidence = 0.88
            topology_provenance = [
                _provenance(
                    document,
                    element_id=vector.element_id,
                    page=vector.page,
                    method="pdf-vector-topology-link",
                    confidence=topology_confidence,
                    source_kind=vector.source_kind,
                    attributes={
                        "points_pt": [list(point) for point in vector.points_pt],
                        **(
                            {"metadata": dict(vector.metadata)}
                            if vector.metadata
                            else {}
                        ),
                    },
                )
                for vector in component
            ]
            source_port: Port | None = None
            load_ports: dict[str, Port] = {}
            for provenance in topology_provenance:
                source_port = port_for(source_entity, "source", provenance)
                for entity in sorted(load_entities, key=lambda item: item.id):
                    load_ports[entity.id] = port_for(entity, "sink", provenance)
            if source_port is None:
                raise ElectricalPdfError("resolved topology did not produce source provenance")

            if circuit_number is not None:
                circuit_identity = circuit_number
                circuit_name = f"{source_entity.name or source_entity.id} {circuit_number}"
            else:
                semantic_load_key = ",".join(sorted(attached_load_ids))
                circuit_identity = f"topology:{semantic_load_key}"
                circuit_name = f"{source_entity.name or source_entity.id} topology"

            circuit_id = stable_id(
                "circuit",
                f"pdf-electrical:{document.source_id}:{source_entity.id}:{circuit_identity}",
            )
            bucket = evidence_bucket(
                circuit_id=circuit_id,
                name=circuit_name,
                source_port_id=source_port.id,
                circuit_number=circuit_number,
            )
            bucket["load_port_ids"].update(
                port.id for port in load_ports.values()
            )
            bucket["confidence"].add(topology_confidence)
            bucket["evidence_methods"].add("pdf-vector-topology-link")
            bucket["provenance"].extend(topology_provenance)
            record_port_provenance(
                bucket,
                (source_port.id, *(port.id for port in load_ports.values())),
                topology_provenance,
            )

            voltage_values: set[float] = set()
            pole_values: set[int] = set()
            phase_values: set[str] = set()
            callout_provenance: list[Provenance] = []
            for observation in nearby_callouts:
                voltage_v, _ = _extract_voltage((observation.text,))
                poles_match = _POLES_RE.search(observation.text)
                phase_match = _PHASE_RE.search(observation.text)
                if voltage_v is not None:
                    voltage_values.add(voltage_v)
                    bucket["voltage_v"].add(voltage_v)
                if poles_match:
                    pole_value = int(poles_match.group("poles"))
                    pole_values.add(pole_value)
                    bucket["poles"].add(pole_value)
                if phase_match:
                    phase_value = f"{phase_match.group('phase')}ph"
                    phase_values.add(phase_value)
                    bucket["phase"].add(phase_value)
                callout_provenance.append(
                    _provenance(
                        document,
                        element_id=observation.element_id,
                        page=observation.page,
                        method="pdf-topology-circuit-callout",
                        confidence=0.86,
                        attributes={"source_text": observation.text},
                    )
                )
            bucket["provenance"].extend(callout_provenance)
            if callout_provenance:
                bucket["evidence_methods"].add("pdf-topology-circuit-callout")

            topology_evidence = {
                "page": page,
                "source_element_id": element_ids[0],
                "source_element_ids": element_ids,
                "source_text": " | ".join(
                    sorted(observation.text for observation in nearby_callouts)
                ),
                "method": "pdf-vector-topology-link",
                "circuit_number": circuit_number,
                "panel_tag": source_entity.name,
                "load_ids": sorted(attached_load_ids),
                "circuit_callout_ids": [
                    observation.element_id for observation in nearby_callouts
                ],
                "voltage_v_candidates": sorted(voltage_values),
                "poles_candidates": sorted(pole_values),
                "phase_candidates": sorted(phase_values),
                "status": "resolved",
            }
            bucket["evidence"].append(topology_evidence)
            topology_resolved_callout_ids.update(
                observation.element_id for observation in nearby_callouts
            )

        if topology_conflicted_callout_ids:
            callout_ids_by_circuit: dict[str, set[str]] = {}
            conflicted_circuit_ids: set[str] = set()
            for circuit_id, bucket in circuit_evidence.items():
                bucket_callout_ids: set[str] = set()
                for evidence in bucket["evidence"]:
                    if evidence.get("method") == "pdf-circuit-text-link":
                        source_element_id = evidence.get("source_element_id")
                        if source_element_id:
                            bucket_callout_ids.add(str(source_element_id))
                    bucket_callout_ids.update(
                        str(item)
                        for item in evidence.get("circuit_callout_ids", ())
                    )
                callout_ids_by_circuit[circuit_id] = bucket_callout_ids
                if bucket_callout_ids & topology_conflicted_callout_ids:
                    conflicted_circuit_ids.add(circuit_id)

            for topology_row in unresolved_topology:
                row_callout_ids = set(topology_row.get("circuit_callout_ids", ()))
                suppressed_circuit_ids = sorted(
                    circuit_id
                    for circuit_id in conflicted_circuit_ids
                    if callout_ids_by_circuit[circuit_id] & row_callout_ids
                )
                if suppressed_circuit_ids:
                    topology_row["suppressed_circuit_ids"] = suppressed_circuit_ids

            for circuit_id in conflicted_circuit_ids:
                circuit_evidence.pop(circuit_id, None)

        effectively_resolved_callout_ids = (
            topology_resolved_callout_ids - topology_conflicted_callout_ids
        )
        if effectively_resolved_callout_ids:
            unresolved_circuits = [
                item
                for item in unresolved_circuits
                if item["source_element_id"] not in effectively_resolved_callout_ids
            ]

        surviving_port_provenance: dict[str, list[Provenance]] = {}
        for bucket in circuit_evidence.values():
            for port_id, provenances in bucket["port_provenance"].items():
                surviving_port_provenance.setdefault(port_id, []).extend(provenances)

        rebuilt_ports: dict[tuple[str, str], Port] = {}
        for key, port in ports_by_owner_role.items():
            provenances = surviving_port_provenance.get(port.id)
            if not provenances:
                continue
            merged_provenance = _merge_provenance(provenances)
            lane_attributes = dict(port.attributes["pdf_electrical"])
            lane_attributes["evidence_methods"] = sorted(
                {
                    item.method
                    for item in merged_provenance
                    if item.method is not None
                }
            )
            rebuilt_ports[key] = replace(
                port,
                confidence=max(item.confidence for item in merged_provenance),
                provenance=merged_provenance,
                attributes={"pdf_electrical": lane_attributes},
            )
        ports_by_owner_role = rebuilt_ports

        circuits: list[Circuit] = []
        for circuit_id, bucket in sorted(circuit_evidence.items()):
            conflicts: dict[str, list[Any]] = {}

            def one_or_none(field: str) -> Any:
                values = sorted(bucket[field])
                if len(values) == 1:
                    return values[0]
                if len(values) > 1:
                    conflicts[field] = values
                return None

            evidence_rows = sorted(
                bucket["evidence"],
                key=lambda item: (
                    int(item["page"]),
                    str(item["source_element_id"]),
                    str(item["source_text"]),
                ),
            )
            voltage_v = one_or_none("voltage_v")
            poles = one_or_none("poles")
            phase = one_or_none("phase")
            lane_attributes: dict[str, Any] = {
                "inference_basis": (
                    "source-supported electrical callout and/or unambiguous vector "
                    "topology evidence; the importer emits semantic endpoints and "
                    "circuit intent but never route geometry"
                ),
                "evidence_methods": sorted(bucket["evidence_methods"]),
                "spatial_attachment_pending": not has_explicit_registration,
                "evidence": evidence_rows,
                "source_texts": sorted(
                    {
                        str(item["source_text"])
                        for item in evidence_rows
                        if item.get("source_text")
                    }
                ),
            }
            if conflicts:
                lane_attributes["conflicting_electrical_evidence"] = conflicts
            circuits.append(
                Circuit(
                    id=circuit_id,
                    name=bucket["name"],
                    source_port_id=bucket["source_port_id"],
                    load_port_ids=tuple(sorted(bucket["load_port_ids"])),
                    circuit_number=bucket["circuit_number"],
                    voltage_v=voltage_v,
                    poles=poles,
                    phase=phase,
                    confidence=max(bucket["confidence"], default=0.0),
                    provenance=_merge_provenance(bucket["provenance"]),
                    attributes={"pdf_electrical": lane_attributes},
                )
            )

        global_notes = [
            {
                "page": observation.page,
                "source_element_id": observation.element_id,
                "text": observation.text,
                "position_pt": {"x": observation.x_pt, "y": observation.y_pt},
            }
            for observation in texts
            if _looks_like_note(observation.text)
            and observation.element_id not in attached_note_ids
        ]

        model_provenance: list[Provenance] = [
            Provenance(
                source_kind="pdf-electrical",
                source_id=document.source_id,
                method="pypdf-text-xobject-annotation-vector extraction",
                confidence=1.0,
                attributes={"page_count": document.page_count},
            )
        ]
        for page in sorted(document.page_provenance):
            model_provenance.append(
                Provenance(
                    source_kind="pdf-electrical",
                    source_id=document.source_id,
                    page=page,
                    method="pypdf-page-display-normalization",
                    confidence=1.0,
                    attributes=dict(document.page_provenance[page]),
                )
            )
        for note in legend_recognition.get("frame_rederivations", ()):
            model_provenance.append(
                Provenance(
                    source_kind="pdf-electrical",
                    source_id=document.source_id,
                    source_element_id=str(note["source_element_id"]),
                    page=int(note["page"]),
                    method="pdf-page-frame-media-box-rederivation",
                    confidence=1.0,
                    attributes={
                        key: value
                        for key, value in note.items()
                        if key not in {"page", "source_element_id"}
                    },
                )
            )
        if not has_explicit_registration and document.page_count > 1:
            for page in range(1, document.page_count + 1):
                model_provenance.append(
                    Provenance(
                        source_kind="pdf-electrical",
                        source_id=document.source_id,
                        page=page,
                        method="deterministic per-page best-effort unregistered placement",
                        confidence=0.25,
                        attributes={
                            "registration_status": "unregistered-best-effort",
                            "page_transform": transforms[page].to_attributes(),
                            "cross_page_registration_asserted": False,
                        },
                    )
                )

        registration_mode = (
            "explicit-page-transforms"
            if has_explicit_registration
            else (
                "deterministic-separated-page-local-best-effort"
                if document.page_count > 1
                else "single-page-local"
            )
        )

        return BuildingModel(
            model_id=stable_id("model", f"pdf-electrical:{document.source_id}"),
            name=f"Electrical PDF recognition: {document.source_id}",
            coordinate_system=CoordinateSystem(frame_id=frame_id),
            electrical_equipment=tuple(sorted(equipment, key=lambda item: item.id)),
            electrical_devices=tuple(sorted(devices, key=lambda item: item.id)),
            ports=tuple(sorted(ports_by_owner_role.values(), key=lambda item: item.id)),
            circuits=tuple(circuits),
            provenance=tuple(model_provenance),
            attributes={
                "pdf_electrical": {
                    "lane": "pdf_electrical",
                    "page_count": document.page_count,
                    "source_length_unit": "pt",
                    "canonicalized_length_unit": "m",
                    "spatial_status": spatial_status,
                    "registration_pending": not has_explicit_registration,
                    "page_transforms_supplied": has_explicit_registration,
                    "registration_mode": registration_mode,
                    "registered_frame_id": frame_id,
                    **(
                        {
                            "page_provenance": {
                                str(page): dict(document.page_provenance[page])
                                for page in sorted(document.page_provenance)
                            }
                        }
                        if document.page_provenance
                        else {}
                    ),
                    "legend_recognition": legend_recognition,
                    "best_effort_page_transforms": (
                        {
                            str(page): transforms[page].to_attributes()
                            for page in range(1, document.page_count + 1)
                        }
                        if not has_explicit_registration and document.page_count > 1
                        else {}
                    ),
                    "unresolved_observations": sorted(
                        unresolved_observations,
                        key=lambda item: (
                            int(item["page"]),
                            str(item["source_element_id"]),
                        ),
                    ),
                    "unresolved_circuits": sorted(
                        unresolved_circuits,
                        key=lambda item: (
                            int(item["page"]),
                            str(item["source_element_id"]),
                        ),
                    ),
                    "unresolved_topology": sorted(
                        unresolved_topology,
                        key=lambda item: (
                            int(item["page"]),
                            tuple(item["source_element_ids"]),
                        ),
                    ),
                    "global_annotations": sorted(
                        global_notes,
                        key=lambda item: (
                            int(item["page"]),
                            str(item["source_element_id"]),
                        ),
                    ),
                }
            },
        )


def import_pdf(
    path: str | Path,
    *,
    source_id: str | None = None,
    page_transforms: Mapping[int, PdfPageTransform] | None = None,
    instance_hints: Sequence[ElectricalInstanceHint] = (),
) -> BuildingModel:
    return ElectricalPdfImporter(instance_hints=instance_hints).import_pdf(
        path,
        source_id=source_id,
        page_transforms=page_transforms,
    )


def import_document(
    document: PdfElectricalDocument,
    *,
    page_transforms: Mapping[int, PdfPageTransform] | None = None,
    instance_hints: Sequence[ElectricalInstanceHint] = (),
) -> BuildingModel:
    return ElectricalPdfImporter(instance_hints=instance_hints).import_document(
        document,
        page_transforms=page_transforms,
    )


__all__ = [
    "DEFAULT_SYMBOL_RULES",
    "POINT_TO_M",
    "ElectricalPdfError",
    "ElectricalPdfImporter",
    "ElectricalInstanceHint",
    "PdfElectricalDocument",
    "PdfPageTransform",
    "PdfSymbolObservation",
    "PdfTextObservation",
    "PdfVectorPathObservation",
    "SymbolRule",
    "extract_pdf",
    "import_document",
    "import_pdf",
]
