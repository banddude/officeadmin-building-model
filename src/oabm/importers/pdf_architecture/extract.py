"""Vector/text extraction from PDF pages.

pdfplumber is intentionally used only to turn PDF primitives into deterministic
source observations.  Semantic interpretation remains in ``importer.py``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from statistics import median
from typing import Iterable

import pdfplumber
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdftypes import PDFObjRef, resolve1
from pdfplumber.page import PDFPageAggregatorWithMarkedContent, Page
from pypdf import PdfReader

from oabm.importers.pdf_display import (
    SHX_TEXT_ANNOTATION_AUTHOR,
    page_display_transform,
)

from .types import (
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
)


def _token(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value)).strip("-")[:80]


def _element_id(kind: str, page_number: int, signature: str) -> str:
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
    return f"p{page_number}:{kind}:{digest}"


def _is_wall_source_layer(layer: str) -> bool:
    return layer.rsplit("|", 1)[-1].upper().lstrip("_") in {"A-WALL", "AE-WALL"}


def _native_id(obj: dict[str, object], kind: str) -> str | None:
    mcid = obj.get("mcid")
    if isinstance(mcid, int):
        tag = _token(obj.get("tag") or kind)
        return f"mcid:{mcid}:{tag}"
    return None


def _group_words(page: object, page_number: int) -> tuple[PdfTextObservation, ...]:
    words = page.extract_words(  # type: ignore[attr-defined]
        use_text_flow=False,
        keep_blank_chars=False,
        extra_attrs=["size"],
    )
    rows: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        top = float(word["top"])
        if not rows or abs(top - float(rows[-1][0]["top"])) > 2.5:
            rows.append([word])
        else:
            rows[-1].append(word)

    result: list[PdfTextObservation] = []
    page_height = float(page.height)  # type: ignore[attr-defined]
    for row in rows:
        row.sort(key=lambda item: float(item["x0"]))
        groups: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        for word in row:
            if current:
                previous = current[-1]
                gap = float(word["x0"]) - float(previous["x1"])
                previous_height = float(previous["bottom"]) - float(previous["top"])
                word_height = float(word["bottom"]) - float(word["top"])
                # PDF plans often place unrelated room labels, dimensions, and
                # keynotes on the same text baseline.  Keep normal word spacing
                # together, but do not merge widely separated annotations into
                # one synthetic source observation.
                split_gap = max(6.0, 1.5 * max(previous_height, word_height))
                if gap > split_gap:
                    groups.append(current)
                    current = []
            current.append(word)
        if current:
            groups.append(current)

        for group in groups:
            text = " ".join(str(item["text"]) for item in group).strip()
            if not text:
                continue
            x0 = min(float(item["x0"]) for item in group)
            x1 = max(float(item["x1"]) for item in group)
            top = min(float(item["top"]) for item in group)
            bottom = max(float(item["bottom"]) for item in group)
            y0 = page_height - bottom
            y1 = page_height - top
            signature = f"{text}|{x0:.3f}|{y0:.3f}|{x1:.3f}|{y1:.3f}"
            font_sizes: list[float] = []
            for item in group:
                value = item.get("size")
                try:
                    size = float(value)
                except (TypeError, ValueError):
                    continue
                if size > 0:
                    font_sizes.append(size)
            result.append(
                PdfTextObservation(
                    element_id=_element_id("text", page_number, signature),
                    text=text,
                    bbox_pt=(x0, y0, x1, y1),
                    font_size_pt=median(font_sizes) if font_sizes else None,
                )
            )
    return tuple(result)


def _annotation_string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


def _shx_annotation_texts(
    reader_page: object,
    page_number: int,
    document: object,
) -> tuple[PdfTextObservation, ...]:
    """Read AutoCAD SHX text annotations as displayed-space text observations.

    AutoCAD draws SHX-font strings as vector strokes, and its PDF export adds
    one invisible /Square annotation per string so the text stays selectable.
    For SHX sheets those annotations are the only text a drawing title, room
    name, or note ever gets, so they are read as first-class text observations
    in displayed, bottom-origin page space.

    Only annotations authored by CAD SHX text are accepted - other authors,
    other subtypes, hidden annotations, and annotations whose optional-content
    group defaults to OFF are ignored, because reviewer markups are not
    drawing text. The author string itself is never copied into an
    observation.
    """
    display_transform = page_display_transform(reader_page)
    annotations = reader_page.get("/Annots") or ()
    result: list[PdfTextObservation] = []
    for annotation_index, annotation_ref in enumerate(annotations, start=1):
        try:
            annotation = annotation_ref.get_object()
        except AttributeError:
            annotation = annotation_ref
        if _annotation_string(annotation.get("/T")) != SHX_TEXT_ANNOTATION_AUTHOR:
            continue
        if _annotation_string(annotation.get("/Subtype")) != "/Square":
            continue
        contents = _annotation_string(annotation.get("/Contents")).strip()
        if not contents:
            continue
        try:
            if int(annotation.get("/F", 0)) & 2:  # flag bit 2: Hidden
                continue
        except (TypeError, ValueError):
            continue
        group = annotation.get("/OC")
        if group is not None and not _optional_group_state(document, group):
            continue
        rect = annotation.get("/Rect")
        if rect is None or len(rect) < 4:
            continue
        # /Rect corners may be reversed; normalize into displayed space.
        corner_first = display_transform.apply(float(rect[0]), float(rect[1]))
        corner_second = display_transform.apply(float(rect[2]), float(rect[3]))
        bbox = (
            min(corner_first[0], corner_second[0]),
            min(corner_first[1], corner_second[1]),
            max(corner_first[0], corner_second[0]),
            max(corner_first[1], corner_second[1]),
        )
        text = " ".join(contents.split())
        if not text:
            continue
        signature = (
            f"shx|{text}|{bbox[0]:.3f}|{bbox[1]:.3f}|{bbox[2]:.3f}|{bbox[3]:.3f}"
        )
        font_size = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
        result.append(
            PdfTextObservation(
                element_id=_element_id("text", page_number, signature),
                text=text,
                bbox_pt=bbox,
                native_id=f"annotation:{annotation_index:04d}:shx",
                font_size_pt=font_size if font_size > 0 else None,
            )
        )
    result.sort(key=lambda item: (item.bbox_pt, item.element_id))
    return tuple(result)


def _unique_rects(rects: Iterable[dict[str, object]], page_number: int) -> tuple[PdfRectObservation, ...]:
    seen: set[tuple[float, float, float, float]] = set()
    result: list[PdfRectObservation] = []
    for obj in rects:
        bbox = (
            round(float(obj["x0"]), 4),
            round(float(obj["y0"]), 4),
            round(float(obj["x1"]), 4),
            round(float(obj["y1"]), 4),
        )
        if bbox in seen or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        seen.add(bbox)
        signature = "|".join(f"{value:.4f}" for value in bbox)
        result.append(
            PdfRectObservation(
                element_id=_element_id("rect", page_number, signature),
                bbox_pt=bbox,
                native_id=_native_id(obj, "rect"),
                filled=bool(obj.get("fill", False)),
            )
        )
    return tuple(sorted(result, key=lambda item: item.bbox_pt))


def _dash_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (tuple, list)) and value:
        pattern = value[0]
        if isinstance(pattern, (tuple, list)):
            return any(float(item) > 0 for item in pattern)
        try:
            return float(pattern) > 0
        except (TypeError, ValueError):
            return True
    return bool(value)


def _unique_lines(lines: Iterable[dict[str, object]], page_number: int) -> tuple[PdfLineObservation, ...]:
    evidence: dict[tuple[float, float, float, float], tuple[dict[str, object], set[str]]] = {}
    for obj in lines:
        a = (round(float(obj["x0"]), 4), round(float(obj["y0"]), 4))
        b = (round(float(obj["x1"]), 4), round(float(obj["y1"]), 4))
        if a == b:
            continue
        start, end = sorted((a, b))
        signature_tuple = (start[0], start[1], end[0], end[1])
        layer = obj.get("_oabm_source_layer")
        if signature_tuple in evidence:
            if isinstance(layer, str) and layer:
                evidence[signature_tuple][1].add(layer)
            continue
        evidence[signature_tuple] = (obj, {layer} if isinstance(layer, str) and layer else set())
    result: list[PdfLineObservation] = []
    for signature_tuple, (obj, source_layers) in evidence.items():
        start = (signature_tuple[0], signature_tuple[1])
        end = (signature_tuple[2], signature_tuple[3])
        signature = "|".join(f"{value:.4f}" for value in signature_tuple)
        primitive_family = str(obj.get("_oabm_primitive_family") or "line")
        result.append(
            PdfLineObservation(
                element_id=_element_id("line", page_number, signature),
                start_pt=start,
                end_pt=end,
                native_id=_native_id(obj, "line"),
                primitive_family=primitive_family,
                dashed=bool(obj.get("_oabm_dashed", False)),
                filled=bool(obj.get("_oabm_filled", False)),
                source_layers=tuple(sorted(source_layers)),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.start_pt, item.end_pt)))


def _curve_primitive_family(curve: dict[str, object]) -> str:
    path = curve.get("path")
    if not isinstance(path, (list, tuple)):
        return "curve"
    for item in path:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        operation = str(item[0]).lower()
        if operation in {"c", "v", "y"}:
            return "curve"
    return "polyline"


def _curve_polyline_segments(
    curves: Iterable[dict[str, object]],
    page_height: float,
) -> tuple[dict[str, object], ...]:
    """Flatten pdfplumber curve/polyline points into bottom-origin line primitives.

    The primitive family and dash state stay attached to each segment so the
    wall recognizer can diagnose which CAD path families contributed evidence.
    """

    segments: list[dict[str, object]] = []
    for curve in curves:
        raw_points = curve.get("pts")
        if not isinstance(raw_points, (list, tuple)):
            continue

        points: list[tuple[float, float]] = []
        for raw_point in raw_points:
            try:
                x = float(raw_point[0])  # type: ignore[index]
                top = float(raw_point[1])  # type: ignore[index]
            except (IndexError, TypeError, ValueError):
                points = []
                break
            points.append((x, page_height - top))

        primitive_family = _curve_primitive_family(curve)
        dashed = _dash_present(curve.get("dash"))
        filled = bool(curve.get("fill", False))
        for start, end in zip(points, points[1:]):
            if start == end:
                continue
            segment: dict[str, object] = {
                "x0": start[0],
                "y0": start[1],
                "x1": end[0],
                "y1": end[1],
                "tag": curve.get("tag") or primitive_family,
                "_oabm_primitive_family": primitive_family,
                "_oabm_dashed": dashed,
                "_oabm_filled": filled,
                "_oabm_source_layer": curve.get("_oabm_source_layer"),
            }
            if isinstance(curve.get("mcid"), int):
                segment["mcid"] = curve["mcid"]
            segments.append(segment)
    return tuple(segments)


def _reference_ids(value: object) -> set[int]:
    members = resolve1(value)
    if not isinstance(members, (tuple, list)):
        return set()
    return {item.objid for item in members if isinstance(item, PDFObjRef)}


def _optional_group_state(document: object, group: object) -> bool:
    """Resolve the PDF default view state; unknown configured states fail closed."""

    catalog = resolve1(document.catalog)  # type: ignore[attr-defined]
    optional = resolve1(catalog.get("OCProperties", {})) if isinstance(catalog, dict) else {}
    if not isinstance(optional, dict):
        return False
    config = resolve1(optional.get("D", {}))
    if not isinstance(config, dict):
        return False
    base = getattr(config.get("BaseState"), "name", "ON")
    if base not in {"ON", "OFF", "Unchanged"}:
        return False
    # The group ref may come from pdfminer (PDFObjRef.objid) or from the
    # pypdf annotation pass (IndirectObject.idnum); both are object numbers
    # in the same file, so they resolve against the same ON/OFF lists.
    group_id = (
        group.objid
        if isinstance(group, PDFObjRef)
        else getattr(group, "idnum", None)
    )
    if group_id is None:
        return base == "ON" and not config.get("ON") and not config.get("OFF")
    on_ids = _reference_ids(config.get("ON", []))
    off_ids = _reference_ids(config.get("OFF", []))
    if group_id in on_ids and group_id in off_ids:
        return False
    if group_id in off_ids:
        return False
    if group_id in on_ids:
        return True
    return base == "ON"


class _LayerAggregator(PDFPageAggregatorWithMarkedContent):
    """Keep optional-content group names attached to PDF source primitives."""

    def __init__(self, *args: object, layer_states: dict[str, tuple[str | None, bool]], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.layer_states = layer_states
        self.layer_stack: list[tuple[str | None, bool]] = []

    def begin_tag(self, tag: object, props: object = None) -> None:
        parent = self.layer_stack[-1] if self.layer_stack else (None, True)
        layer = parent
        if getattr(tag, "name", None) == "OC":
            configured = self.layer_states.get(getattr(props, "name", None))
            layer = (configured[0], parent[1] and configured[1]) if configured else (None, False)
        self.layer_stack.append(layer)
        super().begin_tag(tag, props)

    def end_tag(self) -> None:
        if self.layer_stack:
            self.layer_stack.pop()
        super().end_tag()

    def tag_cur_item(self) -> None:
        super().tag_cur_item()
        if self.cur_item._objs:
            name, visible = self.layer_stack[-1] if self.layer_stack else (None, True)
            self.cur_item._objs[-1]._oabm_source_layer = name
            self.cur_item._objs[-1]._oabm_hidden = not visible


class _LayerPage(Page):
    @property
    def layout(self):  # type: ignore[override]
        if not hasattr(self, "_layout"):
            resources = resolve1(self.page_obj.resources)
            properties = resolve1(resources.get("Properties", {}))
            layer_states: dict[str, tuple[str | None, bool]] = {}
            if isinstance(properties, dict):
                for key, value in properties.items():
                    group = resolve1(value)
                    if not isinstance(group, dict):
                        continue
                    name = group.get("Name")
                    if isinstance(name, bytes):
                        label = name.decode("utf-8", "replace")
                    elif isinstance(name, str):
                        label = name
                    else:
                        label = None
                    is_group = getattr(group.get("Type"), "name", None) == "OCG"
                    layer_states[str(key)] = (
                        label,
                        is_group and _optional_group_state(self.pdf.doc, value),
                    )
            device = _LayerAggregator(
                self.pdf.rsrcmgr,
                pageno=self.page_number,
                laparams=self.pdf.laparams,
                layer_states=layer_states,
            )
            PDFPageInterpreter(self.pdf.rsrcmgr, device).process_page(self.page_obj)
            self._layout = device.get_result()
        return self._layout

    def process_object(self, obj):  # type: ignore[override]
        result = super().process_object(obj)
        layer = getattr(obj, "_oabm_source_layer", None)
        if isinstance(layer, str) and layer:
            result["_oabm_source_layer"] = layer
        if getattr(obj, "_oabm_hidden", False):
            result["_oabm_hidden"] = True
        return result


def extract_pdf(path: str | Path, *, source_id: str | None = None) -> PdfDocumentObservation:
    pdf_path = Path(path)
    payload = pdf_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    logical_source_id = source_id or f"pdf:{_token(pdf_path.stem) or 'document'}"

    pages: list[PdfPageObservation] = []
    reader = PdfReader(pdf_path)
    with pdfplumber.open(pdf_path) as document:
        for index, original_page in enumerate(document.pages, start=1):
            layered_page = _LayerPage(document, original_page.page_obj, index, original_page.initial_doctop)
            hidden_wall_source_present = any(
                item.get("_oabm_hidden", False)
                and isinstance(item.get("_oabm_source_layer"), str)
                and _is_wall_source_layer(item["_oabm_source_layer"])
                for item in (*layered_page.lines, *layered_page.curves, *layered_page.rects)
            )
            page = layered_page.filter(lambda item: not item.get("_oabm_hidden", False))
            pages.append(
                PdfPageObservation(
                    page_number=index,
                    width_pt=float(page.width),
                    height_pt=float(page.height),
                    texts=(
                        *_group_words(page, index),
                        # SHX text reaches the lane only through annotations.
                        *_shx_annotation_texts(
                            reader.pages[index - 1],
                            index,
                            document.doc,
                        ),
                    ),
                    lines=_unique_lines(
                        (
                            *(
                                {
                                    **line,
                                    "_oabm_primitive_family": "line",
                                    "_oabm_dashed": _dash_present(line.get("dash")),
                                    "_oabm_filled": bool(line.get("fill", False)),
                                    "_oabm_source_layer": line.get("_oabm_source_layer"),
                                }
                                for line in page.lines
                            ),
                            *_curve_polyline_segments(page.curves, float(page.height)),
                        ),
                        index,
                    ),
                    rects=_unique_rects(page.rects, index),
                    hidden_wall_source_present=hidden_wall_source_present,
                )
            )
    return PdfDocumentObservation(
        source_id=logical_source_id,
        content_sha256=digest,
        pages=tuple(pages),
    )
