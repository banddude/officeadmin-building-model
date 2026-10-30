"""Generated-PDF registration identity survives coincident empty levels."""
from copy import deepcopy
from dataclasses import replace

import pytest

from test_pdf_sheet_registration import (
    ELECTRICAL, EVSE_AT, _architecture, _electrical, _e_sheet, _register, _text, _local,
)
from oabm.importers.pdf_convergence import converge_pdf_models
from oabm.importers.pdf_electrical import ElectricalPdfImporter
from oabm.model import BuildingModel, Level, Point3, Pose, validate_model


def _case(tmp_path):
    _, source, architecture = _architecture(tmp_path)
    target = architecture.levels[0]
    empty = Level(id="level:empty-other", name="Empty unrelated level",
                  elevation_m=target.elevation_m)
    architecture = replace(architecture, levels=(*architecture.levels, empty))
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
    return architecture, electrical, target


def test_actual_registration_level_wins_over_an_empty_coincident_level(tmp_path):
    architecture, electrical, target = _case(tmp_path)
    before = electrical.to_json()
    architecture_before = architecture.to_json()
    [source] = electrical.electrical_devices
    registration = source.attributes["pdf_electrical"]["page_transform"]["registration"]
    assert registration["level_id"] == target.id
    result = converge_pdf_models(architecture, electrical)
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
    assert BuildingModel.from_json(result.to_json()).to_json() == result.to_json()
    assert result.to_json() == converge_pdf_models(architecture, electrical).to_json()
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
    architecture, electrical, _ = _case(tmp_path)
    electrical = _alter_transform(electrical, path, value)
    result = converge_pdf_models(architecture, electrical)
    [device] = result.electrical_devices
    assert device.level_id is None
    assert "registration_target_level_invalid" in device.attributes["pdf_convergence"]["ambiguity_codes"]
    validate_model(result)


@pytest.mark.parametrize("change", ["duplicate", "unresolved", "other_level", "other_frame", "other_elevation", "malformed_collection"])
def test_registration_must_reference_one_current_resolved_target(tmp_path, change):
    architecture, electrical, target = _case(tmp_path)
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
    [device] = converge_pdf_models(architecture, electrical).electrical_devices
    assert device.level_id is None
    assert "registration_target_level_invalid" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_caller_transform_without_target_evidence_keeps_ambiguity(tmp_path):
    architecture, electrical, _ = _case(tmp_path)
    electrical = _alter_transform(electrical, ("registration",), None)
    [device] = converge_pdf_models(architecture, electrical).electrical_devices
    assert device.level_id is None
    assert "level_ambiguous" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_registration_does_not_override_conflicting_plan_containment(tmp_path):
    architecture, electrical, _ = _case(tmp_path)
    architecture = replace(architecture, spaces=tuple(
        replace(space, level_id="level:empty-other") for space in architecture.spaces
    ))
    [device] = converge_pdf_models(architecture, electrical).electrical_devices
    assert device.level_id is None
    assert "level_evidence_conflict" in device.attributes["pdf_convergence"]["ambiguity_codes"]


def test_registration_does_not_override_a_contradictory_source_z(tmp_path):
    architecture, electrical, target = _case(tmp_path)
    other = Level(id="level:different-elevation", elevation_m=target.elevation_m + 3)
    architecture = replace(architecture, levels=(*architecture.levels, other))
    [device] = electrical.electrical_devices
    p = device.pose.position
    electrical = replace(electrical, electrical_devices=(replace(
        device, pose=Pose(position=Point3(x=p.x, y=p.y, z=other.elevation_m)),
    ),))
    [device] = converge_pdf_models(architecture, electrical).electrical_devices
    assert device.level_id is None
    assert "level_evidence_conflict" in device.attributes["pdf_convergence"]["ambiguity_codes"]
