import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.model import (
    BuildingModel,
    ContractError,
    Level,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    UnsupportedSchemaVersion,
    Vector3,
    stable_id,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"
FIXTURE_DIR = ROOT / "fixtures" / "model" / "v1"


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("fixture", sorted(FIXTURE_DIR.glob("*.json")))
def test_checked_in_fixtures_validate_and_round_trip(fixture: Path) -> None:
    document = json.loads(fixture.read_text(encoding="utf-8"))
    errors = sorted(_schema_validator().iter_errors(document), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors)

    model = BuildingModel.from_dict(document)
    canonical = model.to_dict()
    assert BuildingModel.from_json(model.to_json()).to_dict() == canonical

    second_errors = sorted(
        _schema_validator().iter_errors(canonical), key=lambda error: list(error.path)
    )
    assert not second_errors


def test_stable_id_is_deterministic_and_namespaced() -> None:
    first = stable_id("wall", "roomplan:wall-17")
    second = stable_id("wall", "roomplan:wall-17")
    other_kind = stable_id("device", "roomplan:wall-17")
    assert first == second
    assert first.startswith("wall:")
    assert first != other_kind


def test_duplicate_entity_ids_are_rejected() -> None:
    level = Level(id="level:ground", elevation_m=0)
    with pytest.raises(ContractError, match="duplicate entity id"):
        BuildingModel(model_id="model:test", levels=(level, level))


def test_missing_reference_is_rejected() -> None:
    panel_port = Port(
        id="port:panel",
        owner_id="equipment:missing",
        domain="power",
        role="source",
        pose=Pose(position=Point3(x=0, y=0, z=1)),
        direction=Vector3(x=1, y=0, z=0),
    )
    with pytest.raises(ContractError, match="references missing id"):
        BuildingModel(model_id="model:test", ports=(panel_port,))


def test_route_centerline_must_land_on_declared_ports() -> None:
    provenance = (Provenance(source_kind="synthetic", source_id="test"),)
    start = Port(
        id="port:start",
        owner_id="device:a",
        domain="power",
        role="source",
        pose=Pose(position=Point3(x=0, y=0, z=0)),
        direction=Vector3(x=1, y=0, z=0),
        provenance=provenance,
    )
    end = Port(
        id="port:end",
        owner_id="device:b",
        domain="power",
        role="sink",
        pose=Pose(position=Point3(x=2, y=0, z=0)),
        direction=Vector3(x=-1, y=0, z=0),
        provenance=provenance,
    )
    document = json.loads((FIXTURE_DIR / "garage-route.json").read_text(encoding="utf-8"))
    model = BuildingModel.from_dict(document)
    bad_route = Route(
        id="route:bad",
        route_type="emt",
        start_port_id=model.ports[0].id,
        end_port_id=model.ports[1].id,
        centerline=Polyline3D(
            points=(Point3(x=99, y=99, z=99), model.ports[1].pose.position)
        ),
    )
    with pytest.raises(ContractError, match="must start at the start port position"):
        replace(model, routes=(bad_route,), route_fittings=(), circuits=(), conductors=())


def test_port_connectivity_must_be_symmetric() -> None:
    document = json.loads((FIXTURE_DIR / "garage-route.json").read_text(encoding="utf-8"))
    document["ports"][0]["connected_port_ids"] = [document["ports"][1]["id"]]
    with pytest.raises(ContractError, match="connectivity must be symmetric"):
        BuildingModel.from_dict(document)


def test_unknown_fields_are_rejected() -> None:
    document = json.loads((FIXTURE_DIR / "minimal-room.json").read_text(encoding="utf-8"))
    document["mystery_field"] = 123
    with pytest.raises(ContractError, match="unknown field"):
        BuildingModel.from_dict(document)


def test_wrong_schema_version_is_rejected() -> None:
    document = json.loads((FIXTURE_DIR / "minimal-room.json").read_text(encoding="utf-8"))
    document["schema_version"] = "2.0.0"
    with pytest.raises(UnsupportedSchemaVersion):
        BuildingModel.from_dict(document)


def test_issue_84_electrical_physical_fixture_is_first_class() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "electrical-physical.json")
    assert [box.id for box in model.electrical_boxes] == ["box:evse"]
    assert model.electrical_boxes[0].occupant_ids == ("device:evse",)
    assert model.electrical_boxes[0].listed_volume_m3 == pytest.approx(0.00035)
    assert [selection.id for selection in model.raceway_selections] == [
        "raceway-selection:panel-evse"
    ]
    selection = model.raceway_selections[0]
    assert (selection.start_segment_index, selection.end_segment_index_exclusive) == (0, 3)
    assert selection.catalog_id == "synthetic:emt-v1"
    assert selection.catalog_item_id == "emt-21mm"
    assert selection.trade_size == "synthetic-21-mm"


def test_issue_84_empty_additive_collections_preserve_legacy_serialization_shape() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "garage-route.json")
    document = model.to_dict()
    assert "electrical_boxes" not in document
    assert "raceway_selections" not in document


def test_issue_84_unresolved_reason_is_required_and_survives_round_trip() -> None:
    document = json.loads((FIXTURE_DIR / "garage-route.json").read_text(encoding="utf-8"))
    document["electrical_boxes"] = [
        {
            "id": "box:evse-unresolved",
            "box_type": "device-box",
            "resolution_status": "unresolved",
            "occupant_ids": ["device:evse"],
            "unresolved_reason": "missing-device-host",
        }
    ]
    document["raceway_selections"] = [
        {
            "id": "raceway-selection:unresolved",
            "route_id": "route:panel-evse",
            "start_segment_index": 0,
            "end_segment_index_exclusive": 3,
            "basis": "conductor-fill",
            "resolution_status": "unresolved",
            "product_kind": "raceway",
            "product_type": "emt",
            "unresolved_reason": "no-catalog-item-fits",
        }
    ]
    model = BuildingModel.from_dict(document)
    assert model.electrical_boxes[0].unresolved_reason == "missing-device-host"
    assert model.raceway_selections[0].unresolved_reason == "no-catalog-item-fits"
    assert BuildingModel.from_json(model.to_json()).to_dict() == model.to_dict()

    document["electrical_boxes"][0].pop("unresolved_reason")
    with pytest.raises(ContractError, match="unresolved_reason is required"):
        BuildingModel.from_dict(document)


def test_issue_84_box_occupants_cannot_belong_to_two_boxes() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "electrical-physical.json")
    duplicate = replace(model.electrical_boxes[0], id="box:evse-second")
    with pytest.raises(ContractError, match="cannot occupy both"):
        replace(model, electrical_boxes=(*model.electrical_boxes, duplicate))


def test_issue_84_raceway_selection_spans_must_partition_route() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "electrical-physical.json")
    first = replace(
        model.raceway_selections[0],
        id="raceway-selection:first",
        end_segment_index_exclusive=1,
    )
    second = replace(
        model.raceway_selections[0],
        id="raceway-selection:second",
        start_segment_index=2,
    )
    with pytest.raises(ContractError, match="without gaps or overlaps"):
        replace(model, raceway_selections=(first, second))


def test_issue_84_resolved_selection_cannot_silently_override_route_diameter() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "electrical-physical.json")
    conflicting = replace(model.raceway_selections[0], nominal_diameter_m=0.027)
    with pytest.raises(ContractError, match="conflicts with .*nominal_diameter_m"):
        replace(model, raceway_selections=(conflicting,))


def test_issue_84_selection_span_cannot_extend_past_route_geometry() -> None:
    model = BuildingModel.load(FIXTURE_DIR / "electrical-physical.json")
    invalid = replace(model.raceway_selections[0], end_segment_index_exclusive=4)
    with pytest.raises(ContractError, match="exceeds route segment count"):
        replace(model, raceway_selections=(invalid,))
