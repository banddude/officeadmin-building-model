"""Public synthetic physical review handoff. Display is not clearance proof."""
from dataclasses import replace
import hashlib

import pytest

from oabm.exports.gltf import build_glb_bytes
from oabm.exports.review_bundle import ReviewBundle, _normalize_glb_options, build_review_bundle
from oabm.model import Box3D, ContractError, Obstacle, Point3, Polyline3D, Pose, Route, Size3
from oabm.qa.confidence_review import (
    ConfidenceReviewManifest, EvidenceReference, ReviewConfig, build_confidence_review,
    fingerprint_provenance,
)
from physical_route_fixture import make_physical_route, make_physical_source, supplied
from test_review_bundle import _mutate_glb_binary, _rewrite_glb

OPTIONS = {"physical_routes": True, "include_obstacles": True}


def model_fixture(mixed=False):
    source = replace(make_physical_source(), obstacles=(
        Obstacle(id="obstacle:synthetic", obstacle_type="supplied-box",
                 geometry=Box3D(pose=Pose(position=Point3(x=1, y=1, z=0)),
                                size=Size3(x=0.2, y=0.2, z=0.2)),
                 provenance=supplied()),
    ))
    physical = make_physical_route(source)
    routes = (physical,)
    if mixed:
        routes += (Route(id="route:legacy", route_type="emt",
                         start_port_id="port:a", end_port_id="port:b",
                         centerline=Polyline3D(points=tuple(p.pose.position for p in source.ports))),)
    return replace(source, routes=routes)


def manifest_for(model, **kwargs):
    return build_confidence_review(model, ReviewConfig(0.9), physical_routes=True, **kwargs)


def bundle_for(model, payload, manifest, options=OPTIONS):
    return build_review_bundle(
        model, glb_bytes=payload, model_reference="synthetic/model.json",
        glb_reference="synthetic/model.glb", confidence_manifest=manifest,
        confidence_reference="synthetic/review.json",
        source_reference_mapping_reference="synthetic/source-map.json",
        source_references={("synthetic", "source:physical-fixture"): "synthetic/source"},
        glb_options=options,
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_physical_review_keeps_identity_provenance_and_explicit_quantity_limit(mixed, monkeypatch):
    model = model_fixture(mixed)
    before = model.to_json()
    def forbidden(*args, **kwargs):
        pytest.fail("physical review must not run or silently filter into a partial takeoff")
    monkeypatch.setattr("oabm.qa.confidence_review.extract_quantities", forbidden)
    record = model.routes[0].provenance[0]
    evidence = EvidenceReference(
        ref_id="crop:synthetic", entity_id=model.routes[0].id,
        source_kind=record.source_kind, source_id=record.source_id,
        fingerprint=fingerprint_provenance(record, entity_id=model.routes[0].id),
    )
    manifest = manifest_for(model, evidence=(evidence,))
    limit = next(i for i in manifest.items if "physical_route_quantities_unsupported" in i.reasons)
    assert limit.entity_ids == ("route:physical",)
    assert "entire model quantity pass was not evaluated" in limit.detail
    assert limit.crops[0]["fingerprint"] == evidence.fingerprint
    assert not any(r.startswith("quantity_warning_") for i in manifest.items for r in i.reasons)
    assert ConfidenceReviewManifest.from_dict(manifest.to_dict()).to_json() == manifest.to_json()
    payload = build_glb_bytes(model, **OPTIONS)
    bundle = bundle_for(model, payload, manifest)
    document = bundle.to_dict()
    assert document["artifacts"]["glb"]["sha256"] == hashlib.sha256(payload).hexdigest()
    joined = next(i for i in document["items"] if i["item_id"] == limit.item_id)["entities"][0]
    assert joined["canonical_id"] == "route:physical"
    assert len(joined["rendered_node_indices"]) == 1
    assert joined["provenance"][0]["source_reference"] == "synthetic/source"
    assert ReviewBundle.from_dict(document).to_json() == bundle.to_json()
    assert model.to_json() == before
    assert bundle_for(model, payload, manifest).to_json() == bundle.to_json()


def test_default_refusal_and_false_options_preserve_legacy_bundle_options():
    model = model_fixture()
    with pytest.raises(ContractError, match="does not support physical routes"):
        build_confidence_review(model, ReviewConfig(0.9))
    with pytest.raises(ContractError, match="does not support physical routes"):
        bundle_for(model, b"", None, options={})
    _, default = _normalize_glb_options(None)
    _, explicit = _normalize_glb_options({"physical_routes": False, "include_obstacles": False})
    assert default == explicit
    assert "physical_routes" not in default and "include_obstacles" not in default
    legacy = make_physical_source()
    assert build_confidence_review(legacy, ReviewConfig(0.9)).to_json() == manifest_for(legacy).to_json()
    assert build_glb_bytes(legacy) == build_glb_bytes(legacy, physical_routes=False, include_obstacles=False)


def test_obstacle_coverage_and_exact_physical_reexport_are_required():
    model = model_fixture()
    payload = build_glb_bytes(model, **OPTIONS)
    manifest = manifest_for(model)
    def remove_obstacle(document):
        document["nodes"] = [n for n in document["nodes"]
                             if n.get("extras", {}).get("canonical_id") != "obstacle:synthetic"]
    with pytest.raises(ValueError, match="canonical node coverage"):
        bundle_for(model, _rewrite_glb(payload, remove_obstacle), manifest)
    with pytest.raises(ValueError, match="trusted canonical export"):
        bundle_for(model, _mutate_glb_binary(payload), manifest)
    with pytest.raises(ValueError, match="canonical node coverage"):
        bundle_for(model, payload, manifest, {"physical_routes": True})
    stale = manifest.to_dict()
    stale["items"] = [i for i in stale["items"] if "physical_route_quantities_unsupported" not in i["reasons"]]
    with pytest.raises(ValueError, match="stale or mismatched"):
        bundle_for(model, payload, stale)
    changed = replace(model, name="Changed snapshot")
    with pytest.raises(ValueError, match="canonical_model_sha256"):
        bundle_for(changed, payload, manifest_for(changed))


def test_physical_evidence_remains_source_bound():
    model = model_fixture()
    record = model.routes[0].provenance[0]
    stale = EvidenceReference(
        ref_id="crop:stale", entity_id=model.routes[0].id,
        source_kind=record.source_kind, source_id=record.source_id,
        fingerprint="0" * 64,
    )
    with pytest.raises(ValueError, match="stale"):
        manifest_for(model, evidence=(stale,))
