"""Versioned, explicit detail recipes over canonical supported projections.

No installation design, physical product selection, or alternate model contract.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import html
import math

from oabm.assembly_compat import require_no_populated_building_assemblies
from oabm.model import BuildingModel, Point3, Vector3, ElectricalEquipment
from .generator import (ElevationSpec, SectionSpec, VisibilityPolicy,
                        generate_elevation, generate_section, _entity_points,
                        _fitting_selection_by_id)
from .model import Bounds2, DrawingView
from .projection import frame_from_view_direction
from .company_sheets import measure_sheet_text, validate_sheet_text

RECIPES = {
    "equipment-mounting-elevation@1": "elevation",
    "connection-close-up@1": "elevation",
    "supported-section@1": "section",
}
_COLLECTIONS = ("electrical_equipment", "electrical_boxes", "support_components", "route_fittings")


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} requires nonblank single-line supplied text")
    try:
        validate_sheet_text(value)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from exc
    return value


@dataclass(frozen=True)
class DetailSpec:
    id: str
    number: str
    title: str
    recipe: str
    source_ids: tuple[str, ...]
    origin: Point3
    direction: Vector3
    bounds: Bounds2
    scale: float
    near_m: float
    far_m: float
    mounting_spec: str
    connection_spec: str
    notes: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("id", "number", "title", "mounting_spec", "connection_spec"):
            _text(getattr(self, name), name)
        if self.recipe not in RECIPES:
            raise ValueError("unsupported named/versioned detail recipe")
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("supply unique canonical source IDs")
        for value in self.source_ids:
            _text(value, "source ID")
        for value in self.notes:
            _text(value, "note")
        if any(isinstance(v, bool) or not math.isfinite(v) for v in (self.scale, self.near_m, self.far_m)):
            raise ValueError("scale and depth bounds must be finite numbers")
        if self.scale <= 0 or self.near_m >= self.far_m:
            raise ValueError("supply positive scale and ordered explicit depth bounds")
        if RECIPES[self.recipe] == "section" and not (self.near_m <= 0 < self.far_m):
            raise ValueError("section interval must include its explicit cut plane")
        frame_from_view_direction(origin=self.origin, direction=self.direction)


@dataclass(frozen=True)
class DetailResult:
    spec: DetailSpec
    view: DrawingView
    facts: tuple[str, ...]


def generate_detail(model: BuildingModel, spec: DetailSpec) -> DetailResult:
    """Reject missing dimensions, cropped sources, unsupported geometry and empty cuts."""
    require_no_populated_building_assemblies(model, "generate_detail")
    entities = {e.id: e for name in _COLLECTIONS for e in getattr(model, name)}
    selections = _fitting_selection_by_id(model)
    frame = frame_from_view_direction(origin=spec.origin, direction=spec.direction)
    selected = []
    facts = []
    for source_id in sorted(spec.source_ids):
        if source_id not in entities:
            raise ValueError(f"{source_id}: missing or unsupported detail source")
        entity = entities[source_id]
        size = getattr(entity, "size", None)
        if source_id in selections:
            selection = selections[source_id]
            if selection.geometry_basis == "dimension_derived_envelope":
                size = selection.size
        if size is None or min(size.x, size.y, size.z) <= 0:
            raise ValueError(f"{source_id}: explicit supported positive envelope dimensions required")
        points = _entity_points(entity, fitting_selection_by_id=selections)
        for point in points:
            projected, depth = frame.project(point)
            b = spec.bounds
            if not (b.min_x <= projected.x <= b.max_x and b.min_y <= projected.y <= b.max_y):
                raise ValueError(f"{source_id}: view bounds would clip supplied geometry")
            if not spec.near_m <= depth <= spec.far_m:
                raise ValueError(f"{source_id}: depth bounds would clip supplied geometry")
        selected.append(entity)
        p = entity.pose.position
        facts.append(f"A{len(selected)} {source_id}: local X/Y/Z {size.x:g}/{size.y:g}/{size.z:g} m; position {p.x:g},{p.y:g},{p.z:g} m")
    if spec.recipe == "equipment-mounting-elevation@1" and not any(isinstance(e, ElectricalEquipment) for e in selected):
        raise ValueError("equipment mounting recipe requires supplied equipment")
    visibility = VisibilityPolicy(labels=False, dimensions=False)
    common = dict(id=spec.id, origin=spec.origin, direction=spec.direction,
                  bounds=spec.bounds, scale=spec.scale, visibility=visibility)
    # Call the original model to preserve validation/assembly guards. Unselected
    # primitives are discarded only after the supported engine projection.
    if RECIPES[spec.recipe] == "section":
        view = generate_section(model, SectionSpec(**common, depth_m=spec.far_m, back_depth_m=-spec.near_m), symbol_provider=lambda *_: None)
    else:
        view = generate_elevation(model, ElevationSpec(**common, near_m=spec.near_m, far_m=spec.far_m), symbol_provider=lambda *_: None)
    ids = set(spec.source_ids)
    primitives = tuple(p for p in view.primitives if set(p.source_ids) <= ids)
    for source_id in ids:
        if not any(source_id in p.source_ids and p.kind in {"polygon", "polyline", "line"} for p in primitives):
            raise ValueError(f"{source_id}: recipe produced no supported geometry")
    view = replace(view, title=spec.title, primitives=primitives,
                   metadata=view.metadata + (("detail_recipe", spec.recipe), ("detail_number", spec.number)))
    return DetailResult(spec, view, tuple(facts))


def generate_details(model, specs):
    specs = tuple(specs)
    if not specs or len({s.id for s in specs}) != len(specs) or len({s.number for s in specs}) != len(specs):
        raise ValueError("details require unique stable IDs and numbers")
    return tuple(generate_detail(model, s) for s in sorted(specs, key=lambda s: s.number))


def detail_body_svg(detail: DetailResult, *, drawing_bounds_mm) -> str:
    """Engine SVG in a top-left millimetre group. Never fit or clip content.

    drawing_bounds_mm is (left, top, right, bottom); shared layer owns branding.
    Shared installed-font measurement refuses overflow, including long notes.
    Unsupported text fails before package staging; no glyph substitution occurs.
    """
    left, top, right, bottom = drawing_bounds_mm
    if any(not math.isfinite(v) for v in drawing_bounds_mm) or left >= right or top >= bottom:
        raise ValueError("invalid shared drawing bounds")
    spec, view = detail.spec, detail.view
    width = view.bounds.width * 1000 / view.scale
    height = view.bounds.height * 1000 / view.scale
    lines = (f"{spec.number} | {spec.title} | 1:{spec.scale:g}",
             f"Recipe: {spec.recipe}", *detail.facts,
             f"Mounting: {spec.mounting_spec}", f"Connection: {spec.connection_spec}",
             *spec.notes, "Supplied facts only. No engineering approval.")
    if width > right-left or height + 14 + len(lines)*5 > bottom-top:
        raise ValueError("detail exceeds shared drawing area at supplied scale")
    if any(measure_sheet_text(line, 3) > right-left-2 for line in lines):
        raise ValueError("detail text exceeds shared drawing area")
    def text(x, y, value, anchor="start"):
        occupied_width = measure_sheet_text(value, 3)
        start = x - occupied_width * {"start": 0, "middle": .5, "end": 1}[anchor]
        # Leave an explicit paper margin for ink bearings and shared validation.
        if start < left+.1 or start+occupied_width > right-.1 or y-3 < top or y+.9 > bottom:
            raise ValueError("detail text exceeds shared drawing area")
        return (f'<text x="{x:g}" y="{y:g}" font-family="Liberation Sans" font-size="3" '
                f'text-anchor="{anchor}" fill="#000000">{html.escape(value)}</text>')
    def paper(point):
        return ((point.x-view.bounds.min_x)*1000/view.scale+left,
                (view.bounds.max_y-point.y)*1000/view.scale+top)
    out = ['<g>']
    for primitive in view.primitives:
        if primitive.kind not in {"polygon", "polyline", "line"}:
            raise ValueError("unsupported detail projection primitive")
        tag = "polygon" if primitive.closed else "polyline"
        points = " ".join(f"{x:g},{y:g}" for x,y in map(paper, primitive.points))
        out.append(f'<{tag} points="{points}" fill="none" stroke="#000000" stroke-width="0.3"/>')
    for i, line in enumerate(lines):
        out.append(text(left+1, top+height+10+i*5, line))
    for index, source_id in enumerate(sorted(spec.source_ids), 1):
        pts = [paper(p) for primitive in view.primitives if source_id in primitive.source_ids
               for p in primitive.points]
        x0,x1 = min(p[0] for p in pts),max(p[0] for p in pts)
        y0,y1 = min(p[1] for p in pts),max(p[1] for p in pts)
        if x0 < left+25 or y0 < top+15 or x1 > right-2 or y1 > top+height-2:
            raise ValueError("reserve source margins for labels/dimensions")
        out.append(text(x0+2,y0+5,f"A{index}"))
        # Extent callouts are explicitly projected, never product local axes.
        out.append(f'<polyline points="{x0:g},{y0-2:g} {x0:g},{y0-7:g} {x1:g},{y0-7:g} {x1:g},{y0-2:g}" fill="none" stroke="#000000" stroke-width="0.2"/>')
        out.append(text((x0+x1)/2,y0-9,f"Projected W {(x1-x0)*view.scale/1000:g} m","middle"))
        out.append(f'<polyline points="{x0-2:g},{y0:g} {x0-7:g},{y0:g} {x0-7:g},{y1:g} {x0-2:g},{y1:g}" fill="none" stroke="#000000" stroke-width="0.2"/>')
        out.append(text(x0-10,(y0+y1)/2,f"H {(y1-y0)*view.scale/1000:g} m","end"))
    out.append('</g>')
    return '\n'.join(out)


def company_detail_bodies(model, specs, resolved_sheets):
    """Validate the complete package before calling shared output creation.

    Explicit detail IDs are sheet IDs. No fallback titleblock or PDF converter.
    """
    details = generate_details(model, specs)
    sheets = {sheet.id: sheet for sheet in resolved_sheets}
    if set(sheets) != {d.spec.id for d in details}:
        raise ValueError("detail IDs must exactly match resolved company sheet IDs")
    return {d.spec.id: detail_body_svg(d, drawing_bounds_mm=sheets[d.spec.id].drawing_bounds_mm) for d in details}


def detail_specs_from_dict(request):
    """Read editable explicit recipe inputs. Unknown or omitted fields fail."""
    if not isinstance(request, dict) or set(request) != {"version", "details"} or request["version"] != "detail-sheet-set@1":
        raise ValueError("expected detail-sheet-set@1 and explicit details")
    specs = []
    for raw in request["details"]:
        item = dict(raw)
        item["origin"] = Point3(**item["origin"])
        item["direction"] = Vector3(**item["direction"])
        item["bounds"] = Bounds2(**item["bounds"])
        item["source_ids"] = tuple(item["source_ids"])
        item["notes"] = tuple(item.get("notes", ()))
        specs.append(DetailSpec(**item))
    return tuple(specs)


def create_detail_sheet_set(model_path, details_path, profile_path, job_path, *, output_directory):
    """Real shared company sheet pipeline; preflight all bodies before output."""
    import json
    from pathlib import Path
    from .company_sheets import resolve_company_sheet_set, create_company_sheet_set
    model_bytes = Path(model_path).read_bytes()
    request_bytes = Path(details_path).read_bytes()
    model = BuildingModel.from_json(model_bytes)
    request = json.loads(request_bytes)
    specs = detail_specs_from_dict(request)
    resolved = resolve_company_sheet_set(profile_path, job_path)
    bodies = company_detail_bodies(model, specs, resolved.sheets)
    import hashlib
    import os
    import tempfile
    destination = Path(output_directory)
    if destination.exists():
        raise ValueError("detail output directory must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".detail-stage-", dir=destination.parent) as staging:
        package = Path(staging) / "package"
        create_company_sheet_set(profile_path, job_path, output_directory=package, bodies=bodies)
        retained = {"detail-model.json": model_bytes,
                    "detail-request.json": request_bytes}
        for name, data in retained.items():
            (package / name).write_bytes(data)
        manifest = {"version": "detail-sheet-set@1", "model_id": model.model_id,
                    "recipes": {s.id: s.recipe for s in sorted(specs, key=lambda s:s.number)},
                    "source_ids": {s.id: sorted(s.source_ids) for s in specs},
                    "view_inputs": request["details"],
                    "inputs": {name: hashlib.sha256(data).hexdigest() for name,data in retained.items()},
                    "status": "supplied facts only; no engineering approval"}
        (package / "detail-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2)+"\n")
        os.rename(package, destination)
    return destination
