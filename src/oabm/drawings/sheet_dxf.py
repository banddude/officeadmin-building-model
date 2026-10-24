"""Bounded, editable physical-sheet DXF export; never canonical writeback.

The optional ezdxf dependency is loaded only when exporting. CAD entities live
in a real millimetre paperspace layout. Modelspace is intentionally empty.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from typing import Any

from .model import DrawingSet
from .sheets import SheetSpec, ViewPlacement, compose_sheet

_WEIGHTS = {"normal": 25, "heavy": 50, "thin": 13}
_STROKES = {"object", "cut", "space", "opening", "electrical", "route", "obstacle", "constraint", "annotation"}
_PATTERNS = {"solid": "CONTINUOUS", "dash": "OABM_DASH", "dot": "OABM_DOT"}
_COLORS = {"space": 0x666666, "obstacle": 0x666666, "constraint": 0x990000}
_FONT = "DejaVuSansMono.ttf"
_TEXT_HEIGHT = 2.5


@dataclass(frozen=True, slots=True)
class SheetDxf:
    """UTF-8 DXF and JSON companion; both are derived, not edit/import contracts."""

    dxf: bytes
    manifest_json: str

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_json)


def _plain_text(value: str) -> str:
    # TEXT interprets %% codes and backslashes in CAD clients. Do not interpret
    # arbitrary labels as CAD formatting, URLs, fields, or multiline content.
    if not isinstance(value, str) or not value or any(not 32 <= ord(c) <= 126 for c in value) or "%%" in value or "\\" in value:
        raise ValueError("DXF text must be nonempty printable ASCII without CAD escape codes")
    return value


def _stable_metadata(raw: str, doc: Any, version: str, digest: str) -> bytes:
    """Normalize only known volatile DXF metadata, never source text.

    ezdxf's global testing option would affect unrelated concurrent exports.
    Instead normalize its HEADER variables and identified DICTIONARYVAR handles
    after the ordinary writer runs. The constrained ezdxf minor range and
    byte-repeatability/readback tests guard this adapter boundary.
    """
    meta_handles = {entry.dxf.handle for _, entry in doc.rootdict["EZDXF_META"].items()}
    pairs = raw.splitlines()
    if len(pairs) % 2:
        raise ValueError("invalid DXF tag stream")
    section = variable = handle = ""
    for i in range(0, len(pairs), 2):
        code, value = int(pairs[i]), pairs[i + 1]
        if code == 0:
            handle = ""
        if code == 2 and i >= 2 and pairs[i - 1] == "SECTION":
            section = value
        if section == "HEADER":
            if code == 9:
                variable = value
            elif variable in {"$TDCREATE", "$TDUCREATE", "$TDUPDATE", "$TDUUPDATE"} and code == 40:
                pairs[i + 1] = "2451544.5"
            elif variable in {"$VERSIONGUID", "$FINGERPRINTGUID"} and code == 2:
                # Content-derived document identity, not a random run identity.
                h = digest[:32].upper()
                pairs[i + 1] = "{" + "-".join((h[:8], h[8:12], h[12:16], h[16:20], h[20:])) + "}"
        elif section == "OBJECTS":
            if code == 5:
                handle = value
            elif code == 1 and handle in meta_handles:
                pairs[i + 1] = f"ezdxf {version}; deterministic OABM sheet export"
    # ezdxf discovers required CLASS records through a set. Their order is
    # semantically irrelevant but otherwise changes with PYTHONHASHSEED.
    for i in range(0, len(pairs) - 3, 2):
        if int(pairs[i]) == 0 and pairs[i + 1] == "SECTION" and pairs[i + 3] == "CLASSES":
            start = i + 4
            end = start
            while not (int(pairs[end]) == 0 and pairs[end + 1] == "ENDSEC"):
                end += 2
            records: list[list[str]] = []
            for offset in range(start, end, 2):
                if int(pairs[offset]) == 0:
                    if pairs[offset + 1] != "CLASS":
                        raise ValueError("unexpected DXF CLASSES record")
                    records.append([])
                records[-1].extend(pairs[offset:offset + 2])
            pairs[start:end] = [tag for record in sorted(records) for tag in record]
            break
    return ("\n".join(pairs) + "\n").encode("utf-8")


def sheet_to_dxf(drawing_set: DrawingSet, spec: SheetSpec, placements: tuple[ViewPlacement, ...]) -> SheetDxf:
    """Export a complete bounded composition, or raise without returning output.

    Supports line/polyline/unfilled polygon/plain ASCII text, nonassociative
    dimension linework and a generic ruled titleblock. Symbols are refused.
    Title/revision require a reserved titleblock; arbitrary metadata remains in
    the lossless companion snapshot, not silently rendered as annotations.
    """
    sheet = compose_sheet(drawing_set, spec, placements).to_dict()
    if (spec.title or spec.revision) and not spec.titleblock_height_mm:
        raise ValueError("sheet title/revision require a reserved titleblock")
    try:
        import ezdxf
    except ImportError as exc:
        raise ImportError("DXF export requires officeadmin-building-model[cad]") from exc

    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LWDISPLAY"] = True
    doc.header["$PSLTSCALE"] = 1
    doc.linetypes.new("OABM_DASH", dxfattribs={"description": "3 mm dash / 1.5 mm gap", "pattern": [4.5, 3, -1.5]})
    doc.linetypes.new("OABM_DOT", dxfattribs={"description": "dot / 1 mm gap", "pattern": [1, 0, -1]})
    doc.styles.new("OABM_TEXT", dxfattribs={"font": _FONT})
    layout = doc.layouts.get("Layout1")
    layout.page_setup(size=(spec.width_mm, spec.height_mm), margins=(0, 0, 0, 0), units="mm", scale=(1, 1), name="OABM", device="")
    layout.dxf_layout.dxf.plot_type = 5  # Layout, not extents/fit-to-page.
    layout.set_plot_style("", show=False)
    doc.layouts.set_active_layout("Layout1")
    doc.header["$TILEMODE"] = 0
    entries: list[dict[str, Any]] = []
    layer_map: dict[str, str] = {}

    def layer(source: str) -> str:
        if source not in layer_map:
            # Do not sanitize collisions or allow CAD reserved/control names.
            name = "OABM_" + source.encode("utf-8").hex().upper()
            if len(name) > 255:
                raise ValueError("source layer name exceeds DXF bounded name length")
            doc.layers.new(name, dxfattribs={"color": 7})
            layer_map[source] = name
        return layer_map[source]

    def xy(point: dict[str, float]) -> tuple[float, float]:
        return point["x"], round(spec.height_mm - point["y"], 9)

    def record(entity: Any, role: str, context: dict[str, Any]) -> None:
        entries.append({"handle": entity.dxf.handle, "entity_type": entity.dxftype(), "role": role, **context})

    def line(a: tuple[float, float], b: tuple[float, float], attrs: dict, role: str, context: dict) -> None:
        if a == b:
            raise ValueError(f"degenerate DXF {role}")
        record(layout.add_line(a, b, dxfattribs=attrs), role, context)

    def text(value: str, point: tuple[float, float], attrs: dict, role: str, context: dict, *, angle: float = 0, centered: bool = False) -> None:
        value = _plain_text(value)
        # A conservative rotated box includes ascenders, descenders and side
        # bearings. Font substitution/collision QA is still required, but anchors
        # on the paper boundary must not silently clip ordinary glyphs.
        width = len(value) * _TEXT_HEIGHT
        left = -width / 2 if centered else 0
        right = width / 2 if centered else width
        radians = math.radians(angle)
        c, s = math.cos(radians), math.sin(radians)
        for x in (left - .25, right + .25):
            for y in (-.4 * _TEXT_HEIGHT, 1.5 * _TEXT_HEIGHT):
                px, py = point[0] + x*c-y*s, point[1] + x*s+y*c
                if px < spec.margin_mm or px > spec.width_mm-spec.margin_mm or py < spec.margin_mm or py > spec.height_mm-spec.margin_mm:
                    raise ValueError("text exceeds conservative paper bounds")
        entity = layout.add_text(value, dxfattribs={**attrs, "insert": point, "height": _TEXT_HEIGHT, "style": "OABM_TEXT", "rotation": angle})
        if centered:
            from ezdxf.enums import TextEntityAlignment
            entity.set_placement(point, align=TextEntityAlignment.CENTER)
        record(entity, role, context)

    for view in sheet["views"]:
        base = {"placement_id": view["placement_id"], "view_id": view["view_id"]}
        for primitive in view["primitives"]:
            kind = primitive["kind"]
            if kind not in {"line", "polyline", "polygon", "text"} or primitive["symbol"] is not None:
                raise ValueError(f"unsupported DXF primitive {primitive['id']!r}: {kind}/symbol")
            style = primitive["style"]
            if style["stroke"] not in _STROKES or style["weight"] not in _WEIGHTS or style["pattern"] not in _PATTERNS:
                raise ValueError(f"unsupported DXF style for {primitive['id']!r}")
            attrs = {"layer": layer(primitive["layer"]), "lineweight": _WEIGHTS[style["weight"]], "linetype": _PATTERNS[style["pattern"]], "color": 7, "true_color": _COLORS.get(style["stroke"], 0)}
            context = {**base, "primitive_id": primitive["id"], "source_ids": primitive["source_ids"]}
            points = [xy(p) for p in primitive["points_mm"]]
            if kind == "text":
                if primitive["closed"] or style["pattern"] != "solid":
                    raise ValueError("closed or patterned text is unsupported")
                text(primitive["text"], points[0], attrs, "primitive", context)
            else:
                if primitive["text"] is not None:
                    raise ValueError("geometry carrying text is unsupported")
                if any(a == b for a, b in zip(points, points[1:])):
                    raise ValueError("degenerate DXF segment")
                if kind == "line":
                    if len(points) != 2 or primitive["closed"]:
                        raise ValueError("DXF line requires exactly two points and no closure")
                    line(*points, attrs, "primitive", context)
                else:
                    closed = kind == "polygon" or primitive["closed"]
                    if closed and (len(set(points)) < 3 or points[0] == points[-1]):
                        raise ValueError("closed DXF geometry requires three distinct vertices and implicit closure")
                    record(layout.add_lwpolyline(points, close=closed, dxfattribs=attrs), "primitive", context)
        for dimension in view["dimensions"]:
            context = {**base, "dimension_id": dimension["id"], "source_ids": dimension["source_ids"], "value_m": dimension["value_m"], "associative": False}
            attrs = {"layer": layer("annotations:dimensions"), "lineweight": 13, "color": 7}
            a, b = xy(dimension["line_start_mm"]), xy(dimension["line_end_mm"])
            line(a, b, attrs, "dimension-line", context)
            for measured, endpoint in ((xy(dimension["measured_start_mm"]), a), (xy(dimension["measured_end_mm"]), b)):
                if measured != endpoint:  # zero offset has no extension
                    line(measured, endpoint, attrs, "dimension-extension", context)
            # Existing label and measured value only. No guessed arrows or
            # associative CAD measurement which would measure paper units.
            anchor = xy(dimension["text_anchor_mm"])
            angle = math.degrees(math.atan2(b[1]-a[1], b[0]-a[0]))
            if angle > 90:
                angle -= 180
            elif angle <= -90:
                angle += 180
            radians = math.radians(angle)
            position = (anchor[0] - 1.5*math.sin(radians), anchor[1] + 1.5*math.cos(radians))
            text(dimension["text"], position, attrs, "dimension-label", context, angle=angle, centered=True)

    if spec.titleblock_height_mm:
        x0, y0, x1, y1 = sheet["titleblock"]["bounds_mm"]
        rows = [("Sheet", spec.id), ("Title", spec.title), ("Revision", spec.revision)] + list(spec.titleblock_fields)
        row_height = (y1 - y0) / len(rows)
        if row_height < _TEXT_HEIGHT + 3:
            raise ValueError("titleblock is too short for its rows")
        attrs = {"layer": layer("sheet:titleblock"), "lineweight": 25, "color": 7}
        context = {"sheet_id": spec.id, "source_ids": []}
        points = [(x0, spec.height_mm-y0), (x1, spec.height_mm-y0), (x1, spec.height_mm-y1), (x0, spec.height_mm-y1)]
        record(layout.add_lwpolyline(points, close=True, dxfattribs=attrs), "titleblock-border", context)
        for index, (key, value) in enumerate(rows):
            if index:
                y = spec.height_mm - y0 - index * row_height
                line((x0, y), (x1, y), attrs, "titleblock-rule", context)
            label = _plain_text(key) + ":" + (" " + _plain_text(value) if value else "")
            if len(label) * _TEXT_HEIGHT + 4 > x1 - x0:
                raise ValueError("titleblock text exceeds conservative row width")
            text(label, (x0+2, spec.height_mm-y0-(index+1)*row_height+1.5), attrs, "titleblock-text", {**context, "field": key})

    if not entries:
        raise ValueError("DXF export requires visible content")
    auditor = doc.audit()
    if auditor.errors or auditor.fixes:
        raise ValueError("DXF audit required repairs before serialization")
    stream = io.StringIO()
    doc.write(stream)
    sheet_json = json.dumps(sheet, sort_keys=True, allow_nan=False)
    content_digest = hashlib.sha256(sheet_json.encode()).hexdigest()
    dxf = _stable_metadata(stream.getvalue(), doc, ezdxf.__version__, content_digest)
    # Check the exact returned bytes, not just the in-memory source document.
    readback = ezdxf.read(io.StringIO(dxf.decode("utf-8")))
    audit = readback.audit()
    if audit.errors or audit.fixes or len(readback.modelspace()):
        raise ValueError("serialized DXF failed readback audit")
    manifest = {"kind": "oabm.sheet-dxf", "version": "1.0.0", "dxf_sha256": hashlib.sha256(dxf).hexdigest(),
                "ezdxf_version": ezdxf.__version__, "layout": "Layout1", "length_unit": "mm", "plot_scale": [1, 1],
                "coordinate_system": "bottom-left,+x-right,+y-up", "font": _FONT, "text_height_mm": _TEXT_HEIGHT,
                "layers": layer_map, "entities": entries, "composition": sheet,
                "limitations": ["Derived presentation only; CAD edits do not update the canonical model.",
                                "Dimensions are nonassociative linework and supplied text, not DIMENSION entities.",
                                "Symbols and non-ASCII/CAD-formatted text are unsupported.",
                                "Font is referenced, not embedded; substitution and annotation collisions require visual QA.",
                                "No printer driver, permit approval, native DWG, or production print-readiness is claimed."]}
    return SheetDxf(dxf, json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False))
