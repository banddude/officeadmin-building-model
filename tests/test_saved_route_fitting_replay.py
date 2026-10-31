"""Ephemeral synthetic fixtures for byte-bound route-fitting replay."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os

import pytest

from oabm.drawings import PlanSpec, generate_drawing_set
from oabm.electrical.catalog_manifest import CatalogManifest
from oabm.electrical.fittings import select_route_fittings
from oabm.exports.gltf import build_glb_bytes
from oabm.ifc.adapter import to_ifc
from oabm.model import BuildingModel, ContractError
from oabm.pipeline.saved_route_fitting_replay import VERSION, replay_saved_route_fitting
from oabm.quantities import extract_quantities

from test_route_fittings import build_model, catalog_manifest, rule_for


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write(root, relative, payload):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {"path": relative, "bytes": len(payload), "sha256": _sha(payload)}


def _input_set(*, envelope=True, check_actual_od=False):
    base, intent, product = build_model("connector", with_box=True)
    base = replace(base, name="Synthetic replay model")
    manifest = catalog_manifest()
    if envelope:
        document = json.loads(manifest.raw)
        entry = next(row for row in document["entries"] if row["product_id"] == product.product_id)
        entry["fields"]["size"] = {"x": 0.2, "y": 0.1, "z": 0.05}
        entry["geometry_basis"] = "dimension_derived_envelope"
        entry["evidence"].append({"field": "size", "source_id": "synthetic-source",
                                  "derivation": "observed", "confidence": 0.9})
        manifest = CatalogManifest(_dump(document))
    rule = rule_for(product, "connector", check_actual_od=check_actual_od)
    plans = (PlanSpec(id="plan:replay", level_id="level:one", cut_height_m=1.0,
                      view_depth_below_m=0.2, view_depth_above_m=0.2, scale=40.0),)
    return base, intent, rule, manifest, plans


def _documents(base, intent, rules, catalog, plans):
    return {
        "base_model": base.to_json(indent=None).encode(),
        "catalog_manifest": catalog.raw,
        "intents": _dump({"version": "saved-route-fitting-intents-v1", "items": [asdict(intent)]}),
        "rules": _dump({"version": "saved-route-fitting-rules-v1", "items": [asdict(row) for row in rules]}),
        "drawing_plans": _dump({"version": "saved-route-fitting-plans-v1", "items": [
            {"id": row.id, "level_id": row.level_id, "cut_height_m": row.cut_height_m,
             "view_depth_below_m": row.view_depth_below_m, "view_depth_above_m": row.view_depth_above_m,
             "scale": row.scale} for row in plans]}),
    }


def _outputs(base, intent, rules, catalog, plans):
    selected = select_route_fittings(base, intents=(intent,), catalog=catalog, rules=rules)
    return {
        "canonical_model": selected.to_json(indent=None).encode(),
        "quantities": extract_quantities(selected).to_json(indent=None).encode(),
        "glb": build_glb_bytes(selected),
        "ifc": to_ifc(selected).to_string().encode(),
        "drawings": generate_drawing_set(selected, plans=plans).to_json(indent=None).encode(),
    }


def _saved_tree(root, *, include_rule=True, preselected_base=False, check_actual_od=False, envelope=True):
    base, intent, rule, catalog, plans = _input_set(envelope=envelope)
    current_rule = rule_for(rule.permitted_products[0], "connector", check_actual_od=check_actual_od)
    rules = (current_rule,) if include_rule else ()
    if preselected_base:
        # The pinned canonical base already contains the original producer
        # result under the default false OD-check policy; replay applies the
        # currently pinned policy below and reports whether decision rows move.
        base = select_route_fittings(base, intents=(intent,), catalog=catalog, rules=(rule,))
    input_payloads = _documents(base, intent, rules, catalog, plans)
    input_refs = {name: _write(root, "inputs/" + name + ".bin", payload)
                  for name, payload in input_payloads.items()}
    output_payloads = _outputs(base, intent, rules, catalog, plans)
    output_refs = {name: _write(root, "expected/" + name + ".bin", payload)
                   for name, payload in output_payloads.items()}
    manifest = {"version": VERSION, "inputs": input_refs, "expected_outputs": output_refs}
    path = root / "replay.json"
    path.write_bytes(_dump(manifest))
    return path, manifest


def test_saved_fitting_replay_regenerates_all_fresh_bytes_twice(tmp_path):
    path, _manifest = _saved_tree(tmp_path)
    first = replay_saved_route_fitting(path)
    second = replay_saved_route_fitting(path)
    assert first["freshly_regenerated_outputs"] == ["canonical_model", "drawings", "glb", "ifc", "quantities"]
    assert first["output_receipts"] == second["output_receipts"]
    assert first["comparison"] == "exact fresh byte comparison; no cached output reuse"
    assert first["source_authenticity_verified"] is False
    assert first["ifc_header_timestamp_policy"].startswith("existing IFC exporter pins")
    assert first["application_mode"] == "first_application"
    assert first["base_fitting_inventory"] == {
        "route_fittings": 1, "selections": 0, "interfaces": 0, "outcomes": 0,
    }


def test_saved_fitting_replay_reports_idempotent_selected_base_and_matches_first_apply(tmp_path):
    first_root = tmp_path / "first"
    first_root.mkdir()
    first_path, _ = _saved_tree(first_root)
    first = replay_saved_route_fitting(first_path)
    rerun_root = tmp_path / "rerun"
    rerun_root.mkdir()
    rerun_path, _ = _saved_tree(rerun_root, preselected_base=True)
    rerun = replay_saved_route_fitting(rerun_path)
    assert rerun["application_mode"] == "idempotent_reapplication"
    assert rerun["base_fitting_inventory"] == {
        "route_fittings": 1, "selections": 1, "interfaces": 2, "outcomes": 1,
    }
    assert rerun["output_receipts"] == first["output_receipts"]


def test_saved_fitting_replay_reports_changed_reapplication_policy(tmp_path):
    path, manifest = _saved_tree(tmp_path, preselected_base=True, check_actual_od=True)
    receipt = replay_saved_route_fitting(path)
    assert receipt["application_mode"] == "reapplication_with_changes"
    reference = manifest["expected_outputs"]["canonical_model"]
    canonical = json.loads((tmp_path / reference["path"]).read_bytes())
    outcome = canonical["fitting_selection_outcomes"][0]
    assert outcome["status"] == "unresolved"
    assert "actual_od_unknown" in outcome["reason_codes"]


def test_saved_fitting_replay_regenerates_explicit_unresolved_decision(tmp_path):
    path, manifest = _saved_tree(tmp_path, include_rule=False)
    replay_saved_route_fitting(path)
    reference = manifest["expected_outputs"]["canonical_model"]
    canonical = json.loads((tmp_path / reference["path"]).read_bytes())
    assert canonical["fitting_selection_outcomes"][0]["status"] == "unresolved"
    assert "missing_rule_reference" in canonical["fitting_selection_outcomes"][0]["reason_codes"]


def test_saved_fitting_replay_rejects_rehashed_but_wrong_expected_glb(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    reference = manifest["expected_outputs"]["glb"]
    bad = (tmp_path / reference["path"]).read_bytes() + b"invalid"
    (tmp_path / reference["path"]).write_bytes(bad)
    reference["bytes"] = len(bad)
    reference["sha256"] = _sha(bad)
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="freshly regenerated glb bytes differ"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_rejects_unknown_manifest_field(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    manifest["additional_sharing_target"] = "not accepted"
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="requires exactly these keys"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_refuses_input_path_escape(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    manifest["inputs"]["base_model"]["path"] = "../outside.json"
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="escapes"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_rejects_nonstring_path_before_deduplication(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    manifest["inputs"]["base_model"]["path"] = {"not": "a path"}
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="require string paths"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_rejects_noncanonical_dot_path_alias(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    manifest["inputs"]["base_model"]["path"] = "inputs/./base_model.bin"
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="path must be canonical"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_rejects_hardlinked_expected_output_alias(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    canonical_ref = manifest["expected_outputs"]["canonical_model"]
    canonical_path = tmp_path / canonical_ref["path"]
    alias_path = tmp_path / "expected" / "quantity-hardlink.bin"
    os.link(canonical_path, alias_path)
    alias_bytes = alias_path.read_bytes()
    manifest["expected_outputs"]["quantities"] = {
        "path": "expected/quantity-hardlink.bin", "bytes": len(alias_bytes), "sha256": _sha(alias_bytes),
    }
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="distinct physical files"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_rejects_input_expected_same_physical_file(tmp_path):
    path, manifest = _saved_tree(tmp_path, preselected_base=True)
    expected_ref = manifest["expected_outputs"]["canonical_model"]
    expected_path = tmp_path / expected_ref["path"]
    alias_path = tmp_path / "inputs" / "base-hardlink.json"
    os.link(expected_path, alias_path)
    payload = alias_path.read_bytes()
    manifest["inputs"]["base_model"] = {
        "path": "inputs/base-hardlink.json", "bytes": len(payload), "sha256": _sha(payload),
    }
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="distinct physical files"):
        replay_saved_route_fitting(path)


def test_saved_fitting_replay_requires_array_shaped_rule_policy(tmp_path):
    path, manifest = _saved_tree(tmp_path)
    reference = manifest["inputs"]["rules"]
    rules_path = tmp_path / reference["path"]
    document = json.loads(rules_path.read_bytes())
    document["items"][0]["required_listing_refs"] = "not-an-array"
    payload = _dump(document)
    rules_path.write_bytes(payload)
    reference["bytes"] = len(payload)
    reference["sha256"] = _sha(payload)
    path.write_bytes(_dump(manifest))
    with pytest.raises(ContractError, match="must be arrays"):
        replay_saved_route_fitting(path)


@pytest.mark.parametrize("envelope", [False, True])
def test_completion_reports_fresh_selected_geometry_and_unverified_claims(tmp_path, envelope):
    path, _ = _saved_tree(tmp_path, envelope=envelope)
    receipt = replay_saved_route_fitting(path)
    summary = receipt["fitting_completion"]
    assert summary["outcome_counts"] == {"selected": 1, "unresolved": 0}
    assert summary["selected_geometry_counts"] == {
        "dimension_derived_envelope": int(envelope), "metadata_only": int(not envelope),
    }
    assert summary["reason_counts"] == {}
    assert summary["outcomes"][0]["geometry_basis"] == (
        "dimension_derived_envelope" if envelope else "metadata_only")
    assert summary["outcomes"][0]["actual_od_check_performed"] is False
    assert receipt == replay_saved_route_fitting(path)
    for claim in ("source_authenticity_verified", "product_fit_verified",
                  "code_or_listing_compliance_verified", "native_cad_imported",
                  "engineering_approval"):
        assert receipt[claim] is False


def test_completion_uses_regenerated_unresolved_not_preselected_base(tmp_path):
    path, _ = _saved_tree(tmp_path, preselected_base=True, check_actual_od=True)
    receipt = replay_saved_route_fitting(path)
    assert receipt["base_fitting_inventory"]["selections"] == 1
    summary = receipt["fitting_completion"]
    assert summary["outcome_counts"] == {"selected": 0, "unresolved": 1}
    assert summary["selected_geometry_counts"] == {
        "dimension_derived_envelope": 0, "metadata_only": 0,
    }
    assert summary["reason_counts"] == {"actual_od_unknown": 1}
    assert summary["outcomes"][0]["reason_codes"] == ["actual_od_unknown"]
    assert summary["outcomes"][0]["selected_selection_id"] is None
    assert summary["outcomes"][0]["geometry_basis"] is None


def test_completion_summary_mixed_outcomes_is_order_independent(tmp_path):
    from oabm.pipeline.saved_route_fitting_replay import _fitting_completion_summary
    base, intent, rule, catalog, plans = _input_set(envelope=False)
    extra_fitting = replace(base.route_fittings[0], id="fitting:unresolved")
    base = replace(base, route_fittings=(*base.route_fittings, extra_fitting),
        routes=(replace(base.routes[0], fitting_ids=(*base.routes[0].fitting_ids, extra_fitting.id)),))
    extra_intent = replace(intent, intent_id="intent:unresolved",
        fitting_id=extra_fitting.id, bindings=(), requested_product=None, basis=None)
    model = select_route_fittings(base, intents=(extra_intent, intent),
                                  catalog=catalog, rules=(rule,))
    summary = _fitting_completion_summary(model)
    assert summary["outcome_counts"] == {"selected": 1, "unresolved": 1}
    assert summary["selected_geometry_counts"] == {
        "dimension_derived_envelope": 0, "metadata_only": 1,
    }
    assert summary["reason_counts"] == {
        "missing_requested_product": 1, "missing_rule_reference": 1,
    }
    assert [row["fitting_id"] for row in summary["outcomes"]] == ["fitting:one", "fitting:unresolved"]
    reordered = replace(model, fitting_selection_outcomes=tuple(reversed(model.fitting_selection_outcomes)))
    assert _dump(_fitting_completion_summary(reordered)) == _dump(summary)

    # Exercise the real byte-bound replay as well as summary ordering. Mixed
    # selected/unresolved inventory must survive canonical validation and every
    # exporter, without copying the selected base's claims.
    inputs = _documents(base, intent, (rule,), catalog, plans)
    inputs["intents"] = _dump({"version": "saved-route-fitting-intents-v1",
                              "items": [asdict(extra_intent), asdict(intent)]})
    outputs = {
        "canonical_model": model.to_json(indent=None).encode(),
        "quantities": extract_quantities(model).to_json(indent=None).encode(),
        "glb": build_glb_bytes(model),
        "ifc": to_ifc(model).to_string().encode(),
        "drawings": generate_drawing_set(model, plans=plans).to_json(indent=None).encode(),
    }
    manifest = {"version": VERSION,
        "inputs": {name: _write(tmp_path, "inputs/" + name + ".bin", payload)
                   for name, payload in inputs.items()},
        "expected_outputs": {name: _write(tmp_path, "expected/" + name + ".bin", payload)
                             for name, payload in outputs.items()}}
    path = tmp_path / "replay.json"
    path.write_bytes(_dump(manifest))
    receipt = replay_saved_route_fitting(path)
    assert receipt["fitting_completion"] == summary
