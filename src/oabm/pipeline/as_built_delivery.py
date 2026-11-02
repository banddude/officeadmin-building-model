"""One-command snapshot-bound as-built review delivery, with no network calls."""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import html
import io
import json
import os
from pathlib import Path
import tempfile

from oabm.model import BuildingModel, PhysicalRoute, stable_id
from oabm.quantities import extract_quantities
from oabm.qa.revision_diff import build_revision_diff
from .as_built import (_index, _json, _require, _sha,
                       apply_transaction, replay_history, snapshot)


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: _require(False, "nonfinite JSON"))


def installed_materials(history):
    """Partition real takeoff lines by exact, still-current field confirmations.

    Absence of installation testimony is an unresolved material, not permission
    to call designed quantities installed. Curved routes remain geometric-only.
    """
    model = replay_history(history)
    document = model.to_dict()
    confirmed = {}
    for retained in history["transactions"]:
        request = retained["request"]
        for item in request["installed_assertions"]:
            current = _index(document, item["collection"]).get(item["id"])
            if current is not None and _sha(_json(current)) == item["record_sha256"]:
                confirmed[item["id"]] = {"transaction_id": request["transaction_id"],
                    "actor": request["actor"], "evidence_ids": item["evidence_ids"],
                    "record_sha256": item["record_sha256"]}
    physical = {route.id for route in model.routes if isinstance(route, PhysicalRoute)}
    installed, unresolved = [], []
    takeoff = extract_quantities(model)
    for item in takeoff.items:
        row = item.to_dict()
        sources = set(item.source_entity_ids)
        # A route's installed product must have its own confirmation; asserting
        # geometry cannot turn its existing designed selection into an installation.
        dependencies = set(sources)
        for selection in (*model.raceway_selections, *model.fitting_selections):
            if getattr(selection, "route_id", None) in sources or getattr(selection, "fitting_id", None) in sources:
                dependencies.add(selection.id)
                dependencies.update(interface.id for interface in model.fitting_interfaces
                                    if interface.selection_id == selection.id)
        reasons = []
        if sources & physical:
            reasons.append("physical_route_installed_product_contract_unavailable")
        routes = {route.id for route in model.routes}
        selected_routes = {selection.route_id for selection in model.raceway_selections}
        if (sources & routes) - selected_routes - physical:
            reasons.append("missing_supplied_legacy_route_product")
        fitting_ids = {fitting.id for fitting in model.route_fittings}
        selected_fittings = {selection.fitting_id for selection in model.fitting_selections}
        if (sources & fitting_ids) - selected_fittings:
            reasons.append("missing_supplied_fitting_product")
        missing = sorted(dependencies - set(confirmed))
        if missing:
            reasons.append("missing_current_field_installation_confirmation")
        row.update({"installation_evidence": {identity: confirmed[identity]
                    for identity in sorted(dependencies & set(confirmed))},
                    "unconfirmed_entity_ids": missing, "reason_codes": reasons})
        (unresolved if reasons else installed).append(row)
    return {"model_sha256": _sha(snapshot(model)), "installed_items": installed,
            "unresolved_items": unresolved,
            "quantity_warnings": [warning.to_dict() for warning in takeoff.warnings],
            "limitations": ["Field assertions are supplied testimony; source authenticity is not verified.",
                "No prices, labor, demolition, purchased stock, slack or waste are inferred.",
                "Designed facts without current field confirmation are unresolved.",
                "Physical-route lengths are geometric measurements, not installed product quantities."]}


def _csv(items):
    from oabm.reports.physical_routes import _text_cell
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("status", "category", "item_type", "quantity", "unit", "variant", "source_ids", "reasons"))
    for status in ("installed", "unresolved"):
        for row in items[status + "_items"]:
            writer.writerow([_text_cell(value) for value in (status, row["category"], row["item_type"],
                row["quantity"], row["unit"], _json(row["variant"]),
                _json(row["source_entity_ids"]), ", ".join(row["reason_codes"]))])
    return output.getvalue()


def _drawings(model, revision):
    """Actual scaled vector plan sheets and paginated panel schedule sheets."""
    from oabm.drawings import (DrawingSet, PlanSpec, SheetSpec, ViewPlacement,
                               Bounds2, Point2, Symbol, generate_drawing_set)
    from oabm.drawings.sheet_svg import SheetSymbol, render_sheet_svg
    from oabm.drawings.schedule_set import panel_schedule_drawing_set
    from oabm.drawings.schedule_layout import paginate_schedules
    _require(bool(model.levels), "plan delivery requires a supplied canonical level")
    # A declared location marker is a review convention, never manufacturer CAD
    # or an invented device envelope. Canonical supplied sizes are drawn too.
    marker = SheetSymbol(((Point2(-1, 0), Point2(1, 0)),
                          (Point2(0, -1), Point2(0, 1))))
    symbols = {"as-built-location": marker, "support_attachment": marker}
    plan = generate_drawing_set(model, plans=tuple(
        PlanSpec(stable_id("as-built-plan", level.id), level.id,
                 cut_height_m=1.2, view_depth_below_m=1000, view_depth_above_m=1000)
        for level in model.levels), include_schedules=False,
        symbol_provider=lambda entity, view_type: Symbol("as-built-location"),
        physical_geometry="xy-centerline", assembly_geometry="cut")
    outputs = {"drawings/plans.json": plan.to_json()}
    for ordinal, view in enumerate(plan.views, 1):
        spec = SheetSpec(f"AB-{ordinal}", 1189, 841, titleblock_height_mm=35,
                         title="As-built field review plan", revision=revision)
        sheet = render_sheet_svg(plan, spec, (ViewPlacement(
            f"placement-{ordinal}", view.id, 15, 15),), symbols=symbols)
        outputs[f"drawings/plan-{ordinal}.svg"] = sheet.svg
    report, schedules = panel_schedule_drawing_set(model)
    _require(bool(report.panels), "panel delivery requires a supplied panelboard")
    template = SheetSpec("AB-P", 1189, 841, titleblock_height_mm=35,
                         title="As-built supplied panel facts", revision=revision)
    pages = paginate_schedules(schedules, template, empty_schedule_sources={
        stable_id("schedule", f"panel-facts:{panel.panel.id}"): panel.panel.id
        for panel in report.panels if not panel.circuits})
    for ordinal, page in enumerate(pages, 1):
        spec = replace(page.spec, titleblock_height_mm=35,
                       title="As-built supplied panel facts", revision=revision)
        # Schedule borders sit on their model-view boundary; reserve stroke
        # extents explicitly for the strict physical-sheet SVG consumer.
        views = tuple(replace(view, bounds=Bounds2(view.bounds.min_x - .001,
            view.bounds.min_y - .001, view.bounds.max_x + .001, view.bounds.max_y + .001))
            for view in page.drawing_set.views)
        projected = replace(page.drawing_set, views=views)
        outputs[f"drawings/panel-{ordinal}.svg"] = render_sheet_svg(
            projected, spec, (page.placement,)).svg
    outputs["drawings/panel-facts.json"] = report.to_json()
    outputs["drawings/panel-schedules.json"] = schedules.to_json()
    return outputs


def create_as_built_delivery(history_path, transaction_path, *, output_directory):
    """Publish only after all canonical, reporting and export operations succeed.

    A fresh directory is exclusively reserved at publication; its complete
    payload becomes visible in a single directory rename. A failed exporter
    leaves no successful manifest or partly generated public delivery.
    """
    output = Path(output_directory).absolute()
    _require(not output.exists() and not output.is_symlink(), "fresh output directory required")
    history_raw = Path(history_path).read_bytes()
    request_raw = Path(transaction_path).read_bytes()
    history = _decode(history_raw)
    request = _decode(request_raw)
    parent = replay_history(history)
    revised_history = apply_transaction(history, request)
    current = replay_history(revised_history)
    design = BuildingModel.from_json(history["design_json"])
    current_json = snapshot(current)
    materials = installed_materials(revised_history)
    parent_diff = build_revision_diff(parent, current).to_dict()
    design_diff = build_revision_diff(design, current).to_dict()
    payloads = _drawings(current, request["transaction_id"])
    payloads.update({"canonical-model.json": current_json,
        "design-model.json": history["design_json"], "parent-model.json": snapshot(parent),
        "history.json": _json(revised_history), "materials.json": _json(materials),
        "materials.csv": _csv(materials), "parent-differences.json": _json(parent_diff),
        "design-differences.json": _json(design_diff)})
    title = html.escape(request["transaction_id"])
    payloads["index.html"] = ("<!doctype html><meta charset='utf-8'><title>As-built review</title>"
        f"<h1>As-built {title}</h1><p>Supplied field testimony and unresolved materials require review.</p>"
        + "".join(f'<p><a href="{html.escape(name, quote=True)}">{html.escape(name)}</a></p>'
                  for name in sorted(payloads)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".as-built-", dir=output.parent) as temporary:
        stage = Path(temporary) / "delivery"
        stage.mkdir()
        files = {}
        for name, text in sorted(payloads.items()):
            raw = text.encode("utf-8")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                           "revised_model_sha256": _sha(current_json)}
        for name, raw in (("inputs/history.json", history_raw), ("inputs/transaction.json", request_raw)):
            target = stage / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
            files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                           "revised_model_sha256": _sha(current_json)}
        manifest = {"version": "oabm-as-built-delivery-v1", "status": "complete",
            "transaction_id": request["transaction_id"], "design_sha256": history["design_sha256"],
            "parent_sha256": _sha(snapshot(parent)), "revised_model_sha256": _sha(current_json),
            "input_history_sha256": hashlib.sha256(history_raw).hexdigest(),
            "input_transaction_sha256": hashlib.sha256(request_raw).hexdigest(), "files": files,
            "review_status": "unreviewed", "installed_product_scope": "explicit field-confirmed legacy inventory only"}
        (stage / "MANIFEST.json").write_text(_json(manifest))
        output.mkdir()  # exclusive reservation; never replace somebody else's attempt
        try:
            os.rename(stage, output / "delivery")
        except BaseException:
            output.rmdir()  # only our empty reservation, never an existing delivery
            raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history")
    parser.add_argument("transaction")
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args(argv)
    manifest = create_as_built_delivery(args.history, args.transaction, output_directory=args.output_directory)
    print(_json(manifest))


if __name__ == "__main__":
    main()
