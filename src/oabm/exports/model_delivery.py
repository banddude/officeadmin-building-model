"""One canonical-model input to a fresh, hash-bound plan/GLB review delivery."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform

import oabm
from oabm.drawings import PlanSpec, generate_drawing_set, paper_text_height_m, view_to_svg
from oabm.model import BuildingModel, ContractError, validate_model
from oabm.qa.route_bends import check_route_bends
from .gltf import to_glb

VERSION = "model-delivery-v1"
LIMITATIONS = (
    "Review outputs only; no engineering, code, survey or manufacturer-fit approval.",
    "Bend checks support interior sharp-vertex turns per route, not consolidated cross-route junction bends.",
    "Consolidated endpoint elbows can report elbow_off_turn despite valid canonical topology; do not remove them to pass.",
    "Bend checks do not realize curved geometry or evaluate swept clearance, terminal approach or internal fit.",
    "Plan SVGs are model views, not composed physical permit sheets; label suppression is reported in drawing metadata.",
)
PLAN_OPTIONS = {"dimension_decimals": 2, "label_overlap": "skip_with_dimensions",
                "dimension_overlap": "skip_shorter", "include_schedules": False}


def _json(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"duplicate delivery option key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except ContractError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise ContractError("invalid delivery options JSON") from exc


def _options(data):
    value = _decode(data)
    keys = {"version", "plans", "preview_glb", "bend_policy", "radii_m"}
    if not isinstance(value, dict) or set(value) != keys or value["version"] != VERSION:
        raise ContractError("exact model-delivery-v1 options required")
    if type(value["preview_glb"]) is not bool or value["bend_policy"] not in ("require", "report", "skip"):
        raise ContractError("explicit preview boolean and require/report/skip bend policy required")
    rows = value["plans"]
    if not isinstance(rows, list) or len(rows) > 16:
        raise ContractError("plans must be a list of at most 16 explicit plan specifications")
    if not rows and not value["preview_glb"]:
        raise ContractError("request at least one plan or GLB preview")
    plans = []
    fields = {"id", "level_id", "cut_height_m", "view_depth_below_m", "view_depth_above_m", "scale"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise ContractError("plan requires exact id, level_id, cut/depth and scale fields")
        if any(not isinstance(row[k], str) or not row[k].strip() for k in ("id", "level_id")):
            raise ContractError("plan id and level_id must be nonempty strings")
        for key in fields - {"id", "level_id"}:
            number = row[key]
            try:
                valid = type(number) in (int, float) and math.isfinite(number) and number >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ContractError("plan distances and scale must be finite nonnegative numbers")
        try:
            plans.append(PlanSpec(**row))
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
    if len({plan.id for plan in plans}) != len(plans):
        raise ContractError("duplicate plan IDs")
    radii = value["radii_m"]
    if not isinstance(radii, dict):
        raise ContractError("radii_m must be an explicit object")
    if value["bend_policy"] == "skip" and radii:
        raise ContractError("skip policy requires empty radii_m to avoid ignored assumptions")
    return value, tuple(sorted(plans, key=lambda p: p.id))


def _runtime():
    root = Path(oabm.__file__).parent
    sources = [{"path": path.relative_to(root).as_posix(), "sha256": _sha(path.read_bytes())}
               for path in sorted(root.rglob("*.py"))]
    try:
        package_version = metadata.version("officeadmin-building-model")
    except metadata.PackageNotFoundError:
        package_version = None
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.system(), "distribution_version": package_version,
            "module_version": oabm.__version__, "engine_sources": sources,
            "engine_sources_sha256": _sha(_json(sources))}


class ModelDeliveryError(ContractError):
    """Failed attempt retained without a published delivery; use a fresh path."""
    def __init__(self, attempt_directory, stage, cause):
        self.attempt_directory = Path(attempt_directory)
        self.stage = stage
        super().__init__(f"model delivery failed at {stage}; retained attempt: {attempt_directory}; {cause}")


def deliver_model(model_path, options_path, *, output_directory) -> Path:
    """Snapshot two inputs once, validate, export, then publish by directory rename.

    The output directory is an exclusive fresh attempt. Successful artifacts live
    only in its delivery/ subdirectory with MANIFEST.json. Failed attempts retain
    .pending/ and failure.json, never a completed delivery. Existing paths are
    not overwritten or resumed. All exporters receive the same validated model
    reconstructed from the retained input bytes. No network/provider is called.
    """
    model_bytes, options_bytes = Path(model_path).read_bytes(), Path(options_path).read_bytes()
    options, plans = _options(options_bytes)
    model = BuildingModel.from_json(model_bytes)
    validate_model(model)
    if any(plan.level_id not in {level.id for level in model.levels} for plan in plans):
        raise ContractError("plan references an unknown canonical level")
    attempt = Path(output_directory).absolute()
    attempt.parent.mkdir(parents=True, exist_ok=True)
    attempt.mkdir()  # exclusive reservation; never touch an existing attempt
    pending = attempt / ".pending"
    stage = "snapshot"
    try:
        pending.mkdir()
        receipts = []

        def write(name, data, kind):
            path = pending / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
            receipts.append({"path": name, "kind": kind, "bytes": len(data), "sha256": _sha(data)})

        write("inputs/model.json", model_bytes, "input_model")
        write("inputs/options.json", options_bytes, "input_options")
        canonical = model.to_json(indent=None).encode()
        canonical_sha = _sha(canonical)
        write("canonical-model.json", canonical, "validated_canonical_model")
        stage = "bend_preflight"
        bend_state = "not_evaluated"
        if options["bend_policy"] != "skip":
            report = check_route_bends(model, radii_m=options["radii_m"])
            write("bend-report.json", report.to_json().encode(), "bend_report")
            bend_state = "passed_bounded_checks" if report.ready else "needs_review"
            if not report.ready and options["bend_policy"] == "require":
                raise ContractError("required bend preflight unresolved; inspect retained bend-report.json")
        status = {"outputs": "generated_review_outputs", "bend_policy": options["bend_policy"],
                  "bend_check": bend_state, "engineering_approval": False}
        write("READ-ME.txt", ("MODEL REVIEW DELIVERY\n" + f"Bend check: {bend_state}\n" +
                              "\n".join(LIMITATIONS) + "\n").encode(), "readiness_notice")
        stage = "plans"
        if plans:
            drawings = generate_drawing_set(model, plans=plans, **PLAN_OPTIONS)
            write("drawing-set.json", (drawings.to_json()+"\n").encode(), "derived_drawings")
            for number, view in enumerate(drawings.views, 1):
                svg = view_to_svg(view, pixels_per_model_unit=100,
                                  text_height_m=paper_text_height_m(view.scale))
                write(f"plans/plan-{number:02}.svg", svg.encode(), "plan_svg")
        stage = "preview_glb"
        if options["preview_glb"]:
            path = pending / "preview.glb"
            to_glb(model, path)  # original exporter and original default geometry
            data = path.read_bytes()
            receipts.append({"path": "preview.glb", "kind": "preview_glb",
                             "bytes": len(data), "sha256": _sha(data)})
        stage = "verify_exports"
        if _sha(model.to_json(indent=None).encode()) != canonical_sha:
            raise ContractError("exporter mutated the canonical model")
        if {p.relative_to(pending).as_posix() for p in pending.rglob("*") if p.is_file()} != {r["path"] for r in receipts}:
            raise ContractError("unexpected file in pending delivery")
        for row in receipts:
            data = (pending / row["path"]).read_bytes()
            if len(data) != row["bytes"] or _sha(data) != row["sha256"]:
                raise ContractError("delivery artifact changed before publication")
        stage = "manifest"
        manifest = {"version": VERSION, "canonical_model_sha256": canonical_sha,
                    "model_id": model.model_id, "status": status, "limitations": LIMITATIONS,
                    "effective_plans": [asdict(plan) for plan in plans],
                    "plan_options": PLAN_OPTIONS if plans else None,
                    "svg_options": {"pixels_per_model_unit": 100, "paper_text_height_m": .0025} if plans else None,
                    "glb_options": "existing_exporter_defaults" if options["preview_glb"] else None,
                    "plan_files": [{"view_id": p.id, "path": f"plans/plan-{i:02}.svg"} for i,p in enumerate(plans,1)],
                    "runtime": _runtime(), "files": sorted(receipts, key=lambda r:r["path"])}
        (pending / "MANIFEST.json").write_bytes(_json(manifest))
        stage = "publish"
        os.rename(pending, attempt / "delivery")
        return attempt / "delivery"
    except Exception as exc:
        (attempt / "failure.json").write_bytes(_json({"version": VERSION, "stage": stage,
                                                      "error_type": type(exc).__name__}))
        raise ModelDeliveryError(attempt, stage, exc) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("options", type=Path)
    parser.add_argument("output_directory", type=Path, help="fresh attempt directory, never overwritten")
    args = parser.parse_args(argv)
    try:
        output = deliver_model(args.model, args.options, output_directory=args.output_directory)
    except (OSError, ValueError, ContractError) as exc:
        parser.exit(1, f"model delivery: {exc}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
