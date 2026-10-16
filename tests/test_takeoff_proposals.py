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
    RenderManifest, render_source_image,
)


from functools import lru_cache
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, RectangleObject

@lru_cache(maxsize=8)
def source_pdf(revision="one", rotation=0, offset=False, cropbox=False):
    writer = PdfWriter()
    for index in range(2):
        page = writer.add_blank_page(width=200, height=100)
        stream = DecodedStreamObject()
        # Different asymmetric source content on each page, plus a mark inside
        # the known source-point box [60,58,80,74] on page two.
        color = b".85 .1 .2" if index == 0 else b".1 .3 .85"
        stream.set_data(color + b" rg 0 0 67 100 re f "
            b".2 .8 .3 rg 67 0 91 40 re f 0 0 0 RG 1 w "
            b"75 66 m 75 68.76 72.76 71 70 71 c "
            b"67.24 71 65 68.76 65 66 c 65 63.24 67.24 61 70 61 c "
            b"72.76 61 75 63.24 75 66 c S 60 66 m 65 66 l S")
        page[NameObject("/Contents")] = writer._add_object(stream)
        if offset:
            page.mediabox = RectangleObject([-50,-20,150,80])
        if cropbox:
            page.cropbox = RectangleObject([0,0,100,100])
        page.rotate(rotation)
    writer.add_metadata({"/Title": revision})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def documents_for(request=None, document=None):
    document = source_pdf() if document is None else document
    return {hashlib.sha256(document).hexdigest(): document}


def observations(result, *, images=None, document=None):
    if images is None:
        images = fixture()[2]
    return result.unresolved_observations(
        images=images, documents=documents_for(document=document))


def fixture(document=None):
    document = source_pdf() if document is None else document
    images = {}
    refs = []
    for name, role, box in [
        ("field", "target", (100, 20, 300, 180)),
        ("legend", "legend", (0, 0, 100, 100)),
        ("schedule", "schedule", (0, 100, 100, 200)),
    ]:
        ref, payload = render_source_image(document, source_id="synthetic:lighting",
            page=2, scale=2, crop_px=box, image_id=name, role=role)
        images[name] = payload
        refs.append(ref)
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


def run(request=None, output=None, adapter=None, *, document=None, images=None):
    req, endpoint, default_images, raw = fixture(document)
    req = request or req
    raw = output or raw
    return run_takeoff_experiment(req, endpoint,
        images=default_images if images is None else images,
        documents=documents_for(document=document), adapters={
            "fake": adapter or (lambda *args, **kwargs: raw.to_dict()),
        }, experiment_reason="Explicit synthetic regression experiment")


def test_roundtrip_coordinate_conversion_and_inferred_provenance():
    req, endpoint, _, _ = fixture()
    result = run()
    assert TakeoffResult.from_json(result.to_json()) == result
    assert result.validate_against(req, endpoint, documents=documents_for(), images=fixture()[2]) == result
    row, = observations(result)
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
    assert [r["source_element_id"] for r in observations(forward)] == [r["source_element_id"] for r in observations(reverse)]
    # Raw order remains captured in the output fingerprint, not rewritten.
    assert forward.raw_output.items == tuple(reversed(reverse.raw_output.items))
    changed = run(output=replace(raw, items=(replace(raw.items[0], fixture_tag=None, confidence=.5),)))
    assert observations(first)[0]["source_element_id"] == observations(changed)[0]["source_element_id"]
    assert observations(first)[0]["proposal_id"] != observations(changed)[0]["proposal_id"]


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
    assert observations(run(output=output))[0]["type_proposal"] is None


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
                              images=images,documents=documents_for(),experiment_reason="test")
    assert calls == []
    ref=req.target
    buf=io.BytesIO()
    Image.new("RGB",(1,1)).save(buf,format="PNG")
    wrong=buf.getvalue()
    with pytest.raises(ContractError):
        replace(ref,sha256=hashlib.sha256(wrong).hexdigest()).validate_bytes(wrong, document=source_pdf())


def test_request_output_and_endpoint_staleness():
    req, endpoint, _, _ = fixture()
    result=run()
    with pytest.raises(ContractError):
        result.validate_against(replace(req,question="A different bounded question."),endpoint, documents=documents_for(),images=fixture()[2])
    with pytest.raises(ContractError):
        result.validate_against(req,replace(endpoint,model_alias="different"), documents=documents_for(),images=fixture()[2])
    data=result.to_dict()
    data["raw_output"]["items"][0]["confidence"]=.7
    with pytest.raises(ContractError):
        TakeoffResult.from_dict(data)
    data=result.to_dict()
    data["accepted"]=True
    with pytest.raises(ContractError):
        TakeoffResult.from_dict(data)


def test_source_revision_changes_source_mark_identity():
    old = run()
    document = source_pdf(revision="two")
    new = run(document=document)
    assert observations(old)[0]["source_element_id"] != observations(new, images=fixture(document)[2], document=document)[0]["source_element_id"]


def test_adapter_mutation_cannot_change_recorded_input_or_images():
    req, endpoint, images, output=fixture()
    original=req.to_json()
    def adapter(document, *, images, **kwargs):
        document["question"]="mutated"
        images.clear()
        assert kwargs == dict(provider_id="synthetic",model_alias="vision-test",prompt_version="synthetic-v1")
        return output.to_dict()
    result=run_takeoff_experiment(req,endpoint,images=images,documents=documents_for(),adapters={"fake":adapter},
                                 experiment_reason="Explicit test")
    assert result.request.to_json() == original
    assert len(images) == 3
    row=observations(result)[0]
    row["provenance"][0]["attributes"].clear()
    assert observations(result)[0]["provenance"][0]["attributes"]


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


def _replace_target(request, target):
    return replace(request, images=tuple(
        target if image.id == request.target_image_id else image for image in request.images))


def test_wrong_pdf_digest_rejected_before_adapter():
    req, endpoint, images, _ = fixture()
    called = []
    with pytest.raises(ContractError, match="PDF digest"):
        run_takeoff_experiment(req, endpoint, images=images,
            documents={req.target.source.document_sha256: source_pdf("other")},
            adapters={"fake": lambda *a, **kw: called.append(1)}, experiment_reason="test")
    assert not called


def test_rehashed_wrong_pdf_pixels_are_not_an_attestation():
    req, _, images, _ = fixture()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_blank_page(width=200, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    wrong = buffer.getvalue()
    sha = hashlib.sha256(wrong).hexdigest()
    forged = replace(req, images=tuple(replace(image,
        source=replace(image.source, document_sha256=sha)) for image in req.images))
    with pytest.raises(ContractError, match="pixels differ"):
        run(request=forged, images=images, document=wrong)


def test_wrong_page_with_matching_size_and_hash_is_rejected():
    req, _, images, _ = fixture()
    wrong = replace(req.target, source=replace(req.target.source, page=1))
    with pytest.raises(ContractError, match="pixels differ"):
        run(request=_replace_target(req, wrong), images=images)


def _shifted_target(request):
    # Recompute a self-consistent affine and retain the actual image SHA.
    # Only rerendering can discover that these pixels belong to a different crop.
    target = request.target
    manifest = replace(target.render_manifest,
                       pixel_to_display_pt=(.5,0,0,-.5,40,90))
    return replace(target, crop_px=(80,20,280,180), render_manifest=manifest)


def test_wrong_crop_with_self_consistent_transform_is_rejected():
    req, _, images, _ = fixture()
    forged = _replace_target(req, _shifted_target(req))
    assert TakeoffRequest.from_json(forged.to_json()) == forged
    with pytest.raises(ContractError, match="pixels differ"):
        run(request=forged, images=images)


def test_wrong_rotation_metadata_rejected_even_for_same_raster_size():
    req, _, images, _ = fixture()
    wrong = replace(req.target, source=replace(req.target.source, rotation=180))
    with pytest.raises(ContractError, match="metadata/rotation/raster"):
        run(request=_replace_target(req, wrong), images=images)


def test_resized_then_restored_image_rejected_despite_rehashed_bytes():
    req, _, images, _ = fixture()
    with Image.open(io.BytesIO(images["field"])) as image:
        resized = image.resize((100,80)).resize((200,160))
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
    altered = buffer.getvalue()
    assert altered != images["field"]
    target = replace(req.target, sha256=hashlib.sha256(altered).hexdigest())
    with pytest.raises(ContractError, match="pixels differ"):
        run(request=_replace_target(req, target), images={**images, "field": altered})


def test_forged_full_raster_dimensions_and_affine_rejected():
    req, _, images, _ = fixture()
    target = req.target
    source = replace(target.source, width_px=800, height_px=400)
    manifest = replace(target.render_manifest,
        pixel_to_display_pt=(.25,0,0,-.25,25,95))
    forged = replace(target, source=source, render_manifest=manifest)
    with pytest.raises(ContractError, match="metadata/rotation/raster"):
        run(request=_replace_target(req, forged), images=images)


def test_forged_affine_and_renderer_policy_rejected():
    req, _, _, _ = fixture()
    with pytest.raises(ContractError, match="affine"):
        replace(req.target, render_manifest=replace(req.target.render_manifest,
                pixel_to_display_pt=(1,0,0,-1,0,0)))
    with pytest.raises(ContractError, match="render policy"):
        replace(req.target.render_manifest, policy="caller-attested")


def test_wrong_renderer_version_rejected():
    req, _, images, _ = fixture()
    target = replace(req.target, render_manifest=replace(
        req.target.render_manifest, renderer_version="different-build"))
    with pytest.raises(ContractError, match="engine version"):
        run(request=_replace_target(req, target), images=images)


def test_saved_result_export_and_revalidation_rerender_instead_of_trusting_hashes():
    from oabm.proposals.contracts import digest
    req, endpoint, images, output = fixture()
    forged_request = _replace_target(req, _shifted_target(req))
    # Entire portable record has valid internal hashes; it is still untrusted.
    forged = TakeoffResult(request=forged_request, endpoint=endpoint,
        experiment_reason="synthetic forged record", raw_output=output,
        request_fingerprint=forged_request.fingerprint, output_fingerprint=digest(output.to_dict()))
    restored = TakeoffResult.from_json(forged.to_json())
    with pytest.raises(ContractError, match="pixels differ"):
        restored.unresolved_observations(images=images, documents=documents_for())
    with pytest.raises(ContractError, match="pixels differ"):
        restored.validate_against(forged_request, endpoint, images=images, documents=documents_for())
    with pytest.raises(TypeError):
        restored.unresolved_observations()
    with pytest.raises(ContractError, match="PDF bytes"):
        restored.unresolved_observations(images=images, documents={})


@pytest.mark.parametrize("rotation", [0,90,180,270])
def test_actual_rotated_offset_pdf_render_pixel_and_point_binding(rotation):
    document = source_pdf(rotation=rotation, offset=True, cropbox=True)
    ref, payload = render_source_image(document, source_id="synthetic:offset",
        page=2, scale=2, crop_px=((0,0,200,400) if rotation in (90,270) else (0,0,400,200)), image_id="source", role="target")
    ref.validate_bytes(payload, document=document)
    assert ref.source.media_box_pt == (-50,-20,150,80)
    assert ref.source.rotation == rotation
    expected_size = (200,400) if rotation in (90,270) else (400,200)
    assert (ref.source.width_px, ref.source.height_px) == expected_size
    # The affine maps local crop top-left to displayed top-left, and +Y down
    # to -Y in points, despite the offset raw MediaBox and smaller CropBox.
    sx, _, _, sy, tx, ty = ref.render_manifest.pixel_to_display_pt
    assert (sx,sy,tx,ty) == pytest.approx((.5,-.5,0,ref.source.height_pt))
    with Image.open(io.BytesIO(payload)) as actual:
        assert actual.size == expected_size and actual.mode == "RGB"
        # Independent raw source point (20,10) is inside the blue source fill.
        # Its MediaBox-relative point is (70,30), then the printed quarter turn.
        x,y={0:(70,30),90:(30,130),180:(130,70),270:(70,70)}[rotation]
        sample=actual.getpixel((x*2,(int(ref.source.height_pt)-y)*2))
        assert sample == pytest.approx((26,76,217),abs=2)


def test_valid_resolution_change_preserves_source_point_identity():
    req, endpoint, _, output = fixture()
    refs, images = [], {}
    for old in req.images:
        ref, payload = render_source_image(source_pdf(), source_id=old.source.source_id,
            page=2, scale=4, crop_px=tuple(v*2 for v in old.crop_px),
            image_id=old.id, role=old.role)
        refs.append(ref); images[ref.id] = payload
    larger = replace(req, images=tuple(refs))
    result = run(request=larger, images=images)
    assert observations(run())[0]["source_element_id"] == observations(result, images=images)[0]["source_element_id"]


def test_legacy_v1_record_cannot_be_exported_as_verified_v2():
    request, _, _, _ = fixture()
    data = request.to_dict()
    data["version"] = "1.0.0"
    data["validator_version"] = "source-lighting-marks-v1"
    for image in data["images"]:
        image.pop("render_manifest")
    with pytest.raises(ContractError):
        TakeoffRequest.from_dict(data)


def test_render_requires_actual_valid_page_and_supported_user_unit():
    from pypdf.generic import NumberObject
    with pytest.raises(ContractError):
        render_source_image(b"not a PDF",source_id="synthetic:bad",page=1,scale=2,
                            crop_px=(0,0,10,10),image_id="bad",role="target")
    with pytest.raises(ContractError,match="page absent"):
        render_source_image(source_pdf(),source_id="synthetic:bad",page=3,scale=2,
                            crop_px=(0,0,10,10),image_id="bad",role="target")
    writer=PdfWriter()
    writer.add_blank_page(width=100,height=100)[NameObject("/UserUnit")]=NumberObject(2)
    buf=io.BytesIO();writer.write(buf)
    with pytest.raises(ContractError,match="UserUnit"):
        render_source_image(buf.getvalue(),source_id="synthetic:bad",page=1,scale=2,
                            crop_px=(0,0,10,10),image_id="bad",role="target")
