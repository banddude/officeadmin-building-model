from __future__ import annotations

import hashlib
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
        return cls(
            source_id=str(data["source_id"]),
            page_count=int(data["page_count"]),
            texts=texts,
            symbols=symbols,
            vectors=vectors,
        )


@dataclass(frozen=True, slots=True)
class PdfPageTransform:
    """Affine registration from one PDF page's point coordinates into one canonical frame."""

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
    SymbolRule(r"\b(?:GFCI|GFI|RECEPTACLE|RECEPT|DUPLEX|REC)[A-Z0-9]*\b", "device", "receptacle", 0.94),
    SymbolRule(r"\b(?:JBOX|J-?BOX|JB)[A-Z0-9]*\b", "device", "junction_box", 0.94),
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


@dataclass(frozen=True, slots=True)
class _LegendRegion:
    page: int
    method: str
    rows: tuple[_LegendRow, ...]
    heading: PdfTextObservation | None
    confidence: float


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
        x_pt, y_pt = _transform_text_point(cm, tm)
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
                        x_pt=float(cm[4]),
                        y_pt=float(cm[5]),
                        source_kind="form-xobject",
                    )
                )
            return

        if operator == b"m" and len(operands) >= 2:
            finish_current()
            current_points = [
                _transform_graphics_point(cm, float(operands[0]), float(operands[1]))
            ]
            return

        if operator == b"l" and len(operands) >= 2:
            if current_points:
                current_points.append(
                    _transform_graphics_point(
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
                        _transform_graphics_point(cm, x, y),
                        _transform_graphics_point(cm, x + width, y),
                        _transform_graphics_point(cm, x + width, y + height),
                        _transform_graphics_point(cm, x, y + height),
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
                control_1 = _transform_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                control_2 = _transform_graphics_point(
                    cm, float(operands[2]), float(operands[3])
                )
                end = _transform_graphics_point(
                    cm, float(operands[4]), float(operands[5])
                )
            elif operator == b"v":
                if len(operands) < 4:
                    current_supported = False
                    return
                control_1 = current_points[-1]
                control_2 = _transform_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                end = _transform_graphics_point(
                    cm, float(operands[2]), float(operands[3])
                )
            else:
                if len(operands) < 4:
                    current_supported = False
                    return
                control_1 = _transform_graphics_point(
                    cm, float(operands[0]), float(operands[1])
                )
                end = _transform_graphics_point(
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
    """Extract deterministic text and form-XObject observations from a PDF.

    This is recognition input only. Coordinates remain in the source page frame
    until the later architecture/electrical convergence step.
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

    for page_number, page in enumerate(reader.pages, start=1):
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
            x_pt = (float(rect[0]) + float(rect[2])) / 2.0
            y_pt = (float(rect[1]) + float(rect[3])) / 2.0
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
_LEGEND_MAX_LABEL_CHARS = 64
_LEGEND_MAX_LABEL_WORDS = 10
_UNREGISTERED_PAGE_TILE_OFFSET_M = 100.0
_LEGEND_TITLE_WORDS = frozenset({"LEGEND", "SYMBOL", "SYMBOLS"})
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

    for rotation in range(4):
        records: list[tuple[Any, ...]] = []
        for vector in vectors:
            normalized: list[tuple[float, float]] = []
            for x_pt, y_pt in vector.points_pt:
                x = (x_pt - center_x) / scale
                y = (y_pt - center_y) / scale
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
        clusters.append(
            _VectorCluster(
                page=vectors_in_group[0].page,
                vectors=vectors_in_group,
                bbox_pt=bbox,
                center_pt=((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
                shape_signature=_cluster_shape_signature(vectors_in_group, bbox),
                geometry_key=_cluster_geometry_key(vectors_in_group),
            )
        )

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


def _is_legend_heading(observation: PdfTextObservation) -> bool:
    text = " ".join(observation.text.split())
    if _LEGEND_REFERENCE_PREFIX_RE.search(text):
        return False
    normalized = _normalize_legend_alias(text)
    words = normalized.split()
    if not words or len(words) > 8:
        return False
    return words[-1] in _LEGEND_TITLE_WORDS


def _is_short_legend_label(observation: PdfTextObservation) -> bool:
    text = " ".join(observation.text.split())
    if not text or len(text) > _LEGEND_MAX_LABEL_CHARS:
        return False
    if len(text.split()) > _LEGEND_MAX_LABEL_WORDS:
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


def _detect_legend_regions(
    *,
    headings: Sequence[PdfTextObservation],
    rows: Sequence[_LegendRow],
    clusters: Sequence[_VectorCluster],
    vectors: Sequence[PdfVectorPathObservation],
) -> tuple[_LegendRegion, ...]:
    regions_by_page: dict[int, _LegendRegion] = {}
    rows_by_page: dict[int, list[_LegendRow]] = {}
    for row in rows:
        rows_by_page.setdefault(row.cluster.page, []).append(row)

    for page in sorted({heading.page for heading in headings}):
        page_headings = [heading for heading in headings if heading.page == page]
        page_groups = _aligned_legend_row_groups(
            rows_by_page.get(page, ()),
            require_adjacent_rows=False,
        )
        candidates: list[_LegendRegion] = []
        for heading in page_headings:
            groups = [
                group
                for group in page_groups
                if min(
                    _distance_pt(
                        heading.x_pt,
                        heading.y_pt,
                        row.label.x_pt,
                        row.label.y_pt,
                    )
                    for row in group
                )
                <= _LEGEND_TITLE_REGION_RADIUS_PT
                or _leader_connects_heading_to_rows(heading, group, vectors)
            ]
            if not groups:
                continue
            group = max(
                groups,
                key=lambda candidate: (
                    len(candidate),
                    sum(row.classification is not None for row in candidate),
                    _legend_group_density(candidate),
                    -min(row.cluster.center_pt[0] for row in candidate),
                ),
            )
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
        if len(group) < _LEGEND_TABLE_MIN_ROWS:
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
    headings = tuple(
        observation
        for observation in texts
        if _is_legend_heading(observation)
    )
    rows = _legend_row_candidates(
        clusters,
        texts,
        rules,
        ambiguity_margin=ambiguity_margin,
    )
    regions = _detect_legend_regions(
        headings=headings,
        rows=rows,
        clusters=clusters,
        vectors=vectors,
    )
    legend_text_ids = {observation.element_id for observation in headings}
    prototype_geometry_keys: set[tuple[int, str]] = set()
    entries_by_signature: dict[tuple[int, str], list[_LegendEntry]] = {}
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
            entries_by_signature.setdefault(
                (prototype.page, prototype.shape_signature), []
            ).append(
                _LegendEntry(
                    entity_kind=entity_kind,
                    canonical_type=canonical_type,
                    confidence=confidence,
                    prototype=prototype,
                    label=label,
                    region=region,
                )
            )

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
            }
            for region in regions
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

    for cluster in clusters:
        if (cluster.page, cluster.geometry_key) in prototype_geometry_keys:
            continue

        legend_entry = legend_by_signature.get(
            (cluster.page, cluster.shape_signature)
        )
        reference: _LegendReference | None = None
        if legend_entry is None:
            remote_matches: list[tuple[_LegendReference, _LegendEntry]] = []
            for candidate_reference in references_by_page.get(cluster.page, ()):
                remote_entry = legend_by_signature.get(
                    (
                        candidate_reference.legend_page,
                        cluster.shape_signature,
                    )
                )
                if remote_entry is not None:
                    remote_matches.append((candidate_reference, remote_entry))
            if remote_matches:
                remote_classifications = {
                    (entry.entity_kind, entry.canonical_type)
                    for _reference, entry in remote_matches
                }
                if len(remote_classifications) != 1:
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
                                        for item, _entry in remote_matches
                                    }
                                ),
                            },
                        }
                    )
                    continue
                reference, legend_entry = sorted(
                    remote_matches,
                    key=lambda item: (
                        -item[1].confidence,
                        item[0].legend_page,
                        item[0].source_element_id,
                        item[1].label.element_id,
                    ),
                )[0]

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
        confidence = min(
            0.88 if reference is not None else 0.93,
            legend_entry.confidence,
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
            "legend_source_element_ids": list(prototype.source_element_ids),
            "source_element_ids": list(cluster.source_element_ids),
        }
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
                        "legend_detection_method": region.method,
                        "legend_source_element_ids": list(
                            prototype.source_element_ids
                        ),
                    },
                ),
                method="pdf-legend-shape-match",
            )
        if label.element_id not in candidate.source_element_ids:
            candidate.source_element_ids.append(label.element_id)
        label_attributes: dict[str, Any] = {
            "source_text": label.text,
            "shape_signature": cluster.shape_signature,
            "legend_scope": legend_scope,
            "legend_detection_method": region.method,
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
        (
            shape_candidates,
            shape_matched_vector_ids,
            glyph_vector_ids,
            legend_text_ids,
            unresolved_shape_rows,
            legend_recognition,
        ) = _recognize_legend_shapes(
            document,
            texts=texts,
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
            if observation.element_id in legend_text_ids:
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
