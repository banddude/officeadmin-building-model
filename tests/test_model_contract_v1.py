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
    provenance_applies_to,
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


def test_provenance_scope_is_typed_validated_and_serialized() -> None:
    base = BuildingModel.load(FIXTURE_DIR / "garage-route.json")
    wall = base.walls[0]
    record = Provenance(
        source_kind="synthetic",
        source_id="fixture:scoped-thickness",
        derivation="inferred",
        scope_paths=("thickness_m",),
    )
    model = replace(base, walls=(replace(wall, provenance=(*wall.provenance, record)), *base.walls[1:]))
    assert provenance_applies_to(record, ("thickness_m",))
    assert not provenance_applies_to(record, ("centerline", "height_m"))
    assert provenance_applies_to(record, ())
    assert provenance_applies_to(record, "thickness_m")
    assert provenance_applies_to(record, ("thikness-m",))
    assert list(_schema_validator().iter_errors(model.to_dict())) == []
    assert BuildingModel.from_json(model.to_json()).to_dict() == model.to_dict()

    legacy = Provenance(
        source_kind="synthetic",
        source_id="fixture:legacy-scope",
        derivation="inferred",
        attributes={"assumed_dimension": "thikness_m"},
    )
    assert legacy.scope_paths is None
    assert provenance_applies_to(legacy, ("centerline",))
    assert provenance_applies_to(legacy, ("thickness_m",))
    assert "scope_paths" not in BuildingModel(
        model_id="model:legacy-scope", provenance=(legacy,)
    ).to_dict()["provenance"][0]


def test_provenance_scope_rejects_empty_duplicate_and_unknown_paths() -> None:
    for scope in ((), ("thickness_m", "thickness_m"), ("bad-path",)):
        with pytest.raises(ContractError, match="scope_paths"):
            Provenance(source_kind="synthetic", source_id="fixture:bad", scope_paths=scope)

    base = BuildingModel.load(FIXTURE_DIR / "garage-route.json")
    wall = base.walls[0]
    unknown = Provenance(
        source_kind="synthetic",
        source_id="fixture:unknown",
        derivation="inferred",
        scope_paths=("thikness_m",),
    )
    with pytest.raises(ContractError, match="scope path 'thikness_m'"):
        replace(base, walls=(replace(wall, provenance=(*wall.provenance, unknown)), *base.walls[1:]))

    nested = Provenance(
        source_kind="synthetic",
        source_id="fixture:mounting",
        derivation="inferred",
        scope_paths=("attributes.mounting",),
    )
    mounted = replace(
        wall,
        attributes={**wall.attributes, "mounting": "flush"},
        provenance=(*wall.provenance, nested),
    )
    replace(base, walls=(mounted, *base.walls[1:]))
    assert provenance_applies_to(nested, ("attributes.mounting",))
    assert provenance_applies_to(nested, ("attributes",))
    assert not provenance_applies_to(nested, ("thickness_m",))
    multi_scope = replace(nested, scope_paths=("height_m", "attributes.mounting"))
    assert provenance_applies_to(multi_scope, (path for path in ("attributes.mounting",)))
    with pytest.raises(ContractError, match="scope path 'attributes.mounting'"):
        replace(base, walls=(replace(wall, provenance=(*wall.provenance, nested)), *base.walls[1:]))

    empty_document = base.to_dict()
    empty_document["walls"][0]["provenance"][0]["scope_paths"] = []
    assert list(_schema_validator().iter_errors(empty_document))
    with pytest.raises(ContractError):
        BuildingModel.from_dict(empty_document)


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
