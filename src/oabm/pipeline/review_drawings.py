"""Opt-in review plans and supplied panel facts from one canonical snapshot."""
from __future__ import annotations

from dataclasses import dataclass, replace
import html
import json
import math

from oabm.model import ContractError
from oabm.proposals.contracts import Portable, digest, text
from oabm.drawings import (Bounds2, PlanSpec, VisibilityPolicy, compact_label_provider,
    generate_drawing_set, view_to_svg, paper_text_height_m)
from oabm.drawings.generator import _entity_points
from oabm.drawings.panel_schedules import panel_report_to_drawing_schedules
from oabm.reports import generate_panel_schedule_report
from oabm.exports.review_bundle import canonical_model_sha256

VERSION = "review-electrical-drawings-v1"
BANNER = "INCOMPLETE REVIEW - NOT FOR PERMIT OR CONSTRUCTION"


def _finite(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError(label + " requires a finite number")
    if value < 0 or (positive and value == 0):
        raise ContractError(label + " requires a positive/nonnegative number")
    return float(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewPlan(Portable):
    id: str
    level_id: str
    bounds_m: tuple[float, float, float, float]
    cut_height_m: float
    view_depth_below_m: float
    view_depth_above_m: float
    scale: float
    show_architecture: bool
    show_spaces: bool
    show_routes: bool
    show_dimensions: bool

    def __post_init__(self):
        text(self.id, "review view ID")
        text(self.level_id, "review level ID")
        if not isinstance(self.bounds_m, tuple) or len(self.bounds_m) != 4:
            raise ContractError("explicit four-coordinate plan bounds required")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in self.bounds_m):
            raise ContractError("plan bounds must be finite numbers")
        object.__setattr__(self, "bounds_m", tuple(float(v) for v in self.bounds_m))
        Bounds2(min_x=self.bounds_m[0], min_y=self.bounds_m[1], max_x=self.bounds_m[2], max_y=self.bounds_m[3])
        for key in ("cut_height_m", "view_depth_below_m", "view_depth_above_m", "scale"):
            object.__setattr__(self, key, _finite(getattr(self, key), key, positive=key == "scale"))
        for key in ("show_architecture", "show_spaces", "show_routes", "show_dimensions"):
            if not isinstance(getattr(self, key), bool):
                raise ContractError(key + " must be explicit boolean")

    def spec(self):
        return PlanSpec(id=self.id, level_id=self.level_id,
            bounds=Bounds2(min_x=self.bounds_m[0], min_y=self.bounds_m[1], max_x=self.bounds_m[2], max_y=self.bounds_m[3]),
            cut_height_m=self.cut_height_m, view_depth_below_m=self.view_depth_below_m,
            view_depth_above_m=self.view_depth_above_m, scale=self.scale,
            visibility=VisibilityPolicy(architecture=self.show_architecture, electrical=True,
                spaces=self.show_spaces, routes=self.show_routes, obstacles=False, constraints=False,
                labels=True, dimensions=self.show_dimensions))


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDrawingConfig(Portable):
    version: str
    plans: tuple[ReviewPlan, ...]
    pixels_per_model_unit: float

    def __post_init__(self):
        if self.version != VERSION:
            raise ContractError("unsupported review drawing version")
        if not isinstance(self.plans, tuple) or any(not isinstance(p, ReviewPlan) for p in self.plans):
            raise ContractError("explicit review plan tuple required")
        if len(self.plans) > 64 or len({p.id for p in self.plans}) != len(self.plans):
            raise ContractError("at most 64 uniquely identified review plans supported")
        object.__setattr__(self, "pixels_per_model_unit", _finite(self.pixels_per_model_unit, "render density", positive=True))

    @property
    def artifact_kinds(self):
        return ("drawing_set", "panel_facts", *("review_plan:" + p.id for p in sorted(self.plans, key=lambda p: p.id)))


def _svg(view, config):
    # A separate visible paper banner; no fabricated canonical geometry or IDs.
    width = max(720.0, view.bounds.width * config.pixels_per_model_unit)
    height = view.bounds.height * config.pixels_per_model_unit
    if not all(math.isfinite(v) and v > 0 for v in (width, height,
            height + 64, paper_text_height_m(view.scale) * config.pixels_per_model_unit)):
        raise ContractError("review SVG display extent must remain finite and positive")
    body = view_to_svg(view, pixels_per_model_unit=config.pixels_per_model_unit,
                       text_height_m=paper_text_height_m(view.scale))
    body = body[body.index(">", body.index("<svg "))+1:body.rindex("</svg>")]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.6f} {height+64:.6f}" data-review-status="incomplete" data-view-id="{html.escape(view.id)}" data-view-type="plan">\n'
        f'<rect width="{width:.6f}" height="64" fill="white"/>\n'
        f'<text x="12" y="22" font-size="16" fill="black">{html.escape(BANNER)}</text>\n'
        f'<text x="12" y="45" font-size="12" fill="black">{html.escape(view.title.split(" | ", 1)[-1])}; supplied model facts only</text>\n'
        f'<defs><clipPath id="review-window"><rect width="{view.bounds.width*config.pixels_per_model_unit:.6f}" height="{height:.6f}"/></clipPath></defs>\n'
        '<g transform="translate(0 64)" clip-path="url(#review-window)">'
        + body + '</g>\n</svg>\n').encode()


def generate_review_drawings(model, config):
    """Return supplemental bytes and an honest visibility/coverage report."""
    if not isinstance(config, ReviewDrawingConfig):
        raise ContractError("typed review drawing configuration required")
    config = ReviewDrawingConfig.from_dict(config.to_dict())
    before = canonical_model_sha256(model)
    levels = {level.id: level for level in model.levels}
    if any(p.level_id not in levels for p in config.plans):
        raise ContractError("requested review plan references missing canonical level")
    plans = tuple(p.spec() for p in sorted(config.plans, key=lambda p: p.id))
    drawing = generate_drawing_set(model, plans=plans, include_schedules=False,
        label_provider=compact_label_provider)
    facts = generate_panel_schedule_report(model)
    facts.validate_against_model(model)
    schedules = panel_report_to_drawing_schedules(facts)
    schedules = tuple(replace(s, title=BANNER + " | " + s.title) for s in schedules)
    drawing = replace(drawing, schedules=schedules,
        views=tuple(replace(v, title=BANNER + " | " + v.title,
            metadata=(*v.metadata, ("review_status", "incomplete"), ("permit_ready", "false"),
                      ("canonical_model_sha256", before))) for v in drawing.views))
    candidates = {e.id: e for collection in (model.electrical_devices, model.electrical_equipment,
        model.electrical_boxes, model.support_components, model.support_interfaces,
        model.routes, model.route_fittings) for e in collection}
    visibility = []
    payloads = {"panel_facts": json.dumps({"review": {"status": "incomplete_review",
        "permit_ready": False, "canonical_model_sha256": before,
        "source_coverage": "unavailable"}, "panel_facts": facts.to_dict()},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()}
    for view, plan in zip(drawing.views, sorted(config.plans, key=lambda p: p.id)):
        shown = {s for p in view.primitives if p.layer != "annotations:labels" for s in p.source_ids} & candidates.keys()
        clipped = []
        low = levels[plan.level_id].elevation_m - plan.view_depth_below_m
        high = levels[plan.level_id].elevation_m + plan.cut_height_m + plan.view_depth_above_m
        for identity in sorted(shown):
            if any(not (plan.bounds_m[0] <= p.x <= plan.bounds_m[2]
                        and plan.bounds_m[1] <= p.y <= plan.bounds_m[3] and low <= p.z <= high)
                   for p in _entity_points(candidates[identity])):
                clipped.append(identity)
        visibility.append({"view_id": view.id, "level_id": plan.level_id,
            "depth_interval_m": [low, high], "bounds_m": list(plan.bounds_m),
            "rendered_electrical_ids": sorted(shown), "clipped_electrical_ids": clipped,
            "omitted_electrical_ids": sorted(candidates.keys() - shown),
            "clipped_meaning": "rendered source extents cross requested XY or depth; solid depth selection does not imply solid geometry clipping",
            "omitted_meaning": "no non-label primitive in this view; may be other level, depth, bounds, disabled routes or unsupported presentation"})
        payloads["review_plan:" + view.id] = _svg(view, config)
    status = {"version": VERSION, "status": "incomplete_review", "permit_ready": False,
        "canonical_model_sha256": before, "config_fingerprint": digest(config.to_dict()),
        "source_coverage": "unavailable", "physical_completeness": "not_verified",
        "panel_facts_scope": "listed canonical circuits only; missing facts are not source zeroes",
        "panel_count": len(facts.panels), "listed_circuit_count": len(model.circuits),
        "unassigned_circuit_ids": [r.circuit.id for r in facts.unassigned_circuits],
        "views": visibility, "no_plan_views_requested": not bool(config.plans),
        "no_canonical_levels": not bool(model.levels)}
    payloads["drawing_set"] = json.dumps({"review": status, "drawing_set": drawing.to_dict()},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if before != canonical_model_sha256(model):
        raise ContractError("review drawing generation mutated canonical model")
    return payloads, status
