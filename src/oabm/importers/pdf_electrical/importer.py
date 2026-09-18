from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
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
class PdfElectricalDocument:
    source_id: str
    page_count: int
    texts: tuple[PdfTextObservation, ...] = ()
    symbols: tuple[PdfSymbolObservation, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ElectricalPdfError("source_id is required")
        if self.page_count < 1:
            raise ElectricalPdfError("page_count must be >= 1")
        for item in (*self.texts, *self.symbols):
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
        return cls(
            source_id=str(data["source_id"]),
            page_count=int(data["page_count"]),
            texts=texts,
            symbols=symbols,
        )


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
    page: int
    x_pt: float
    y_pt: float
    confidence: float
    primary_method: str
    source_element_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    symbol_names: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)

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
            self.x_pt = x_pt
            self.y_pt = y_pt
            self.primary_method = method


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


def _make_page_visitors(
    *,
    page_number: int,
    form_names: frozenset[str],
    texts: list[PdfTextObservation],
    symbols: list[PdfSymbolObservation],
):
    text_counter = 0
    operator_counter = 0

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

    def visitor_operand_before(
        operator: bytes,
        operands: Sequence[Any],
        cm: Sequence[float],
        tm: Sequence[float],
    ) -> None:
        nonlocal operator_counter
        operator_counter += 1
        if operator != b"Do" or not operands:
            return
        name = str(operands[0])
        if form_names and name not in form_names:
            return
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
        )

        try:
            page.extract_text(
                visitor_text=visitor_text,
                visitor_operand_before=visitor_operand_before,
            )
        except Exception as exc:
            raise ElectricalPdfError(
                f"failed to extract page {page_number}: {exc}"
            ) from exc

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
_DEVICE_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?P<tag>EVSE(?:[-_.]?[A-Z0-9]+)?)\b", re.IGNORECASE), "evse"),
    (
        re.compile(r"\b(?P<tag>(?:GFCI|GFI|RECEPT|REC)(?:[-_.]?[A-Z0-9]+)?)\b", re.IGNORECASE),
        "receptacle",
    ),
    (
        re.compile(r"\b(?P<tag>(?:JBOX|J-?BOX|JB)(?:[-_.]?[A-Z0-9]+)?)\b", re.IGNORECASE),
        "junction_box",
    ),
    (
        re.compile(r"\b(?P<tag>(?:LIGHT|LTG|LUM)(?:[-_.]?[A-Z0-9]+)?)\b", re.IGNORECASE),
        "luminaire",
    ),
    (
        re.compile(r"\b(?P<tag>(?:DISC|DISCONNECT)(?:[-_.]?[A-Z0-9]+)?)\b", re.IGNORECASE),
        "disconnect",
    ),
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


def _classify_symbol(
    symbol: PdfSymbolObservation,
    rules: Sequence[SymbolRule],
    *,
    ambiguity_margin: float,
) -> tuple[tuple[str, str, float] | None, list[dict[str, Any]]]:
    semantic = _semantic_text(symbol)
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
        symbol_label_radius_pt: float = 96.0,
        annotation_radius_pt: float = 144.0,
        ambiguity_margin: float = 0.08,
    ) -> None:
        self.symbol_rules = tuple(symbol_rules)
        self.symbol_label_radius_pt = float(symbol_label_radius_pt)
        self.annotation_radius_pt = float(annotation_radius_pt)
        self.ambiguity_margin = float(ambiguity_margin)

    def import_pdf(
        self,
        path: str | Path,
        *,
        source_id: str | None = None,
    ) -> BuildingModel:
        return self.import_document(extract_pdf(path, source_id=source_id))

    def import_document(self, document: PdfElectricalDocument) -> BuildingModel:
        texts = tuple(sorted(document.texts, key=lambda item: (item.page, item.element_id)))
        symbols = tuple(sorted(document.symbols, key=lambda item: (item.page, item.element_id)))
        candidates: dict[str, _EntityCandidate] = {}
        unresolved_observations: list[dict[str, Any]] = []

        # Text recognition comes first so recognized symbols can bind to nearby labels.
        for observation in texts:
            for kind, canonical_type, tag, confidence in _text_entity_hits(observation.text):
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
                key = f"p{symbol.page}:{kind}:symbol:{symbol.element_id}"
                candidate = _EntityCandidate(
                    key=key,
                    entity_kind=kind,
                    canonical_type=canonical_type,
                    tag=None,
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

            # A graphical symbol centroid is a better source-page position than label text.
            candidate.x_pt = symbol.x_pt
            candidate.y_pt = symbol.y_pt

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

        for candidate in sorted(candidates.values(), key=lambda item: item.key):
            source_key = (
                candidate.source_element_ids[0]
                if candidate.source_element_ids
                else candidate.key
            )
            id_kind = "equipment" if candidate.entity_kind == "equipment" else "device"
            entity_id = stable_id(
                id_kind,
                f"pdf-electrical:{document.source_id}:{source_key}",
            )
            all_texts = tuple(dict.fromkeys(candidate.texts))
            mounting = _mounting_heights_m(all_texts)
            hosts = _host_hints(all_texts)
            rated_voltage_v, system = _extract_voltage(all_texts)

            lane_attributes: dict[str, Any] = {
                "spatial_status": "source-page-local-unregistered",
                "source_page": candidate.page,
                "source_position_pt": {"x": candidate.x_pt, "y": candidate.y_pt},
                "source_element_ids": sorted(set(candidate.source_element_ids)),
                "pose_interpretation": "source-page position in metres; z=0 is the PDF page plane",
            }
            if candidate.tag:
                lane_attributes["tag"] = candidate.tag
            if candidate.symbol_names:
                lane_attributes["symbol_names"] = sorted(set(candidate.symbol_names))
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
                "pose": Pose(
                    position=Point3(
                        x=candidate.x_pt * POINT_TO_M,
                        y=candidate.y_pt * POINT_TO_M,
                        z=0.0,
                    )
                ),
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

            if candidate.tag:
                entity_by_page_tag[(candidate.page, candidate.tag)] = entity

        ports_by_owner_role: dict[tuple[str, str], Port] = {}
        circuits: list[Circuit] = []
        unresolved_circuits: list[dict[str, Any]] = []

        def port_for(
            entity: ElectricalEquipment | ElectricalDevice,
            role: str,
            provenance: Provenance,
        ) -> Port:
            key = (entity.id, role)
            existing = ports_by_owner_role.get(key)
            if existing is not None:
                return existing
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
                        "spatial_status": "source-page-local-unregistered",
                    }
                },
            )
            ports_by_owner_role[key] = port
            return port

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
                if re.search(rf"(?<![A-Z0-9_.-]){re.escape(tag)}(?![A-Z0-9_.-])", observation.text, re.IGNORECASE):
                    load_entities.append(entity)

            evidence = {
                "page": observation.page,
                "source_element_id": observation.element_id,
                "source_text": observation.text,
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
            circuit = Circuit(
                id=stable_id(
                    "circuit",
                    f"pdf-electrical:{document.source_id}:p{observation.page}:{panel_tag}:{circuit_number}",
                ),
                name=f"{panel_tag} {circuit_number}",
                source_port_id=source_port.id,
                load_port_ids=tuple(port.id for port in load_ports),
                circuit_number=circuit_number,
                voltage_v=voltage_v,
                poles=int(poles_match.group("poles")) if poles_match else None,
                phase=(f"{phase_match.group('phase')}ph" if phase_match else None),
                confidence=circuit_confidence,
                provenance=(source_provenance,),
                attributes={
                    "pdf_electrical": {
                        "inference_basis": "same text observation explicitly names panel, circuit, and load tag",
                        "source_text": observation.text,
                        "spatial_attachment_pending": True,
                    }
                },
            )
            circuits.append(circuit)

        # Duplicate circuit text is common in extracted PDFs. Keep one deterministic semantic circuit.
        unique_circuits: dict[str, Circuit] = {}
        for circuit in sorted(circuits, key=lambda item: item.id):
            unique_circuits.setdefault(circuit.id, circuit)

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

        model_provenance = Provenance(
            source_kind="pdf-electrical",
            source_id=document.source_id,
            method="pypdf-text-xobject-annotation extraction",
            confidence=1.0,
            attributes={"page_count": document.page_count},
        )

        return BuildingModel(
            model_id=stable_id("model", f"pdf-electrical:{document.source_id}"),
            name=f"Electrical PDF recognition: {document.source_id}",
            coordinate_system=CoordinateSystem(
                frame_id=stable_id("frame", f"pdf-electrical:{document.source_id}")
            ),
            electrical_equipment=tuple(sorted(equipment, key=lambda item: item.id)),
            electrical_devices=tuple(sorted(devices, key=lambda item: item.id)),
            ports=tuple(sorted(ports_by_owner_role.values(), key=lambda item: item.id)),
            circuits=tuple(unique_circuits.values()),
            provenance=(model_provenance,),
            attributes={
                "pdf_electrical": {
                    "lane": "pdf_electrical",
                    "page_count": document.page_count,
                    "source_length_unit": "pt",
                    "canonicalized_length_unit": "m",
                    "spatial_status": "source-page-local-unregistered",
                    "registration_pending": True,
                    "pages_share_no_asserted_building_registration": document.page_count > 1,
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


def import_pdf(path: str | Path, *, source_id: str | None = None) -> BuildingModel:
    return ElectricalPdfImporter().import_pdf(path, source_id=source_id)


def import_document(document: PdfElectricalDocument) -> BuildingModel:
    return ElectricalPdfImporter().import_document(document)


__all__ = [
    "DEFAULT_SYMBOL_RULES",
    "POINT_TO_M",
    "ElectricalPdfError",
    "ElectricalPdfImporter",
    "PdfElectricalDocument",
    "PdfSymbolObservation",
    "PdfTextObservation",
    "SymbolRule",
    "extract_pdf",
    "import_document",
    "import_pdf",
]
