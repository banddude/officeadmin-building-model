"""Independent synthetic assertions through public source-verified intake APIs."""
import hashlib
import json
from dataclasses import replace

import pytest

from oabm.model import BuildingModel, CoordinateSystem, Level, Provenance, ContractError
from oabm.proposals.contracts import digest
from oabm.importers.pdf_electrical import PdfPageTransform
from oabm.importers.pdf_electrical.symbol_stage import (
    build_symbol_review, SymbolDecision, SymbolPlacement, SymbolRegistration)
from oabm.importers.pdf_electrical.placement_assertions import (
    PlacementEvidence, RegistrationControl, PlacementAssertion,
    PlacementAssertionReview, materialize_asserted_symbols)
from test_symbol_proposals import fixture, run


def setup(tmp_path):
    tmp_path.mkdir(parents=True,exist_ok=True)
    request,images,documents,_=fixture()
    result=run(tmp_path)
    journals={request.fingerprint:tmp_path/"attempts.jsonl"}
    review=build_symbol_review((result,),documents=documents,images=images,journal_paths=journals)
    row,=review.proposals
    model=BuildingModel(model_id="synthetic-assertions", schema_version="1.3.0",
        coordinate_system=CoordinateSystem(frame_id="building"),
        levels=(Level(id="floor",elevation_m=3),))
    source=request.images[0].source
    prov=Provenance(source_kind="project-design",source_id="synthetic:design",
                    derivation="user",confidence=.8)
    design=b'{"design":"explicit synthetic placement","datum":"local-project"}'
    sha=hashlib.sha256(design).hexdigest()
    evidence=PlacementEvidence(source_id=prov.source_id,document_sha256=sha,
        provenance=prov,kind="project_decision",locator="Explicit caller-approved design record.")
    source_evidence=PlacementEvidence(source_id=source.source_id,
        document_sha256=source.document_sha256,image_id="field",page=source.page,
        bounds_pt=tuple(row["bounds_pt"]),kind="source_fact",locator="Synthetic source mark.",
        provenance=Provenance(source_kind="pdf",source_id=source.source_id,
            page=source.page,derivation="observed",confidence=.6))
    reg=SymbolRegistration(source_id=source.source_id,document_sha256=source.document_sha256,
        page=source.page,source_bbox_pt=(0,0,200,100),level_id="floor",
        transform=PdfPageTransform(frame_id="building",m11_m_per_pt=.1,m12_m_per_pt=0,
            m21_m_per_pt=0,m22_m_per_pt=.1,tx_m=1,ty_m=2,z_m=3),
        provenance=(prov,))
    placement=SymbolPlacement(registration_fingerprint=reg.fingerprint,entity_kind="device",
        entity_type="dimmer",anchor_pt=tuple(row["anchor_pt"]),height_above_level_m=1.2,
        yaw_radians=.5,remaining_unknowns=tuple(row["unknowns"]))
    decision=SymbolDecision(manifest_fingerprint=review.fingerprint,
        proposal_id=row["proposal_id"],proposal_fingerprint=digest(row),
        reviewer_id="synthetic-reviewer",reviewer_derivation="user",
        rationale="Explicit reviewed synthetic design.",action="accept",placement=placement)
    args=dict(runs=(result,),documents=documents,images=images,journal_paths=journals,
              decisions=(decision,),registrations=(reg,),evidence_documents={sha:design})
    common=dict(proposal_id=row["proposal_id"],proposal_fingerprint=digest(row),
                frame_id="building",role="designed",evidence=(evidence,))
    fields=[
        dict(field="registration_xy",value=(.1,0.,0.,.1,1.,2.),unit="m/pt;m",
             reference_id=reg.fingerprint,basis="displayed_bottom_pt_to_canonical_m",role="project_defined"),
        dict(field="level_elevation",value=3.,unit="m",reference_id="local-project",
             basis="canonical_datum",role="project_defined"),
        dict(field="anchor",value=tuple(row["anchor_pt"]),unit="pt",
             reference_id=reg.fingerprint,basis="displayed_bottom_pt",
             role="source_observed",evidence=(source_evidence,)),
        dict(field="elevation",value=1.2,unit="m",reference_id="floor",basis="above_level"),
        dict(field="yaw",value=.5,unit="rad",reference_id="building",basis="canonical_product_axis"),
        dict(field="entity_type",value="dimmer",unit="token",reference_id="device",basis="canonical_type")]
    facts=tuple(PlacementAssertion(**{**common,**f,"assertion_id":f["field"]}) for f in fields)
    return model,review,args,facts


def apply(model,review,args,facts,**review_changes):
    envelope=PlacementAssertionReview.capture(model,review,decisions=args["decisions"],
        registrations=args["registrations"],canonical_datum_id="local-project",reviewer_id="synthetic-reviewer",
        reviewer_derivation="user",rationale="Explicit project review.",assertions=facts)
    envelope=replace(envelope,**review_changes)
    assert PlacementAssertionReview.from_json(envelope.to_json())==envelope
    return materialize_asserted_symbols(model,review,assertion_review=envelope,**args)


def test_project_design_intake_preserves_roles_coordinates_and_original_observation(tmp_path):
    model,review,args,facts=setup(tmp_path)
    before=model.to_json();original=review.to_json()
    outcome=apply(model,review,args,facts)
    entity,=outcome.model.electrical_devices
    # The fixture's independently rendered source anchor is (70,66) pt.
    assert entity.pose.position.x==pytest.approx(8)
    assert entity.pose.position.y==pytest.approx(8.6)
    assert entity.pose.position.z==pytest.approx(4.2)
    assert entity.confidence==.6
    assert entity.provenance[0].derivation=="inferred"  # observation never upgraded by review
    records=[p for p in entity.provenance if p.source_kind=="placement-assertion-review"]
    assert len(records)==6
    assert {p.attributes["role"] for p in records}=={"project_defined","source_observed","designed"}
    assert next(p for p in records if p.source_element_id=="elevation").scope_paths==("pose.position.z",)
    assert model.to_json()==before and review.to_json()==original
    assert BuildingModel.from_json(outcome.model.to_json())==outcome.model


@pytest.mark.parametrize("field",["registration_xy","level_elevation","anchor","elevation","yaw","entity_type"])
def test_each_missing_required_fact_defers_without_default_or_canonical_entity(tmp_path,field):
    model,review,args,facts=setup(tmp_path)
    outcome=apply(model,review,args,tuple(f for f in facts if f.field!=field))
    assert not outcome.model.electrical_devices
    assert outcome.dispositions[0]["status"]=="defer"
    results=outcome.model.attributes["placement_assertion_reviews"][0]["results"]
    assert "missing:"+field in results[0]["unresolved"]


def test_conflicting_and_explicit_unknown_height_remain_visible(tmp_path):
    model,review,args,facts=setup(tmp_path)
    height=next(f for f in facts if f.field=="elevation")
    conflict=replace(height,assertion_id="alternate-height",value=2.)
    outcome=apply(model,review,args,(*facts,conflict))
    assert not outcome.model.electrical_devices
    assert "conflict:elevation" in outcome.model.attributes["placement_assertion_reviews"][0]["results"][0]["unresolved"]
    unknown=replace(height,role="unknown",value=None,evidence=())
    outcome=apply(model,review,args,tuple(unknown if f.field=="elevation" else f for f in facts))
    assert not outcome.model.electrical_devices
    saved=outcome.model.attributes["placement_assertion_reviews"][0]["review"]["assertions"]
    assert next(f for f in saved if f["field"]=="elevation")["value"] is None


@pytest.mark.parametrize("field",["registration_xy","level_elevation","anchor","elevation","yaw"])
def test_diagram_conventions_cannot_satisfy_physical_placement(tmp_path,field):
    model,review,args,facts=setup(tmp_path)
    outcome=apply(model,review,args,tuple(replace(f,role="diagram_convention") if f.field==field else f for f in facts))
    assert not outcome.model.electrical_devices
    assert "physical_fact_unresolved:"+field in outcome.model.attributes["placement_assertion_reviews"][0]["results"][0]["unresolved"]


def test_absolute_elevation_must_name_same_datum_and_convert_to_level_relative(tmp_path):
    model,review,args,facts=setup(tmp_path)
    absolute=tuple(replace(f,value=4.2,reference_id="local-project",basis="absolute_in_datum")
                   if f.field=="elevation" else f for f in facts)
    entity,=apply(model,review,args,absolute).model.electrical_devices
    assert entity.pose.position.z==pytest.approx(4.2)
    wrong=tuple(replace(f,reference_id="other-datum") if f.field=="elevation" else f for f in absolute)
    outcome=apply(model,review,args,wrong)
    assert not outcome.model.electrical_devices


@pytest.mark.parametrize("change",[
    {"field":"elevation","basis":"AFF"},
    {"field":"elevation","basis":"above_ceiling"},
    {"field":"yaw","basis":"drawn_symbol_angle"},
    {"field":"anchor","frame_id":"unrelated-frame"},
    {"field":"registration_xy","value":(.2,0.,0.,.2,1.,2.)},
    {"field":"level_elevation","value":0.},
    {"field":"entity_type","value":"switch"},
])
def test_incompatible_frame_datum_orientation_and_values_defer(tmp_path,change):
    model,review,args,facts=setup(tmp_path)
    field=change["field"]
    changed=tuple(replace(f,**change) if f.field==field else f for f in facts)
    assert not apply(model,review,args,changed).model.electrical_devices


def test_units_and_measured_evidence_fail_closed(tmp_path):
    model,review,args,facts=setup(tmp_path)
    height=next(f for f in facts if f.field=="elevation")
    with pytest.raises(ContractError,match="unit"):
        replace(height,unit="in")
    with pytest.raises(ContractError,match="field-measurement"):
        apply(model,review,args,tuple(replace(f,role="field_measured") if f.field=="elevation" else f for f in facts))
    measured=replace(height.evidence[0],kind="measurement_record",provenance=replace(height.evidence[0].provenance,
        source_kind="field-measurement",derivation="observed"))
    result=apply(model,review,args,tuple(replace(f,role="field_measured",evidence=(measured,))
        if f.field=="elevation" else f for f in facts))
    assert len(result.model.electrical_devices)==1
    field_record=next(p for p in result.model.electrical_devices[0].provenance
                      if p.source_kind=="placement-assertion-review" and p.source_element_id=="elevation")
    assert field_record.attributes["role"]=="field_measured"


@pytest.mark.parametrize("kind",["model","review","decision","registration","association","evidence","image","bounds"])
def test_stale_or_wrong_evidence_bindings_fail_atomically(tmp_path,kind):
    model,review,args,facts=setup(tmp_path)
    before=model.to_json()
    changes={}
    if kind in ("model","review","decision","registration"):
        changes[{ "model":"model_fingerprint","review":"symbol_review_fingerprint",
            "decision":"decisions_fingerprint","registration":"registrations_fingerprint"}[kind]]="0"*64
    elif kind=="association":
        facts=tuple(replace(f,proposal_fingerprint="0"*64) for f in facts)
    elif kind=="evidence":
        args["evidence_documents"]={k:b"altered" for k in args["evidence_documents"]}
    else:
        f=next(f for f in facts if f.field=="anchor")
        ev=replace(f.evidence[0],image_id="absent") if kind=="image" else replace(
            f.evidence[0],bounds_pt=(-10.,-10.,-1.,-1.))
        facts=tuple(replace(a,evidence=(ev,)) if a.field=="anchor" else a for a in facts)
    with pytest.raises(ContractError):
        apply(model,review,args,facts,**changes)
    assert model.to_json()==before


def test_observed_registration_requires_controls_and_checks_residuals(tmp_path):
    model,review,args,facts=setup(tmp_path)
    anchor=next(f for f in facts if f.field=="anchor")
    # Source control evidence must be the actual registered source page.
    source=replace(anchor.evidence[0],bounds_pt=(50.,50.,100.,90.))
    controls=tuple(RegistrationControl(source_pt=p,canonical_xy_m=q,independent_check=c)
        for p,q,c in (((60,60),(7,8),False),((80,60),(9,8),False),((70,70),(8,9),True)))
    observed=tuple(replace(f,role="source_observed",evidence=(source,),controls=controls,tolerance_m=.001)
                   if f.field=="registration_xy" else f for f in facts)
    assert len(apply(model,review,args,observed).model.electrical_devices)==1
    for bad in ((), controls[:2], tuple(replace(c,canonical_xy_m=(100,100)) for c in controls)):
        changed=tuple(replace(f,controls=bad) if f.field=="registration_xy" else f for f in observed)
        assert not apply(model,review,args,changed).model.electrical_devices


def test_per_device_association_rejects_an_absent_proposal(tmp_path):
    model,review,args,facts=setup(tmp_path)
    with pytest.raises(ContractError,match="association"):
        apply(model,review,args,tuple(replace(f,proposal_id="different-device") for f in facts))


def test_repeatability_without_mutating_the_input_snapshot(tmp_path):
    model,review,args,facts=setup(tmp_path)
    assert apply(model,review,args,facts).model.to_json()==apply(model,review,args,facts).model.to_json()


def test_conflicting_control_metadata_is_not_washed_out(tmp_path):
    model,review,args,facts=setup(tmp_path)
    reg=next(f for f in facts if f.field=="registration_xy")
    control=RegistrationControl(source_pt=(60,60),canonical_xy_m=(100,100),independent_check=True)
    conflict=replace(reg,assertion_id="contradictory-control",controls=(control,),tolerance_m=.001)
    outcome=apply(model,review,args,(*facts,conflict))
    assert not outcome.model.electrical_devices
    assert "conflict:registration_xy" in outcome.model.attributes["placement_assertion_reviews"][0]["results"][0]["unresolved"]


def test_coordinate_evidence_must_cover_asserted_device_point(tmp_path):
    model,review,args,facts=setup(tmp_path)
    anchor=next(f for f in facts if f.field=="anchor")
    evidence=replace(anchor.evidence[0],bounds_pt=(60,58,61,59))
    with pytest.raises(ContractError,match="does not cover"):
        apply(model,review,args,tuple(replace(f,evidence=(evidence,)) if f.field=="anchor" else f for f in facts))


def test_level_must_name_envelope_datum(tmp_path):
    model,review,args,facts=setup(tmp_path)
    result=apply(model,review,args,facts,canonical_datum_id="unrelated-datum")
    assert not result.model.electrical_devices


def test_actual_model_change_invalidates_saved_review(tmp_path):
    model,review,args,facts=setup(tmp_path)
    envelope=PlacementAssertionReview.capture(model,review,decisions=args["decisions"],
        registrations=args["registrations"],canonical_datum_id="local-project",
        reviewer_id="synthetic-reviewer",reviewer_derivation="user",rationale="Saved review.",assertions=facts)
    changed=replace(model,levels=(replace(model.levels[0],elevation_m=4),))
    with pytest.raises(ContractError,match="stale"):
        materialize_asserted_symbols(changed,review,assertion_review=envelope,**args)


def test_known_value_requires_bound_provenance(tmp_path):
    _,_,_,facts=setup(tmp_path)
    with pytest.raises(ContractError,match="requires immutable"):
        replace(facts[0],evidence=())
    with pytest.raises(ContractError,match="source mismatch"):
        replace(facts[0].evidence[0],source_id="different-owner")


def test_agent_review_remains_inferred(tmp_path):
    model,review,args,facts=setup(tmp_path)
    result=apply(model,review,args,facts,reviewer_derivation="inferred")
    records=[p for p in result.model.electrical_devices[0].provenance
             if p.source_kind=="placement-assertion-review"]
    assert records and all(p.derivation=="inferred" for p in records)
    assert next(p for p in records if p.source_element_id=="level_elevation").attributes["role"]=="project_defined"


def test_legend_examples_cannot_become_physical_height_facts(tmp_path):
    model,review,args,facts=setup(tmp_path)
    anchor=next(f for f in facts if f.field=="anchor")
    height=next(f for f in facts if f.field=="elevation")
    example=replace(anchor.evidence[0],kind="legend_example")
    changed=tuple(replace(f,role="source_observed",evidence=(example,)) if f.field=="elevation" else f for f in facts)
    with pytest.raises(ContractError,match="legend example"):
        apply(model,review,args,changed)
    unknown=tuple(replace(f,role="unknown",value=None,evidence=(example,))
                  if f.field=="elevation" else f for f in facts)
    pending=apply(model,review,args,unknown)
    assert not pending.model.electrical_devices
    saved=pending.model.attributes["placement_assertion_reviews"][0]["review"]["assertions"]
    assert next(f for f in saved if f["field"]=="elevation")["evidence"][0]["kind"]=="legend_example"


def test_project_definition_requires_an_explicit_project_decision_record(tmp_path):
    model,review,args,facts=setup(tmp_path)
    changed=tuple(replace(f,evidence=tuple(replace(e,kind="source_fact") for e in f.evidence))
                  if f.field=="level_elevation" else f for f in facts)
    with pytest.raises(ContractError,match="project-decision"):
        apply(model,review,args,changed)


def test_registration_controls_can_be_covered_by_separate_evidence_windows(tmp_path):
    model,review,args,facts=setup(tmp_path)
    ev=next(f for f in facts if f.field=="anchor").evidence[0]
    evidence=(replace(ev,bounds_pt=(50,50,65,90)),replace(ev,bounds_pt=(65,50,100,90)))
    controls=tuple(RegistrationControl(source_pt=p,canonical_xy_m=q,independent_check=c)
        for p,q,c in (((60,60),(7,8),False),((80,60),(9,8),False),((70,70),(8,9),True)))
    changed=tuple(replace(f,role="source_observed",evidence=evidence,controls=controls,tolerance_m=.001)
                  if f.field=="registration_xy" else f for f in facts)
    assert len(apply(model,review,args,changed).model.electrical_devices)==1


def test_source_coordinate_evidence_cannot_cite_a_different_page(tmp_path):
    model,review,args,facts=setup(tmp_path)
    anchor=next(f for f in facts if f.field=="anchor")
    other_page=1 if anchor.evidence[0].page!=1 else 2
    ev=replace(anchor.evidence[0],page=other_page,
               provenance=replace(anchor.evidence[0].provenance,page=other_page))
    with pytest.raises(ContractError,match="image/page mismatch"):
        apply(model,review,args,tuple(replace(f,evidence=(ev,)) if f.field=="anchor" else f for f in facts))


def test_two_actual_proposals_cannot_exchange_assertion_fingerprints(tmp_path):
    model,old_review,args,facts=setup(tmp_path/"old")
    _,_,_,output=fixture()
    mark=replace(output.marks[0],bounds=(.5,.5,.6,.6),anchor=(.55,.55),instance_label="second-source-mark")
    (tmp_path/"new").mkdir()
    result=run(tmp_path/"new",output=replace(output,marks=(*output.marks,mark)))
    args={**args,"runs":(result,),"journal_paths":{result.request.fingerprint:tmp_path/"new/attempts.jsonl"}}
    review=build_symbol_review((result,),documents=args["documents"],images=args["images"],journal_paths=args["journal_paths"])
    first=next(r for r in review.proposals if tuple(r["anchor_pt"])==args["decisions"][0].placement.anchor_pt)
    second=next(r for r in review.proposals if r["proposal_id"]!=first["proposal_id"])
    args["decisions"]=(replace(args["decisions"][0],manifest_fingerprint=review.fingerprint,
        proposal_id=first["proposal_id"],proposal_fingerprint=digest(first)),)
    facts=tuple(replace(f,proposal_id=first["proposal_id"],proposal_fingerprint=digest(first)) for f in facts)
    assert len(apply(model,review,args,facts).model.electrical_devices)==1
    crossed=(replace(facts[0],proposal_id=second["proposal_id"]),*facts[1:])
    with pytest.raises(ContractError,match="association"):
        apply(model,review,args,crossed)


@pytest.mark.parametrize("jitter",[0.,.001])
def test_fitting_point_cannot_be_reused_as_independent_check(tmp_path,jitter):
    model,review,args,facts=setup(tmp_path)
    ev=next(f for f in facts if f.field=="anchor").evidence[0]
    controls=tuple(RegistrationControl(source_pt=p,canonical_xy_m=q,independent_check=False)
        for p,q in (((60,60),(7,8)),((80,60),(9,8)),((70,70),(8,9))))
    reused=RegistrationControl(source_pt=(60+jitter,60),canonical_xy_m=(7+.1*jitter,8),independent_check=True)
    changed=tuple(replace(f,role="source_observed",evidence=(replace(ev,bounds_pt=(50,50,100,90)),),
                          controls=(*controls,reused),tolerance_m=.001)
                  if f.field=="registration_xy" else f for f in facts)
    outcome=apply(model,review,args,changed)
    assert not outcome.model.electrical_devices
    assert "reused_fit_control_as_independent_check" in outcome.model.attributes[
        "placement_assertion_reviews"][0]["results"][0]["unresolved"]


def duplicate_case(tmp_path):
    model,_,args,facts=setup(tmp_path/"original")
    _,_,_,output=fixture()
    second=replace(output.marks[0],instance_label="duplicate-source-mark")
    (tmp_path/"paired").mkdir()
    result=run(tmp_path/"paired",output=replace(output,marks=(*output.marks,second)))
    args={**args,"runs":(result,),"journal_paths":{result.request.fingerprint:tmp_path/"paired/attempts.jsonl"}}
    review=build_symbol_review((result,),documents=args["documents"],images=args["images"],journal_paths=args["journal_paths"])
    first,second=review.proposals
    survivor=replace(args["decisions"][0],manifest_fingerprint=review.fingerprint,
        proposal_id=first["proposal_id"],proposal_fingerprint=digest(first))
    duplicate=replace(survivor,proposal_id=second["proposal_id"],proposal_fingerprint=digest(second),
        action="duplicate",placement=None,duplicate_of=first["proposal_id"])
    args["decisions"]=(duplicate,survivor)  # Dependency may precede survivor.
    facts=tuple(replace(f,proposal_id=first["proposal_id"],proposal_fingerprint=digest(first)) for f in facts)
    return model,review,args,facts


def test_missing_survivor_height_propagates_duplicate_deferral_and_keeps_evidence(tmp_path):
    model,review,args,facts=duplicate_case(tmp_path)
    outcome=apply(model,review,args,tuple(f for f in facts if f.field!="elevation"))
    assert not outcome.model.electrical_devices
    assert {d["status"] for d in outcome.dispositions}=={"defer"}
    entry=outcome.model.attributes["placement_assertion_reviews"][0]
    assert sorted(d["action"] for d in entry["requested_decisions"])==["accept","duplicate"]
    dependent=next(r for r in entry["results"] if r["requested_action"]=="duplicate")
    assert any(r.startswith("duplicate_survivor_deferred:") for r in dependent["unresolved"])
    assert outcome.review.to_json()==review.to_json()
    assert next(d for d in entry["requested_decisions"] if d["action"]=="duplicate")["duplicate_of"]


@pytest.mark.parametrize("kind",["wrong_survivor","stale_duplicate","duplicate_id","unacknowledged_unknown"])
def test_group_deferral_does_not_mask_malformed_original_decisions(tmp_path,kind):
    model,review,args,facts=duplicate_case(tmp_path)
    duplicate,survivor=args["decisions"]
    if kind=="wrong_survivor": duplicate=replace(duplicate,duplicate_of="absent")
    elif kind=="stale_duplicate": duplicate=replace(duplicate,proposal_fingerprint="0"*64)
    elif kind=="duplicate_id": args["decisions"]=(duplicate,survivor,survivor)
    else: survivor=replace(survivor,placement=replace(survivor.placement,remaining_unknowns=()))
    if kind!="duplicate_id":args["decisions"]=(duplicate,survivor)
    before=model.to_json()
    with pytest.raises(ContractError):
        apply(model,review,args,tuple(f for f in facts if f.field!="elevation"))
    assert model.to_json()==before


def test_independent_registration_check_survives_three_fit_controls(tmp_path):
    model,review,args,facts=setup(tmp_path)
    ev=next(f for f in facts if f.field=="anchor").evidence[0]
    controls=tuple(RegistrationControl(source_pt=p,canonical_xy_m=q,independent_check=c)
        for p,q,c in (((60,60),(7,8),False),((80,60),(9,8),False),
                      ((70,70),(8,9),False),((90,80),(10,10),True)))
    changed=tuple(replace(f,role="source_observed",evidence=(replace(ev,bounds_pt=(50,50,100,90)),),
                          controls=controls,tolerance_m=.001)
                  if f.field=="registration_xy" else f for f in facts)
    assert len(apply(model,review,args,changed).model.electrical_devices)==1


@pytest.mark.parametrize("kind",["duplicate_fit","collinear_check","tolerance_collinear_check"])
def test_registration_rejects_duplicate_fit_and_collinear_check_evidence(tmp_path,kind):
    model,review,args,facts=setup(tmp_path)
    ev=next(f for f in facts if f.field=="anchor").evidence[0]
    controls=[RegistrationControl(source_pt=p,canonical_xy_m=q,independent_check=False)
              for p,q in (((60,60),(7,8)),((80,60),(9,8)))]
    if kind=="duplicate_fit":
        controls.append(controls[0])
        check=RegistrationControl(source_pt=(70,70),canonical_xy_m=(8,9),independent_check=True)
        reason="duplicate_registration_fit_control"
    else:
        jitter=.001 if kind=="tolerance_collinear_check" else 0.
        check=RegistrationControl(source_pt=(90,60+jitter),canonical_xy_m=(10,8+.1*jitter),independent_check=True)
        reason="degenerate:registration_controls"
    controls.append(check)
    changed=tuple(replace(f,role="source_observed",evidence=(replace(ev,bounds_pt=(50,50,100,90)),),
                          controls=tuple(controls),tolerance_m=.001)
                  if f.field=="registration_xy" else f for f in facts)
    result=apply(model,review,args,changed)
    assert not result.model.electrical_devices
    assert reason in result.model.attributes["placement_assertion_reviews"][0]["results"][0]["unresolved"]


def test_complete_duplicate_survivor_still_admits_exactly_one_entity(tmp_path):
    model,review,args,facts=duplicate_case(tmp_path)
    result=apply(model,review,args,facts)
    assert len(result.model.electrical_devices)==1
    assert {d["status"] for d in result.dispositions}=={"accept","duplicate"}
    ids={d["canonical_id"] for d in result.dispositions}
    assert ids=={result.model.electrical_devices[0].id}
