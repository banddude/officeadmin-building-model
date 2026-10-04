"""Vector/text extraction from PDF pages.

pdfplumber is intentionally used only to turn PDF primitives into deterministic
source observations.  Semantic interpretation remains in ``importer.py``.
"""

from __future__ import annotations

import hashlib
import math
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

from .curve_geometry import fit_circle
from .types import (
    PdfCurveObservation,
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


def _is_wall_pattern_layer(layer: str) -> bool:
    # A wall-pattern layer carries the poché of drawn walls: it extends the
    # wall layer it belongs to rather than being a separate drawing family.
    name = layer.rsplit("|", 1)[-1].upper().lstrip("_")
    return name.startswith(("A-WALL-PATT", "AE-WALL-PATT"))


def _is_wall_source_layer(layer: str) -> bool:
    name = layer.rsplit("|", 1)[-1].upper().lstrip("_")
    if name in {"A-WALL", "AE-WALL"}:
        return True
    return _is_wall_pattern_layer(layer)


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
        # /Rect corners are absolute user-space dictionary values (raw pypdf,
        # unrotated); apply() subtracts the MediaBox origin before rotating.
        # Corners may be reversed; normalize into displayed space.
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
    seen: set[tuple[float, float, float, float, str | None]] = set()
    result: list[PdfRectObservation] = []
    for obj in rects:
        bbox = (
            round(float(obj["x0"]), 4),
            round(float(obj["y0"]), 4),
            round(float(obj["x1"]), 4),
            round(float(obj["y1"]), 4),
        )
        layer_value = obj.get("_oabm_source_layer")
        source_layer = layer_value if isinstance(layer_value, str) and layer_value else None
        # Identical outlines on different optional-content layers are distinct
        # source evidence and must not dedupe into one rectangle.
        if (*bbox, source_layer) in seen or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        seen.add((*bbox, source_layer))
        signature = "|".join(f"{value:.4f}" for value in bbox)
        if source_layer is not None:
            signature = f"{signature}|{source_layer}"
        result.append(
            PdfRectObservation(
                element_id=_element_id("rect", page_number, signature),
                bbox_pt=bbox,
                native_id=_native_id(obj, "rect"),
                filled=bool(obj.get("fill", False)),
                source_layer=source_layer,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.bbox_pt, item.source_layer or "")))


def _wall_layer_rects(page: PdfPageObservation) -> tuple[PdfRectObservation, ...]:
    """Rectangles on a source layer already accepted as a wall layer."""

    return tuple(sorted(
        (
            rect
            for rect in page.rects
            if rect.source_layer is not None and _is_wall_source_layer(rect.source_layer)
        ),
        key=lambda rect: (rect.bbox_pt, rect.element_id),
    ))


def _rect_edge_segments(
    rects: Iterable[PdfRectObservation],
    page_number: int,
) -> tuple[PdfLineObservation, ...]:
    """Turn rectangles into the four outline segments as line primitives.

    The segment family stays ``rect`` so diagnostics can tell rectangle-derived
    wall evidence apart from drawn lines, and the layer rides along in
    ``source_layers`` so wall-layer provenance is preserved. A segment takes
    its rectangle's stroke style when the rect observation carries one; rect
    observations carry none yet, so the style stays unknown.
    """

    segments: list[PdfLineObservation] = []
    for rect in rects:
        x0, y0, x1, y1 = rect.bbox_pt
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        for index in range(4):
            start, end = sorted((corners[index], corners[(index + 1) % 4]))
            segments.append(
                PdfLineObservation(
                    element_id=_element_id(
                        "rect-edge", page_number, f"{rect.element_id}|{index}"
                    ),
                    start_pt=start,
                    end_pt=end,
                    primitive_family="rect",
                    filled=rect.filled,
                    source_layers=(rect.source_layer,) if rect.source_layer else (),
                    line_width_pt=getattr(rect, "line_width_pt", None),
                    stroke_gray=getattr(rect, "stroke_gray", None),
                )
            )
    return tuple(sorted(segments, key=lambda item: (item.start_pt, item.end_pt)))


def _wall_layer_rect_segments(page: PdfPageObservation) -> tuple[PdfLineObservation, ...]:
    """Outline segments of wall-layer rectangles, ready for wall evidence."""

    return _rect_edge_segments(_wall_layer_rects(page), page.page_number)


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


# Perceived luminance weights for a DeviceRGB stroking colour.
_RGB_LUMINANCE = (0.299, 0.587, 0.114)


def _stroke_gray(value: object) -> float | None:
    """Luminance 0 (black) to 1 (white) of a PDF stroking colour.

    DeviceGray is taken directly, DeviceRGB mixes 0.299/0.587/0.114, and
    DeviceCMYK converts to RGB first (the PDF spec's 1-C etc. with K).
    Pattern colours and anything unparseable stay unknown. Out-of-range
    components are clipped, as the PDF spec requires of consumers. Rounded
    to 4 decimals.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float, list, tuple)):
        return None
    components = (
        (float(value),) if isinstance(value, (int, float)) else tuple(float(item) for item in value)
    )
    if not components or any(not math.isfinite(item) for item in components):
        return None
    if len(components) == 1:
        gray = components[0]
    elif len(components) == 3:
        gray = sum(weight * item for weight, item in zip(_RGB_LUMINANCE, components))
    elif len(components) == 4:
        c, m, y, k = components
        rgb = ((1.0 - c) * (1.0 - k), (1.0 - m) * (1.0 - k), (1.0 - y) * (1.0 - k))
        gray = sum(weight * item for weight, item in zip(_RGB_LUMINANCE, rgb))
    else:
        return None
    return round(min(1.0, max(0.0, gray)), 4)


def _line_width_pt(value: object) -> float | None:
    """Displayed stroke width in points, rounded to 4 decimals.

    pdfplumber (0.11.x) reports ``linewidth`` with the page CTM already
    applied (a ``1 w`` line under a ``2 0 0 2`` scale reports 2.0), so the
    displayed width is recorded as reported and is not rescaled again.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    width = float(value)
    if not math.isfinite(width) or width < 0:
        return None
    return round(width, 4)


def _merge_stroke(
    kept: tuple[float | None, float | None],
    new: tuple[float | None, float | None],
) -> tuple[float | None, float | None]:
    """Fold duplicate geometry styles: keep the MAX width and the MIN gray.

    ``None`` (style unknown) never overrides a value seen for the same
    geometry.
    """

    kept_width, kept_gray = kept
    new_width, new_gray = new
    widths = [item for item in (kept_width, new_width) if item is not None]
    grays = [item for item in (kept_gray, new_gray) if item is not None]
    return (max(widths) if widths else None, min(grays) if grays else None)


def _unique_lines(lines: Iterable[dict[str, object]], page_number: int) -> tuple[PdfLineObservation, ...]:
    evidence: dict[
        tuple[float, float, float, float],
        tuple[dict[str, object], set[str], float | None, float | None],
    ] = {}
    for obj in lines:
        a = (round(float(obj["x0"]), 4), round(float(obj["y0"]), 4))
        b = (round(float(obj["x1"]), 4), round(float(obj["y1"]), 4))
        if a == b:
            continue
        start, end = sorted((a, b))
        signature_tuple = (start[0], start[1], end[0], end[1])
        layer = obj.get("_oabm_source_layer")
        stroke = (_line_width_pt(obj.get("linewidth")), _stroke_gray(obj.get("stroking_color")))
        if signature_tuple in evidence:
            kept_obj, source_layers, kept_stroke = evidence[signature_tuple]
            if isinstance(layer, str) and layer:
                source_layers.add(layer)
            evidence[signature_tuple] = (kept_obj, source_layers, _merge_stroke(kept_stroke, stroke))
            continue
        evidence[signature_tuple] = (obj, {layer} if isinstance(layer, str) and layer else set(), stroke)
    result: list[PdfLineObservation] = []
    for signature_tuple, (obj, source_layers, (line_width_pt, stroke_gray)) in evidence.items():
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
                line_width_pt=line_width_pt,
                stroke_gray=stroke_gray,
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
                # Stroke style rides along so _unique_lines merges curve
                # segments exactly like drawn lines.
                "linewidth": curve.get("linewidth"),
                "stroking_color": curve.get("stroking_color"),
            }
            if isinstance(curve.get("mcid"), int):
                segment["mcid"] = curve["mcid"]
            segments.append(segment)
    return tuple(segments)


def _sample_curves(
    curves: Iterable[dict[str, object]],
    page_height: float,
    page_number: int,
) -> tuple[PdfCurveObservation, ...]:
    """Flatten pure cubic subpaths by a convex-hull chord-error bound (0.1 pt).

    Control points must be near the finite chord, not just its infinite line;
    this rejects flat-looking backtracking. Depth exhaustion fails closed.
    Legacy line observations remain byte-identical for existing consumers.
    """
    tolerance = 0.1

    def distance(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        denominator = dx * dx + dy * dy
        t = (
            max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / denominator))
            if denominator
            else 0.0
        )
        return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)

    def mid(a, b):
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    def sample(a, b, c, d, depth=0):
        if max(distance(b, a, d), distance(c, a, d)) <= tolerance:
            return [a, d]
        if depth >= 20:
            raise ValueError("curve subdivision exhausted")
        ab, bc, cd = mid(a, b), mid(b, c), mid(c, d)
        abc, bcd = mid(ab, bc), mid(bc, cd)
        center = mid(abc, bcd)
        return sample(a, ab, abc, center, depth + 1)[:-1] + sample(
            center, bcd, cd, d, depth + 1
        )

    result = {}
    for curve in curves:
        if not curve.get("stroke"):
            continue
        path = curve.get("path")
        if not isinstance(path, (tuple, list)) or not path:
            continue
        if str(path[0][0]).lower() != "m":
            continue
        operations = [str(item[0]).lower() for item in path[1:]]
        cubic = bool(operations) and all(op == "c" for op in operations)
        polyline = len(operations) >= 4 and all(op == "l" for op in operations)
        if not cubic and not polyline:
            continue
        try:
            convert = lambda p: (float(p[0]), page_height - float(p[1]))
            start = convert(path[0][1])
            points = [start]
            chords = []
            for item in path[1:]:
                if cubic:
                    b, c, d = (convert(p) for p in item[1:])
                    if not all(math.isfinite(v) for p in (start, b, c, d) for v in p):
                        raise ValueError("nonfinite curve")
                    points.extend(sample(start, b, c, d)[1:])
                else:
                    d = convert(item[1])
                    if not all(math.isfinite(v) for v in d):
                        raise ValueError("nonfinite polyline")
                    points.append(d)
                chords.append((start, d))
                start = d
            if len(points) < 3:
                continue
        except (ValueError, TypeError, IndexError):
            continue
        pts = tuple((round(x, 6), round(y, 6)) for x, y in points)
        pts = min(pts, tuple(reversed(pts)))
        signature = repr(pts)
        layer = curve.get("_oabm_source_layer")
        layers = (layer,) if isinstance(layer, str) and layer else ()
        key = (pts, _dash_present(curve.get("dash")))
        if key in result:
            layers = tuple(sorted(set(layers) | set(result[key].source_layers)))
        observation = PdfCurveObservation(
            element_id=_element_id("curve", page_number, signature),
            points_pt=pts,
            chord_endpoints=tuple(chords),
            dashed=key[1],
            source_layers=layers,
            max_chord_error_pt=tolerance if cubic else 1.0,
            primitive_family="curve" if cubic else "polyline",
        )
        if polyline:
            # Polygonal CAD arcs already carry their sampling. Retain only
            # genuinely circular chains, with bounded source chord sag.
            circle = fit_circle(observation)
            if circle is None:
                continue
            center, radius, _, _ = circle
            if any(
                abs(math.dist(mid(a, b), center) - radius) > 1.0
                for a, b in zip(pts, pts[1:])
            ):
                continue
        result[key] = observation
    return tuple(result[key] for key in sorted(result))


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
        # pdfplumber re-adds the rotated MediaBox origin onto x0/x1, pts,
        # path, top and bottom (its point2coord and the #1181 reversion), so
        # on an offset-MediaBox page those keys land an absolute shifted
        # float-step away from the zero-origin values, while y0/y1 stay
        # page-relative (pdfminer already shifted them). Neutralizing the
        # mediabox for the duration of the call sends offset pages through
        # the exact float path a zero-origin page already takes - no
        # arithmetic, no rounding dust. Zero-origin pages skip this
        # entirely, keeping existing observations byte-identical.
        mediabox = self.mediabox
        if mediabox[0] != 0.0 or mediabox[1] != 0.0:
            self.mediabox = (0.0, 0.0, mediabox[2], mediabox[3])
            try:
                result = super().process_object(obj)
            finally:
                self.mediabox = mediabox
        else:
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
                    curves=_sample_curves(page.curves, float(page.height), index),
                    hidden_wall_source_present=hidden_wall_source_present,
                )
            )
    return PdfDocumentObservation(
        source_id=logical_source_id,
        content_sha256=digest,
        pages=tuple(pages),
    )
