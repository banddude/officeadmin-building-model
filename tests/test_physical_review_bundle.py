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
from test_confidence_review import _review_model, _wall
from oabm.quantities import extract_quantities

OPTIONS = {"physical_routes": True, "include_obstacles": True}


def model_fixture(mixed=False):
    source = replace(make_physical_source(), obstacles=(
        Obstacle(id="obstacle:synthetic", obstacle_type="supplied-box",
                 geometry=Box3D(pose=Pose(position=Point3(x=1, y=1, z=0)),
                                size=Size3(x=0.2, y=0.2, z=0.2)),
                 provenance=supplied()),
    ))
    if mixed:
        architecture = _review_model(_wall(provenance=supplied()))
        source = replace(source, levels=architecture.levels, walls=architecture.walls)
    physical = make_physical_route(source)
    routes = (physical,)
    if mixed:
        routes += (Route(id="route:legacy", route_type="emt",
                         start_port_id="port:a", end_port_id="port:b",
                         centerline=Polyline3D(points=tuple(p.pose.position for p in source.ports))),)
        routes += (replace(routes[-1], id="route:legacy-overlap"),)
    return replace(source, routes=routes)


def manifest_for(model, **kwargs):
    return build_confidence_review(model, ReviewConfig(0.9), physical_routes=True, **kwargs)


def bundle_for(model, payload, manifest, options=OPTIONS):
    return build_review_bundle(
        model, glb_bytes=payload, model_reference="synthetic/model.json",
        glb_reference="synthetic/model.glb", confidence_manifest=manifest,
        confidence_reference="synthetic/review.json",
        source_reference_mapping_reference="synthetic/source-map.json",
        source_references={
            ("synthetic", "source:physical-fixture"): "synthetic/source",
            **({("drawing", "plan:synthetic"): "synthetic/plan"} if model.levels else {}),
        },
        glb_options=options,
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_physical_review_keeps_whole_model_warning_parity_and_evidence(mixed):
    model = model_fixture(mixed)
    route = replace(model.routes[0], confidence=0.4)
    model = replace(model, routes=(route, *model.routes[1:]))
    before = model.to_json()
    evidence = tuple(EvidenceReference(
        ref_id=f"crop:{owner.id}", entity_id=owner.id,
        source_kind=owner.provenance[0].source_kind,
        source_id=owner.provenance[0].source_id,
        fingerprint=fingerprint_provenance(owner.provenance[0], entity_id=owner.id),
    ) for owner in (route,))
    manifest = manifest_for(model, evidence=evidence)
    actual = set()
    for item in manifest.items:
        if item.reasons[0].startswith("quantity_warning_"):
            code = item.reasons[0].removeprefix("quantity_warning_")
            actual.add((code, item.detail.removeprefix(f"quantity warning {code}: "), item.entity_ids))
    expected = {(w.code, w.message, tuple(sorted(w.source_entity_ids)))
                for w in extract_quantities(model).warnings}
    assert actual == expected
    codes = {row[0] for row in actual}
    assert {"physical_route_geometric_length_only", "physical_route_overlap_not_evaluated",
            "unmeasured_entities"} <= codes
    if mixed:
        assert {"assemblies_unresolved", "overlapping_route_runs"} <= codes
        assert any("wall:review-south" in item.source_entity_ids
                   for item in extract_quantities(model).items)
    assert not any("physical_route_quantities_unsupported" in i.reasons for i in manifest.items)
    limit = next(i for i in manifest.items
                 if i.reasons == ("quantity_warning_physical_route_geometric_length_only",))
    assert limit.entity_ids == (route.id,)
    assert "live clearance approval" in limit.detail
    assert limit.crops == (evidence[0].to_dict(),)
    owner = next(i for i in manifest.items if i.item_id == f"review:entity:{route.id}")
    assert "low_entity_confidence" in owner.reasons
    assert owner.crops == (evidence[0].to_dict(),)
    assert owner.provenance[0] in limit.provenance
    # Nested geometry remains in its canonical route, not fabricated top-level entities.
    assert model.routes[0].joints == route.joints
    assert model.routes[0].segments == route.segments
    assert model.routes[0].requirements == route.requirements
    assert ConfidenceReviewManifest.from_dict(manifest.to_dict()).to_json() == manifest.to_json()
    payload = build_glb_bytes(model, **OPTIONS)
    bundle = bundle_for(model, payload, manifest)
    document = bundle.to_dict()
    assert document["artifacts"]["glb"]["sha256"] == hashlib.sha256(payload).hexdigest()
    joined = next(i for i in document["items"] if i["item_id"] == limit.item_id)["entities"][0]
    assert joined["canonical_id"] == route.id
    assert len(joined["rendered_node_indices"]) == 1
    assert joined["provenance"][0]["source_reference"] == "synthetic/source"
    assert ReviewBundle.from_dict(document).to_json() == bundle.to_json()
    assert model.to_json() == before
    assert build_glb_bytes(model, **OPTIONS) == payload
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
    stale["items"] = [i for i in stale["items"] if "quantity_warning_physical_route_geometric_length_only" not in i["reasons"]]
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
