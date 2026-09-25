from __future__ import annotations

import bisect
import hashlib
from collections import Counter
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
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
    # Evidence for a transform proposed by sheet registration (#104). A caller
    # supplying its own transform leaves this empty.
    registration: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.frame_id:
            raise ElectricalPdfError("page transform frame_id is required")
        if self.registration is not None and not isinstance(self.registration, Mapping):
            raise ElectricalPdfError("page transform registration must be a mapping")
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

    def to_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "frame_id": self.frame_id,
            "m11_m_per_pt": self.m11_m_per_pt,
            "m12_m_per_pt": self.m12_m_per_pt,
            "m21_m_per_pt": self.m21_m_per_pt,
            "m22_m_per_pt": self.m22_m_per_pt,
            "tx_m": self.tx_m,
            "ty_m": self.ty_m,
            "z_m": self.z_m,
        }
        if self.registration is not None:
            attributes["registration"] = dict(self.registration)
        return attributes


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
        r"^(?!.*\bCOMBINATION\b).*\bDUPLEX\b.*\b(?:(?:ELECTRICAL\s+)?OUTLET|RECEPTACLE)\b",
        "device",
        "receptacle_duplex",
        1.0,
    ),
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b).*\b(?:QUADRUPLEX|QUADRUPLE|QUAD|FOURPLEX|FOUR-PLEX)\b"
        r".*\b(?:(?:ELECTRICAL\s+)?OUTLET|RECEPTACLE)\b",
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
        r"\b(?:CABLE\s+TV|CATV|TELEVISION)\b.*\bOUTLET\b",
        "device",
        "catv_outlet",
        1.0,
    ),
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b).*\bCAT\s*-?\s*[56]E?\b.*\b(?:OUTLET|HOOK-?UP|JACK|DROP)\b",
        "device",
        "data_outlet",
        1.0,
    ),
    SymbolRule(
        r"\bSPECIAL\s+PURPOSE\s+(?:OUTLET|RECEPTACLE)\b",
        "device",
        "special_purpose_outlet",
        0.98,
    ),
    # A bare TELEPHONE legend row is the telephone outlet; the telephone
    # junction-box rule above keeps precedence for junction boxes.
    SymbolRule(r"\bTELEPHONE\b", "device", "data_outlet", 0.95),
    SymbolRule(r"\bSPEAKER\b", "device", "speaker", 0.95),
    SymbolRule(
        r"\bSMOKE\s*/\s*(?:CARBON\s+)?MONOXIDE\b",
        "device",
        "smoke_co_alarm",
        0.95,
    ),
    SymbolRule(r"\bSMOKE\s+ALARM\b", "device", "smoke_alarm", 0.95),
    SymbolRule(r"\bHEAT\s+DETECTOR\b", "device", "heat_detector", 0.95),
    # The generic receptacle yields to the specific duplex and quad rules, and
    # "RECESSED" is not a receptacle abbreviation.
    SymbolRule(
        r"^(?!.*\bCOMBINATION\b)"
        r"(?!.*\b(?:DUPLEX|QUADRUPLEX|QUADRUPLE|QUAD|FOURPLEX|FOUR-PLEX)\b.*\b(?:(?:ELECTRICAL\s+)?OUTLET|RECEPTACLE)\b)"
        r".*\b(?:GFCI|GFI|RECEPTACLE|RECEPT|DUPLEX|REC(?!ESS))[A-Z0-9]*\b",
        "device",
        "receptacle",
        0.94,
    ),
    SymbolRule(r"^(?!.*\b(?:ELECTRICAL|POWER|DATA|TELE|TELEPHONE)\b.*\b(?:JBOX|J-?BOX|JUNCTION\s+BOX)\b).*\b(?:(?:JBOX|J-?BOX|JB)[A-Z0-9]*|JUNCTION\s+BOX)\b", "device", "junction_box", 0.94),
    SymbolRule(r"\b(?:LUMINAIRE|LIGHT|LTG|FIXTURE|SCONCE|FLOODLIGHT|DOWNLIGHT|PENDANT)\b", "device", "luminaire", 0.91),
    SymbolRule(r"\b(?:DISCONNECT|DISC)\b", "device", "disconnect", 0.92),
    SymbolRule(r"\b(?:SWITCH|SW)\b", "device", "switch", 0.75),
    SymbolRule(r"\b(?:TOGGLE|DIMMER)\b", "device", "switch", 0.9),
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
    lighting_recognition: dict[str, Any] | None = None

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
    # The wrapped source lines of a multi-line label, first line first.
    label_lines: tuple[PdfTextObservation, ...] = ()


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
    legend_frame: Mapping[str, Any] | None = None


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


@dataclass(frozen=True, slots=True)
class _LightingScheduleRow:
    page: int
    tag: str
    fields: Mapping[str, str]
    source_element_ids: tuple[str, ...]
    source_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LightingLegendEntry:
    page: int
    tag: str
    prototype: _VectorCluster
    label: PdfTextObservation
    heading: PdfTextObservation
    confidence: float


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


def _rendered_font_size(
    font_size: float | None,
    cm: Sequence[float],
    tm: Sequence[float],
) -> float | None:
    """Font size as drawn on the page, not the raw ``Tf`` operand.

    CAD exports often set a large ``Tf`` size and shrink it with the text
    matrix (or the reverse). The drawn glyph height is the ``Tf`` size times the
    length of the text-space y axis after the text and graphics matrices. A
    negative ``Tf`` mirrors the glyphs but still draws them at its magnitude.
    """

    if font_size is None:
        return None
    size = abs(float(font_size))
    # Text-space y axis (0, 1) through Tm, then through the CTM.
    tx = float(tm[2])
    ty = float(tm[3])
    x = float(cm[0]) * tx + float(cm[2]) * ty
    y = float(cm[1]) * tx + float(cm[3]) * ty
    scale = math.hypot(x, y)
    if not math.isfinite(scale) or scale <= 0.0:
        return size
    return size * scale


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


_STROKING_PAINT_OPERATORS = frozenset({b"S", b"s", b"B", b"B*", b"b", b"b*"})
_FILLING_PAINT_OPERATORS = frozenset({b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"})


def _resolved_pdf_object(value: Any) -> Any:
    try:
        return value.get_object()
    except AttributeError:
        return value


def _ext_gstate_alpha(
    resources: Any,
    name: str,
) -> tuple[float | None, float | None]:
    """Stroke (/CA) and fill (/ca) constant alpha of one named graphics state.

    A value the state does not set, or cannot be read, is None and leaves the
    current alpha unchanged.
    """

    if not isinstance(resources, Mapping):
        return None, None
    states = _resolved_pdf_object(resources.get("/ExtGState"))
    if not isinstance(states, Mapping):
        return None, None
    state = _resolved_pdf_object(states.get(name))
    if not isinstance(state, Mapping):
        return None, None

    def alpha(key: str) -> float | None:
        raw = state.get(key)
        if raw is None:
            return None
        try:
            value = float(_resolved_pdf_object(raw))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return min(1.0, max(0.0, value))

    return alpha("/CA"), alpha("/ca")


def _form_xobject_resources(resources: Any, name: str) -> Any | None:
    """The resources a named form XObject paints with, or None for no form."""

    if not isinstance(resources, Mapping):
        return None
    xobjects = _resolved_pdf_object(resources.get("/XObject"))
    if not isinstance(xobjects, Mapping):
        return None
    xobject = _resolved_pdf_object(xobjects.get(name))
    if not isinstance(xobject, Mapping) or str(xobject.get("/Subtype")) != "/Form":
        return None
    own = _resolved_pdf_object(xobject.get("/Resources"))
    return own if isinstance(own, Mapping) else resources


def _make_page_visitors(
    *,
    page_number: int,
    form_names: frozenset[str],
    display_transform: PdfPageDisplayTransform,
    texts: list[PdfTextObservation],
    symbols: list[PdfSymbolObservation],
    vectors: list[PdfVectorPathObservation],
    resources: Any = None,
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
    # Constant stroke and fill alpha from ExtGState (/CA, /ca), saved by q/Q
    # and around each form XObject, which paints with its own resources.
    stroke_alpha = 1.0
    fill_alpha = 1.0
    alpha_stack: list[tuple[float, float]] = []
    resource_stack: list[Any] = [_resolved_pdf_object(resources)]
    form_frames: list[tuple[bool, float, float, int]] = []

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
                font_size_pt=_rendered_font_size(font_size, cm, tm),
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
            # Only a translucent paint is recorded; opaque paths keep their
            # metadata unchanged.
            if operator in _STROKING_PAINT_OPERATORS and stroke_alpha < 1.0:
                metadata["stroke_alpha"] = round(stroke_alpha, 6)
            if operator in _FILLING_PAINT_OPERATORS and fill_alpha < 1.0:
                metadata["fill_alpha"] = round(fill_alpha, 6)
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
        nonlocal stroke_alpha, fill_alpha
        operator_counter += 1

        if operator == b"q":
            alpha_stack.append((stroke_alpha, fill_alpha))
            return
        if operator == b"Q":
            if alpha_stack:
                stroke_alpha, fill_alpha = alpha_stack.pop()
            return
        if operator == b"gs" and operands:
            new_stroke, new_fill = _ext_gstate_alpha(
                resource_stack[-1], str(operands[0])
            )
            if new_stroke is not None:
                stroke_alpha = new_stroke
            if new_fill is not None:
                fill_alpha = new_fill
            return
        if operator == b"Do":
            form_resources = (
                _form_xobject_resources(resource_stack[-1], str(operands[0]))
                if operands
                else None
            )
            form_frames.append(
                (form_resources is not None, stroke_alpha, fill_alpha, len(alpha_stack))
            )
            if form_resources is not None:
                resource_stack.append(form_resources)

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

    def visitor_operand_after(
        operator: bytes,
        operands: Sequence[Any],
        cm: Sequence[float],
        tm: Sequence[float],
    ) -> None:
        nonlocal stroke_alpha, fill_alpha
        if operator != b"Do" or not form_frames:
            return
        is_form, saved_stroke, saved_fill, depth = form_frames.pop()
        if not is_form:
            return
        # A form XObject paints inside an implicit q/Q with its own resources.
        if len(resource_stack) > 1:
            resource_stack.pop()
        stroke_alpha, fill_alpha = saved_stroke, saved_fill
        del alpha_stack[depth:]

    return visitor_text, visitor_operand_before, visitor_operand_after


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

        visitor_text, visitor_operand_before, visitor_operand_after = _make_page_visitors(
            page_number=page_number,
            form_names=frozenset(form_names),
            display_transform=display_transform,
            texts=texts,
            symbols=symbols,
            vectors=vectors,
            resources=resources,
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
                visitor_operand_after=visitor_operand_after,
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
            title = _clean_pdf_string(annotation.get("/T"))
            element_root = f"p{page_number}:annotation:{annotation_index:04d}"
            # CAD text keeps its drawn string outline in /Rect (AutoCAD SHX
            # text annotations). Keep the displayed rectangle so letter
            # strokes drawn inside it can be told from device glyphs.
            corner_first = display_transform.apply(float(rect[0]), float(rect[1]))
            corner_second = display_transform.apply(float(rect[2]), float(rect[3]))
            rect_pt = [
                min(corner_first[0], corner_second[0]),
                min(corner_first[1], corner_second[1]),
                max(corner_first[0], corner_second[0]),
                max(corner_first[1], corner_second[1]),
            ]

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
                                "title": title,
                                "rect_pt": rect_pt,
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
# A prose mention such as "panel located ..." is not equipment identity.
# Only a standalone label or a label followed by explicit electrical ratings
# can materialize a panelboard from text. Circuit callouts are handled below.
_PANEL_EQUIPMENT_RE = re.compile(
    r"\s*(?:PANEL|PNL)\s+(?P<tag>[A-Z][A-Z0-9_.-]*)"
    r"(?:\s+\d+(?:/\d+)?\s*V)?(?:\s+[123]\s*PH)?(?:\s+\d+\s*A)?\s*",
    re.IGNORECASE,
)
_PANEL_PROSE_TAGS = frozenset({
    "AS", "AT", "BY", "CEILINGS", "DESIGNS", "FOR", "IN", "LOCATED",
    "LOCATIONS", "OF", "ON", "SCHEDULE", "SHOWN", "THE", "TO", "WITH",
})
_PANEL_BARE_TAGS = frozenset({
    "D", "DP", "EDP", "EP", "H", "HP", "L", "LP", "M", "MDP", "MP",
    "P", "PP", "R", "RP", "S", "SP", "UPS",
})
_PANEL_NUMBERED_TAG_RE = re.compile(r"[A-Z]{1,4}[-_.]?\d+[A-Z0-9_.-]*")
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
_PANEL_SCHEDULE_NUMBER_RE = re.compile(r"^\s*(?P<circuit>\d+)\s*$")
_NOTES_HEADING_RE = re.compile(r"^\s*(?:(?:DETAIL|GENERAL)\s+)?NOTES?\b", re.IGNORECASE)
_SCHEDULE_CIRCUIT_COLUMN_RE = re.compile(r"^\s*(?:CKT|CIRCUITS?)\s*$", re.IGNORECASE)
_SCHEDULE_LOAD_COLUMN_RE = re.compile(r"^\s*(?:LOAD|DESCRIPTION|SERVES)\s*$", re.IGNORECASE)
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
    confirmed_schedules: set[str],
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
    if scheduled is not None and panel_tag not in confirmed_schedules:
        return panel_tag, numbers, "schedule_structure_not_confirmed"
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
) -> tuple[dict[str, set[int]], set[str], set[str]]:
    schedules: dict[str, set[int]] = {}
    consumed_ids: set[str] = set()
    confirmed: set[str] = set()
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

    # Only a two-column source grid can validate rows. A single-column notes
    # box can carry the same words and numbers, so it stays present/unconfirmed.
    rectangles: list[tuple[PdfVectorPathObservation, float, float, float, float]] = []
    for vector in vectors:
        if not vector.closed or len(vector.points_pt) != 4:
            continue
        xs = {point[0] for point in vector.points_pt}
        ys = {point[1] for point in vector.points_pt}
        if len(xs) != 2 or len(ys) != 2:
            continue
        if set(vector.points_pt) != {(x, y) for x in xs for y in ys}:
            continue
        rectangles.append((vector, min(xs), min(ys), max(xs), max(ys)))

    for heading, panel_tag in headings:
        candidate_tables: list[
            tuple[float, tuple[tuple[PdfTextObservation, int], ...]]
        ] = []
        for frame, left, bottom, right, top in rectangles:
            if not (frame.page == heading.page and left < heading.x_pt < right and bottom < heading.y_pt < top):
                continue
            titles = [
                (cell, cell_bottom)
                for cell, x0, cell_bottom, x1, cell_top in rectangles
                if cell.page == heading.page and cell.element_id != frame.element_id
                and (x0, x1, cell_top) == (left, right, top)
                and cell_bottom < heading.y_pt < cell_top
                and not any(t.page == heading.page and t.element_id != heading.element_id
                            and x0 < t.x_pt < x1 and cell_bottom < t.y_pt < cell_top
                            for t in texts)
            ]
            if len(titles) != 1:
                continue
            title_bottom = titles[0][1]
            dividers = {
                line.points_pt[0][0]
                for line in vectors
                if line.page == heading.page and not line.closed and len(line.points_pt) == 2
                and line.points_pt[0][0] == line.points_pt[1][0]
                and {line.points_pt[0][1], line.points_pt[1][1]} == {bottom, title_bottom}
                and left < line.points_pt[0][0] < right
            }
            if len(dividers) != 1:
                continue
            divider = next(iter(dividers))

            def cell_text(x0: float, y0: float, x1: float, y1: float) -> list[PdfTextObservation]:
                return [t for t in texts if t.page == heading.page
                        and x0 < t.x_pt < x1 and y0 < t.y_pt < y1]

            headers = [
                (a, b, y0)
                for a, ax0, y0, ax1, y1 in rectangles
                for b, bx0, by0, bx1, by1 in rectangles
                if a.page == heading.page and b.page == heading.page
                and (ax0, ax1, bx0, bx1) == (left, divider, divider, right)
                and (y0, y1) == (by0, by1) and y1 == title_bottom and y0 > bottom
                and len(cell_text(ax0, y0, ax1, y1)) == 1
                and len(cell_text(bx0, y0, bx1, y1)) == 1
                and _SCHEDULE_CIRCUIT_COLUMN_RE.fullmatch(cell_text(ax0, y0, ax1, y1)[0].text)
                and _SCHEDULE_LOAD_COLUMN_RE.fullmatch(cell_text(bx0, y0, bx1, y1)[0].text)
            ]
            if len(headers) != 1:
                continue
            edge = headers[0][2]
            parsed_rows: list[tuple[PdfTextObservation, int]] = []
            while True:
                touching = [(a, b, y0)
                    for a, ax0, y0, ax1, y1 in rectangles
                    for b, bx0, by0, bx1, by1 in rectangles
                    if a.page == heading.page and b.page == heading.page
                    and (ax0, ax1, bx0, bx1) == (left, divider, divider, right)
                    and (y0, y1) == (by0, by1) and y1 == edge
                    and bottom <= y0 < edge]
                if len(touching) != 1:
                    break
                row_bottom = touching[0][2]
                numbers = cell_text(left, row_bottom, divider, edge)
                loads = cell_text(divider, row_bottom, right, edge)
                if len(numbers) != 1 or len(loads) != 1 or not loads[0].text.strip():
                    break
                match = _PANEL_SCHEDULE_NUMBER_RE.fullmatch(numbers[0].text)
                if match is None:
                    break
                parsed_rows.append((numbers[0], int(match.group("circuit"))))
                edge = row_bottom
            if parsed_rows:
                candidate_tables.append((top - bottom, tuple(parsed_rows)))

        if not candidate_tables:
            continue
        shortest = min(height for height, _rows in candidate_tables)
        owned = [rows for height, rows in candidate_tables if height == shortest]
        if len(owned) != 1:
            # Two equally tight source tables cannot uniquely own the rows.
            continue
        confirmed.add(panel_tag)
        for row, circuit in owned[0]:
            schedules[panel_tag].add(circuit)
            consumed_ids.add(row.element_id)
    return schedules, consumed_ids, confirmed


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
    classification, ranked = _classify_semantic_text(
        _semantic_text(symbol),
        rules,
        ambiguity_margin=ambiguity_margin,
    )
    # Metadata can contain free-form notes and opaque native IDs. A panelboard
    # symbol needs a valid label in the symbol name itself; annotation text is
    # examined separately and cannot establish equipment through its symbol.
    if (
        classification is not None
        and classification[:2] == ("equipment", "panelboard")
        and (
            symbol.source_kind.startswith("annotation:")
            or not any(
                kind == "equipment" and canonical_type == "panelboard"
                for kind, canonical_type, _tag, _confidence in _text_entity_hits(
                    symbol.name.replace("/", " ").replace("_", " ")
                )
            )
        )
    ):
        return None, ranked
    return classification, ranked


def _text_entity_hits(text: str) -> list[tuple[str, str, str, float]]:
    if re.match(r"\s*(?:NOTE|KEYNOTE|GENERAL\s+NOTE)\b", text, re.IGNORECASE):
        return []
    if _CIRCUIT_RE.search(text):
        # A circuit callout can name equipment and loads without locating them.
        # Keep those names as circuit evidence, but require independent spatial
        # recognition before materializing canonical equipment or devices.
        return []
    hits: list[tuple[str, str, str, float]] = []
    panel = _PANEL_EQUIPMENT_RE.fullmatch(text)
    if panel:
        tag = _normalize_tag(panel.group("tag"))
        has_rating = bool(text[panel.end("tag"):].strip())
        if tag not in _PANEL_PROSE_TAGS and (
            tag in _PANEL_BARE_TAGS
            or _PANEL_NUMBERED_TAG_RE.fullmatch(tag)
            or (has_rating and re.fullmatch(r"[A-Z]{1,4}", tag))
        ):
            hits.append(("equipment", "panelboard", tag, 0.97))
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

# Scope of work comes from the sheet's own status legend, not from a fixed
# letter convention: one set uses R for "removed", another for "removed and
# salvaged for relocation". A legend row is a single marker letter immediately
# left of its meaning on the same baseline, or "R = MEANING" in one string.
SCOPE_NEW = "new"
SCOPE_RELOCATED = "relocated"
SCOPE_EXISTING = "existing_to_remain"
SCOPE_REMOVED = "removed"
SCOPE_UNRESOLVED = "unresolved"
_SCOPE_LEGEND_INLINE_RE = re.compile(r"^\(?(?P<marker>[ENR])\)?\s*[-=:]\s*(?P<meaning>.+)$")
_SCOPE_LEGEND_ROW_Y_PT = 3.0
_SCOPE_LEGEND_ROW_GAP_PT = 60.0
_SCOPE_LEGEND_MAX_MEANING_CHARS = 64


def _scope_meaning(text: str) -> str | None:
    """Map one legend meaning phrase to a scope-of-work status."""

    upper = " ".join(text.upper().split())
    if len(upper) > _SCOPE_LEGEND_MAX_MEANING_CHARS:
        return None
    if "RELOCAT" in upper:
        return SCOPE_RELOCATED
    if re.search(r"\b(?:REMOV|DEMOLISH|DEMO\b)", upper):
        return SCOPE_REMOVED
    if re.fullmatch(r"EXISTING(?:\s+(?:DEVICE|DEVICES|FIXTURE|FIXTURES))?(?:\s+TO\s+REMAIN)?", upper):
        return SCOPE_EXISTING
    if re.fullmatch(r"NEW(?:\s+(?:DEVICE|DEVICES|FIXTURE|FIXTURES|WORK|CONSTRUCTION))?", upper):
        return SCOPE_NEW
    return None


def _scope_status_legends(
    texts: Sequence[PdfTextObservation],
) -> dict[int, dict[str, list[tuple[str, tuple[str, ...], str]]]]:
    """Per page: marker letter -> [(scope, source element ids, meaning text)]."""

    legends: dict[int, dict[str, list[tuple[str, tuple[str, ...], str]]]] = {}
    by_page: dict[int, list[PdfTextObservation]] = {}
    for observation in texts:
        by_page.setdefault(observation.page, []).append(observation)
    for page, observations in sorted(by_page.items()):
        for observation in observations:
            inline = _SCOPE_LEGEND_INLINE_RE.fullmatch(" ".join(observation.text.split()))
            if inline:
                scope = _scope_meaning(inline.group("meaning"))
                if scope is not None:
                    legends.setdefault(page, {}).setdefault(inline.group("marker"), []).append(
                        (scope, (observation.element_id,), inline.group("meaning").strip())
                    )
                continue
            status = _FIELD_STATUS_RE.fullmatch(observation.text)
            if status is None:
                continue
            row = [
                other for other in observations
                if other is not observation
                and abs(other.y_pt - observation.y_pt) <= _SCOPE_LEGEND_ROW_Y_PT
                and 0.0 < other.x_pt - observation.x_pt <= _SCOPE_LEGEND_ROW_GAP_PT
            ]
            if not row:
                continue
            meaning = min(row, key=lambda item: (item.x_pt - observation.x_pt, item.element_id))
            scope = _scope_meaning(meaning.text)
            if scope is None:
                continue
            legends.setdefault(page, {}).setdefault(status.group("status").upper(), []).append(
                (scope, (observation.element_id, meaning.element_id), meaning.text.strip())
            )
    return legends


def _scope_status_attributes(
    status: str | None,
    status_source_element_id: str | None,
    page: int,
    legends: Mapping[int, Mapping[str, Sequence[tuple[str, tuple[str, ...], str]]]],
) -> dict[str, Any]:
    """Resolve one device's scope from its marker and its own sheet's legend."""

    if status is None:
        return {
            "scope_status": SCOPE_UNRESOLVED,
            "scope_reason": "no_scope_marker",
        }
    entries = legends.get(page, {}).get(status, ())
    scopes = sorted({scope for scope, _, _ in entries})
    evidence = {
        "scope_marker": status,
        "scope_marker_source_element_id": status_source_element_id,
    }
    if not scopes:
        return {**evidence, "scope_status": SCOPE_UNRESOLVED, "scope_reason": "scope_marker_undefined"}
    legend_ids = sorted({element for _, ids, _ in entries for element in ids})
    if len(scopes) > 1:
        return {
            **evidence,
            "scope_status": SCOPE_UNRESOLVED,
            "scope_reason": "scope_legend_conflict",
            "scope_legend_source_element_ids": legend_ids,
        }
    return {
        **evidence,
        "scope_status": scopes[0],
        "scope_method": "sheet status legend",
        "scope_legend_text": sorted({text for _, _, text in entries})[0],
        "scope_legend_source_element_ids": legend_ids,
    }

# Lighting is intentionally a separate recognition path from power-device
# legends. A fixture's readable type tag is the semantic evidence; geometry
# only confirms that the tag is attached to a fixture instance.
_LIGHTING_TAG_RE = re.compile(r"^[A-Z]{1,2}(?:-?\d{1,2})?$", re.IGNORECASE)
# The tag narrows the candidates to one fixture type, so the power-device
# margin rule reduces to its match minimum. The absolute floor only marks
# where a shape is certainly not the prototype; it never confirms one.
_LIGHTING_GLYPH_CONFIRM_SCORE = _GLYPH_MATCH_SCORE_MIN
# A round legend prototype and a polygonal field instance (or the reverse)
# disagree on shape class. Chamfer scoring alone leaves a square only a
# little under the circle it most resembles, so such a pair may confirm
# only at the strong-score bar; anything thinner fails closed.
_LIGHTING_SHAPE_CLASS_STRONG_SCORE = _GLYPH_MATCH_STRONG_SCORE
# Maximum ratio of extreme centroid distances for a boundary cloud to count
# as round. Circles sit near 1.0 and octagons near 1.08; squares reach
# sqrt(2) and triangles reach 2.
_LIGHTING_SHAPE_CLASS_ROUND_MAX_RADIAL_RANGE = 1.2
_LIGHTING_TAG_CLUSTER_RADIUS_PT = 42.0
_LIGHTING_TAG_ASSOCIATION_MARGIN_PT = 6.0
_LIGHTING_LEGEND_VERTICAL_SPAN_PT = 190.0
_LIGHTING_SCHEDULE_VERTICAL_SPAN_PT = 300.0
_LIGHTING_ROW_Y_TOLERANCE_PT = 5.0
_LIGHTING_HEADER_Y_TOLERANCE_PT = 8.0
_LIGHTING_HEADER_MAX_GAP_PT = 90.0
_LIGHTING_SWITCH_CODES: Mapping[str, tuple[str, str]] = {
    "S": ("switch", "single_pole"),
    "S3": ("switch", "three_way"),
    "SD": ("switch", "dimmer"),
    "OS": ("occupancy_sensor", "occupancy_sensor"),
}
# A switch-code legend row is a switch prototype only when its own description
# names a switching device. S1/S2/S3 are also common strip-fixture type tags.
_LIGHTING_SWITCH_DESCRIPTION_WORDS = frozenset(
    {"SWITCH", "SWITCHES", "DIMMER", "OCCUPANCY", "VACANCY"}
)
_LIGHTING_LEGEND_DESCRIPTION_SPAN_PT = 220.0
_LIGHTING_LEGEND_ROW_GLYPH_Y_TOLERANCE_PT = 18.0
# Legend rows are read as table structure. The heading anchors the table's
# row grid over its own column; a further printed column joins the table
# only when it starts within one inch past where the admitted table's
# printed descriptions are estimated to end, every one of its labels
# continues the grid on a distinct row, and each of its rows carries
# description text to the right the way a legend row does. A vertical run
# of tagged field fixtures on the grid rows must not be mistaken for a
# legend column, so no one of these signals admits alone.
_LIGHTING_LEGEND_HEADING_COLUMN_SPAN_PT = 220.0
_LIGHTING_LEGEND_LABEL_COLUMN_TOLERANCE_PT = 48.0
_LIGHTING_LEGEND_ROW_GRID_TOLERANCE_PT = 10.0
_LIGHTING_LEGEND_COLUMN_GAP_PT = 72.0
_LIGHTING_SCHEDULE_HEADER_ALIASES: Mapping[str, str] = {
    "TYPE": "tag",
    "TAG": "tag",
    "MARK": "tag",
    "FIXTURE TYPE": "tag",
    "DESCRIPTION": "description",
    "DESC": "description",
    "LAMP": "lamp",
    "LAMPS": "lamp",
    "WATTS": "wattage",
    "WATTAGE": "wattage",
    "W": "wattage",
    "MOUNTING": "mounting",
    "MOUNT": "mounting",
    "MANUFACTURER": "manufacturer",
    "MFR": "manufacturer",
    "MODEL": "model",
    "CATALOG": "model",
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


def _rotate_point_cloud(
    cloud: Sequence[tuple[float, float]],
    theta: float,
) -> tuple[tuple[float, float], ...]:
    cos_theta = math.cos(theta)
    sin_theta = math.sin(theta)
    return tuple(
        sorted(
            {
                (
                    round(x * cos_theta - y * sin_theta, 5),
                    round(x * sin_theta + y * cos_theta, 5),
                )
                for x, y in cloud
            }
        )
    )


def _cloud_principal_angle(
    cloud: Sequence[tuple[float, float]],
) -> float | None:
    """Orientation of a cloud's major axis, defined only up to half a turn.

    Second-moment principal-axis angle. Radially uniform clouds such as
    circles have a degenerate axis; any orientation matches them anyway, so
    the arbitrary zero the formula returns there is harmless.
    """
    count = len(cloud)
    if count < 2:
        return None
    mean_x = sum(x for x, _ in cloud) / count
    mean_y = sum(y for _, y in cloud) / count
    covariance_xx = sum((x - mean_x) ** 2 for x, _ in cloud) / count
    covariance_yy = sum((y - mean_y) ** 2 for _, y in cloud) / count
    covariance_xy = sum((x - mean_x) * (y - mean_y) for x, y in cloud) / count
    if not math.isfinite(covariance_xx + covariance_yy + covariance_xy):
        return None
    return 0.5 * math.atan2(2.0 * covariance_xy, covariance_xx - covariance_yy)


def _principal_axis_aligned_cloud(
    cloud: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...] | None:
    """Re-express a resampled cloud in a rotation-invariant canonical frame.

    Centroid and root-mean-square radius remove translation and uniform
    scale; aligning the principal axis removes in-plane rotation. Both
    clouds of a comparison land in the same frame only up to half a turn,
    so the caller keeps the half-turn variant as an explicit alternative.
    """
    count = len(cloud)
    if count < 2:
        return None
    mean_x = sum(x for x, _ in cloud) / count
    mean_y = sum(y for _, y in cloud) / count
    radius = math.sqrt(
        sum((x - mean_x) ** 2 + (y - mean_y) ** 2 for x, y in cloud) / count
    )
    if radius <= 1e-9 or not math.isfinite(radius):
        return None
    centered = tuple(
        ((x - mean_x) / radius, (y - mean_y) / radius) for x, y in cloud
    )
    angle = _cloud_principal_angle(centered)
    if angle is None:
        return None
    return _rotate_point_cloud(centered, -angle)


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

    # Quarter turns never cover skewed wings that print fixtures at
    # arbitrary angles. Comparing both clouds in their principal-axis frames
    # removes the remaining rotation (and uniform scale, which bbox
    # normalization only handles for axis-aligned extents). The aligned
    # clouds live at root-mean-square radius 1, a wider frame than the
    # bbox-normalized clouds above, so aligned distances only win the min
    # when the shapes genuinely coincide once rotated together.
    prototype_aligned = _principal_axis_aligned_cloud(prototype_cloud)
    if prototype_aligned is not None:
        prototype_aligned_half_turn = _rotate_point_cloud(
            prototype_aligned,
            math.pi,
        )
        for mirrored in (False, True):
            cluster_aligned = _principal_axis_aligned_cloud(
                _resampled_point_cloud(cluster, mirrored=mirrored)
            )
            if cluster_aligned is None:
                continue
            distance = min(
                distance,
                _point_cloud_distance(cluster_aligned, prototype_aligned),
                _point_cloud_distance(
                    cluster_aligned,
                    prototype_aligned_half_turn,
                ),
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


_SHX_TEXT_AUTHOR = "AutoCAD SHX Text"
# Letter strokes are drawn inside their string's outline; a hairline of slack
# absorbs stroke width without reaching outside the drawn text.
_SHX_TEXT_BOX_TOLERANCE_PT = 0.5


def _is_shx_text_annotation(symbol: PdfSymbolObservation) -> bool:
    """Whether one symbol observation is an AutoCAD SHX drawn-text annotation."""

    return (
        symbol.source_kind == "annotation:square"
        and str(symbol.metadata.get("title") or "") == _SHX_TEXT_AUTHOR
    )


def _is_multi_character_shx_text(contents: str) -> bool:
    """Whether drawn SHX text reads as words rather than a glyph code.

    Multi-word or five-or-more character strings are sheet text (titles,
    view names, wrapped legend labels). Short unspaced strings stay glyphs:
    on CAD electrical sheets the switch symbols themselves are the single
    letters S, D, V and the digit subscripts drawn beside them.
    """

    cleaned = " ".join(contents.split())
    return " " in cleaned or len(cleaned) >= 5


def _shx_text_boxes(
    symbols: Sequence[PdfSymbolObservation],
    *,
    multi_character_only: bool = True,
) -> dict[int, tuple[tuple[float, float, float, float], ...]]:
    """Displayed rectangles of the SHX text on each page.

    By default only multi-character (word) text; with
    ``multi_character_only=False`` every drawn string, glyph codes included.
    """

    boxes: dict[int, list[tuple[float, float, float, float]]] = {}
    for symbol in symbols:
        if not _is_shx_text_annotation(symbol):
            continue
        rect = symbol.metadata.get("rect_pt")
        contents = str(symbol.metadata.get("contents") or "")
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            continue
        if multi_character_only and not _is_multi_character_shx_text(contents):
            continue
        boxes.setdefault(symbol.page, []).append(
            (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        )
    return {
        page: tuple(boxes[page]) for page in sorted(boxes)
    }


def _points_inside_box_pt(
    points: Sequence[tuple[float, float]],
    box: tuple[float, float, float, float],
    *,
    tolerance_pt: float,
) -> bool:
    x0, y0, x1, y1 = box
    return all(
        x0 - tolerance_pt <= x <= x1 + tolerance_pt
        and y0 - tolerance_pt <= y <= y1 + tolerance_pt
        for x, y in points
    )


_DASH_ARC_MIN_DASHES = 4
_DASH_ARC_MIN_SEGMENT_PT = 2.0
_DASH_ARC_MAX_SEGMENT_PT = 34.0
_DASH_ARC_CHAIN_GAP_PT = 30.0
# A circuit arc's circle is larger than any device glyph: an arc that could
# close inside the glyph size limit may be a glyph outline drawn in pieces.
_DASH_ARC_MIN_RADIUS_PT = _GLYPH_PATH_MAX_EXTENT_PT / 2.0
_DASH_ARC_MAX_RADIUS_PT = 600.0
_DASH_ARC_FIT_TOLERANCE_PT = 1.0
_DASH_ARC_FIT_TOLERANCE_RATIO = 0.01
_DASH_ARC_TANGENT_TOLERANCE_RAD = 0.61
# A curved dash bends away from its chord by at most this share of the chord.
_DASH_CURVE_MAX_SAGITTA_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class _Dash:
    vector: PdfVectorPathObservation
    start: tuple[float, float]
    end: tuple[float, float]
    middle: tuple[float, float]


def _dash_segment(vector: PdfVectorPathObservation) -> _Dash | None:
    """One short open dash, straight or gently curved, or None.

    A dash runs one way along its chord: every point projects forward along
    the chord and stays within a small bend of it. Its middle is the point
    halfway along the drawn path, which lies on the arc a curved dash follows.
    """

    points = vector.points_pt
    if vector.closed or len(points) < 2:
        return None
    start, end = points[0], points[-1]
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    if not _DASH_ARC_MIN_SEGMENT_PT <= chord <= _DASH_ARC_MAX_SEGMENT_PT:
        return None
    if len(points) == 2:
        return _Dash(
            vector,
            start,
            end,
            ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0),
        )
    unit = ((end[0] - start[0]) / chord, (end[1] - start[1]) / chord)
    bend_limit = _DASH_CURVE_MAX_SAGITTA_RATIO * chord
    previous = 0.0
    for x, y in points:
        along = (x - start[0]) * unit[0] + (y - start[1]) * unit[1]
        across = (x - start[0]) * unit[1] - (y - start[1]) * unit[0]
        if abs(across) > bend_limit or along < previous - 1e-6:
            return None
        previous = along
    lengths = [
        math.hypot(second[0] - first[0], second[1] - first[1])
        for first, second in zip(points, points[1:])
    ]
    remaining = sum(lengths) / 2.0
    middle = end
    for (first, second), length in zip(zip(points, points[1:]), lengths):
        if remaining <= length:
            share = remaining / length if length > 0.0 else 0.0
            middle = (
                first[0] + share * (second[0] - first[0]),
                first[1] + share * (second[1] - first[1]),
            )
            break
        remaining -= length
    return _Dash(vector, start, end, middle)


def _fit_circle(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float, float] | None:
    """Least-squares circle through points, as (x, y, radius), or None."""

    count = len(points)
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_x2 = sum(x * x for x, _ in points)
    sum_y2 = sum(y * y for _, y in points)
    sum_xy = sum(x * y for x, y in points)
    sum_xz = sum(x * (x * x + y * y) for x, y in points)
    sum_yz = sum(y * (x * x + y * y) for x, y in points)
    sum_z = sum(x * x + y * y for x, y in points)
    matrix = (
        (sum_x2, sum_xy, sum_x),
        (sum_xy, sum_y2, sum_y),
        (sum_x, sum_y, float(count)),
    )
    rhs = (-sum_xz, -sum_yz, -sum_z)

    def determinant3(m: tuple[tuple[float, ...], ...]) -> float:
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    det = determinant3(matrix)
    if abs(det) <= 1e-9:
        return None
    swapped = tuple(
        tuple(rhs[index] if column == 0 else matrix[index][column] for column in range(3))
        for index in range(3)
    )
    a = determinant3(swapped) / det
    swapped = tuple(
        tuple(rhs[index] if column == 1 else matrix[index][column] for column in range(3))
        for index in range(3)
    )
    b = determinant3(swapped) / det
    swapped = tuple(
        tuple(rhs[index] if column == 2 else matrix[index][column] for column in range(3))
        for index in range(3)
    )
    c = determinant3(swapped) / det
    center_x = -a / 2.0
    center_y = -b / 2.0
    radius_squared = center_x * center_x + center_y * center_y - c
    if radius_squared <= 0.0:
        return None
    return center_x, center_y, math.sqrt(radius_squared)


def _dashed_arc_train_vector_ids(
    vectors: Sequence[PdfVectorPathObservation],
) -> set[str]:
    """Element ids of dashes that run along a common circuit-sized arc.

    Circuit arcs are drawn as chains of separate short dashes, straight or
    gently curved. Dashes are linked end to end into chains (each dash end to
    the nearest end of another dash, when that link is mutual), so a device
    glyph, a tag or a second arc meeting a chain cannot pull it apart. Any
    four consecutive dashes of a chain whose middles share one circle, larger
    than a glyph and each dash running along its tangent, are wiring, not
    glyph strokes. Straight dashed runs fit no such circle and are left
    untouched.
    """

    by_page: dict[int, list[PdfVectorPathObservation]] = {}
    for vector in vectors:
        by_page.setdefault(vector.page, []).append(vector)
    matched: set[str] = set()
    for page in sorted(by_page):
        matched.update(_page_dashed_arc_train_vector_ids(by_page[page]))
    return matched


def _dash_window_on_arc(window: Sequence[_Dash]) -> bool:
    """Whether consecutive dashes lie along one circuit-sized circle."""

    circle = _fit_circle([dash.middle for dash in window])
    if circle is None:
        return False
    center_x, center_y, radius = circle
    if not _DASH_ARC_MIN_RADIUS_PT <= radius <= _DASH_ARC_MAX_RADIUS_PT:
        return False
    tolerance = _DASH_ARC_FIT_TOLERANCE_PT + _DASH_ARC_FIT_TOLERANCE_RATIO * radius
    alignment = math.cos(_DASH_ARC_TANGENT_TOLERANCE_RAD)
    for dash in window:
        offset = (dash.middle[0] - center_x, dash.middle[1] - center_y)
        if abs(math.hypot(*offset) - radius) > tolerance:
            return False
        direction = (dash.end[0] - dash.start[0], dash.end[1] - dash.start[1])
        tangent = (-offset[1], offset[0])
        norm = math.hypot(*direction) * math.hypot(*tangent)
        if norm <= 1e-9:
            return False
        dot = direction[0] * tangent[0] + direction[1] * tangent[1]
        if not math.isfinite(dot) or abs(dot) / norm < alignment:
            return False
    return True


def _page_dashed_arc_train_vector_ids(
    vectors: Sequence[PdfVectorPathObservation],
) -> set[str]:
    """Dashed-arc dash ids among the vectors of one page."""

    dashes = sorted(
        (
            dash
            for dash in (_dash_segment(vector) for vector in vectors)
            if dash is not None
        ),
        key=lambda dash: dash.vector.element_id,
    )
    if len(dashes) < _DASH_ARC_MIN_DASHES:
        return set()

    # Every dash end, bucketed on a grid one chain gap wide.
    ends: list[tuple[float, float]] = []
    for dash in dashes:
        ends.extend((dash.start, dash.end))
    cell = _DASH_ARC_CHAIN_GAP_PT
    grid: dict[tuple[int, int], list[int]] = {}
    for index, (x, y) in enumerate(ends):
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(index)

    nearest: dict[int, int] = {}
    for index, (x, y) in enumerate(ends):
        cell_x, cell_y = math.floor(x / cell), math.floor(y / cell)
        best: tuple[float, int] | None = None
        for neighbour_x in (cell_x - 1, cell_x, cell_x + 1):
            for neighbour_y in (cell_y - 1, cell_y, cell_y + 1):
                for other in grid.get((neighbour_x, neighbour_y), ()):
                    if other // 2 == index // 2:
                        continue
                    distance = math.hypot(ends[other][0] - x, ends[other][1] - y)
                    if distance > _DASH_ARC_CHAIN_GAP_PT:
                        continue
                    key = (distance, other)
                    if best is None or key < best:
                        best = key
        if best is not None:
            nearest[index] = best[1]

    # Mutual nearest ends link two dashes; each dash end links at most once,
    # so the links form simple chains.
    links: dict[int, list[int]] = {}
    for index, other in nearest.items():
        if nearest.get(other) == index and index < other:
            links.setdefault(index // 2, []).append(other // 2)
            links.setdefault(other // 2, []).append(index // 2)

    matched: set[str] = set()
    visited: set[int] = set()
    for first in range(len(dashes)):
        if first in visited or len(links.get(first, ())) == 2:
            continue
        chain = [first]
        visited.add(first)
        while True:
            following = [
                other for other in sorted(links.get(chain[-1], ()))
                if other not in visited
            ]
            if not following:
                break
            chain.append(following[0])
            visited.add(following[0])
        for offset in range(len(chain) - _DASH_ARC_MIN_DASHES + 1):
            window = [dashes[index] for index in chain[offset:offset + _DASH_ARC_MIN_DASHES]]
            if _dash_window_on_arc(window):
                matched.update(dash.vector.element_id for dash in window)
    # Closed loops of dashes (every dash linked on both ends).
    for first in range(len(dashes)):
        if first in visited:
            continue
        chain = [first]
        visited.add(first)
        while True:
            following = [
                other for other in sorted(links.get(chain[-1], ()))
                if other not in visited
            ]
            if not following:
                break
            chain.append(following[0])
            visited.add(following[0])
        if len(chain) < _DASH_ARC_MIN_DASHES:
            continue
        loop = chain + chain[: _DASH_ARC_MIN_DASHES - 1]
        for offset in range(len(chain)):
            window = [dashes[index] for index in loop[offset:offset + _DASH_ARC_MIN_DASHES]]
            if len(window) == _DASH_ARC_MIN_DASHES and _dash_window_on_arc(window):
                matched.update(dash.vector.element_id for dash in window)
    return matched


# A path painted at or below this constant alpha is a screened background
# layer (architecture traced under the electrical work), not device linework.
_SCREENED_BACKGROUND_ALPHA_MAX = 0.5


def _painted_alpha(vector: PdfVectorPathObservation) -> float:
    """The most opaque alpha among the components the path actually paints."""

    operator = str(vector.metadata.get("paint_operator") or "").encode("ascii", "replace")
    alphas: list[float] = []
    if operator in _STROKING_PAINT_OPERATORS:
        alphas.append(float(vector.metadata.get("stroke_alpha", 1.0)))
    if operator in _FILLING_PAINT_OPERATORS:
        alphas.append(float(vector.metadata.get("fill_alpha", 1.0)))
    return max(alphas) if alphas else 1.0


def _screened_background_vector_ids(
    vectors: Sequence[PdfVectorPathObservation],
) -> set[str]:
    """Paths painted translucent on a page that also paints opaque paths.

    CAD sheets trace the architecture under the electrical work through a
    low constant alpha while the devices stay opaque. Those screened strokes
    are not device glyph strokes, and chaining them into device clusters
    makes the clusters too large to match. A page painted entirely
    translucent keeps every path, since nothing there sets the devices apart.
    """

    alphas = {vector.element_id: _painted_alpha(vector) for vector in vectors}
    opaque_pages = {
        vector.page
        for vector in vectors
        if alphas[vector.element_id] > _SCREENED_BACKGROUND_ALPHA_MAX
    }
    return {
        vector.element_id
        for vector in vectors
        if vector.page in opaque_pages
        and alphas[vector.element_id] <= _SCREENED_BACKGROUND_ALPHA_MAX
    }


def _glyph_cluster_vectors(
    document: PdfElectricalDocument,
    vectors: Sequence[PdfVectorPathObservation],
) -> tuple[PdfVectorPathObservation, ...]:
    """Vector paths that may take part in device-glyph clustering.

    Letter strokes inside a drawn SHX text string are text, the separate
    dashes of a circuit arc are wiring, and translucent strokes on a sheet
    that paints its devices opaque are the screened architecture; none of
    them may chain real glyphs into oversized clusters or pose as glyph
    strokes itself.
    """

    text_boxes = _shx_text_boxes(document.symbols)
    all_text_boxes = _shx_text_boxes(document.symbols, multi_character_only=False)
    screened_ids = _screened_background_vector_ids(vectors)
    # A path lies wholly inside an axis-aligned text box exactly when its
    # bounding box does.
    bboxes = {vector.element_id: _vector_bbox(vector) for vector in vectors}

    def inside_any(
        vector: PdfVectorPathObservation,
        boxes: Mapping[int, tuple[tuple[float, float, float, float], ...]],
    ) -> bool:
        x0, y0, x1, y1 = bboxes[vector.element_id]
        return any(
            _points_inside_box_pt(
                ((x0, y0), (x1, y1)),
                box,
                tolerance_pt=_SHX_TEXT_BOX_TOLERANCE_PT,
            )
            for box in boxes.get(vector.page, ())
        )

    # Arc dashes are looked for among the opaque linework outside any drawn
    # SHX string: letter strokes, even of one-letter glyph codes, are never
    # dashes, and screened architecture would chain unrelated strokes in.
    arc_ids = _dashed_arc_train_vector_ids(
        [
            vector
            for vector in vectors
            if vector.element_id not in screened_ids
            and not inside_any(vector, all_text_boxes)
        ]
    )
    if not text_boxes and not arc_ids and not screened_ids:
        return tuple(vectors)
    excluded = set(arc_ids) | screened_ids
    excluded.update(
        vector.element_id
        for vector in vectors
        if vector.element_id not in excluded and inside_any(vector, text_boxes)
    )
    return tuple(
        vector for vector in vectors if vector.element_id not in excluded
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

    alias_targets: dict[str, tuple[str, ...]] = {
        "CR": ("access_control_device",),
        "TV": ("catv_outlet",),
        "CATV": ("catv_outlet",),
        "J": ("junction_box_power", "junction_box_data"),
        "JB": ("junction_box_power", "junction_box_data"),
        "D": ("data_outlet",),
        "DATA": ("data_outlet",),
    }
    # A legend that lists a dimmer switch draws its dimmer glyph as a bare
    # "D"; on such a sheet D is never a data-outlet abbreviation.
    if any(
        entry.canonical_type == "switch"
        and "DIMMER" in entry.label.text.upper()
        for entry in entries
    ):
        alias_targets.pop("D", None)
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
    require_legend_title: bool = False,
) -> PdfTextObservation | None:
    if not rows:
        return None
    page = rows[0].cluster.page
    # Every line of a wrapped row label is label text, never the group's heading.
    row_label_ids = {
        element_id
        for row in rows
        for element_id in (row.label_source_element_ids or (row.label.element_id,))
    }
    candidates: list[tuple[float, float, str, PdfTextObservation]] = []
    for observation in texts:
        if observation.page != page or observation.element_id in row_label_ids:
            continue
        if not _looks_like_section_heading(observation):
            continue
        if require_legend_title and not _is_legend_heading(observation):
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
        _is_glyph_cluster(row.cluster)
        and _is_short_legend_label(
            row.label_lines[0] if row.label_lines else row.label
        )
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

    paired_label_ids = {
        element_id
        for row in rows
        for element_id in (row.label_source_element_ids or (row.label.element_id,))
    }
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


_LEGEND_CONTINUATION_X_TOLERANCE_PT = 1.5
_LEGEND_CONTINUATION_MAX_PITCH_RATIO = 1.5
_LEGEND_CONTINUATION_FONT_RATIO = 1.2
# A baseline sits below the visual middle of a capital line by about a third of
# the rendered size; glyphs are drawn centred on that middle, not the baseline.
_TEXT_VISUAL_CENTER_RATIO = 0.35


def _is_continuation_pair(
    upper: PdfTextObservation,
    lower: PdfTextObservation,
) -> bool:
    """Whether ``lower`` can be the next wrapped line of the label ``upper``."""

    if upper.page != lower.page:
        return False
    if not upper.font_size_pt or not lower.font_size_pt:
        return False
    if upper.font_size_pt <= 0.0 or lower.font_size_pt <= 0.0:
        return False
    ratio = max(upper.font_size_pt, lower.font_size_pt) / min(
        upper.font_size_pt,
        lower.font_size_pt,
    )
    if ratio > _LEGEND_CONTINUATION_FONT_RATIO:
        return False
    if abs(upper.x_pt - lower.x_pt) > _LEGEND_CONTINUATION_X_TOLERANCE_PT:
        return False
    pitch = upper.y_pt - lower.y_pt
    return (
        0.0
        < pitch
        <= _LEGEND_CONTINUATION_MAX_PITCH_RATIO * upper.font_size_pt
    )


def _legend_label_blocks(
    labels: Sequence[PdfTextObservation],
    clusters: Sequence[_VectorCluster],
) -> tuple[tuple[PdfTextObservation, ...], ...]:
    """Group the wrapped lines of legend labels into one block per legend row.

    CAD legends wrap long labels onto several lines that share the label's
    left edge and sit one line pitch apart. The row's glyph is drawn beside
    one of those lines, not necessarily the first, so pairing each line with
    its nearest glyph splits one label into pieces and gives the glyph the
    wrong words. Lines are first chained by column and pitch; a chain beside
    several glyphs is then split midway between neighbouring glyphs, so each
    glyph's block spans the wrapped lines around its own line. A chain beside
    one glyph is that row's whole label. A chain with no glyph beside it
    stays as single lines.
    """

    ordered = sorted(
        labels,
        key=lambda item: (item.page, item.x_pt, -item.y_pt, item.element_id),
    )
    keys = [(item.page, item.x_pt) for item in ordered]
    predecessor: dict[str, PdfTextObservation] = {}
    for lower in ordered:
        low = bisect.bisect_left(
            keys,
            (lower.page, lower.x_pt - _LEGEND_CONTINUATION_X_TOLERANCE_PT),
        )
        high = bisect.bisect_right(
            keys,
            (lower.page, lower.x_pt + _LEGEND_CONTINUATION_X_TOLERANCE_PT),
        )
        above = [
            upper
            for upper in ordered[low:high]
            if upper.element_id != lower.element_id
            and _is_continuation_pair(upper, lower)
        ]
        if above:
            predecessor[lower.element_id] = min(
                above,
                key=lambda item: (item.y_pt - lower.y_pt, item.element_id),
            )
    successor: dict[str, PdfTextObservation] = {}
    for lower in ordered:
        upper = predecessor.get(lower.element_id)
        if upper is None:
            continue
        current = successor.get(upper.element_id)
        if current is None or (
            upper.y_pt - lower.y_pt,
            lower.element_id,
        ) < (upper.y_pt - current.y_pt, current.element_id):
            successor[upper.element_id] = lower
    heads = [
        label
        for label in ordered
        if label.element_id not in predecessor
        or successor.get(predecessor[label.element_id].element_id) is not label
    ]

    blocks: list[tuple[PdfTextObservation, ...]] = []
    for head in heads:
        chain = [head]
        while chain[-1].element_id in successor:
            chain.append(successor[chain[-1].element_id])
        if len(chain) == 1:
            blocks.append((head,))
            continue
        top = chain[0]
        bottom = chain[-1]

        def visual_center(line: PdfTextObservation) -> float:
            return line.y_pt + _TEXT_VISUAL_CENTER_RATIO * float(line.font_size_pt or 0.0)

        anchors: set[int] = set()
        for cluster in clusters:
            if cluster.page != top.page:
                continue
            gap = top.x_pt - cluster.bbox_pt[2]
            if not 0.0 <= gap <= _LEGEND_LABEL_HORIZONTAL_DISTANCE_PT:
                continue
            center_y = cluster.center_pt[1]
            if not (
                bottom.y_pt - _LEGEND_ROW_VERTICAL_TOLERANCE_PT
                <= center_y
                <= top.y_pt + _LEGEND_ROW_VERTICAL_TOLERANCE_PT
            ):
                continue
            anchors.add(
                min(
                    range(len(chain)),
                    key=lambda index: (
                        abs(visual_center(chain[index]) - center_y),
                        index,
                    ),
                )
            )
        if not anchors:
            blocks.extend((line,) for line in chain)
            continue
        # Between two neighbouring glyph lines the rows part at the widest
        # line gap: a row gap is drawn at least as wide as the pitch inside a
        # wrapped label, and a glyph need not sit beside its label's middle
        # line. Equal gaps part nearest the midpoint, then at the upper gap.
        # Every block keeps the line its own glyph sits beside; one glyph owns
        # the whole chain.
        starts = sorted(anchors)
        bounds = [0]
        for first, second in zip(starts, starts[1:]):
            middle = (first + second) / 2.0
            bounds.append(
                min(
                    range(first + 1, second + 1),
                    key=lambda index: (
                        -round(chain[index - 1].y_pt - chain[index].y_pt, 3),
                        abs(index - 0.5 - middle),
                        index,
                    ),
                )
            )
        bounds.append(len(chain))
        for index, start in enumerate(bounds[:-1]):
            stop = bounds[index + 1]
            if start < stop:
                blocks.append(tuple(chain[start:stop]))
    return tuple(
        sorted(
            blocks,
            key=lambda block: (block[0].page, -block[0].y_pt, block[0].x_pt, block[0].element_id),
        )
    )


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
            tuple[PdfTextObservation, ...],
        ]
    ] = []
    blocks = _legend_label_blocks(
        [label for label in texts if _is_short_legend_label(label)],
        clusters,
    )
    for block in blocks:
        first = block[0]
        # A wrapped label is classified and reported as its whole text; the
        # row keeps the first line's position.
        label = (
            first
            if len(block) == 1
            else replace(first, text=" ".join(line.text for line in block))
        )
        classification, ranked = _classify_semantic_text(
            label.text,
            rules,
            ambiguity_margin=ambiguity_margin,
        )
        for cluster in clusters:
            if cluster.page != label.page:
                continue
            vertical_delta = min(
                abs(line.y_pt - cluster.center_pt[1])
                for line in block
            )
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
                    block,
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
        block,
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
                label_source_element_ids=(
                    tuple(line.element_id for line in block)
                    if len(block) > 1
                    else ()
                ),
                label_lines=block if len(block) > 1 else (),
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


_LEGEND_FRAME_MAX_PAGE_FRACTION = 0.6
_LEGEND_FRAME_MIN_SIDE_PT = 72.0


def _legend_frame_around(
    heading: PdfTextObservation,
    vectors: Sequence[PdfVectorPathObservation],
) -> tuple[float, float, float, float] | None:
    """The ruled rectangle that encloses a legend title, when the sheet draws one.

    A frame needs a vertical rule on each side of the title that spans the
    title's baseline, and horizontal rules closing the top and bottom between
    them. A frame covering most of the sheet is the sheet border, not a legend.
    """

    horizontal, vertical = _rule_segments(vectors, page=heading.page)
    font = float(heading.font_size_pt or 8.0)
    title_end = heading.x_pt + 0.5 * len(heading.text) * font
    tolerance = 2.0 * _LEGEND_RULE_AXIS_TOLERANCE_PT
    spanning = [
        rule for rule in vertical
        if rule[1] - tolerance <= heading.y_pt <= rule[2] + tolerance
    ]
    left = max((rule for rule in spanning if rule[0] <= heading.x_pt), default=None)
    right = min((rule for rule in spanning if rule[0] >= title_end), default=None)
    if left is None or right is None:
        return None
    bottom = max(left[1], right[1])
    top = min(left[2], right[2])
    if right[0] - left[0] < _LEGEND_FRAME_MIN_SIDE_PT or top - bottom < _LEGEND_FRAME_MIN_SIDE_PT:
        return None

    def closed_at(y: float) -> bool:
        return any(
            abs(rule[1] - y) <= tolerance
            and rule[0] <= left[0] + tolerance
            and rule[2] >= right[0] - tolerance
            for rule in horizontal
        )

    if not (closed_at(bottom) and closed_at(top)):
        return None
    xs = [value for rule in horizontal for value in (rule[0], rule[2])]
    xs.extend(rule[0] for rule in vertical)
    ys = [value for rule in vertical for value in (rule[1], rule[2])]
    ys.extend(rule[1] for rule in horizontal)
    sheet_width = (max(xs) - min(xs)) if xs else 0.0
    sheet_height = (max(ys) - min(ys)) if ys else 0.0
    if (
        sheet_width > 0
        and (right[0] - left[0]) > _LEGEND_FRAME_MAX_PAGE_FRACTION * sheet_width
    ) or (
        sheet_height > 0
        and (top - bottom) > _LEGEND_FRAME_MAX_PAGE_FRACTION * sheet_height
    ):
        return None
    return (left[0], bottom, right[0], top)


_LEGEND_ROW_FRAME_TOLERANCE_PT = 3.0


def _row_inside(row: _LegendRow, bbox: tuple[float, float, float, float]) -> bool:
    """Whether one legend row sits inside a ruled frame.

    CAD glyphs and labels are often drawn touching the frame rules, so a small
    tolerance keeps a frame from being rejected over a stroke that grazes its
    edge; the frame itself is still derived from the rules.
    """

    x0, y0, x1, y1 = bbox
    cx0, cy0, cx1, cy1 = row.cluster.bbox_pt
    tolerance = _LEGEND_ROW_FRAME_TOLERANCE_PT
    return (
        x0 - tolerance <= cx0 and cx1 <= x1 + tolerance
        and y0 - tolerance <= cy0 and cy1 <= y1 + tolerance
        and x0 - tolerance <= row.label.x_pt <= x1 + tolerance
        and y0 - tolerance <= row.label.y_pt <= y1 + tolerance
    )


def _drop_rows_in_rejected_sections(
    rows: Sequence[_LegendRow],
    frame: tuple[float, float, float, float],
    texts: Sequence[PdfTextObservation],
) -> tuple[list[_LegendRow], int, list[str]]:
    """Inside a legend frame, keep rows out of sections such as ABBREVIATIONS.

    A section heading is larger text inside the frame. Each row belongs to the
    nearest heading above it in its own column; rows under a heading with a
    rejected legend context (abbreviations, notes, schedules) are not symbols.
    """

    sizes = sorted(row.label.font_size_pt for row in rows if row.label.font_size_pt)
    if not sizes:
        return list(rows), 0, []
    base = sizes[len(sizes) // 2]
    page = rows[0].cluster.page
    x0, y0, x1, y1 = frame
    headings = [
        text for text in texts
        if text.page == page
        and x0 <= text.x_pt <= x1
        and y0 <= text.y_pt <= y1
        and (text.font_size_pt or 0.0) >= 1.15 * base
        and _looks_like_section_heading(text)
    ]
    kept: list[_LegendRow] = []
    dropped = 0
    for row in rows:
        above = [
            heading for heading in headings
            if heading.y_pt > row.label.y_pt
            and row.cluster.bbox_pt[0] - 40.0 <= heading.x_pt <= row.label.x_pt + 40.0
        ]
        section = min(
            above,
            key=lambda heading: (heading.y_pt - row.label.y_pt, heading.element_id),
            default=None,
        )
        if section is not None and _heading_has_rejected_legend_context(section):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped, sorted({" ".join(heading.text.split()) for heading in headings})


def _join_framed_legend_columns(
    region: _LegendRegion,
    page_groups: Sequence[tuple[_LegendRow, ...]],
    texts: Sequence[PdfTextObservation],
    vectors: Sequence[PdfVectorPathObservation],
) -> _LegendRegion:
    """Add the other columns of a ruled legend block to its title-matched region.

    A legend often runs in columns, and only one sits under the title; the
    others sit under their own section headings (power, controls). Row
    groups entirely inside the same ruled frame as the title belong to that
    legend. A group headed as notes, keynotes or a schedule does not.
    """

    if region.heading is None:
        return region
    frame = _legend_frame_around(region.heading, vectors)
    if frame is None or not all(_row_inside(row, frame) for row in region.rows):
        return region
    present = {(row.cluster.geometry_key, row.label.element_id) for row in region.rows}
    joined: list[_LegendRow] = list(region.rows)
    joined_groups = 0
    for group in page_groups:
        if any((row.cluster.geometry_key, row.label.element_id) in present for row in group):
            continue
        if not all(_row_inside(row, frame) for row in group):
            continue
        heading = _nearest_section_heading(group, texts, vectors, allow_beside=True)
        if heading is not None and _heading_has_rejected_legend_context(heading):
            # A rejected section heading is only fatal for the whole group when
            # it sits outside the legend frame (a notes column beside the
            # legend). ABBREVIATIONS inside a legend frame heads one section of
            # a shared ruled block: join the group and let the per-row section
            # drop below remove just its rows.
            heading_inside_frame = (
                frame[0] <= heading.x_pt <= frame[2]
                and frame[1] <= heading.y_pt <= frame[3]
            )
            if not heading_inside_frame:
                continue
        joined.extend(group)
        joined_groups += 1
        present.update((row.cluster.geometry_key, row.label.element_id) for row in group)
    joined, dropped, sections = _drop_rows_in_rejected_sections(joined, frame, texts)
    return replace(
        region,
        rows=tuple(
            sorted(
                joined,
                key=lambda row: (
                    row.cluster.page,
                    -row.cluster.center_pt[1],
                    row.cluster.center_pt[0],
                    row.label.element_id,
                ),
            )
        ),
        table_bbox_pt=frame,
        legend_frame={
            "method": "ruled frame around the legend title",
            "bbox_pt": list(frame),
            "joined_row_groups": joined_groups,
            "section_headings": sections,
            "rows_dropped_in_rejected_sections": dropped,
        },
    )


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
                require_legend_title=True,
            )
            if heading is None:
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
            regions_by_page[page] = _join_framed_legend_columns(
                max(
                    candidates,
                    key=lambda region: (
                        len(region.rows),
                        sum(row.classification is not None for row in region.rows),
                        (region.heading.font_size_pt or 0.0) if region.heading else 0.0,
                        region.heading.element_id if region.heading else "",
                    ),
                ),
                page_groups,
                texts,
                vectors,
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
    derivation: str = DERIVATION_OBSERVED,
    attributes: Mapping[str, Any] | None = None,
) -> Provenance:
    """Record one piece of recognition evidence.

    Defaults to ``observed`` because every caller here is reporting something
    actually found on a sheet. Anything this importer *synthesizes* rather than
    reads -- a port invented to give a circuit an endpoint -- must override this.
    """
    return Provenance(
        source_kind=source_kind,
        source_id=document.source_id,
        source_element_id=element_id,
        page=page,
        method=method,
        confidence=confidence,
        derivation=derivation,
        attributes=dict(attributes or {}),
    )




def _normalize_lighting_tag(value: str) -> str | None:
    cleaned = re.sub(r"\s+", "", value.strip().upper())
    if not cleaned or _LIGHTING_TAG_RE.fullmatch(cleaned) is None:
        return None
    return cleaned


def _lighting_heading_kind(observation: PdfTextObservation) -> str | None:
    normalized = _normalize_legend_alias(observation.text)
    words = set(normalized.split())
    has_lighting_cue = bool(
        words
        & {
            "LIGHT",
            "LIGHTING",
            "FIXTURE",
            "FIXTURES",
            "LUMINAIRE",
            "LUMINAIRES",
        }
    )
    if not has_lighting_cue:
        return None
    if "SCHEDULE" in words:
        return "fixture_schedule"
    if "LEGEND" in words:
        return "lighting_legend"
    return None


def _lighting_schedule_header_key(value: str) -> str | None:
    normalized = _normalize_legend_alias(value)
    return _LIGHTING_SCHEDULE_HEADER_ALIASES.get(normalized)


def _detect_lighting_fixture_schedules(
    texts: Sequence[PdfTextObservation],
) -> tuple[
    dict[tuple[int, str], _LightingScheduleRow],
    set[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    headings = sorted(
        (
            observation
            for observation in texts
            if _lighting_heading_kind(observation) == "fixture_schedule"
        ),
        key=lambda item: (item.page, -item.y_pt, item.x_pt, item.element_id),
    )
    claimed_text_ids = {heading.element_id for heading in headings}
    rows_by_key: dict[tuple[int, str], list[_LightingScheduleRow]] = {}
    schedule_metadata: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for heading in headings:
        header_candidates = [
            observation
            for observation in texts
            if observation.page == heading.page
            and observation.element_id != heading.element_id
            and 0.0 < heading.y_pt - observation.y_pt <= _LIGHTING_HEADER_MAX_GAP_PT
            and _lighting_schedule_header_key(observation.text) is not None
        ]
        header_groups: list[list[PdfTextObservation]] = []
        for candidate in sorted(
            header_candidates,
            key=lambda item: (-item.y_pt, item.x_pt, item.element_id),
        ):
            group = next(
                (
                    item
                    for item in header_groups
                    if abs(item[0].y_pt - candidate.y_pt)
                    <= _LIGHTING_HEADER_Y_TOLERANCE_PT
                ),
                None,
            )
            if group is None:
                group = []
                header_groups.append(group)
            group.append(candidate)

        valid_header_groups = []
        for group in header_groups:
            keys = {
                _lighting_schedule_header_key(observation.text)
                for observation in group
            }
            if "tag" in keys and len(keys - {None, "tag"}) >= 1:
                valid_header_groups.append(group)
        if not valid_header_groups:
            unresolved.append(
                {
                    "kind": "lighting_fixture_schedule",
                    "page": heading.page,
                    "source_element_id": heading.element_id,
                    "source_text": heading.text,
                    "status": "unresolved_lighting_schedule",
                    "reason_code": "lighting_schedule_header_not_confirmed",
                    "reason": (
                        "lighting fixture schedule heading is present but a tag/type "
                        "column plus a descriptive column was not confirmed"
                    ),
                }
            )
            continue

        header_group = min(
            valid_header_groups,
            key=lambda group: (
                heading.y_pt - max(item.y_pt for item in group),
                -len(group),
                tuple(item.element_id for item in group),
            ),
        )
        headers: dict[str, PdfTextObservation] = {}
        for observation in sorted(
            header_group,
            key=lambda item: (item.x_pt, item.element_id),
        ):
            key = _lighting_schedule_header_key(observation.text)
            if key is not None and key not in headers:
                headers[key] = observation
        claimed_text_ids.update(item.element_id for item in header_group)

        tag_header = headers["tag"]
        header_y = sum(item.y_pt for item in header_group) / len(header_group)
        ordered_headers = sorted(headers.items(), key=lambda item: item[1].x_pt)
        min_x = ordered_headers[0][1].x_pt - 24.0
        max_x = ordered_headers[-1][1].x_pt + 160.0

        tag_cells = [
            observation
            for observation in texts
            if observation.page == heading.page
            and observation.element_id not in claimed_text_ids
            and 0.0 < header_y - observation.y_pt <= _LIGHTING_SCHEDULE_VERTICAL_SPAN_PT
            and abs(observation.x_pt - tag_header.x_pt) <= 42.0
            and min_x <= observation.x_pt <= max_x
            and _normalize_lighting_tag(observation.text) is not None
        ]
        parsed_rows: list[_LightingScheduleRow] = []
        for tag_cell in sorted(
            tag_cells,
            key=lambda item: (-item.y_pt, item.x_pt, item.element_id),
        ):
            tag = _normalize_lighting_tag(tag_cell.text)
            assert tag is not None
            row_cells = [
                observation
                for observation in texts
                if observation.page == heading.page
                and min_x <= observation.x_pt <= max_x
                and abs(observation.y_pt - tag_cell.y_pt)
                <= _LIGHTING_ROW_Y_TOLERANCE_PT
                and observation.element_id != heading.element_id
            ]
            if tag_cell not in row_cells:
                row_cells.append(tag_cell)
            fields: dict[str, str] = {"tag": tag}
            source_ids: list[str] = []
            source_texts: list[str] = []
            for cell in sorted(row_cells, key=lambda item: (item.x_pt, item.element_id)):
                nearest_key, nearest_header = min(
                    ordered_headers,
                    key=lambda item: abs(cell.x_pt - item[1].x_pt),
                )
                if nearest_key == "tag":
                    cell_tag = _normalize_lighting_tag(cell.text)
                    if cell_tag != tag:
                        continue
                    value = tag
                else:
                    value = " ".join(cell.text.split())
                    if not value:
                        continue
                if nearest_key in fields and fields[nearest_key] != value:
                    continue
                fields[nearest_key] = value
                source_ids.append(cell.element_id)
                source_texts.append(cell.text)

            if len(fields) <= 1:
                continue
            row = _LightingScheduleRow(
                page=heading.page,
                tag=tag,
                fields=dict(sorted(fields.items())),
                source_element_ids=tuple(sorted(set(source_ids))),
                source_texts=tuple(source_texts),
            )
            parsed_rows.append(row)
            claimed_text_ids.update(row.source_element_ids)

        for row in parsed_rows:
            rows_by_key.setdefault((row.page, row.tag), []).append(row)
        schedule_metadata.append(
            {
                "page": heading.page,
                "method": "lighting-fixture-schedule-text-columns",
                "heading_element_id": heading.element_id,
                "heading_text": heading.text,
                "header_element_ids": sorted(
                    item.element_id for item in header_group
                ),
                "columns": [
                    {
                        "field": key,
                        "source_text": observation.text,
                        "x_pt": observation.x_pt,
                    }
                    for key, observation in ordered_headers
                ],
                "row_count": len(parsed_rows),
            }
        )

    resolved: dict[tuple[int, str], _LightingScheduleRow] = {}
    for key, rows in sorted(rows_by_key.items()):
        unique_payloads = {
            tuple(sorted(row.fields.items()))
            for row in rows
        }
        if len(unique_payloads) != 1:
            page, tag = key
            unresolved.append(
                {
                    "kind": "lighting_fixture_schedule",
                    "page": page,
                    "source_element_id": min(
                        source_id
                        for row in rows
                        for source_id in row.source_element_ids
                    ),
                    "fixture_tag": tag,
                    "status": "unresolved_lighting_schedule",
                    "reason_code": "lighting_schedule_tag_conflict",
                    "reason": (
                        "fixture schedule contains conflicting rows for the same tag"
                    ),
                    "candidate_rows": [
                        dict(row.fields)
                        for row in sorted(
                            rows,
                            key=lambda item: (
                                item.page,
                                item.source_element_ids,
                            ),
                        )
                    ],
                }
            )
            continue
        resolved[key] = sorted(
            rows,
            key=lambda item: (item.source_element_ids, item.source_texts),
        )[0]

    return resolved, claimed_text_ids, unresolved, schedule_metadata


def _lighting_legend_label_columns(
    labels: Sequence[PdfTextObservation],
) -> tuple[tuple[PdfTextObservation, ...], ...]:
    """Group legend labels into x-aligned table columns."""
    columns: list[list[PdfTextObservation]] = []
    for label in sorted(
        labels,
        key=lambda item: (item.x_pt, -item.y_pt, item.element_id),
    ):
        column = next(
            (
                column
                for column in columns
                if abs(label.x_pt - column[0].x_pt)
                <= _LIGHTING_LEGEND_LABEL_COLUMN_TOLERANCE_PT
            ),
            None,
        )
        if column is None:
            columns.append([label])
        else:
            column.append(label)
    return tuple(
        tuple(
            sorted(
                column,
                key=lambda item: (-item.y_pt, item.x_pt, item.element_id),
            )
        )
        for column in columns
    )


def _lighting_legend_row_grid(
    labels: Sequence[PdfTextObservation],
) -> tuple[float, ...]:
    """Collapse legend-label y positions into descending table row bands."""
    rows: list[float] = []
    for y in sorted({label.y_pt for label in labels}, reverse=True):
        if not rows or rows[-1] - y > _LIGHTING_LEGEND_ROW_GRID_TOLERANCE_PT:
            rows.append(y)
    return tuple(rows)


def _lighting_legend_column_grid_rows(
    column: Sequence[PdfTextObservation],
    row_grid: Sequence[float],
) -> list[int] | None:
    """Row index per label, or None when the column is not pure table rows.

    Pure means every label continues the grid and no two labels share a
    row: a column with an off-grid or doubled-up member is not legend
    structure even though some of its rows line up.
    """
    assignments: list[int] = []
    for label in column:
        index = next(
            (
                index
                for index, row_y in enumerate(row_grid)
                if abs(label.y_pt - row_y)
                <= _LIGHTING_LEGEND_ROW_GRID_TOLERANCE_PT
            ),
            None,
        )
        if index is None:
            return None
        assignments.append(index)
    if len(set(assignments)) != len(assignments):
        return None
    return assignments


def _lighting_legend_row_description_text(
    label: PdfTextObservation,
    *,
    texts: Sequence[PdfTextObservation],
) -> PdfTextObservation | None:
    """Nearest non-tag text on a legend label's baseline to its right.

    Legend rows carry descriptions such as ``DIMMER SWITCH``; a tagged
    field fixture's label has nothing but other tags beside it.
    """
    return min(
        (
            observation
            for observation in texts
            if observation.page == label.page
            and observation.element_id != label.element_id
            and abs(observation.y_pt - label.y_pt) <= _LIGHTING_ROW_Y_TOLERANCE_PT
            and label.x_pt
            < observation.x_pt
            <= label.x_pt + _LIGHTING_LEGEND_DESCRIPTION_SPAN_PT
            and _normalize_lighting_tag(observation.text) is None
        ),
        key=lambda observation: (observation.x_pt, observation.element_id),
        default=None,
    )


def _lighting_legend_row_has_description(
    label: PdfTextObservation,
    *,
    texts: Sequence[PdfTextObservation],
) -> bool:
    """Whether a legend row carries description text on its baseline."""
    return _lighting_legend_row_description_text(label, texts=texts) is not None


def _lighting_legend_row_description_edge(
    label: PdfTextObservation,
    *,
    texts: Sequence[PdfTextObservation],
) -> float:
    """Estimated right edge of a legend row's printed description.

    Character-count estimate at the description's own font size; the
    estimate may legitimately run past the evidence search span, which is
    what lets a legend printed with long descriptions admit the column
    beside it. A row with no description at all is assumed to occupy the
    full description span, so an undescribed heading column still bounds
    how far the next printed column can sit.
    """
    description = _lighting_legend_row_description_text(label, texts=texts)
    if description is None:
        return label.x_pt + _LIGHTING_LEGEND_DESCRIPTION_SPAN_PT
    font_size = float(description.font_size_pt or 8.0)
    return description.x_pt + len(description.text) * font_size * 0.6


def _detect_lighting_legend_entries(
    *,
    texts: Sequence[PdfTextObservation],
    clusters: Sequence[_VectorCluster],
    excluded_text_ids: set[str],
) -> tuple[
    dict[int, tuple[_LightingLegendEntry, ...]],
    set[tuple[int, str]],
    set[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    headings = sorted(
        (
            observation
            for observation in texts
            if _lighting_heading_kind(observation) == "lighting_legend"
        ),
        key=lambda item: (item.page, -item.y_pt, item.x_pt, item.element_id),
    )
    claimed_text_ids = {heading.element_id for heading in headings}
    prototype_keys: set[tuple[int, str]] = set()
    unresolved: list[dict[str, Any]] = []
    region_metadata: list[dict[str, Any]] = []
    entries_by_page: dict[int, list[_LightingLegendEntry]] = {}

    for heading in headings:
        band_labels = [
            observation
            for observation in texts
            if observation.page == heading.page
            and observation.element_id not in excluded_text_ids
            and observation.element_id != heading.element_id
            and 0.0 < heading.y_pt - observation.y_pt <= _LIGHTING_LEGEND_VERTICAL_SPAN_PT
            and _normalize_lighting_tag(observation.text) is not None
        ]
        # Legend rows are read as table structure, not as raw distance from
        # the heading: the heading's own column anchors the table's row
        # grid, and a further printed column joins the same table only when
        # all three of these hold, so no one signal admits a column alone:
        #   contiguity - it starts within about one inch past where the
        #     admitted table's printed descriptions are estimated to end,
        #     never anywhere on the sheet;
        #   table purity - every one of its labels continues the grid, each
        #     on a distinct row (a field-fixture run usually has off-grid
        #     or doubled-up members);
        #   row evidence - every row carries description text to the right,
        #     the way a printed legend row does and a bare field tag does
        #     not.
        heading_column_labels = [
            observation
            for observation in band_labels
            if abs(observation.x_pt - heading.x_pt)
            <= _LIGHTING_LEGEND_HEADING_COLUMN_SPAN_PT
        ]
        row_grid = _lighting_legend_row_grid(heading_column_labels)
        heading_label_ids = {label.element_id for label in heading_column_labels}
        candidate_labels: list[PdfTextObservation] = list(heading_column_labels)
        pending_columns: list[tuple[PdfTextObservation, ...]] = []
        # A column straddling the heading span is admitted whole, so its
        # labels anchor contiguity for the next column out.
        for column in _lighting_legend_label_columns(band_labels):
            if any(
                abs(label.x_pt - heading.x_pt)
                <= _LIGHTING_LEGEND_HEADING_COLUMN_SPAN_PT
                for label in column
            ):
                candidate_labels.extend(
                    label
                    for label in column
                    if label.element_id not in heading_label_ids
                )
            else:
                pending_columns.append(column)
        admitted_labels = list(candidate_labels)
        table_description_edge = max(
            (
                _lighting_legend_row_description_edge(label, texts=texts)
                for label in admitted_labels
            ),
            default=math.inf,
        )
        # Chained expansion admits multi-column legends column by column;
        # a rejected column never anchors anything farther out.
        progressed = True
        while progressed and pending_columns:
            progressed = False
            remaining: list[tuple[PdfTextObservation, ...]] = []
            for column in pending_columns:
                column_x = min(label.x_pt for label in column)
                if column_x <= table_description_edge:
                    # Starts inside the text the table is estimated to have
                    # already printed. The edge only grows, so this column
                    # never becomes the next one.
                    continue
                if (
                    column_x
                    > table_description_edge + _LIGHTING_LEGEND_COLUMN_GAP_PT
                ):
                    # Well past the printed text; re-tested if a nearer
                    # admission extends the edge outward.
                    remaining.append(column)
                    continue
                grid_rows = _lighting_legend_column_grid_rows(column, row_grid)
                if (
                    grid_rows is None
                    or len(column) < 2
                    or not all(
                        _lighting_legend_row_has_description(
                            label,
                            texts=texts,
                        )
                        for label in column
                    )
                ):
                    continue
                candidate_labels.extend(column)
                admitted_labels.extend(column)
                table_description_edge = max(
                    table_description_edge,
                    *(
                        _lighting_legend_row_description_edge(
                            label,
                            texts=texts,
                        )
                        for label in column
                    ),
                )
                progressed = True
            pending_columns = remaining
        local_entries: list[_LightingLegendEntry] = []
        used_keys: set[tuple[int, str]] = set()
        for label in sorted(
            candidate_labels,
            key=lambda item: (-item.y_pt, item.x_pt, item.element_id),
        ):
            tag = _normalize_lighting_tag(label.text)
            assert tag is not None
            nearby = [
                cluster
                for cluster in clusters
                if cluster.page == heading.page
                and (cluster.page, cluster.geometry_key) not in used_keys
                and abs(cluster.center_pt[1] - label.y_pt)
                <= _LIGHTING_LEGEND_ROW_GLYPH_Y_TOLERANCE_PT
                and _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    label.x_pt,
                    label.y_pt,
                )
                <= _LIGHTING_TAG_CLUSTER_RADIUS_PT + 18.0
            ]
            nearby.sort(
                key=lambda cluster: (
                    _distance_pt(
                        cluster.center_pt[0],
                        cluster.center_pt[1],
                        label.x_pt,
                        label.y_pt,
                    ),
                    cluster.geometry_key,
                )
            )
            if not nearby:
                continue
            if (
                len(nearby) > 1
                and _distance_pt(
                    nearby[1].center_pt[0],
                    nearby[1].center_pt[1],
                    label.x_pt,
                    label.y_pt,
                )
                - _distance_pt(
                    nearby[0].center_pt[0],
                    nearby[0].center_pt[1],
                    label.x_pt,
                    label.y_pt,
                )
                < _LIGHTING_TAG_ASSOCIATION_MARGIN_PT
            ):
                unresolved.append(
                    {
                        "kind": "lighting_legend_tag",
                        "page": label.page,
                        "source_element_id": label.element_id,
                        "source_text": label.text,
                        "fixture_tag": tag,
                        "status": "unresolved_lighting_legend",
                        "reason_code": "lighting_legend_tag_geometry_ambiguous",
                        "reason": (
                            "lighting legend tag is equally close to more than one "
                            "glyph prototype"
                        ),
                    }
                )
                claimed_text_ids.add(label.element_id)
                continue
            cluster = nearby[0]
            used_keys.add((cluster.page, cluster.geometry_key))
            local_entries.append(
                _LightingLegendEntry(
                    page=heading.page,
                    tag=tag,
                    prototype=cluster,
                    label=label,
                    heading=heading,
                    confidence=0.99,
                )
            )

        # A heading plus one coincidental letter is too weak to establish a
        # lighting legend. Two tagged rows make the path distinct from room
        # labels and ordinary drafting notes.
        if len(local_entries) < 2:
            if candidate_labels:
                unresolved.append(
                    {
                        "kind": "lighting_legend",
                        "page": heading.page,
                        "source_element_id": heading.element_id,
                        "source_text": heading.text,
                        "status": "unresolved_lighting_legend",
                        "reason_code": "lighting_legend_structure_not_confirmed",
                        "reason": (
                            "lighting legend heading did not contain at least two "
                            "unambiguous tag-to-glyph rows"
                        ),
                    }
                )
            continue

        entries_by_page.setdefault(heading.page, []).extend(local_entries)
        for entry in local_entries:
            prototype_keys.add((entry.page, entry.prototype.geometry_key))
            claimed_text_ids.add(entry.label.element_id)
        region_metadata.append(
            {
                "page": heading.page,
                "method": "lighting-letter-tag-legend",
                "heading_element_id": heading.element_id,
                "heading_text": heading.text,
                "row_count": len(local_entries),
                "tags": sorted({entry.tag for entry in local_entries}),
            }
        )

    return (
        {
            page: tuple(
                sorted(
                    entries,
                    key=lambda entry: (
                        entry.tag,
                        entry.label.element_id,
                        entry.prototype.geometry_key,
                    ),
                )
            )
            for page, entries in sorted(entries_by_page.items())
        },
        prototype_keys,
        claimed_text_ids,
        unresolved,
        region_metadata,
    )


def _cluster_shape_class(cluster: _VectorCluster) -> str:
    """Round versus polygonal glyph class from the boundary cloud.

    Arc-length-uniform boundary sampling keeps the ratio of extreme
    centroid distances stable, so circles (and near-circular polygons such
    as octagons) separate from shapes whose corners stick out, like squares
    and triangles. Classification is rotation- and scale-invariant.
    """
    cloud = _resampled_point_cloud(cluster)
    if len(cloud) < 2:
        return "polygonal"
    count = len(cloud)
    mean_x = sum(x for x, _ in cloud) / count
    mean_y = sum(y for _, y in cloud) / count
    radii = [math.hypot(x - mean_x, y - mean_y) for x, y in cloud]
    min_radius = min(radii)
    if min_radius <= 1e-9:
        return "polygonal"
    if max(radii) <= _LIGHTING_SHAPE_CLASS_ROUND_MAX_RADIAL_RANGE * min_radius:
        return "round"
    return "polygonal"


def _lighting_shape_class_guard(
    score: float,
    *,
    candidate_class: str,
    prototype: _VectorCluster,
) -> tuple[float, dict[str, Any] | None]:
    """Cap a cross-class score below the confirm minimum.

    A square next to an `A` tag scores about 0.567 against a circle legend
    row, barely over the match minimum; the round-versus-polygonal class
    disagreement may only confirm at the strong-score bar instead.
    """
    if score >= _LIGHTING_SHAPE_CLASS_STRONG_SCORE:
        return score, None
    prototype_class = _cluster_shape_class(prototype)
    if prototype_class == candidate_class:
        return score, None
    return (
        round(min(score, _LIGHTING_GLYPH_CONFIRM_SCORE - 0.01), 6),
        {
            "candidate_shape_class": candidate_class,
            "prototype_shape_class": prototype_class,
            "raw_shape_score": score,
            "shape_class_strong_score": _LIGHTING_SHAPE_CLASS_STRONG_SCORE,
        },
    )


def _lighting_shape_support(
    cluster: _VectorCluster,
    entries: Sequence[_LightingLegendEntry],
    *,
    tag: str | None = None,
) -> tuple[_LightingLegendEntry | None, float | None, dict[str, Any] | None]:
    eligible = [
        entry
        for entry in entries
        if tag is None or entry.tag == tag
    ]
    if not eligible:
        return None, None, None
    candidate_class = _cluster_shape_class(cluster)
    ranked = []
    for entry in eligible:
        score, guard = _lighting_shape_class_guard(
            _cluster_match_score(cluster, entry.prototype),
            candidate_class=candidate_class,
            prototype=entry.prototype,
        )
        ranked.append((score, guard, entry))
    ranked.sort(
        key=lambda item: (
            -item[0],
            item[2].tag,
            item[2].label.element_id,
            item[2].prototype.geometry_key,
        ),
    )
    score, guard, entry = ranked[0]
    return entry, score, guard


def _is_lighting_shaped(
    cluster: _VectorCluster,
    entries: Sequence[_LightingLegendEntry],
) -> bool:
    """Whether the sheet's own lighting legend says this glyph is a fixture.

    Lighting claims only such geometry. Anything else it rejects stays
    available to the power-device path instead of vanishing into a lighting
    diagnostic.
    """
    _entry, score, _guard = _lighting_shape_support(cluster, entries)
    return score is not None and score >= _LIGHTING_GLYPH_CONFIRM_SCORE


def _lighting_legend_row_description(
    entry: _LightingLegendEntry,
    *,
    texts: Sequence[PdfTextObservation],
    clusters: Sequence[_VectorCluster],
) -> tuple[PdfTextObservation, ...]:
    """Text to the right of a legend label, up to the next legend entry.

    A multi-column legend puts the next entry's glyph, label and description
    in the same row band, so the window ends at the first other glyph or
    tag-shaped label to the right. A row never borrows a neighbouring
    column's description.
    """
    label = entry.label
    row_texts = [
        observation
        for observation in texts
        if observation.page == label.page
        and observation.element_id != label.element_id
        and abs(observation.y_pt - label.y_pt) <= _LIGHTING_ROW_Y_TOLERANCE_PT
        and observation.x_pt > label.x_pt
    ]
    end_x = min(
        [
            label.x_pt + _LIGHTING_LEGEND_DESCRIPTION_SPAN_PT,
            *(
                observation.x_pt
                for observation in row_texts
                if _normalize_lighting_tag(observation.text) is not None
            ),
            *(
                cluster.bbox_pt[0]
                for cluster in clusters
                if cluster.page == label.page
                and cluster.geometry_key != entry.prototype.geometry_key
                and abs(cluster.center_pt[1] - label.y_pt)
                <= _LIGHTING_LEGEND_ROW_GLYPH_Y_TOLERANCE_PT
                and cluster.bbox_pt[0] > label.x_pt
            ),
        ]
    )
    return tuple(
        sorted(
            (observation for observation in row_texts if observation.x_pt < end_x),
            key=lambda item: (item.x_pt, item.element_id),
        )
    )


def _lighting_schedule_attributes(row: _LightingScheduleRow) -> dict[str, Any]:
    attributes: dict[str, Any] = dict(row.fields)
    wattage = row.fields.get("wattage")
    if wattage:
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:W|WATT|WATTS)?\b", wattage, re.I)
        if match:
            attributes["wattage_w"] = float(match.group(1))
    attributes["source_page"] = row.page
    attributes["source_element_ids"] = list(row.source_element_ids)
    return attributes


def _recognize_lighting(
    document: PdfElectricalDocument,
    *,
    texts: Sequence[PdfTextObservation],
    vectors: Sequence[PdfVectorPathObservation],
) -> tuple[
    dict[str, _EntityCandidate],
    set[str],
    set[str],
    set[str],
    list[dict[str, Any]],
    dict[str, Any],
]:
    clusters = tuple(
        cluster
        for cluster in _cluster_small_vector_glyphs(
            _glyph_cluster_vectors(document, vectors)
        )
        if _is_glyph_cluster(cluster)
    )
    (
        schedules,
        schedule_text_ids,
        schedule_unresolved,
        schedule_metadata,
    ) = _detect_lighting_fixture_schedules(texts)
    (
        legend_entries_by_page,
        prototype_keys,
        legend_text_ids,
        legend_unresolved,
        legend_metadata,
    ) = _detect_lighting_legend_entries(
        texts=texts,
        clusters=clusters,
        excluded_text_ids=set(schedule_text_ids),
    )

    claimed_text_ids = set(schedule_text_ids) | set(legend_text_ids)
    claimed_vector_ids = {
        element_id
        for cluster in clusters
        if (cluster.page, cluster.geometry_key) in prototype_keys
        for element_id in cluster.source_element_ids
    }
    matched_vector_ids: set[str] = set()
    unresolved: list[dict[str, Any]] = [
        *schedule_unresolved,
        *legend_unresolved,
    ]
    candidates: dict[str, _EntityCandidate] = {}

    schedule_tags_by_page: dict[int, set[str]] = {}
    for page, tag in schedules:
        schedule_tags_by_page.setdefault(page, set()).add(tag)

    # A legend row labelled with an exact switching code is that code's switch
    # prototype only when the row's own description names a switching device.
    # A fixture schedule that defines the same string as a fixture type takes
    # precedence. A switch-code row established as neither stays out of both
    # roles instead of guessing between a strip-fixture tag and a switch.
    fixture_entries_by_page: dict[int, tuple[_LightingLegendEntry, ...]] = {}
    switch_entries_by_page: dict[int, tuple[_LightingLegendEntry, ...]] = {}
    for page, entries in legend_entries_by_page.items():
        schedule_tags = schedule_tags_by_page.get(page, set())
        fixture_entries: list[_LightingLegendEntry] = []
        switch_entries: list[_LightingLegendEntry] = []
        for entry in entries:
            if entry.tag not in _LIGHTING_SWITCH_CODES or entry.tag in schedule_tags:
                fixture_entries.append(entry)
                continue
            description = _lighting_legend_row_description(
                entry,
                texts=texts,
                clusters=clusters,
            )
            claimed_text_ids.update(item.element_id for item in description)
            words = {
                word
                for item in description
                for word in _normalize_legend_alias(item.text).split()
            }
            if words & _LIGHTING_SWITCH_DESCRIPTION_WORDS:
                switch_entries.append(entry)
                continue
            unresolved.append(
                {
                    "kind": "lighting_legend_tag",
                    "page": entry.page,
                    "source_element_id": entry.label.element_id,
                    "source_text": entry.label.text,
                    "legend_code": entry.tag,
                    "description_text": [item.text for item in description],
                    "status": "unresolved_lighting_legend",
                    "reason_code": "lighting_legend_code_role_ambiguous",
                    "reason": (
                        "legend row label is a switching code, but neither a "
                        "fixture schedule row nor the row's description "
                        "establishes whether it is a fixture type or a switch"
                    ),
                }
            )
        fixture_entries_by_page[page] = tuple(fixture_entries)
        switch_entries_by_page[page] = tuple(switch_entries)

    fixture_tags_by_page: dict[int, set[str]] = {
        page: set(tags) for page, tags in schedule_tags_by_page.items()
    }
    for page, entries in fixture_entries_by_page.items():
        fixture_tags_by_page.setdefault(page, set()).update(
            entry.tag for entry in entries
        )

    field_clusters = [
        cluster
        for cluster in clusters
        if (cluster.page, cluster.geometry_key) not in prototype_keys
    ]
    assignments: dict[tuple[int, str], list[tuple[PdfTextObservation, str]]] = {}
    ambiguous_cluster_keys: set[tuple[int, str]] = set()
    diagnosed_cluster_keys: set[tuple[int, str]] = set()

    for observation in texts:
        if observation.element_id in claimed_text_ids:
            continue
        tag = _normalize_lighting_tag(observation.text)
        if tag is None or tag not in fixture_tags_by_page.get(observation.page, set()):
            continue
        nearby = [
            cluster
            for cluster in field_clusters
            if cluster.page == observation.page
            and _distance_pt(
                cluster.center_pt[0],
                cluster.center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            <= _LIGHTING_TAG_CLUSTER_RADIUS_PT
        ]
        nearby.sort(
            key=lambda cluster: (
                _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    observation.x_pt,
                    observation.y_pt,
                ),
                cluster.geometry_key,
            )
        )
        if not nearby:
            # A bare schedule-known letter elsewhere on the plan is not enough:
            # it may be a room name or other drafting label.
            continue
        if (
            len(nearby) > 1
            and _distance_pt(
                nearby[1].center_pt[0],
                nearby[1].center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            - _distance_pt(
                nearby[0].center_pt[0],
                nearby[0].center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            < _LIGHTING_TAG_ASSOCIATION_MARGIN_PT
        ):
            unresolved.append(
                {
                    "kind": "lighting_fixture",
                    "page": observation.page,
                    "source_element_id": observation.element_id,
                    "source_text": observation.text,
                    "fixture_tag": tag,
                    "status": "unresolved_classification",
                    "reason_code": "lighting_fixture_tag_association_ambiguous",
                    "reason": (
                        "readable fixture tag is not uniquely associated with one "
                        "nearby glyph"
                    ),
                    "candidate_geometry_keys": [
                        cluster.geometry_key for cluster in nearby[:4]
                    ],
                }
            )
            claimed_text_ids.add(observation.element_id)
            for cluster in nearby:
                ambiguous_cluster_keys.add((cluster.page, cluster.geometry_key))
                if _is_lighting_shaped(
                    cluster,
                    legend_entries_by_page.get(cluster.page, ()),
                ):
                    claimed_vector_ids.update(cluster.source_element_ids)
            continue
        cluster = nearby[0]
        assignments.setdefault(
            (cluster.page, cluster.geometry_key),
            [],
        ).append((observation, tag))

    for cluster in field_clusters:
        key = (cluster.page, cluster.geometry_key)
        if key in ambiguous_cluster_keys:
            continue
        claims = assignments.get(key, ())
        distinct_tags = sorted({tag for _observation, tag in claims})
        if len(distinct_tags) > 1:
            unresolved.append(
                {
                    "kind": "lighting_fixture",
                    "page": cluster.page,
                    "source_element_id": cluster.source_element_ids[0],
                    "source_element_ids": list(cluster.source_element_ids),
                    "position_pt": {
                        "x": cluster.center_pt[0],
                        "y": cluster.center_pt[1],
                    },
                    "status": "unresolved_classification",
                    "reason_code": "lighting_fixture_tag_ambiguous",
                    "reason": (
                        "fixture glyph has more than one readable nearby fixture tag"
                    ),
                    "candidate_tags": distinct_tags,
                    "tag_source_element_ids": sorted(
                        observation.element_id
                        for observation, _tag in claims
                    ),
                }
            )
            diagnosed_cluster_keys.add(key)
            if _is_lighting_shaped(
                cluster,
                legend_entries_by_page.get(cluster.page, ()),
            ):
                claimed_vector_ids.update(cluster.source_element_ids)
            claimed_text_ids.update(
                observation.element_id for observation, _tag in claims
            )
            continue
        if len(distinct_tags) != 1:
            continue

        tag = distinct_tags[0]
        tag_observation = min(
            (
                observation
                for observation, claim_tag in claims
                if claim_tag == tag
            ),
            key=lambda observation: (
                _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    observation.x_pt,
                    observation.y_pt,
                ),
                observation.element_id,
            ),
        )
        legend_entry, shape_score, shape_guard = _lighting_shape_support(
            cluster,
            fixture_entries_by_page.get(cluster.page, ()),
            tag=tag,
        )
        schedule_row = schedules.get((cluster.page, tag))

        # A schedule enriches a confirmed fixture but never proves one: a
        # schedule-known letter beside arbitrary small geometry is exactly what
        # a room name or grid label looks like. Only the tag's own lighting-
        # legend prototype, matched at the glyph match minimum, confirms that
        # the adjacent geometry is that fixture.
        if (
            legend_entry is None
            or shape_score is None
            or shape_score < _LIGHTING_GLYPH_CONFIRM_SCORE
        ):
            if legend_entry is None:
                miss: dict[str, Any] = {
                    "reason_code": "lighting_fixture_symbol_unconfirmed",
                    "reason": (
                        "readable fixture tag is known only from a fixture "
                        "schedule; no tag-specific lighting-legend prototype on "
                        "this sheet confirms the adjacent geometry is a fixture"
                    ),
                }
            else:
                miss = {
                    "reason_code": "lighting_fixture_symbol_mismatch",
                    "reason": (
                        "readable fixture tag is present but adjacent geometry does "
                        "not match that tag's lighting-legend prototype"
                    ),
                    "shape_score": shape_score,
                    "match_minimum": _LIGHTING_GLYPH_CONFIRM_SCORE,
                }
                if shape_guard is not None:
                    miss["reason"] += (
                        "; the instance and the prototype disagree on the "
                        "round-versus-polygonal shape class"
                    )
                    miss["shape_class_guard"] = shape_guard
            unresolved.append(
                {
                    "kind": "lighting_fixture",
                    "page": cluster.page,
                    "source_element_id": tag_observation.element_id,
                    "source_text": tag_observation.text,
                    "fixture_tag": tag,
                    "source_element_ids": list(cluster.source_element_ids),
                    "status": "unresolved_classification",
                    **miss,
                }
            )
            diagnosed_cluster_keys.add(key)
            if _is_lighting_shaped(
                cluster,
                legend_entries_by_page.get(cluster.page, ()),
            ):
                claimed_vector_ids.update(cluster.source_element_ids)
            claimed_text_ids.add(tag_observation.element_id)
            continue

        confidence = min(0.99, max(0.60, float(shape_score)))
        recognition: dict[str, Any] = {
            "method": "lighting-fixture-letter-tag",
            "legend_type": "lighting",
            "fixture_tag": tag,
            "tag_source_element_id": tag_observation.element_id,
            "source_geometry_key": cluster.geometry_key,
            "shape_signature": cluster.shape_signature,
            "shape_score": shape_score,
            "tag_is_primary_type_evidence": True,
            "legend": {
                "page": legend_entry.page,
                "tag_source_element_id": legend_entry.label.element_id,
                "heading_element_id": legend_entry.heading.element_id,
                "prototype_geometry_key": legend_entry.prototype.geometry_key,
            },
        }
        if schedule_row is not None:
            recognition["fixture_schedule"] = _lighting_schedule_attributes(
                schedule_row
            )

        candidate = _EntityCandidate(
            key=f"p{cluster.page}:lighting:{cluster.geometry_key}",
            entity_kind="device",
            canonical_type="luminaire",
            tag=tag,
            identity_key=(
                f"lighting:p{cluster.page}:{tag}:{cluster.geometry_key}"
            ),
            page=cluster.page,
            x_pt=cluster.center_pt[0],
            y_pt=cluster.center_pt[1],
            confidence=confidence,
            primary_method="pdf-lighting-fixture-tag",
            lighting_recognition=recognition,
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
                    method="pdf-lighting-fixture-geometry",
                    confidence=confidence,
                    source_kind=vector.source_kind,
                    attributes={
                        "fixture_tag": tag,
                        "source_geometry_key": cluster.geometry_key,
                        "shape_signature": cluster.shape_signature,
                        "shape_score": shape_score,
                    },
                ),
                method="pdf-lighting-fixture-tag",
            )
        candidate.merge_source(
            element_id=tag_observation.element_id,
            text=tag_observation.text,
            symbol_name=None,
            x_pt=cluster.center_pt[0],
            y_pt=cluster.center_pt[1],
            confidence=0.99,
            provenance=_provenance(
                document,
                element_id=tag_observation.element_id,
                page=tag_observation.page,
                method="pdf-lighting-fixture-tag",
                confidence=0.99,
                attributes={
                    "source_text": tag_observation.text,
                    "fixture_tag": tag,
                    "tag_is_primary_type_evidence": True,
                    "source_geometry_key": cluster.geometry_key,
                },
            ),
            method="pdf-lighting-fixture-tag",
        )
        candidate.provenance.append(
            _provenance(
                document,
                element_id=legend_entry.label.element_id,
                page=legend_entry.page,
                method="pdf-lighting-legend-tag",
                confidence=legend_entry.confidence,
                attributes={
                    "source_text": legend_entry.label.text,
                    "fixture_tag": tag,
                    "prototype_geometry_key": legend_entry.prototype.geometry_key,
                },
            )
        )
        if schedule_row is not None:
            schedule_attributes = _lighting_schedule_attributes(schedule_row)
            for source_element_id in schedule_row.source_element_ids:
                candidate.provenance.append(
                    _provenance(
                        document,
                        element_id=source_element_id,
                        page=schedule_row.page,
                        method="pdf-lighting-fixture-schedule",
                        confidence=0.99,
                        attributes=schedule_attributes,
                    )
                )

        candidates[candidate.key] = candidate
        matched_vector_ids.update(cluster.source_element_ids)
        claimed_vector_ids.update(cluster.source_element_ids)
        claimed_text_ids.add(tag_observation.element_id)

    # A symbol that looks like a known lighting prototype but has no readable
    # local tag is evidence of a miss, never permission to infer its type.
    # Exact switching codes are handled below and therefore are not fixture misses.
    for cluster in field_clusters:
        key = (cluster.page, cluster.geometry_key)
        if (
            key in ambiguous_cluster_keys
            or key in diagnosed_cluster_keys
            or set(cluster.source_element_ids) & claimed_vector_ids
        ):
            continue
        nearby_switch_code = any(
            observation.page == cluster.page
            and re.sub(r"\s+", "", observation.text.strip().upper())
            in _LIGHTING_SWITCH_CODES
            and re.sub(r"\s+", "", observation.text.strip().upper())
            not in fixture_tags_by_page.get(cluster.page, set())
            and _distance_pt(
                cluster.center_pt[0],
                cluster.center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            <= _LIGHTING_TAG_CLUSTER_RADIUS_PT
            for observation in texts
        )
        if nearby_switch_code:
            continue
        entry, score, _shape_guard = _lighting_shape_support(
            cluster,
            fixture_entries_by_page.get(cluster.page, ()),
        )
        if entry is None or score is None or score < _GLYPH_MATCH_STRONG_SCORE:
            continue
        nearby_short_text = sorted(
            {
                " ".join(observation.text.split())
                for observation in texts
                if observation.page == cluster.page
                and observation.element_id not in claimed_text_ids
                and _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    observation.x_pt,
                    observation.y_pt,
                )
                <= _LIGHTING_TAG_CLUSTER_RADIUS_PT
                and len(" ".join(observation.text.split())) <= 16
            }
        )
        unresolved.append(
            {
                "kind": "lighting_fixture",
                "page": cluster.page,
                "source_element_id": cluster.source_element_ids[0],
                "source_element_ids": list(cluster.source_element_ids),
                "position_pt": {
                    "x": cluster.center_pt[0],
                    "y": cluster.center_pt[1],
                },
                "status": "unresolved_classification",
                "reason_code": (
                    "lighting_fixture_tag_unreadable_or_unknown"
                    if nearby_short_text
                    else "lighting_fixture_tag_missing"
                ),
                "reason": (
                    "fixture-like geometry has no unique readable schedule/legend "
                    "tag; type is not inferred from symbol shape alone"
                ),
                "nearest_legend_tag": entry.tag,
                "shape_score": score,
                "nearby_text_candidates": nearby_short_text,
            }
        )
        claimed_vector_ids.update(cluster.source_element_ids)

    lighting_pages = set(fixture_tags_by_page)
    lighting_pages.update(
        observation.page
        for observation in texts
        if _lighting_heading_kind(observation) is not None
    )
    fixture_field_text_ids = set(claimed_text_ids)

    # Switching is semantic only when the printed code is exact, uniquely
    # attached to one small glyph on a confirmed lighting page, and that glyph
    # matches the code's own lighting-legend prototype. The code table alone
    # is not evidence: SD inside a circle is a smoke detector and S in a bubble
    # is a grid line. Without that prototype the code stays an explicit,
    # counted miss rather than a guessed switch.
    for observation in texts:
        if observation.page not in lighting_pages:
            continue
        if observation.element_id in fixture_field_text_ids:
            continue
        code = re.sub(r"\s+", "", observation.text.strip().upper())
        switch_classification = _LIGHTING_SWITCH_CODES.get(code)
        if switch_classification is None:
            continue
        if code in fixture_tags_by_page.get(observation.page, set()):
            continue
        nearby = [
            cluster
            for cluster in field_clusters
            if cluster.page == observation.page
            and not (set(cluster.source_element_ids) & claimed_vector_ids)
            and _distance_pt(
                cluster.center_pt[0],
                cluster.center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            <= _LIGHTING_TAG_CLUSTER_RADIUS_PT
        ]
        nearby.sort(
            key=lambda cluster: (
                _distance_pt(
                    cluster.center_pt[0],
                    cluster.center_pt[1],
                    observation.x_pt,
                    observation.y_pt,
                ),
                cluster.geometry_key,
            )
        )
        if not nearby:
            continue
        if (
            len(nearby) > 1
            and _distance_pt(
                nearby[1].center_pt[0],
                nearby[1].center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            - _distance_pt(
                nearby[0].center_pt[0],
                nearby[0].center_pt[1],
                observation.x_pt,
                observation.y_pt,
            )
            < _LIGHTING_TAG_ASSOCIATION_MARGIN_PT
        ):
            unresolved.append(
                {
                    "kind": "lighting_switch",
                    "page": observation.page,
                    "source_element_id": observation.element_id,
                    "source_text": observation.text,
                    "status": "unresolved_classification",
                    "reason_code": "lighting_switch_association_ambiguous",
                    "reason": (
                        "legible switching code is not uniquely associated with one "
                        "nearby glyph"
                    ),
                    "candidate_geometry_keys": [
                        cluster.geometry_key for cluster in nearby[:4]
                    ],
                    "source_element_ids": sorted(
                        {
                            element_id
                            for cluster in nearby
                            for element_id in cluster.source_element_ids
                        }
                    ),
                }
            )
            claimed_text_ids.add(observation.element_id)
            for cluster in nearby:
                if _is_lighting_shaped(
                    cluster,
                    legend_entries_by_page.get(cluster.page, ()),
                ):
                    claimed_vector_ids.update(cluster.source_element_ids)
            continue

        cluster = nearby[0]
        canonical_type, switch_type = switch_classification
        legend_entry, shape_score, _shape_guard = _lighting_shape_support(
            cluster,
            switch_entries_by_page.get(cluster.page, ()),
            tag=code,
        )
        if (
            legend_entry is None
            or shape_score is None
            or shape_score < _LIGHTING_GLYPH_CONFIRM_SCORE
        ):
            if legend_entry is None:
                miss = {
                    "reason_code": "lighting_switch_symbol_unconfirmed",
                    "reason": (
                        "legible switching code has no code-specific "
                        "lighting-legend prototype on this sheet; the code "
                        "alone does not prove a switch"
                    ),
                }
            else:
                miss = {
                    "reason_code": "lighting_switch_symbol_mismatch",
                    "reason": (
                        "legible switching code is present but adjacent geometry "
                        "does not match that code's lighting-legend prototype"
                    ),
                    "shape_score": shape_score,
                    "match_minimum": _LIGHTING_GLYPH_CONFIRM_SCORE,
                }
            unresolved.append(
                {
                    "kind": "lighting_switch",
                    "page": observation.page,
                    "source_element_id": observation.element_id,
                    "source_text": observation.text,
                    "switch_code": code,
                    "candidate_switch_type": switch_type,
                    "source_element_ids": list(cluster.source_element_ids),
                    "status": "unresolved_classification",
                    **miss,
                }
            )
            claimed_text_ids.add(observation.element_id)
            if _is_lighting_shaped(
                cluster,
                legend_entries_by_page.get(cluster.page, ()),
            ):
                claimed_vector_ids.update(cluster.source_element_ids)
            continue

        confidence = min(0.96, max(0.60, float(shape_score)))
        recognition = {
            "method": "lighting-switch-code",
            "switch_code": code,
            "switch_type": switch_type,
            "code_source_element_id": observation.element_id,
            "source_geometry_key": cluster.geometry_key,
            "shape_signature": cluster.shape_signature,
            "shape_score": shape_score,
            "legend": {
                "page": legend_entry.page,
                "code_source_element_id": legend_entry.label.element_id,
                "heading_element_id": legend_entry.heading.element_id,
                "prototype_geometry_key": legend_entry.prototype.geometry_key,
            },
        }
        candidate = _EntityCandidate(
            key=f"p{cluster.page}:lighting-switch:{cluster.geometry_key}",
            entity_kind="device",
            canonical_type=canonical_type,
            tag=code,
            identity_key=(
                f"lighting-switch:p{cluster.page}:{code}:{cluster.geometry_key}"
            ),
            page=cluster.page,
            x_pt=cluster.center_pt[0],
            y_pt=cluster.center_pt[1],
            confidence=confidence,
            primary_method="pdf-lighting-switch-code",
            lighting_recognition=recognition,
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
                    method="pdf-lighting-switch-geometry",
                    confidence=confidence,
                    source_kind=vector.source_kind,
                    attributes=recognition,
                ),
                method="pdf-lighting-switch-code",
            )
        candidate.merge_source(
            element_id=observation.element_id,
            text=observation.text,
            symbol_name=None,
            x_pt=cluster.center_pt[0],
            y_pt=cluster.center_pt[1],
            confidence=confidence,
            provenance=_provenance(
                document,
                element_id=observation.element_id,
                page=observation.page,
                method="pdf-lighting-switch-code",
                confidence=confidence,
                attributes={
                    "source_text": observation.text,
                    **recognition,
                },
            ),
            method="pdf-lighting-switch-code",
        )
        candidate.provenance.append(
            _provenance(
                document,
                element_id=legend_entry.label.element_id,
                page=legend_entry.page,
                method="pdf-lighting-legend-switch-code",
                confidence=legend_entry.confidence,
                attributes={
                    "source_text": legend_entry.label.text,
                    "switch_code": code,
                    "prototype_geometry_key": legend_entry.prototype.geometry_key,
                },
            )
        )
        candidates[candidate.key] = candidate
        matched_vector_ids.update(cluster.source_element_ids)
        claimed_vector_ids.update(cluster.source_element_ids)
        claimed_text_ids.add(observation.element_id)

    # Misses are counted beside recognitions so any recall a stricter rule
    # costs stays visible in the output instead of silently disappearing.
    unresolved_by_reason: dict[str, int] = {}
    for item in unresolved:
        reason_code = str(item.get("reason_code"))
        unresolved_by_reason[reason_code] = unresolved_by_reason.get(reason_code, 0) + 1
    recognition_summary = {
        "legend_type": "lighting",
        "legend_regions": legend_metadata,
        "fixture_schedules": schedule_metadata,
        "recognized_fixture_count": sum(
            candidate.canonical_type == "luminaire"
            for candidate in candidates.values()
        ),
        "recognized_switch_count": sum(
            candidate.canonical_type in {"switch", "occupancy_sensor"}
            for candidate in candidates.values()
        ),
        "unresolved_fixture_count": sum(
            item.get("kind") == "lighting_fixture" for item in unresolved
        ),
        "unresolved_switch_count": sum(
            item.get("kind") == "lighting_switch" for item in unresolved
        ),
        "unresolved_by_reason": dict(sorted(unresolved_by_reason.items())),
        "fixture_tags": sorted(
            {
                candidate.tag
                for candidate in candidates.values()
                if candidate.canonical_type == "luminaire"
                and candidate.tag is not None
            }
        ),
    }
    return (
        candidates,
        matched_vector_ids,
        claimed_vector_ids,
        claimed_text_ids,
        unresolved,
        recognition_summary,
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
    dict[int, tuple[float, float, float, float]],
]:
    clusters = tuple(
        cluster
        for cluster in _cluster_small_vector_glyphs(
            _glyph_cluster_vectors(document, vectors)
        )
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

    # Glyphs drawn inside a legend frame or legend table are legend samples,
    # never installed devices. A glyph straddling a frame rule is treated as
    # inside too: only its center inside would let a sample drawn over the
    # bottom rule leak out as an installed device.
    legend_boxes = {
        region.page: region.table_bbox_pt
        for region in regions
        if region.table_bbox_pt is not None
    }

    def cluster_inside_legend_frame(
        cluster: _VectorCluster,
    ) -> bool:
        box = legend_boxes.get(cluster.page)
        if box is None:
            return False
        x0, y0, x1, y1 = box
        cx0, cy0, cx1, cy1 = cluster.bbox_pt
        return cx0 <= x1 and cx1 >= x0 and cy0 <= y1 and cy1 >= y0

    inframe_excluded_counts: dict[int, int] = {}
    for cluster in clusters:
        if cluster_inside_legend_frame(cluster):
            inframe_excluded_counts[cluster.page] = (
                inframe_excluded_counts.get(cluster.page, 0) + 1
            )

    # Text inside a legend frame that no legend row claimed (a stray note, a
    # leftover annotation) must not claim a device identity through the text
    # rules either. Keep it as unresolved evidence instead.
    for text in texts:
        if text.element_id in legend_text_ids or text.page not in legend_boxes:
            continue
        box = legend_boxes[text.page]
        if not (
            box[0] <= text.x_pt <= box[2] and box[1] <= text.y_pt <= box[3]
        ):
            continue
        if _field_modifier_text(" ".join(text.text.split())) is not None:
            continue
        if not _text_entity_hits(text.text):
            continue
        legend_text_ids.add(text.element_id)
        unresolved.append(
            {
                "kind": "text",
                "page": text.page,
                "source_element_id": text.element_id,
                "text": text.text,
                "position_pt": {"x": text.x_pt, "y": text.y_pt},
                "status": "unresolved_identity",
                "reason": (
                    "text inside a legend frame did not join a legend row and "
                    "cannot claim a device identity"
                ),
            }
        )

    field_clusters = _prepare_field_clusters(
        tuple(
            cluster
            for cluster in clusters
            if not cluster_inside_legend_frame(cluster)
        ),
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
                **(
                    {
                        "legend_frame": {
                            **dict(region.legend_frame),
                            "inframe_glyph_clusters_excluded": (
                                inframe_excluded_counts.get(region.page, 0)
                            ),
                        }
                    }
                    if region.legend_frame is not None
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
        legend_boxes,
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
        scope_legends = _scope_status_legends(texts)
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
            lighting_candidates,
            lighting_matched_vector_ids,
            lighting_claimed_vector_ids,
            lighting_claimed_text_ids,
            unresolved_lighting_rows,
            lighting_recognition,
        ) = _recognize_lighting(
            document,
            texts=legend_texts,
            vectors=vectors,
        )
        generic_legend_texts = tuple(
            observation
            for observation in legend_texts
            if observation.element_id not in lighting_claimed_text_ids
        )
        generic_vectors = tuple(
            vector
            for vector in vectors
            if vector.element_id not in lighting_claimed_vector_ids
        )
        (
            shape_candidates,
            shape_matched_vector_ids,
            glyph_vector_ids,
            legend_text_ids,
            unresolved_shape_rows,
            legend_recognition,
            annotation_legend_entries,
            legend_frame_boxes,
        ) = _recognize_legend_shapes(
            document,
            texts=generic_legend_texts,
            vectors=generic_vectors,
            rules=self.symbol_rules,
            ambiguity_margin=self.ambiguity_margin,
        )
        shape_matched_vector_ids.update(lighting_matched_vector_ids)
        glyph_vector_ids.update(lighting_claimed_vector_ids)
        legend_text_ids.update(lighting_claimed_text_ids)
        candidates: dict[str, _EntityCandidate] = {
            **shape_candidates,
            **lighting_candidates,
        }
        unresolved_observations: list[dict[str, Any]] = list(
            unresolved_lighting_rows
        )
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
                    derivation=DERIVATION_USER,
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

        inframe_symbols_skipped: dict[int, int] = {}
        for symbol in symbols:
            if (symbol.page, symbol.element_id) in claimed_source_ids:
                continue

            # A legend glyph often carries a letter code annotation of its own
            # (the dimmer "D", a "$" drawn as "S"). Codes are only read on the
            # field side; inside the legend frame the symbol is a sample.
            frame_box = legend_frame_boxes.get(symbol.page)
            if frame_box is not None and (
                frame_box[0] <= symbol.x_pt <= frame_box[2]
                and frame_box[1] <= symbol.y_pt <= frame_box[3]
            ):
                inframe_symbols_skipped[symbol.page] = (
                    inframe_symbols_skipped.get(symbol.page, 0) + 1
                )
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
            if candidate.lighting_recognition is not None:
                lane_attributes["lighting_recognition"] = dict(
                    candidate.lighting_recognition
                )
                if "fixture_tag" in candidate.lighting_recognition:
                    lane_attributes["fixture_tag"] = candidate.lighting_recognition[
                        "fixture_tag"
                    ]
                if "fixture_schedule" in candidate.lighting_recognition:
                    lane_attributes["fixture_schedule"] = dict(
                        candidate.lighting_recognition["fixture_schedule"]
                    )
                if "switch_type" in candidate.lighting_recognition:
                    lane_attributes["switch_type"] = candidate.lighting_recognition[
                        "switch_type"
                    ]
                    lane_attributes["switch_code"] = candidate.lighting_recognition[
                        "switch_code"
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
            lane_attributes.update(
                _scope_status_attributes(
                    lane_attributes.get("status"),
                    (
                        (candidate.shape_recognition or {}).get("status_source_element_id")
                        or (candidate.annotation_recognition or {}).get("status_source_element_id")
                    ),
                    candidate.page,
                    scope_legends,
                )
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
            # Every record on a synthesized port describes evidence for a port
            # we invented so a circuit has an endpoint. The evidence is real;
            # the port is not something a sheet draws. Normalize here so the
            # merge path below cannot quietly re-add an observed record.
            provenance = replace(provenance, derivation=DERIVATION_INFERRED)
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
                # The evidence behind this port is real, but the port itself is
                # synthesized so a circuit has an endpoint -- no sheet draws it.
                # Carrying the evidence record unchanged would let a consumer
                # read this port as observed, which it is not.
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
            # These records are the evidence FOR a port we synthesized, not
            # evidence that a sheet drew the port. The ports are later rebuilt
            # from exactly this collection, so marking here is what actually
            # reaches the model.
            provenance_items = tuple(
                replace(record, derivation=DERIVATION_INFERRED)
                for record in provenances
            )
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
        schedule_circuits, schedule_text_ids, confirmed_schedules = _panel_schedule_circuits(
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
                    "schedule_validated": panel_tag in confirmed_schedules,
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
                        "schedule_validated": panel_tag in confirmed_schedules,
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
                        confirmed_schedules=confirmed_schedules,
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
                        "reason": (
                            "schedule found, structure not confirmed"
                            if reason_code == "schedule_structure_not_confirmed"
                            else "homerun annotation failed closed"
                        ),
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
                confirmed_schedules=confirmed_schedules,
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
                        "reason": (
                            "schedule found, structure not confirmed"
                            if reason_code == "schedule_structure_not_confirmed"
                            else "direct circuit tag failed closed"
                        ),
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
                derivation=DERIVATION_OBSERVED,
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
                    derivation=DERIVATION_OBSERVED,
                    page=page,
                    method="pypdf-page-display-normalization",
                    confidence=1.0,
                    attributes=dict(document.page_provenance[page]),
                )
            )
        # Symbols inside a legend frame are legend samples; say how many were
        # kept out of the field so the skip stays visible in provenance.
        for region_record in legend_recognition.get("regions", ()):
            if "legend_frame" in region_record:
                region_record["legend_frame"]["inframe_symbols_skipped"] = (
                    inframe_symbols_skipped.get(int(region_record["page"]), 0)
                )
        for note in legend_recognition.get("frame_rederivations", ()):
            model_provenance.append(
                Provenance(
                    source_kind="pdf-electrical",
                    source_id=document.source_id,
                    derivation=DERIVATION_OBSERVED,
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
        if has_explicit_registration:
            for page in range(1, document.page_count + 1):
                registration = transforms[page].registration
                if registration is None:
                    continue
                # The page's positions are observed; the transform that places
                # them in the building frame is a proposal from matched evidence.
                model_provenance.append(
                    Provenance(
                        source_kind="pdf-electrical",
                        source_id=document.source_id,
                        page=page,
                        method=str(registration.get("method", "sheet registration")),
                        confidence=float(registration.get("confidence", 0.5)),
                        derivation=DERIVATION_INFERRED,
                        attributes={
                            "registration_status": "registered-from-matched-evidence",
                            "page_transform": transforms[page].to_attributes(),
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
                        # We chose this placement because the document states
                        # no registration. It is our proposal, not something
                        # the sheet shows.
                        derivation=DERIVATION_INFERRED,
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
                    "lighting_recognition": lighting_recognition,
                    "panel_schedules": {
                        panel: {
                            "status": (
                                "validated" if panel in confirmed_schedules
                                else "structure_not_confirmed"
                            ),
                            "circuits": sorted(schedule_circuits[panel]),
                            **(
                                {}
                                if panel in confirmed_schedules
                                else {"reason": "schedule found, structure not confirmed"}
                            ),
                        }
                        for panel in sorted(schedule_circuits)
                    },
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
