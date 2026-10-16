"""Independent synthetic fixtures; no private plans or reviewed labels."""
import hashlib
import io
from dataclasses import replace

import pytest
from PIL import Image

from oabm.model import ContractError
from oabm.proposals import Endpoint
from oabm.proposals.takeoff import (
    VERSION, TASK, VALIDATOR, SourcePage, SourceImage, TakeoffRequest,
    ProposedMark, TakeoffOutput, TakeoffResult, run_takeoff_experiment,
)


def fixture():
    page = SourcePage(source_id="synthetic:lighting", document_sha256="a"*64,
                      page=2, width_pt=200, height_pt=100, width_px=400, height_px=200,
                      media_box_pt=(0,0,200,100), rotation=0)
    images = {}
    refs = []
    for name, role, box in [
        ("field", "target", (100, 20, 300, 180)),
        ("legend", "legend", (0, 0, 100, 100)),
        ("schedule", "schedule", (0, 100, 100, 200)),
    ]:
        buf = io.BytesIO()
        Image.new("RGB", (box[2]-box[0], box[3]-box[1]), "white").save(buf, format="PNG")
        images[name] = buf.getvalue()
        refs.append(SourceImage(id=name, source=page, role=role, crop_px=box,
                                sha256=hashlib.sha256(images[name]).hexdigest()))
    request = TakeoffRequest(
        version=VERSION, task=TASK, validator_version=VALIDATOR, prompt_version="synthetic-v1",
        question="Locate visible lighting marks inside this bounded region.",
        images=tuple(refs), target_image_id="field", allowed_types=("wall_sconce",),
        allowed_tags=("F1",), confidence_threshold=.9,
    )
    endpoint = Endpoint(adapter_id="fake", provider_id="synthetic", model_alias="vision-test")
    output = TakeoffOutput(version=VERSION, items=(
        ProposedMark(bounds=(.1,.2,.3,.4), type_proposal="wall_sconce", fixture_tag="F1",
                     confidence=.8, source_mark="Small circle and wall tick beside F1.",
                     evidence_image_ids=("field","legend","schedule"), unknowns=("scope unknown",)),
    ), coverage_notes=("One bounded synthetic region scanned; completeness needs review.",))
    return request, endpoint, images, output


def run(request=None, output=None, adapter=None):
    req, endpoint, images, raw = fixture()
    req = request or req
    raw = output or raw
    return run_takeoff_experiment(req, endpoint, images=images, adapters={
        "fake": adapter or (lambda *args, **kwargs: raw.to_dict()),
    }, experiment_reason="Explicit synthetic regression experiment")


def test_roundtrip_coordinate_conversion_and_inferred_provenance():
    req, endpoint, _, _ = fixture()
    result = run()
    assert TakeoffResult.from_json(result.to_json()) == result
    assert result.validate_against(req, endpoint) == result
    row, = result.unresolved_observations()
    assert row["bounds_px"] == pytest.approx([120,52,160,84])
    assert row["bounds_normalized"] == pytest.approx([.3,.26,.4,.42])
    assert row["bounds_pt"] == pytest.approx([60,58,80,74])
    assert row["position_pt"] == pytest.approx({"x":70,"y":66})
    assert row["page"] == 2
    assert row["review_state"] == "pending"
    assert row["accepted"] is False
    assert "below_configured_confidence_threshold" in row["unknowns"]
    assert row["provenance"][0]["source_kind"] == "ai-proposed"
    assert row["provenance"][0]["derivation"] == "inferred"


def test_ids_do_not_depend_on_tag_confidence_type_or_iteration_order():
    _, _, _, raw = fixture()
    first = run()
    second_mark = replace(raw.items[0], bounds=(.6,.6,.7,.7))
    forward = run(output=replace(raw, items=(raw.items[0],second_mark)))
    reverse = run(output=replace(raw, items=(second_mark,raw.items[0])))
    assert [r["source_element_id"] for r in forward.unresolved_observations()] == [r["source_element_id"] for r in reverse.unresolved_observations()]
    # Raw order remains captured in the output fingerprint, not rewritten.
    assert forward.raw_output.items == tuple(reversed(reverse.raw_output.items))
    changed = run(output=replace(raw, items=(replace(raw.items[0], fixture_tag=None, confidence=.5),)))
    assert first.unresolved_observations()[0]["source_element_id"] == changed.unresolved_observations()[0]["source_element_id"]
    assert first.unresolved_observations()[0]["proposal_id"] != changed.unresolved_observations()[0]["proposal_id"]


@pytest.mark.parametrize("bounds", [
    (0,0,0,1), (-.1,0,1,1), (0,0,1.1,1), (0,.5,1,.4),
    (float("nan"),0,1,1), (False,0,1,1),
])
def test_invalid_or_nonfinite_geometry_rejected(bounds):
    _, _, _, raw = fixture()
    with pytest.raises(ContractError):
        replace(raw.items[0], bounds=bounds)


@pytest.mark.parametrize("extra", [
    {"circuits":[{"number":1}]}, {"accepted":True},
    {"canonical_entities":[{}]}, {"review_state":"confirmed"},
])
def test_provider_cannot_promote_or_invent_canonical_state(extra):
    _, _, _, output = fixture()
    raw = output.to_dict()
    raw.update(extra)
    with pytest.raises(ContractError):
        run(adapter=lambda *args, **kwargs: raw)


@pytest.mark.parametrize("change", [
    {"evidence_image_ids":("field","phantom")},
    {"evidence_image_ids":("legend","schedule")},
    {"evidence_image_ids":("field","schedule")},
    {"evidence_image_ids":("field","legend")},
    {"type_proposal":"invented"},
    {"fixture_tag":"invented"},
])
def test_unbound_evidence_and_classification_fail(change):
    _, _, _, raw = fixture()
    with pytest.raises(ContractError):
        run(output=replace(raw, items=(replace(raw.items[0], **change),)))


def test_unknown_type_requires_reason_and_remains_unresolved():
    _, _, _, raw = fixture()
    with pytest.raises(ContractError):
        replace(raw.items[0],type_proposal=None,unknowns=())
    output = replace(raw, items=(replace(raw.items[0], type_proposal=None,fixture_tag=None,
                                        evidence_image_ids=("field",)),))
    assert run(output=output).unresolved_observations()[0]["type_proposal"] is None


def test_exact_duplicate_marks_rejected():
    _, _, _, raw = fixture()
    with pytest.raises(ContractError):
        run(output=replace(raw,items=(raw.items[0],raw.items[0])))


def test_pixel_bytes_and_dimensions_checked_before_call():
    req, endpoint, images, raw = fixture()
    calls=[]
    images["field"] += b"changed"
    with pytest.raises(ContractError):
        run_takeoff_experiment(req,endpoint,adapters={"fake":lambda *a,**k:calls.append(1)},
                              images=images,experiment_reason="test")
    assert calls == []
    ref=req.target
    buf=io.BytesIO()
    Image.new("RGB",(1,1)).save(buf,format="PNG")
    wrong=buf.getvalue()
    with pytest.raises(ContractError):
        replace(ref,sha256=hashlib.sha256(wrong).hexdigest()).validate_bytes(wrong)


def test_request_output_and_endpoint_staleness():
    req, endpoint, _, _ = fixture()
    result=run()
    with pytest.raises(ContractError):
        result.validate_against(replace(req,question="A different bounded question."),endpoint)
    with pytest.raises(ContractError):
        result.validate_against(req,replace(endpoint,model_alias="different"))
    data=result.to_dict()
    data["raw_output"]["items"][0]["confidence"]=.7
    with pytest.raises(ContractError):
        TakeoffResult.from_dict(data)
    data=result.to_dict()
    data["accepted"]=True
    with pytest.raises(ContractError):
        TakeoffResult.from_dict(data)


def test_source_revision_changes_source_mark_identity():
    req, _, _, _ = fixture()
    changed=replace(req,images=tuple(replace(im,source=replace(im.source,document_sha256="b"*64))
                                    for im in req.images))
    assert run().unresolved_observations()[0]["source_element_id"] != run(request=changed).unresolved_observations()[0]["source_element_id"]


def test_adapter_mutation_cannot_change_recorded_input_or_images():
    req, endpoint, images, output=fixture()
    original=req.to_json()
    def adapter(document, *, images, **kwargs):
        document["question"]="mutated"
        images.clear()
        assert kwargs == dict(provider_id="synthetic",model_alias="vision-test",prompt_version="synthetic-v1")
        return output.to_dict()
    result=run_takeoff_experiment(req,endpoint,images=images,adapters={"fake":adapter},
                                 experiment_reason="Explicit test")
    assert result.request.to_json() == original
    assert len(images) == 3
    row=result.unresolved_observations()[0]
    row["provenance"][0]["attributes"].clear()
    assert result.unresolved_observations()[0]["provenance"][0]["attributes"]


def test_no_automatic_retry_on_invalid_output_or_availability():
    from oabm.proposals import AdapterAvailabilityError
    for exc in (AdapterAvailabilityError("unavailable"), RuntimeError("provider failed")):
        calls=[]
        def failing(*args,**kwargs):
            calls.append(1)
            raise exc
        with pytest.raises(type(exc)):
            run(adapter=failing)
        assert calls == [1]


def test_bounds_budgets_and_strict_decode():
    req, _, _, raw=fixture()
    with pytest.raises(ContractError):
        replace(req,images=tuple(replace(req.target,id=str(i)) for i in range(25)))
    with pytest.raises(ContractError):
        replace(raw,items=raw.items*129)
    with pytest.raises(ContractError):
        replace(req,confidence_threshold=True)
    with pytest.raises(ContractError):
        replace(req,images=(req.target,))
    with pytest.raises(ContractError):
        TakeoffRequest.from_json('{"version":"1","version":"2"}')
    data=req.to_dict()
    data["unrecognized"]=1
    with pytest.raises(ContractError):
        TakeoffRequest.from_dict(data)


@pytest.mark.parametrize("rotation", [0,90,180,270])
def test_rotated_nonzero_origin_page_uses_existing_display_transform(rotation):
    from pypdf import PageObject
    from pypdf.generic import RectangleObject
    from oabm.importers.pdf_display import page_display_transform
    page=PageObject.create_blank_page(width=200,height=100)
    page.mediabox=RectangleObject([-50,-20,150,80])
    page.rotate(rotation)
    shared=page_display_transform(page)
    source=SourcePage.capture(page,source_id="synthetic:offset",document_sha256="d"*64,
                              page=1,width_px=int(shared.displayed_width_pt*2),
                              height_px=int(shared.displayed_height_pt*2))
    assert source.display_transform == shared
    expected={0:(10,10),90:(10,190),180:(190,90),270:(90,10)}[rotation]
    assert source.display_transform.apply(-40,-10) == expected
    assert source.media_box_pt == (-50,-20,150,80)
    assert SourcePage.from_json(source.to_json()) == source
    with pytest.raises(ContractError):
        replace(source,width_pt=source.width_pt+1)


def test_source_page_identity_independent_of_render_resolution():
    req, _, _, _=fixture()
    page=req.target.source
    assert replace(page,width_px=800,height_px=400).id == page.id
    with pytest.raises(ContractError):
        replace(page,rotation=0.0)
