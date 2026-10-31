"""Independent invented fixtures; no production issuer or private PDF content."""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
import hashlib
import json
import pickle

import pytest

from oabm.importers import pdf_region_review as rr


def frame(page=1, rotation=0):
    # A square page makes all four rotations share dimensions, but the affine
    # and original box origin still change. These are independently specified.
    transforms = {0: (1, 0, 0, 1, -10, -20), 90: (0, -1, 1, 0, -20, 110),
                  180: (-1, 0, 0, -1, 110, 120), 270: (0, 1, -1, 0, 120, -10)}
    return dict(page_number=page, coordinate_space="displayed-bottom-origin-pt/v1",
                media_box_pdf_units=[10, 20, 110, 120], crop_box_pdf_units=[10, 20, 110, 120],
                user_unit=1, rotation_degrees=rotation, displayed_width_pt=100,
                displayed_height_pt=100, pdf_to_display_affine=transforms[rotation],
                visible_page_bbox_pt=[0, 0, 100, 100], normalization_version="test-frame/1")


def observation(page=1, source_id="glyph", kind="vector", **extra):
    return dict(page_number=page, kind=kind, source_id=source_id,
                payload={"points": [[20, 20], [30, 30]], "paint": {"visible": True}, **extra})


def issued(*, source=b"%PDF-1.7\nsynthetic fixture", adapter="pdf_electrical", rotation=0,
           observations=None, frames=None):
    frames = frames if frames is not None else [frame(rotation=rotation)]
    observations = observations if observations is not None else [observation()]
    snapshot = rr._extract_snapshot(rr.PdfByteSnapshot(source), "synthetic", adapter,
                                   "fixture-extractor/1", "test-frame/1",
                                   lambda data: (frames, observations))
    receipt = rr._issue_preview(snapshot, 1, "fixture-preview", "1",
                                lambda data, page: (b"fixture raster bytes", 100, 100,
                                                    (1, 0, 0, -1, 0, 100)))
    return snapshot, receipt


def region(receipt, *, disposition="non_plan", bbox=(10, 10, 50, 50), rid="r1", page=1,
           review_id="review1"):
    return dict(review_id=review_id, region_id=rid, page_number=page, bbox_pt=bbox,
                disposition=disposition, scope="automatic_plan_object_admission",
                reviewer_id="synthetic-reviewer", reviewed_at="2026-01-01T12:00:00+00:00",
                review_basis="issued_default_view_preview", render_context=rr._plain(receipt.render_context),
                basis_reference="inert:fixture-session")


def review(snapshot, receipt, regions=None):
    regions = regions if regions is not None else [region(receipt)]
    pages = sorted({r["page_number"] for r in regions})
    return dict(version="1.0", content_sha256=snapshot.context["content_sha256"],
                source_id=snapshot.context["source_id"],
                page_frames=[rr._plain(snapshot.context["pages"][p]) for p in pages],
                extraction_bindings=[rr._plain(snapshot.context["extraction"])], regions=regions)


def plan(regions=None, **kwargs):
    snapshot, receipt = issued(**kwargs)
    return rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, regions)), snapshot, receipt


def support(bbox=(20, 20, 30, 30), **kwargs):
    return rr.CandidateSupport("candidate", 1, "device", ((1, "vector", "glyph"),), (), bbox,
                               kwargs.pop("complete", True), **kwargs)


def assert_error(code, func):
    with pytest.raises(rr.RegionReviewError) as caught:
        func()
    assert caught.value.code == code


def test_snapshot_single_bytes_capture_survives_file_replacement(tmp_path):
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"first immutable PDF")
    captured = rr.PdfByteSnapshot.read(path)
    seen = []

    def extractor(data):
        path.write_bytes(b"different PDF after capture")
        seen.append(data)
        return [frame()], [observation()]

    snapshot = rr._extract_snapshot(captured, "synthetic", "pdf_architecture", "fixture/1",
                                   "test-frame/1", extractor)
    receipt = rr._issue_preview(snapshot, 1, "fixture", "1",
                                lambda data, f: (seen.append(data) or data, 100, 100, (1, 0, 0, -1, 0, 100)))
    assert seen[0] is captured.source_bytes and seen[1] is captured.source_bytes
    assert snapshot.context["content_sha256"] == hashlib.sha256(b"first immutable PDF").hexdigest()
    assert receipt.render_context["output_raster_sha256"] == snapshot.context["content_sha256"]
    assert path.read_bytes() != snapshot.source_bytes


def test_snapshot_is_deeply_copied_and_frozen():
    frames, observations = [frame()], [observation()]
    snapshot, receipt = issued(frames=frames, observations=observations)
    before = rr._canonical(snapshot.observations)
    frames[0]["media_box_pdf_units"][0] = 999
    observations[0]["payload"]["paint"]["visible"] = False
    observations[0]["payload"]["points"][0][0] = 999
    assert rr._canonical(snapshot.observations) == before
    with pytest.raises(TypeError):
        snapshot.observations[0]["payload"]["paint"]["visible"] = False
    with pytest.raises(AttributeError):
        snapshot._data = None
    with pytest.raises(TypeError):
        receipt.render_context["renderer_id"] = "other"
    rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt))


@pytest.mark.parametrize("operation", [copy, deepcopy, pickle.dumps])
def test_capabilities_cannot_be_copied_or_serialized(operation):
    snapshot, receipt = issued()
    validated = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt))
    for cap in (snapshot, receipt, validated):
        assert_error("review_snapshot_not_issued", lambda: operation(cap))


def test_equal_valued_forged_snapshot_and_receipt_refused():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    fake = object.__new__(rr.ExtractionSnapshot)
    object.__setattr__(fake, "_data", snapshot._data)
    assert_error("review_snapshot_not_issued", lambda: rr.validate_region_reviews(fake, [receipt], data))
    fake_receipt = object.__new__(rr.PreviewReceipt)
    object.__setattr__(fake_receipt, "_data", receipt._data)
    assert_error("review_render_context_mismatch", lambda: rr.validate_region_reviews(snapshot, [fake_receipt], data))
    assert_error("review_snapshot_not_issued", lambda: rr.ExtractionSnapshot(object(), snapshot._data))


def test_production_entrypoints_refuse_without_verified_adapters(tmp_path):
    for adapter in ("pdf_architecture", "pdf_electrical"):
        assert_error("review_adapter_support_incomplete", lambda: rr.extract_review_snapshot(tmp_path / "absent.pdf", adapter))
    snapshot, _ = issued()
    assert_error("review_adapter_support_incomplete", lambda: rr.issue_default_review_preview(snapshot, 1))
    # No caller-selectable fixture adapter or renderer exists.
    assert_error("review_extraction_mismatch", lambda: rr.extract_review_snapshot("unused", "synthetic"))


def test_stable_receipt_id_is_content_not_runtime_authority():
    a, ra = issued()
    b, rb = issued()
    assert a is not b and ra is not rb
    assert ra.render_context == rb.render_context
    assert_error("review_render_context_mismatch", lambda: rr.validate_region_reviews(a, [rb], review(a, ra)))
    # With all fresh issued evidence, reuse of an old human decision is not
    # detectable. This documented limitation must not become an audit claim.
    rr.validate_region_reviews(b, [rb], review(a, ra))


@pytest.mark.parametrize("adapter", ["pdf_architecture", "pdf_electrical"])
@pytest.mark.parametrize("disposition,action", [("plan", "unchanged"), ("non_plan", "reviewed_exclusion"), ("unresolved", "unresolved")])
def test_each_disposition_preserves_candidate_and_source_references(adapter, disposition, action):
    snapshot, receipt = issued(adapter=adapter)
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [region(receipt, disposition=disposition)]))
    candidate = support()
    result = rr.evaluate_region_candidate(p, candidate)
    assert result.action == action
    assert result.candidate is candidate
    assert result.candidate.identity_support_refs == ((1, "vector", "glyph"),)
    assert result.containing_region_ids == ("r1",)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_display_frame_rotations_and_nonzero_source_origin(rotation):
    p, _, _ = plan(rotation=rotation)
    assert rr.evaluate_region_candidate(p, support()).action == "reviewed_exclusion"


@pytest.mark.parametrize("bbox", [(5, 20, 30, 30), (10, 20, 30, 30), (10 + 0.5e-6, 20, 30, 30),
                                  (20, 5, 30, 30), (20, 20, 55, 30), (20, 20, 30, 55)])
def test_full_support_crossing_or_touching_each_boundary_is_unresolved(bbox):
    p, _, _ = plan()
    result = rr.evaluate_region_candidate(p, support(bbox))
    assert result.action == "unresolved" and result.reason == "review_boundary_contact"


def test_zero_area_support_can_be_strictly_inside():
    p, _, _ = plan()
    assert rr.evaluate_region_candidate(p, support((20, 20, 20, 20))).action == "reviewed_exclusion"


def test_same_disposition_large_rectangle_beats_nested_boundary():
    snapshot, receipt = issued()
    regions = [region(receipt, bbox=(10, 10, 50, 50), rid="large"),
               region(receipt, bbox=(25, 25, 40, 40), rid="small")]
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, regions))
    result = rr.evaluate_region_candidate(p, support())
    assert result.action == "reviewed_exclusion"
    assert result.region_ids == ("large", "small")
    assert result.containing_region_ids == ("large",)


def test_union_of_regions_is_not_a_single_authorization():
    snapshot, receipt = issued()
    regions = [region(receipt, bbox=(10, 10, 25, 50), rid="a"), region(receipt, bbox=(25, 10, 50, 50), rid="b")]
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, regions))
    assert rr.evaluate_region_candidate(p, support()).action == "unresolved"


def test_conflicting_interiors_reject_even_with_no_candidates():
    snapshot, receipt = issued(observations=[])
    regions = [region(receipt), region(receipt, disposition="plan", bbox=(40, 40, 60, 60), rid="r2")]
    assert_error("review_disposition_conflict", lambda: rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, regions)))


def test_different_disposition_contact_is_unresolved_not_input_overlap():
    snapshot, receipt = issued()
    regions = [region(receipt, bbox=(10, 10, 25, 50)),
               region(receipt, disposition="plan", bbox=(25, 10, 50, 50), rid="r2")]
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, regions))
    assert rr.evaluate_region_candidate(p, support()).reason == "mixed_review_contact"


def test_outside_empty_regions_and_unknown_support_are_distinct():
    p, _, _ = plan()
    assert rr.evaluate_region_candidate(p, support((60, 60, 70, 70))).action == "unchanged"
    assert rr.evaluate_region_candidate(p, support(complete=False)).reason == "support_not_complete"
    assert_error("review_adapter_support_incomplete", lambda: rr.evaluate_region_candidate(p, support(None, complete=False)))
    empty, _, _ = plan(regions=[])
    assert rr.evaluate_region_candidate(empty, support(None, complete=False)).action == "unchanged"


@pytest.mark.parametrize("disposition", ["non_plan", "unresolved"])
def test_explicit_hint_conflicts_are_atomic(disposition):
    snapshot, receipt = issued()
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [region(receipt, disposition=disposition)]))
    candidate = support(explicit_hint=True)
    assert_error("review_hint_conflict", lambda: rr.evaluate_region_candidate(p, candidate))
    assert candidate.explicit_hint is True


def test_plan_region_does_not_conflict_with_explicit_hint():
    snapshot, receipt = issued()
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [region(receipt, disposition="plan")]))
    assert rr.evaluate_region_candidate(p, support(explicit_hint=True)).action == "unchanged"


def test_hash_binding_changes_reject_without_partial_result():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["content_sha256"] = "0" * 64
    assert_error("review_source_mismatch", lambda: rr.validate_region_reviews(snapshot, [receipt], data))


def test_observation_payload_change_changes_fingerprint_and_rejects():
    a, ra = issued()
    b, rb = issued(observations=[observation(extra_field="preserved, not discarded")])
    assert a.context["extraction"] != b.context["extraction"]
    assert_error("review_extraction_mismatch", lambda: rr.validate_region_reviews(b, [rb], review(a, ra)))


def test_cross_adapter_binding_requires_own_fingerprint():
    a, ra = issued(adapter="pdf_architecture")
    e, re = issued(adapter="pdf_electrical")
    assert_error("review_extraction_mismatch", lambda: rr.validate_region_reviews(e, [re], review(a, ra)))
    combined = review(a, ra)
    combined["extraction_bindings"].append(rr._plain(e.context["extraction"]))
    rr.validate_region_reviews(e, [re], combined)


@pytest.mark.parametrize("field,value", [("rotation_degrees", 90), ("user_unit", 2),
                                        ("crop_box_pdf_units", [20, 20, 110, 120]),
                                        ("media_box_pdf_units", [11, 20, 111, 120])])
def test_same_size_different_frame_is_rejected(field, value):
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["page_frames"][0][field] = value
    assert_error("review_frame_mismatch", lambda: rr.validate_region_reviews(snapshot, [receipt], data))


def test_original_page_number_selection_and_unknown_pages():
    snapshot, receipt = issued(frames=[frame(1), frame(3)], observations=[observation(), observation(page=3)])
    assert_error("review_page_not_selected", lambda: rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt), selected_pages=[3]))
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt), selected_pages=[3, 1])
    assert rr.evaluate_region_candidate(p, support()).action == "reviewed_exclusion"
    assert_error("review_page_not_selected", lambda: rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt), selected_pages=[2]))


def test_duplicate_observation_ids_are_rejected():
    assert_error("review_extraction_mismatch", lambda: issued(observations=[observation(), observation()]))


def test_review_duplicate_idempotence_and_conflict():
    snapshot, receipt = issued()
    one = region(receipt)
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [one, deepcopy(one)]))
    assert len(p.review_set["regions"]) == 1
    changed = {**one, "disposition": "plan"}
    assert_error("review_id_conflict", lambda: rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [one, changed])))


@pytest.mark.parametrize("bbox", [(True, 10, 50, 50), (float("nan"), 10, 50, 50),
                                  (float("inf"), 10, 50, 50), (10, 10, 10, 50),
                                  (50, 10, 10, 50), (-10, 10, 50, 50)])
def test_invalid_geometry_never_coerced_or_clipped(bbox):
    snapshot, receipt = issued()
    with pytest.raises(rr.RegionReviewError):
        rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [region(receipt, bbox=bbox)]))


@pytest.mark.parametrize("mutation", [lambda d: d.update(unknown=1),
                                      lambda d: d["regions"][0].update(unknown=1),
                                      lambda d: d["regions"][0].update(reviewed_at="2026-01-01"),
                                      lambda d: d["regions"][0].update(page_number=True),
                                      lambda d: d["regions"][0].update(disposition="delete"),
                                      lambda d: d["regions"][0].update(disposition=[]),
                                      lambda d: d["page_frames"][0].update(pdf_to_display_affine=[0]*6)])
def test_strict_schema(mutation):
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    mutation(data)
    with pytest.raises(rr.RegionReviewError):
        rr.validate_region_reviews(snapshot, [receipt], data)


@pytest.mark.parametrize("key,value", [("interactive_overrides", True), ("view_event", "Print"),
                                       ("annotation_policy", "none"), ("optional_content_policy", "all-visible")])
def test_unsupported_render_profiles_reject(key, value):
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["regions"][0]["render_context"][key] = value
    assert_error("review_render_profile_unsupported", lambda: rr.validate_region_reviews(snapshot, [receipt], data))


def test_changed_raster_or_renderer_receipt_rejects_old_review():
    snapshot, receipt = issued()
    other = rr._issue_preview(snapshot, 1, "fixture-preview", "2",
                              lambda data, page: (b"changed raster", 100, 100, (1, 0, 0, -1, 0, 100)))
    assert_error("review_render_context_mismatch", lambda: rr.validate_region_reviews(snapshot, [other], review(snapshot, receipt)))
    assert_error("review_render_context_mismatch", lambda: rr.validate_region_reviews(snapshot, [], review(snapshot, receipt)))


def test_safe_basis_reference_is_inert_and_preserved():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["regions"][0]["basis_reference"] = "https://invalid.example/never-fetch; rm -rf nowhere"
    p = rr.validate_region_reviews(snapshot, [receipt], data)
    assert p.review_set["regions"][0]["basis_reference"] == data["regions"][0]["basis_reference"]


def test_candidate_refs_include_cross_page_context_but_not_identity():
    snapshot, receipt = issued(frames=[frame(1), frame(3)], observations=[observation(), observation(3, "legend", "text")])
    p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt))
    c = replace(support(), contextual_support_refs=((3, "text", "legend"),))
    assert rr.evaluate_region_candidate(p, c).candidate.contextual_support_refs == ((3, "text", "legend"),)
    wrong = replace(c, identity_support_refs=((3, "text", "legend"),))
    assert_error("review_adapter_support_incomplete", lambda: rr.evaluate_region_candidate(p, wrong))
    missing = replace(c, contextual_support_refs=((3, "text", "absent"),))
    assert_error("review_adapter_support_incomplete", lambda: rr.evaluate_region_candidate(p, missing))


def test_order_permutations_preserve_fingerprints_and_decisions():
    observations = [observation(), observation(source_id="other")]
    a, ra = issued(observations=observations)
    b, rb = issued(observations=list(reversed(observations)))
    assert a.context["extraction"] == b.context["extraction"]
    regions = [region(ra, rid="b", bbox=(25, 25, 40, 40)), region(ra, rid="a")]
    pa = rr.validate_region_reviews(a, [ra], review(a, ra, regions))
    pb = rr.validate_region_reviews(b, [rb], review(b, rb, list(reversed(regions))))
    assert rr.evaluate_region_candidate(pa, support()) == rr.evaluate_region_candidate(pb, support())


def test_module_has_no_legacy_importer_admission_wiring():
    from oabm.importers.pdf_architecture import importer as architecture
    from oabm.importers.pdf_electrical import importer as electrical
    assert "pdf_region_review" not in architecture.__dict__
    assert "pdf_region_review" not in electrical.__dict__


def test_json_record_roundtrip_has_no_runtime_authority():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    parsed = rr.review_set_from_json(json.dumps(data))
    p = rr.validate_region_reviews(snapshot, [receipt], parsed)
    assert rr.evaluate_region_candidate(p, support()).action == "reviewed_exclusion"
    assert_error("review_render_context_mismatch",
                 lambda: rr.validate_region_reviews(snapshot, [dict(receipt.render_context)], parsed))


@pytest.mark.parametrize("text", ['{"version":"1.0","version":"2.0"}', '{"x":NaN}', '[', 'null'])
def test_invalid_json_and_duplicate_keys_reject(text):
    assert_error("review_schema_invalid", lambda: rr.review_set_from_json(text))


def test_cyclic_and_excessive_review_input_is_rejected():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["regions"][0]["note"] = data
    assert_error("review_schema_invalid", lambda: rr.validate_region_reviews(snapshot, [receipt], data))
    assert_error("review_schema_invalid", lambda: rr.review_set_from_json(" " * (rr.MAX_JSON_BYTES + 1)))
    data = review(snapshot, receipt)
    data["regions"] = [region(receipt, rid=str(i)) for i in range(rr.MAX_REGIONS + 1)]
    assert_error("review_schema_invalid", lambda: rr.validate_region_reviews(snapshot, [receipt], data))


def test_missing_or_invalid_collection_inputs_reject_with_typed_errors():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    assert_error("review_render_context_mismatch", lambda: rr.validate_region_reviews(snapshot, None, data))
    assert_error("review_page_not_selected", lambda: rr.validate_region_reviews(snapshot, [receipt], data, selected_pages={1}))
    assert_error("review_extraction_mismatch", lambda: rr.extract_review_snapshot("unused", []))


def test_validated_review_is_deeply_immutable_and_copied():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    p = rr.validate_region_reviews(snapshot, [receipt], data)
    data["regions"][0]["disposition"] = "plan"
    data["regions"][0]["render_context"]["renderer_id"] = "changed"
    assert rr.evaluate_region_candidate(p, support()).action == "reviewed_exclusion"
    with pytest.raises(TypeError):
        p.review_set["regions"][0]["disposition"] = "plan"


def test_geometric_decisions_are_translation_invariant_in_display_space():
    snapshot, receipt = issued()
    baseline = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt))
    shifted = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, [
        region(receipt, bbox=(40, 40, 80, 80))]))
    a = rr.evaluate_region_candidate(baseline, support())
    b = rr.evaluate_region_candidate(shifted, support((50, 50, 60, 60)))
    assert (a.action, a.reason, a.region_ids) == (b.action, b.reason, b.region_ids)


def test_reviewed_page_with_missing_safe_localization_refuses_even_hint():
    p, _, _ = plan()
    candidate = support(None, complete=False, explicit_hint=True)
    assert_error("review_adapter_support_incomplete", lambda: rr.evaluate_region_candidate(p, candidate))


def test_different_dispositions_do_not_gain_priority_by_input_order():
    snapshot, receipt = issued()
    regions = [region(receipt, bbox=(10, 10, 25, 50)),
               region(receipt, disposition="plan", bbox=(25, 10, 50, 50), rid="r2")]
    decisions = []
    for ordered in (regions, list(reversed(regions))):
        p = rr.validate_region_reviews(snapshot, [receipt], review(snapshot, receipt, ordered))
        decisions.append(rr.evaluate_region_candidate(p, support()))
    assert decisions[0] == decisions[1]


def test_candidate_source_lists_are_copied_and_identity_keys_not_renumbered():
    p, _, _ = plan()
    refs = [[1, "vector", "glyph"]]
    c = rr.CandidateSupport("persistent-candidate", 1, "device", refs, [], [20, 20, 30, 30], True)
    refs[0][2] = "mutated"
    result = rr.evaluate_region_candidate(p, c)
    assert result.candidate.candidate_key == "persistent-candidate"
    assert result.candidate.identity_support_refs == ((1, "vector", "glyph"),)


@pytest.mark.parametrize("kind", ["snapshot", "receipt", "plan"])
def test_issued_capabilities_reject_normal_attribute_deletion(kind):
    p, snapshot, receipt = plan()
    capability = {"snapshot": snapshot, "receipt": receipt, "plan": p}[kind]
    before = capability._data
    with pytest.raises(AttributeError, match="immutable"):
        del capability._data
    assert capability._data is before
    assert rr.evaluate_region_candidate(p, support()).action == "reviewed_exclusion"


@pytest.mark.parametrize("value", [10**400, -(10**400)])
@pytest.mark.parametrize("via_json", [False, True])
def test_out_of_float_range_coordinates_are_typed_schema_errors(value, via_json):
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["regions"][0]["bbox_pt"] = [value, 10, 50, 50]
    action = (lambda: rr.review_set_from_json(json.dumps(data))) if via_json else (
        lambda: rr.validate_region_reviews(snapshot, [receipt], data))
    assert_error("review_schema_invalid", action)


@pytest.mark.parametrize("value", ["\ud800", "\udfff"])
@pytest.mark.parametrize("location", ["note", "source_id", "key"])
@pytest.mark.parametrize("via_json", [False, True])
def test_lone_surrogates_are_typed_schema_errors(value, location, via_json):
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    if location == "note":
        data["regions"][0]["note"] = value
    elif location == "source_id":
        data["source_id"] = value
    else:
        data["regions"][0][value] = "invalid key"
    action = (lambda: rr.review_set_from_json(json.dumps(data))) if via_json else (
        lambda: rr.validate_region_reviews(snapshot, [receipt], data))
    assert_error("review_schema_invalid", action)


def test_literal_lone_surrogate_json_input_is_typed_schema_error():
    assert_error("review_schema_invalid", lambda: rr.review_set_from_json('"\ud800"'))


def test_valid_non_ascii_and_supplementary_unicode_are_preserved():
    snapshot, receipt = issued()
    data = review(snapshot, receipt)
    data["regions"][0]["note"] = "Café — \U0001f3e0"
    parsed = rr.review_set_from_json(json.dumps(data))
    p = rr.validate_region_reviews(snapshot, [receipt], parsed)
    assert p.review_set["regions"][0]["note"] == data["regions"][0]["note"]


@pytest.mark.parametrize("bbox", [
    (20, 20, 30, 30), (5, 20, 30, 30), (20, 5, 30, 30),
    (20, 20, 55, 30), (20, 20, 30, 55), (10, 20, 30, 30),
    (50, 20, 60, 30), (10 + rr.EPSILON_PT, 20, 30, 30),
])
def test_complete_explicit_hint_is_retained_across_plan_boundaries(bbox):
    snapshot, receipt = issued()
    p = rr.validate_region_reviews(snapshot, [receipt],
                                   review(snapshot, receipt, [region(receipt, disposition="plan")]))
    candidate = support(bbox, explicit_hint=True)
    result = rr.evaluate_region_candidate(p, candidate)
    assert result.action == "unchanged" and result.reason == "explicit_hint_retained"
    assert result.candidate is candidate


def test_incomplete_explicit_hint_cannot_be_silently_suppressed_at_plan_boundary():
    snapshot, receipt = issued()
    p = rr.validate_region_reviews(snapshot, [receipt],
                                   review(snapshot, receipt, [region(receipt, disposition="plan")]))
    assert_error("review_adapter_support_incomplete", lambda: rr.evaluate_region_candidate(
        p, support((5, 20, 30, 30), complete=False, explicit_hint=True)))


@pytest.mark.parametrize("disposition", ["non_plan", "unresolved"])
def test_explicit_hint_crossing_nonplan_boundary_is_still_a_conflict(disposition):
    snapshot, receipt = issued()
    p = rr.validate_region_reviews(snapshot, [receipt],
                                   review(snapshot, receipt, [region(receipt, disposition=disposition)]))
    assert_error("review_hint_conflict", lambda: rr.evaluate_region_candidate(
        p, support((5, 20, 30, 30), explicit_hint=True)))
