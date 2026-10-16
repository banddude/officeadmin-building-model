"""Independently specified synthetic review/registration cases, no customer data."""
import math
from dataclasses import replace

import pytest

from oabm.model import (BuildingModel, ContractError, CoordinateSystem, Level,
                        Provenance)
from oabm.proposals.contracts import digest
from oabm.importers.pdf_electrical import PdfPageTransform
from oabm.importers.pdf_electrical.symbol_stage import (
    SymbolRegistration, SymbolPlacement, SymbolDecision, SymbolReview,
    _review_from_rows, _materialize_verified,
)


def row(key="a", *, page=1, box=(10, 20, 14, 24), category="switch"):
    return {"proposal_id": key, "source_mark_id": f"native:{key}",
            "source_id": "synthetic:sheet", "document_sha256": "a" * 64,
            "page": page, "category": category, "type_proposal": "switch",
            "type_tag": None, "instance_label": None, "geometry_kind": "point",
            "bounds_pt": list(box), "anchor_pt": [12, 22], "polyline_pt": [],
            "confidence": .61, "unknowns": ["mounting height absent"],
            "evidence_image_ids": ["floor", "legend"],
            "request_fingerprint": "b" * 64, "result_fingerprint": "c" * 64,
            "accepted": False, "review_state": "pending",
            "provenance": [{"source_kind": "ai-proposed", "source_id": "synthetic:sheet",
                "source_element_id": f"native:{key}", "page": page,
                "confidence": .61, "derivation": "inferred", "attributes": {}}]}


def model():
    return BuildingModel(model_id="synthetic-model", schema_version="1.3.0",
        coordinate_system=CoordinateSystem(frame_id="building"),
        levels=(Level(id="upper", elevation_m=3.2),))


def registration(**changes):
    values = dict(source_id="synthetic:sheet", document_sha256="a"*64, page=1,
        source_bbox_pt=(0, 0, 200, 100), level_id="upper",
        transform=PdfPageTransform(frame_id="building", m11_m_per_pt=0,
            m12_m_per_pt=-.1, m21_m_per_pt=.1, m22_m_per_pt=0,
            tx_m=10, ty_m=20, z_m=3.2),
        provenance=(Provenance(source_kind="registration-review", source_id="synthetic-control",
            derivation="user", confidence=.9),))
    values.update(changes)
    return SymbolRegistration(**values)


def review(*rows):
    return _review_from_rows(rows or [row()], [{"coverage_notes": ["bounded source, not completeness proof"]}])


def decision(manifest, key="a", *, reg=None, **changes):
    reg = reg or registration()
    proposal = next(r for r in manifest.proposals if r["proposal_id"] == key)
    values = dict(manifest_fingerprint=manifest.fingerprint, proposal_id=key,
        proposal_fingerprint=digest(proposal), reviewer_id="synthetic-reviewer",
        reviewer_derivation="user", rationale="Reviewed source mark and explicit placement facts.",
        action="accept", resolved_unknowns=("mounting height absent",),
        placement=SymbolPlacement(registration_fingerprint=reg.fingerprint,
            entity_kind="device", entity_type="dimmer", anchor_pt=(12,22),
            height_above_level_m=1.1, yaw_radians=math.pi/2, remaining_unknowns=()))
    values.update(changes)
    return SymbolDecision(**values)


def apply(manifest=None, decisions=None, registrations=None, target=None):
    manifest = manifest or review()
    return _materialize_verified(target or model(), manifest,
        [decision(manifest)] if decisions is None else decisions,
        [registration()] if registrations is None else registrations)


def test_reviewed_correction_maps_to_canonical_units_keeps_ai_confidence_and_no_topology():
    original = model()
    manifest = review()
    result = apply(manifest, target=original)
    entity, = result.model.electrical_devices
    assert entity.device_type == "dimmer"  # explicit correction, proposal was switch
    assert (entity.pose.position.x, entity.pose.position.y, entity.pose.position.z) == pytest.approx((7.8,21.2,4.3))
    assert entity.pose.rotation.z == pytest.approx(math.sin(math.pi/4))
    assert entity.level_id == "upper"
    assert entity.confidence == .61
    assert entity.provenance[0].source_kind == "ai-proposed"
    assert entity.provenance[0].derivation == "inferred"
    assert entity.provenance[-1].derivation == "user"
    assert entity.size is None and entity.host_id is None and entity.space_id is None
    assert result.model.circuits == result.model.ports == result.model.routes == ()
    assert original.electrical_devices == () and original.attributes == {}
    assert BuildingModel.from_json(result.model.to_json()) == result.model
    assert manifest.proposals[0]["accepted"] is False


def test_ai_review_does_not_become_user_or_observed_evidence():
    manifest = review()
    result = apply(manifest, [decision(manifest, reviewer_derivation="inferred")])
    assert result.model.electrical_devices[0].provenance[-1].derivation == "inferred"


def test_roundtrip_review_decision_and_registration():
    manifest, reg = review(), registration()
    assert SymbolReview.from_json(manifest.to_json()) == manifest
    assert SymbolRegistration.from_json(reg.to_json()) == reg
    record = decision(manifest)
    assert SymbolDecision.from_json(record.to_json()) == record


def test_no_review_creates_no_instances_but_retains_full_pending_evidence():
    manifest = review()
    result = apply(manifest, [])
    assert result.model.electrical_devices == ()
    assert result.dispositions[0]["status"] == "pending"
    assert result.model.attributes["ai_symbol_stage_runs"][0]["manifest"] == manifest.to_dict()


@pytest.mark.parametrize("action", ["reject", "defer"])
def test_explicit_nonacceptance(action):
    manifest = review()
    result = apply(manifest, [decision(manifest, action=action, placement=None)])
    assert not result.model.electrical_devices
    assert result.dispositions[0]["status"] == action


def test_missing_registration_stays_pending():
    result = apply(registrations=[])
    assert not result.model.electrical_devices
    assert result.dispositions[0]["reason"] == "reviewed_registration_missing"


@pytest.mark.parametrize("changes,match", [
    ({"source_id": "synthetic:other"}, "source/page"),
    ({"document_sha256": "d"*64}, "source/page"),
    ({"page": 2}, "source/page"),
    ({"level_id": "missing"}, "missing level"),
    ({"transform": PdfPageTransform(frame_id="other", z_m=3.2)}, "frame mismatch"),
    ({"transform": PdfPageTransform(frame_id="building", z_m=0)}, "z datum"),
    ({"source_bbox_pt": (50,50,90,90)}, "outside registered"),
])
def test_wrong_registration_fails_without_model_mutation(changes, match):
    manifest, reg = review(), registration(**changes)
    with pytest.raises(ContractError, match=match):
        apply(manifest, [decision(manifest, reg=reg)], [reg])


def test_corrected_anchor_outside_source_mark_fails():
    manifest = review()
    record = decision(manifest)
    record = replace(record, placement=replace(record.placement, anchor_pt=(80,80)))
    with pytest.raises(ContractError, match="source mark bounds"):
        apply(manifest, [record])


@pytest.mark.parametrize("changes", [
    {"manifest_fingerprint": "d"*64}, {"proposal_fingerprint": "d"*64},
    {"proposal_id": "missing"},
])
def test_stale_decisions_fail(changes):
    manifest = review()
    with pytest.raises(ContractError, match="stale"):
        apply(manifest, [decision(manifest, **changes)])


def test_unknowns_must_be_acknowledged_and_remaining_facts_stay_visible():
    manifest = review()
    record = decision(manifest, resolved_unknowns=())
    with pytest.raises(ContractError, match="unacknowledged"):
        apply(manifest, [record])
    record = replace(record, placement=replace(record.placement,
        remaining_unknowns=("mounting height absent", "scope unknown")))
    result = apply(manifest, [record])
    assert result.model.electrical_devices[0].attributes["symbol_review"]["remaining_unknowns"] == [
        "mounting height absent", "scope unknown"]


def test_overlapping_tiles_not_silently_deduplicated_or_accepted():
    manifest = review(row("a"), row("b", box=(11,21,15,25)))
    assert manifest.duplicate_candidates == (("a", "b"),)
    result = apply(manifest, [decision(manifest)])
    assert not result.model.electrical_devices
    assert result.dispositions[0]["reason"] == "duplicate_candidates_require_review"


def test_duplicate_review_materializes_only_explicit_survivor():
    manifest = review(row("a"), row("b", box=(11,21,15,25), category="detector"))
    records = [decision(manifest), decision(manifest, "b", action="duplicate", placement=None,
                                           duplicate_of="a")]
    result = apply(manifest, records)
    assert len(result.model.electrical_devices) == 1
    assert [d["status"] for d in result.dispositions] == ["accept", "duplicate"]
    assert len(result.review.proposals) == 2


def test_overlapping_distinct_symbols_need_separate_rationale():
    manifest = review(row("a"), row("b", box=(11,21,15,25)))
    records = [decision(manifest), decision(manifest, "b")]
    assert not apply(manifest, records).model.electrical_devices
    records = [replace(r, overlap_resolution="Separate concentric source marks reviewed as distinct.")
               for r in records]
    assert len(apply(manifest, records).model.electrical_devices) == 2


def test_cross_page_marks_never_deduplicated():
    manifest = review(row("a"), row("b", page=2))
    assert manifest.duplicate_candidates == ()
    reg2 = registration(page=2)
    result = apply(manifest, [decision(manifest), decision(manifest, "b", reg=reg2)],
                   [registration(), reg2])
    assert len(result.model.electrical_devices) == 2


def test_duplicate_survivor_must_exist_and_be_accepted():
    manifest = review(row("a"), row("b", box=(11,21,15,25)))
    with pytest.raises(ContractError, match="accepted candidate survivor"):
        apply(manifest, [decision(manifest, "b", action="duplicate", placement=None, duplicate_of="a")])


def test_equipment_and_linear_source_geometry_do_not_create_routes():
    linear = row(category="linear_symbol")
    linear.update(geometry_kind="polyline", anchor_pt=None, polyline_pt=[[10,20],[14,24]])
    manifest = review(linear)
    record = decision(manifest)
    record = replace(record, placement=replace(record.placement, entity_kind="equipment", entity_type="linear_luminaire"))
    result = apply(manifest, [record])
    assert not result.model.electrical_devices
    entity, = result.model.electrical_equipment
    assert entity.equipment_type == "linear_luminaire"
    assert entity.attributes["symbol_review"]["proposal"]["polyline_pt"] == [[10,20],[14,24]]
    assert entity.size is None and not result.model.routes


def test_replay_is_idempotent_and_conflicting_existing_entity_is_not_overwritten():
    manifest = review()
    first = apply(manifest)
    second = apply(manifest, target=first.model)
    assert first.model == second.model
    entity = first.model.electrical_devices[0]
    changed = replace(first.model, electrical_devices=(replace(entity, device_type="receptacle"),))
    with pytest.raises(ContractError, match="different content"):
        apply(manifest, target=changed)


def test_identity_survives_reviewed_type_and_pose_correction():
    manifest = review()
    before = apply(manifest).model.electrical_devices[0]
    record = decision(manifest)
    record = replace(record, placement=replace(record.placement, entity_type="switch", anchor_pt=(13,23)))
    after = apply(manifest, [record]).model.electrical_devices[0]
    assert after.id == before.id and after.pose != before.pose


def test_repeat_and_input_order_are_deterministic():
    rows = [row("a"), row("b", box=(30,40,34,44))]
    a, b = review(*rows), review(*reversed(rows))
    assert a.to_json() == b.to_json()
    assert apply(a, []).model.to_json() == apply(b, []).model.to_json()


@pytest.mark.parametrize("kwargs", [
    {"height_above_level_m": float("nan")}, {"height_above_level_m": -1},
    {"yaw_radians": float("inf")}, {"anchor_pt": (True,2)},
    {"entity_type": "unknown"}, {"entity_kind": "route"},
])
def test_invalid_placement_fails(kwargs):
    record = decision(review()).placement
    with pytest.raises(ContractError):
        replace(record, **kwargs)


def test_reviewed_model_drives_existing_plan_counts_and_verified_glb_bundle(tmp_path):
    from oabm.drawings import PlanSpec, generate_plan, view_to_svg, generate_schedules
    from oabm.exports import to_glb
    from oabm.exports.review_bundle import build_review_bundle
    from oabm.qa.confidence_review import ReviewConfig, build_confidence_review
    from oabm.quantities import extract_quantities

    result = apply()
    canonical = result.model
    entity, = canonical.electrical_devices
    view = generate_plan(canonical, PlanSpec(id="reviewed-plan", level_id="upper",
        cut_height_m=1.1, view_depth_above_m=.2, view_depth_below_m=.2))
    assert any(entity.id in p.source_ids for p in view.primitives)
    assert "<svg" in view_to_svg(view)
    schedules = generate_schedules(canonical)
    assert any(entity.id in str(schedule) for schedule in schedules)
    quantities = extract_quantities(canonical)
    item, = [item for item in quantities.items if entity.id in item.source_entity_ids]
    assert (item.item_type, item.quantity, item.unit, item.confidence) == ("dimmer", 1, "ea", .61)
    glb_path = tmp_path / "model.glb"
    to_glb(canonical, glb_path)
    confidence = build_confidence_review(canonical, ReviewConfig(confidence_threshold=.8))
    references = {(p.source_kind, p.source_id): f"synthetic-evidence/{p.source_kind}"
                  for p in entity.provenance}
    bundle = build_review_bundle(canonical, glb_bytes=glb_path.read_bytes(),
        model_reference="synthetic/model.json", glb_reference="synthetic/model.glb",
        confidence_manifest=confidence, confidence_reference="synthetic/review.json",
        source_reference_mapping_reference="synthetic/source-map.json",
        source_references=references)
    assert entity.id in bundle.to_json()
    assert "ai-proposed" in bundle.to_json()
    assert "inferred" in bundle.to_json()


def test_duplicate_keeps_pending_when_survivor_has_no_registration():
    manifest = review(row("a"), row("b", box=(11,21,15,25)))
    records = [decision(manifest), decision(manifest, "b", action="duplicate", placement=None,
                                           duplicate_of="a")]
    result = apply(manifest, records, [])
    assert not result.model.electrical_devices
    assert result.dispositions[1]["status"] == "pending"
    assert result.dispositions[1]["reason"] == "duplicate_survivor_not_materialized"


def test_registration_preserves_control_point_evidence():
    reg = registration()
    reg = replace(reg, transform=replace(reg.transform, registration={
        "source_controls_pt": [[0,0],[10,0]], "target_controls_m": [[10,20],[10,21]],
        "residual_m": 0, "derivation": "inferred"}))
    assert SymbolRegistration.from_json(reg.to_json()) == reg
    assert reg.fingerprint == SymbolRegistration.from_json(reg.to_json()).fingerprint


def test_weak_registration_and_level_evidence_limit_instance_confidence():
    manifest = review()
    reg = registration(provenance=(Provenance(source_kind="registration-review",
        source_id="synthetic-control", derivation="inferred", confidence=.2),))
    result = apply(manifest, [decision(manifest, reg=reg)], [reg])
    assert result.model.electrical_devices[0].confidence == .2
    assert result.model.electrical_devices[0].provenance[0].confidence == .61
    target = replace(model(), levels=(replace(model().levels[0], confidence=.1),))
    result = apply(manifest, target=target)
    assert result.model.electrical_devices[0].confidence == .1


def symbol_source_fixture(tmp_path, *, missing_notes=False, adapter_override=None):
    """Actual synthetic PDF and configured fake transport, all nine categories."""
    import hashlib
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from oabm.proposals import Endpoint, RoutingConfig
    from oabm.proposals.takeoff import render_source_image
    from oabm.proposals.symbols import (VERSION, TASK, VALIDATOR, CATEGORIES,
        SymbolContext, SymbolRequest, SymbolMark, SymbolOutput, SymbolAdapterReply,
        SymbolRoutingPolicy, AttemptJournal)

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    stream = DecodedStreamObject()
    # Independently authored source marks at documented centres. Exact output
    # geometry below is a fake provider response, not recognition accuracy proof.
    stream.set_data(b"0 0 0 RG 1 w "
        b"10 25 10 10 re S 15 25 m 20 35 l S "
        b"40 25 10 10 re S 43 27 m 43 33 l S 47 27 m 47 33 l S "
        b"70 25 10 10 re S 70 25 m 80 35 l S 70 35 m 80 25 l S "
        b"100 25 10 10 re S 100 25 m 110 35 l S "
        b"130 25 10 10 re S 130 30 m 140 30 l S 135 25 m 135 35 l S "
        b"160 25 10 10 re S 160 25 m 170 35 l S "
        b"10 55 10 10 re S 10 57 m 20 57 l S "
        b"40 60 m 80 60 l S 100 55 10 10 re S")
    page[NameObject("/Contents")] = writer._add_object(stream)
    # Separate synthetic context sheet. Its rows do not force field counts.
    context = writer.add_blank_page(width=200, height=100)
    context_stream = DecodedStreamObject()
    context_stream.set_data(b"0 0 0 RG 1 w 5 5 190 90 re S 5 50 m 195 50 l S")
    context[NameObject("/Contents")] = writer._add_object(context_stream)
    pdf_io = io.BytesIO()
    writer.write(pdf_io)
    document = pdf_io.getvalue()
    pdf_hash = hashlib.sha256(document).hexdigest()
    images, refs = {}, []
    for key, role, number in [("field", "target", 1), ("legend", "legend", 2),
                              ("schedule", "schedule", 2), ("notes", "notes", 2)]:
        if missing_notes and role == "notes":
            continue
        ref, payload = render_source_image(document, source_id="synthetic:whole-electrical",
            page=number, scale=1, crop_px=(0,0,200,100), image_id=key, role=role)
        refs.append(ref)
        images[key] = payload
    contexts = tuple(SymbolContext(role=role,
        status="missing" if missing_notes and role == "notes" else "supplied",
        image_ids=() if missing_notes and role == "notes" else (role,),
        rationale="Explicit synthetic context selection.") for role in ("legend", "schedule", "notes"))
    request = SymbolRequest(version=VERSION, task=TASK, validator_version=VALIDATOR,
        prompt_version="synthetic-all-electrical-v1", question="Locate all electrical source marks in the supplied field.",
        images=tuple(refs), target_image_ids=("field",), contexts=contexts,
        allowed_types=tuple(c for c in CATEGORIES if c != "unknown"), confidence_threshold=.8)
    centres = {"luminaire": (165,30), "switch": (15,30), "dimmer": (105,30),
        "detector": (75,30), "fan": (135,30), "receptacle": (45,30),
        "equipment": (15,60), "linear_symbol": (60,60), "unknown": (105,60)}
    marks = []
    for category in CATEGORIES:
        x, y = centres[category]
        linear = category == "linear_symbol"
        bounds = ((40/200,35/100,80/200,45/100) if linear else
                  ((x-5)/200, (100-y-5)/100, (x+5)/200, (100-y+5)/100))
        marks.append(SymbolMark(image_id="field", category=category,
            type_proposal=None if category == "unknown" else category,
            type_tag=None, instance_label=None, bounds=bounds,
            anchor=None if linear else (x/200,(100-y)/100),
            polyline=((.2,.4),(.4,.4)) if linear else (), confidence=.75,
            source_mark=f"Independent synthetic {category} mark.",
            evidence_image_ids=("field", "legend"), unknowns=("type unclear",) if category == "unknown" else ()))
    output = SymbolOutput(version=VERSION, marks=tuple(marks), scanned_image_ids=("field",),
        coverage_notes=("Bounded synthetic field scanned; completeness is separately reviewed.",))
    endpoints = {key: Endpoint(adapter_id="fake-symbols", provider_id="synthetic",
                    model_alias=key) for key in ("text_glm","text_codex","vision_luna","vision_sol","diagnostic_astra")}
    policy = SymbolRoutingPolicy(config=RoutingConfig(version="1.0.0", **endpoints))
    calls = []

    def fake(payload, **kwargs):
        calls.append((payload, kwargs))
        if adapter_override:
            return adapter_override(payload, **kwargs)
        return SymbolAdapterReply(output_json=output.to_json(), resolved_model_id="synthetic-engine",
                                  provider_request_id="synthetic-request")

    journal = AttemptJournal.create(tmp_path / "attempts.jsonl", run_id="synthetic-stage-test")
    return request, policy, {"fake-symbols": fake}, {pdf_hash: document}, images, journal, calls, centres


def test_public_ai_stage_to_reviewed_model_all_categories_without_legacy_recognition(tmp_path, monkeypatch):
    import oabm.importers.pdf_electrical.importer as legacy
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review, materialize_reviewed_symbols
    from oabm.drawings import PlanSpec, generate_plan, view_to_svg
    from oabm.quantities import extract_quantities

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy recognition must not gate AI extraction")

    monkeypatch.setattr(legacy, "_recognize_lighting", forbidden)
    req, policy, adapters, documents, images, journal, calls, centres = symbol_source_fixture(tmp_path)
    with journal:
        extraction = extract_symbol_review([req], policy, adapters=adapters,
            documents=documents, images=images, journals={req.fingerprint: journal})
    assert len(calls) == 1 and calls[0][1]["model_alias"] == "vision_sol"
    assert len(extraction.review.proposals) == 9
    assert extraction.review.coverage[0]["status"] == "completed"
    reg = registration(source_id="synthetic:whole-electrical", document_sha256=next(iter(documents)))
    records = []
    for proposal in extraction.review.proposals:
        category = proposal["category"]
        records.append(SymbolDecision(manifest_fingerprint=extraction.review.fingerprint,
            proposal_id=proposal["proposal_id"], proposal_fingerprint=digest(proposal),
            reviewer_id="synthetic-independent-review", reviewer_derivation="user", action="accept",
            rationale="Checked independently authored source and supplied mounting inputs.",
            resolved_unknowns=tuple(proposal["unknowns"]),
            placement=SymbolPlacement(registration_fingerprint=reg.fingerprint,
                entity_kind="equipment" if category == "equipment" else "device",
                entity_type="junction_box" if category == "unknown" else category,
                anchor_pt=centres[category], height_above_level_m=1.1,
                yaw_radians=0, remaining_unknowns=("physical size not supplied",))))
    outcome = materialize_reviewed_symbols(model(), extraction.review, runs=extraction.runs,
        documents=documents, images=images, journal_paths={req.fingerprint: journal.path}, decisions=records, registrations=[reg])
    assert len(outcome.model.electrical_devices) == 8
    assert len(outcome.model.electrical_equipment) == 1
    assert not outcome.model.circuits and not outcome.model.ports and not outcome.model.routes
    assert sum(item.quantity for item in extract_quantities(outcome.model).items) == 9
    plan = generate_plan(outcome.model, PlanSpec(id="all-electrical", level_id="upper",
        cut_height_m=1.1, view_depth_above_m=.1, view_depth_below_m=.1))
    assert "<svg" in view_to_svg(plan)
    assert len({key for p in plan.primitives for key in p.source_ids}) == 9
    assert all(p["accepted"] is False for p in outcome.review.proposals)
    assert all(e.confidence == .75 for e in outcome.model.electrical_devices)


def test_public_intake_reverifies_pixels_and_rejects_modified_manifest(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review, materialize_reviewed_symbols
    req, policy, adapters, documents, images, journal, calls, _ = symbol_source_fixture(tmp_path)
    with journal:
        extraction = extract_symbol_review([req], policy, adapters=adapters,
            documents=documents, images=images, journals={req.fingerprint: journal})
    forged = extraction.review.to_dict()
    forged["proposals"][0]["category"] = "detector"
    forged["proposals"][0]["confidence"] = 1.0
    with pytest.raises(ContractError, match="stale or modified"):
        materialize_reviewed_symbols(model(), SymbolReview.from_dict(forged), runs=extraction.runs,
            documents=documents, images=images, journal_paths={req.fingerprint: journal.path}, decisions=[], registrations=[])
    corrupted_images = {**images, "field": b"different pixels"}
    with pytest.raises(ContractError):
        materialize_reviewed_symbols(model(), extraction.review, runs=extraction.runs,
            documents=documents, images=corrupted_images, journal_paths={req.fingerprint: journal.path}, decisions=[], registrations=[])
    assert len(calls) == 1  # source verification never calls the provider


def test_missing_context_is_present_in_pending_review_and_cannot_silently_accept(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review, materialize_reviewed_symbols
    req, policy, adapters, documents, images, journal, _, _ = symbol_source_fixture(tmp_path, missing_notes=True)
    with journal:
        extraction = extract_symbol_review([req], policy, adapters=adapters,
            documents=documents, images=images, journals={req.fingerprint: journal})
    assert all(any(reason.startswith("missing_notes:") for reason in p["unknowns"])
               for p in extraction.review.proposals)
    pending = materialize_reviewed_symbols(model(), extraction.review, runs=extraction.runs,
        documents=documents, images=images, journal_paths={req.fingerprint: journal.path}, decisions=[], registrations=[])
    assert not pending.model.electrical_devices and not pending.model.electrical_equipment
    assert len(pending.dispositions) == 9


def test_cross_page_same_instance_label_requires_explicit_review_not_forced_merge():
    a, b = row("a"), row("b", page=2)
    a["instance_label"], b["instance_label"] = "EQ-8", " eq-8 "
    manifest = review(a, b)
    assert manifest.duplicate_candidates == (("a", "b"),)
    assert not apply(manifest, [decision(manifest)]).model.electrical_devices
    reg2 = registration(page=2)
    records = [replace(decision(manifest), overlap_resolution="Different instances despite repeated label."),
        replace(decision(manifest, "b", reg=reg2), overlap_resolution="Different instances despite repeated label.")]
    result = apply(manifest, records, [registration(), reg2])
    assert len(result.model.electrical_devices) == 2


def test_type_tags_never_force_cross_page_deduplication():
    a, b = row("a"), row("b", page=2)
    a["type_tag"] = b["type_tag"] = "T-2"
    assert review(a, b).duplicate_candidates == ()


def test_incompatible_history_is_not_overwritten():
    target = replace(model(), attributes={"ai_symbol_stage_runs": "caller-owned content"})
    with pytest.raises(ContractError, match="incompatible content"):
        apply(target=target)
    assert target.attributes["ai_symbol_stage_runs"] == "caller-owned content"


def test_returned_disposition_mutation_cannot_rewrite_audit_or_break_replay():
    manifest = review()
    result = apply(manifest)
    original = result.model.to_json()
    result.dispositions[0]["status"] = "forged"
    result.dispositions[0]["canonical_id"] = "forged:id"
    assert result.model.to_json() == original
    replay = apply(manifest, target=result.model)
    assert replay.model == result.model
    assert replay.dispositions[0]["status"] == "accept"


def test_public_stage_rejects_self_consistent_saved_output_that_differs_from_trusted_journal(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review, build_symbol_review
    from oabm.proposals.symbols import SymbolOutput
    req, policy, adapters, documents, images, journal, _, _ = symbol_source_fixture(tmp_path)
    with journal:
        extraction = extract_symbol_review([req], policy, adapters=adapters,
            documents=documents, images=images, journals={req.fingerprint: journal})
    run, = extraction.runs
    output = SymbolOutput.from_json(run.attempts[-1].raw_output)
    changed = replace(output, marks=(replace(output.marks[0], confidence=.99), *output.marks[1:]))
    forged_finish = replace(run.attempts[-1], raw_output=changed.to_json())
    forged_run = replace(run, attempts=(*run.attempts[:-1], forged_finish))
    with pytest.raises(ContractError):
        build_symbol_review([forged_run], documents=documents, images=images, journal_paths={req.fingerprint: journal.path})
    # The original saved result still verifies against the same persisted journal.
    assert build_symbol_review([run], documents=documents, images=images,
                               journal_paths={req.fingerprint: journal.path}) == extraction.review


def test_batch_uses_one_trusted_journal_per_request_and_rebuilds_after_close(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review, build_symbol_review
    from oabm.proposals.symbols import AttemptJournal
    req, policy, adapters, documents, images, first, calls, _ = symbol_source_fixture(tmp_path)
    second_req = replace(req, question=req.question + " Independently requested second interpretation.")
    second = AttemptJournal.create(tmp_path / "second-attempts.jsonl", run_id="second-request")
    journals = {req.fingerprint: first, second_req.fingerprint: second}
    with first, second:
        extraction = extract_symbol_review([req, second_req], policy, adapters=adapters,
            documents=documents, images=images, journals=journals)
    assert len(calls) == 2
    assert len(extraction.runs) == 2
    assert len(extraction.review.proposals) == 18  # all candidates retained for review
    assert len(AttemptJournal.read(first.path)) == len(AttemptJournal.read(second.path)) == 2
    assert build_symbol_review(extraction.runs, documents=documents, images=images,
        journal_paths={key: journal.path for key, journal in journals.items()}) == extraction.review


def test_shared_or_incomplete_journal_mapping_fails_before_provider_call(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review
    req, policy, adapters, documents, images, journal, calls, _ = symbol_source_fixture(tmp_path)
    second_req = replace(req, question=req.question + " Second requested interpretation.")
    with journal:
        with pytest.raises(ContractError, match="exactly cover"):
            extract_symbol_review([req, second_req], policy, adapters=adapters,
                documents=documents, images=images, journals={req.fingerprint: journal})
        with pytest.raises(ContractError, match="own attempt journal"):
            extract_symbol_review([req, second_req], policy, adapters=adapters,
                documents=documents, images=images,
                journals={req.fingerprint: journal, second_req.fingerprint: journal})
    assert calls == []
    assert journal.receipts == ()


def test_distinct_target_batches_send_complete_declared_context_without_other_target_leakage(tmp_path):
    import json
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review
    from oabm.proposals.symbols import AttemptJournal
    req, policy, adapters, documents, images, first, calls, _ = symbol_source_fixture(tmp_path)
    second_req = replace(req,
        images=tuple(replace(im, id="second-field") if im.id == "field" else im for im in req.images),
        target_image_ids=("second-field",))
    images = {**images, "second-field": images["field"]}
    second = AttemptJournal.create(tmp_path / "second-field.jsonl", run_id="second-field")
    original_adapter = adapters["fake-symbols"]

    def adapter(payload, **kwargs):
        assert set(kwargs["images"]) == {im["id"] for im in payload["images"]}
        reply = original_adapter(payload, **kwargs)
        if payload["target_image_ids"] == ["second-field"]:
            value = json.loads(reply.output_json)
            value["scanned_image_ids"] = ["second-field"]
            for mark in value["marks"]:
                mark["image_id"] = "second-field"
                mark["evidence_image_ids"] = ["second-field" if key == "field" else key
                                                for key in mark["evidence_image_ids"]]
            reply = replace(reply, output_json=json.dumps(value))
        return reply

    with first, second:
        extraction = extract_symbol_review([req, second_req], policy,
            adapters={"fake-symbols": adapter}, documents=documents, images=images,
            journals={req.fingerprint: first, second_req.fingerprint: second})
    assert len(extraction.review.proposals) == 18
    assert len(calls) == 2
    assert set(calls[0][1]["images"]) == {"field", "legend", "notes", "schedule"}
    assert set(calls[1][1]["images"]) == {"second-field", "legend", "notes", "schedule"}


def test_reused_journal_is_rejected_before_second_provider_call_without_receipt_changes(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review
    req, policy, adapters, documents, images, journal, calls, _ = symbol_source_fixture(tmp_path)
    with journal:
        extract_symbol_review([req], policy, adapters=adapters, documents=documents,
            images=images, journals={req.fingerprint: journal})
        original_bytes = journal.path.read_bytes()
        original_receipts = journal.receipts
        with pytest.raises(ContractError, match="empty attempt journals"):
            extract_symbol_review([req], policy, adapters=adapters, documents=documents,
                images=images, journals={req.fingerprint: journal})
        assert len(calls) == 1
        assert journal.path.read_bytes() == original_bytes
        assert journal.receipts == original_receipts


def test_nonempty_disk_journal_is_rejected_even_when_in_memory_receipts_are_empty(tmp_path):
    from oabm.importers.pdf_electrical.symbol_stage import extract_symbol_review
    from oabm.proposals.symbols import AttemptJournal
    req, policy, adapters, documents, images, original, calls, _ = symbol_source_fixture(tmp_path)
    with original:
        extract_symbol_review([req], policy, adapters=adapters, documents=documents,
            images=images, journals={req.fingerprint: original})
    saved_bytes = original.path.read_bytes()
    with AttemptJournal.create(tmp_path / "externally-populated.jsonl") as candidate:
        candidate.path.write_bytes(saved_bytes)
        assert candidate.receipts == ()
        with pytest.raises(ContractError, match="empty attempt journals"):
            extract_symbol_review([req], policy, adapters=adapters, documents=documents,
                images=images, journals={req.fingerprint: candidate})
        assert len(calls) == 1
        assert candidate.path.read_bytes() == saved_bytes
    assert original.path.read_bytes() == saved_bytes


def test_source_provenance_reconstruction_preserves_explicit_class_scope_and_attributes():
    source = row()
    source["provenance"][0].update(scope_paths=["device_type"], method="synthetic-source-reader")
    source["provenance"][0]["attributes"]["independent_evidence"] = {"source_token": "glyph-a"}
    manifest = review(source)
    result = apply(manifest)
    record = result.model.electrical_devices[0].provenance[0]
    assert record.derivation == "inferred"
    assert record.scope_paths == ("device_type",)
    assert record.method == "synthetic-source-reader"
    assert record.attributes == {"independent_evidence": {"source_token": "glyph-a"},
                                 "source_document_sha256": "a"*64}
    record.attributes["independent_evidence"]["source_token"] = "changed"
    assert manifest.proposals[0]["provenance"][0]["attributes"]["independent_evidence"]["source_token"] == "glyph-a"
