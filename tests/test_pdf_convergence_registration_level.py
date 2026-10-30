"""Generated-PDF registration identity survives coincident empty levels."""
from copy import deepcopy
from dataclasses import replace

import pytest

from test_pdf_sheet_registration import (
    ELECTRICAL, EVSE_AT, _architecture, _electrical, _e_sheet, _register, _text, _local,
)
from oabm.importers.pdf_convergence import converge_pdf_models
from oabm.importers.pdf_electrical import ElectricalPdfImporter
from oabm.model import BuildingModel, Level, Point3, Pose, Port, Vector3, validate_model


def _case(tmp_path, *, registration_confidence=None):
    _, source, architecture = _architecture(tmp_path)
    target = architecture.levels[0]
    empty = Level(id="level:empty-other", name="Empty unrelated level",
                  elevation_m=target.elevation_m)
    architecture = replace(architecture, levels=(*architecture.levels, empty))
    if registration_confidence is not None:
        attributes = deepcopy(architecture.attributes)
        attributes["pdf_architecture"]["drawing_regions"][0]["confidence"] = registration_confidence
        architecture = replace(architecture, attributes=attributes)
    # Use an explicit source text instance; Form-XObject origin handling has
    # separate existing coverage and ownership.
    x, y = _local(ELECTRICAL, EVSE_AT)
    sheet = replace(_e_sheet(replace(ELECTRICAL, evse=False)), extra=(
        _text(x, y, 'EVSE-1 +48" AFF WALL MTD', size=9),
    ))
    pdf = _electrical(tmp_path, sheet)
    registration = _register(architecture, source, pdf)
    assert registration.all_registered
    electrical = ElectricalPdfImporter().import_pdf(
        pdf, source_id="fixture:electrical",
        page_transforms=registration.page_transforms(),
    )
    assert len(electrical.electrical_devices) == 1
    assert not pdf.with_suffix(".expected.json").exists()
    return architecture, electrical, target, registration


def test_actual_registration_level_wins_over_an_empty_coincident_level(tmp_path):
    architecture, electrical, target, registration = _case(tmp_path)
    before = electrical.to_json()
    registration_before = deepcopy(registration.to_dict())
    architecture_before = architecture.to_json()
    [source] = electrical.electrical_devices
    claim = source.attributes["pdf_electrical"]["page_transform"]["registration"]
    assert claim["level_id"] == target.id
    result = converge_pdf_models(architecture, electrical, registration=registration)
    [device] = result.electrical_devices
    assert device.level_id == target.id
    assert device.id == source.id
    assert len(result.electrical_devices) == len(electrical.electrical_devices)
    assert device.pose.position.x == source.pose.position.x
    assert device.pose.position.y == source.pose.position.y
    assert device.pose.rotation == source.pose.rotation
    assert device.pose.position.z == pytest.approx(target.elevation_m + 48 * 0.0254)
    assert device.attributes["pdf_electrical"] == source.attributes["pdf_electrical"]
    assert device.confidence <= source.confidence
    assert device.confidence <= target.confidence
    assert device.attributes["pdf_convergence"]["level_resolution_method"] == "accepted_sheet_registration_target"
    assert device.provenance[:len(source.provenance)] == source.provenance
    assert electrical.to_json() == before
    assert architecture.to_json() == architecture_before
    assert registration.to_dict() == registration_before
    assert BuildingModel.from_json(result.to_json()).to_json() == result.to_json()
    assert result.to_json() == converge_pdf_models(architecture, electrical, registration=registration).to_json()
    validate_model(result)


def _alter_transform(electrical, path, value):
    [device] = electrical.electrical_devices
    attributes = deepcopy(device.attributes)
    target = attributes["pdf_electrical"]["page_transform"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return replace(electrical, electrical_devices=(replace(device, attributes=attributes),))


@pytest.mark.parametrize("path,value", [
    (("registration", "level_id"), "level:missing"),
    (("registration", "level_id"), None),
    (("registration", "target_region_id"), None),
    (("registration", "target_region_id"), "region:stale"),
    (("registration", "target_page"), 999),
    (("registration", "target_page"), True),
    (("registration", "level_elevation_m"), 999),
    (("frame_id",), "frame:other"),
    (("z_m",), 999),
])
def test_stale_or_conflicting_registration_does_not_fall_back_to_z(tmp_path, path, value):
    architecture, electrical, _, registration = _case(tmp_path)
    electrical = _alter_transform(electrical, path, value)
    result = converge_pdf_models(architecture, electrical, registration=registration)
    [device] = result.electrical_devices
    assert device.level_id is None
    assert "registration_target_level_invalid" in device.attributes["pdf_convergence"]["ambiguity_codes"]
    validate_model(result)


@pytest.mark.parametrize("change", ["duplicate", "unresolved", "other_level", "other_frame", "other_elevation", "malformed_collection"])
def test_registration_must_reference_one_current_resolved_target(tmp_path, change):
    architecture, electrical, target, registration = _case(tmp_path)
    attributes = deepcopy(architecture.attributes)
    regions = attributes["pdf_architecture"]["drawing_regions"]
    assert len(regions) == 1
    if change == "malformed_collection":
        attributes["pdf_architecture"]["drawing_regions"] = None
    elif change == "duplicate":
        regions.append(deepcopy(regions[0]))
    elif change == "unresolved":
        regions[0]["status"] = "unresolved"
    elif change == "other_level":
        regions[0]["level"]["level_id"] = "level:empty-other"
    elif change == "other_frame":
        regions[0]["frame"]["frame_id"] = "frame:other"
    else:
        regions[0]["level"]["elevation_m"] = target.elevation_m + 1
    architecture = replace(architecture, attributes=attributes)
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "registration_target_level_invalid" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_caller_transform_without_target_evidence_keeps_ambiguity(tmp_path):
    architecture, electrical, _, registration = _case(tmp_path)
    electrical = _alter_transform(electrical, ("registration",), None)
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "level_ambiguous" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_registration_does_not_override_conflicting_plan_containment(tmp_path):
    architecture, electrical, _, registration = _case(tmp_path)
    architecture = replace(architecture, spaces=tuple(
        replace(space, level_id="level:empty-other") for space in architecture.spaces
    ))
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "level_evidence_conflict" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_registration_does_not_override_a_contradictory_source_z(tmp_path):
    architecture, electrical, target, registration = _case(tmp_path)
    other = Level(id="level:different-elevation", elevation_m=target.elevation_m + 3)
    architecture = replace(architecture, levels=(*architecture.levels, other))
    [device] = electrical.electrical_devices
    p = device.pose.position
    electrical = replace(electrical, electrical_devices=(replace(
        device, pose=Pose(position=Point3(x=p.x, y=p.y, z=other.elevation_m)),
    ),))
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "level_evidence_conflict" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def _assert_invalid_target(architecture, electrical, registration):
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "registration_target_level_invalid" in device.attributes["pdf_convergence"]["ambiguity_codes"]


@pytest.mark.parametrize("change", ["missing", "other_source", "other_page", "other_transform"])
def test_target_claim_requires_matching_model_registration_provenance(tmp_path, change):
    architecture, electrical, _, registration = _case(tmp_path)
    provenance = []
    for item in electrical.provenance:
        if item.attributes.get("registration_status") == "registered-from-matched-evidence":
            if change == "missing":
                continue
            if change == "other_source":
                item = replace(item, source_id="fixture:another-source")
            elif change == "other_page":
                item = replace(item, page=item.page + 1)
            else:
                attributes = deepcopy(item.attributes)
                attributes["page_transform"]["tx_m"] += 1
                item = replace(item, attributes=attributes)
        provenance.append(item)
    _assert_invalid_target(architecture, replace(electrical, provenance=tuple(provenance)), registration)


def test_fabricated_caller_registration_cannot_authorize_level(tmp_path):
    architecture, electrical, _, registration = _case(tmp_path)
    [device] = electrical.electrical_devices
    claim = device.attributes["pdf_electrical"]["page_transform"]["registration"]
    # A caller can supply arbitrary mappings to PdfPageTransform. Merely
    # matching target identifiers does not establish accepted registration.
    minimal = {key: claim[key] for key in (
        "level_id", "target_region_id", "target_page", "level_elevation_m",
    )}
    electrical = _alter_transform(electrical, ("registration",), minimal)
    _assert_invalid_target(architecture, electrical, registration)


@pytest.mark.parametrize("change", ["other_source", "other_page"])
def test_copied_registration_binding_cannot_authorize_another_source(tmp_path, change):
    architecture, electrical, _, registration = _case(tmp_path)
    attributes = deepcopy(electrical.electrical_devices[0].attributes)
    attributes["pdf_electrical"]["source_page"] += change == "other_page"
    source = "fixture:another-source" if change == "other_source" else "fixture:electrical"
    page = attributes["pdf_electrical"]["source_page"]
    [device] = electrical.electrical_devices
    device = replace(device, attributes=attributes, provenance=tuple(
        replace(item, source_id=source, page=page) for item in device.provenance
    ))
    # Even when the importer has recorded that copied transform under the
    # receiving source/page, its original producer binding must disagree.
    provenance = tuple(replace(item, source_id=source, page=page)
                       if item.attributes.get("registration_status") else item
                       for item in electrical.provenance)
    _assert_invalid_target(architecture, replace(electrical,
        electrical_devices=(device,), provenance=provenance), registration)


@pytest.mark.parametrize("kind", ["containment", "z"])
def test_multiple_conflicting_candidates_do_not_bypass_target_conflict(tmp_path, kind):
    architecture, electrical, target, registration = _case(tmp_path)
    other1 = Level(id="level:conflict-one", elevation_m=target.elevation_m + 3)
    other2 = replace(other1, id="level:conflict-two")
    architecture = replace(architecture, levels=(*architecture.levels, other1, other2))
    if kind == "containment":
        [space] = architecture.spaces
        architecture = replace(architecture, spaces=(
            replace(space, level_id=other1.id),
            replace(space, id="space:other-conflict", level_id=other2.id),
        ))
    else:
        [device] = electrical.electrical_devices
        point = device.pose.position
        electrical = replace(electrical, electrical_devices=(replace(device,
            pose=replace(device.pose, position=replace(point, z=other1.elevation_m))),))
    [device] = converge_pdf_models(architecture, electrical, registration=registration).electrical_devices
    assert device.level_id is None
    assert "level_evidence_conflict" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_low_registration_confidence_caps_owner_port_and_placement(tmp_path):
    architecture, electrical, _, registration = _case(tmp_path, registration_confidence=0.1)
    [owner] = electrical.electrical_devices
    port = Port(id="port:synthetic-load", owner_id=owner.id, domain="power",
                role="load", pose=owner.pose, direction=Vector3(x=1, y=0, z=0),
                confidence=0.95, provenance=owner.provenance)
    electrical = replace(electrical, ports=(port,))
    result = converge_pdf_models(architecture, electrical, registration=registration)
    [device] = result.electrical_devices
    assert device.level_id is not None
    assert device.confidence <= 0.1
    assert result.ports
    assert all(port.confidence <= 0.1 for port in result.ports)
    for entity in (device, *result.ports):
        added = [p for p in entity.provenance if p.source_kind == "pdf-convergence"]
        assert added and all(p.confidence <= 0.1 for p in added)


def test_registration_attributes_and_model_provenance_alone_are_not_acceptance(tmp_path):
    architecture, electrical, _, _ = _case(tmp_path)
    _assert_invalid_target(architecture, electrical, None)


def test_changed_entity_transform_cannot_reuse_an_accepted_result(tmp_path):
    architecture, electrical, _, registration = _case(tmp_path)
    electrical = _alter_transform(electrical, ("tx_m",), 123.0)
    _assert_invalid_target(architecture, electrical, registration)


def test_caller_mapping_recorded_by_importer_is_not_an_acceptance_result(tmp_path):
    from oabm.importers.pdf_electrical import PdfPageTransform

    architecture, electrical, _, _ = _case(tmp_path)
    [device] = electrical.electrical_devices
    attributes = deepcopy(device.attributes["pdf_electrical"]["page_transform"])
    attributes["registration"].pop("source_binding")
    fabricated = PdfPageTransform(**attributes)
    electrical = ElectricalPdfImporter().import_pdf(
        tmp_path / "electrical.pdf", source_id="fixture:electrical",
        page_transforms={1: fabricated},
    )
    assert any(p.attributes.get("registration_status") == "registered-from-matched-evidence"
               for p in electrical.provenance)
    _assert_invalid_target(architecture, electrical, None)


@pytest.mark.parametrize("change", ["pending", "other_model", "other_source", "other_page"])
def test_wrong_or_incomplete_result_is_not_an_acceptance_receipt(tmp_path, change):
    architecture, electrical, _, registration = _case(tmp_path)
    if change == "pending":
        registration = replace(registration, pages=(replace(
            registration.pages[0], status="registration_pending", transform=None,
        ),))
    elif change == "other_model":
        registration = replace(registration, architecture_model_id="model:other")
    elif change == "other_source":
        registration = replace(registration, electrical_source_id="fixture:other")
    else:
        registration = replace(registration, pages=(replace(registration.pages[0], page=2),))
    _assert_invalid_target(architecture, electrical, registration)


def test_per_drawing_producer_records_keep_the_exact_bound_transforms(tmp_path):
    from test_pdf_sheet_registration import _distinct_floor_architecture, LOWER, UPPER

    _, source, architecture = _distinct_floor_architecture(tmp_path)
    path = _electrical(tmp_path, _e_sheet(LOWER, UPPER))
    registration = _register(architecture, source, path)
    assert registration.all_registered
    [page] = registration.pages
    assert page.transform is None
    assert len(page.drawing_transforms) == 2
    for record, drawing in zip(page.record["drawings"], page.drawing_transforms):
        assert record["page_transform"] == drawing.transform.to_attributes()
        assert record["registration"] == drawing.transform.registration
        binding = record["registration"]["source_binding"]
        assert binding["source_page"] == page.page
        assert binding["electrical_source_id"] == registration.electrical_source_id
        assert binding["architecture_model_id"] == architecture.model_id
        assert binding["page_transform"] == {key: value for key, value in
            drawing.transform.to_attributes().items() if key != "registration"}
