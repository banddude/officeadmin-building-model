"""``Wall.construction``: closed vocabulary, omitted-when-unknown, round trips.

Covers the #157 contract change: the four-token vocabulary, unknown-token
rejection on construction and on load, byte-identical serialization of every
checked-in model document, JSON-schema acceptance, and the lossless canonical
pset carrying the token through the IFC round trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.ifc import round_trip
from oabm.model import (
    WALL_CONSTRUCTION_TOKENS,
    BuildingModel,
    ContractError,
    Level,
    Point3,
    Polyline3D,
    Provenance,
    Wall,
    validate_model,
)
from oabm.qa import canonical_digest, canonical_json, load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"
MODEL_FIXTURE_DIR = ROOT / "fixtures" / "model" / "v1"
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"

# Known-answer digests of canonical_json() for the checked-in model fixtures,
# recorded before Wall.construction existed. The field serializes only when
# set, so these documents must keep producing the exact same bytes.
MODEL_FIXTURE_DIGESTS = {
    "garage-route.json": "713fd3d116047775b6c2bd861d4a4617d05836ede83a51e255a9099d9ce61b3f",
    "minimal-room.json": "04a646e4792f7ec9bdd78feadd450d7353f66b4ac1715ba0a053caf0897e2e0e",
}


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _wall(**overrides: object) -> Wall:
    values: dict[str, object] = dict(
        id="wall:south",
        level_id="level:ground",
        centerline=Polyline3D(points=(Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0))),
        thickness_m=0.12,
        height_m=2.7,
    )
    values.update(overrides)
    return Wall(**values)  # type: ignore[arg-type]


def _model(*walls: Wall) -> BuildingModel:
    return BuildingModel(
        model_id="model:construction",
        name="Synthetic construction wall",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=walls,
    )


@pytest.mark.parametrize("token", sorted(WALL_CONSTRUCTION_TOKENS))
def test_each_token_round_trips_through_json_and_validates(token: str) -> None:
    model = _model(_wall(id="wall:token", construction=token))
    validate_model(model)
    loaded = BuildingModel.from_json(model.to_json())
    assert loaded.walls[0].construction == token
    validate_model(loaded)
    assert BuildingModel.from_dict(loaded.to_dict()) == loaded


def test_assumed_construction_carries_scoped_provenance() -> None:
    record = Provenance(
        source_kind="synthetic",
        source_id="fixture:assumed-glazing",
        derivation="inferred",
        scope_paths=("construction",),
    )
    model = _model(_wall(id="wall:glazed", construction="glazed", provenance=(record,)))
    validate_model(model)
    loaded = BuildingModel.from_json(model.to_json())
    assert loaded.walls[0].construction == "glazed"
    assert loaded.walls[0].provenance[0].scope_paths == ("construction",)


@pytest.mark.parametrize("token", ["glass", "brick"])
def test_unknown_token_is_rejected_on_construction_and_on_load(token: str) -> None:
    with pytest.raises(ContractError, match="construction"):
        _wall(construction=token)

    document = _model(_wall(id="wall:x", construction="framed")).to_dict()
    document["walls"][0]["construction"] = token
    with pytest.raises(ContractError, match="construction"):
        BuildingModel.from_dict(document)


def test_every_checked_in_model_document_serializes_byte_identically() -> None:
    model_fixtures = sorted(MODEL_FIXTURE_DIR.glob("*.json"))
    assert {path.name for path in model_fixtures} == set(MODEL_FIXTURE_DIGESTS)
    valid_golden = [case for case in load_golden_cases(GOLDEN_ROOT) if case.valid]

    checked = 0
    for fixture in model_fixtures:
        model = BuildingModel.from_json(fixture.read_text(encoding="utf-8"))
        # No checked-in contract document sets the field, so no serialization
        # may grow a "construction" key, and the canonical bytes match the
        # digests recorded before the field existed.
        assert "construction" not in canonical_json(model)
        assert canonical_digest(model) == MODEL_FIXTURE_DIGESTS[fixture.name]
        assert BuildingModel.from_dict(model.to_dict()).to_dict() == model.to_dict()
        checked += 1
    for case in valid_golden:
        model = load_golden_model(case)
        assert canonical_digest(model) == case.sha256
        # Golden fixtures may state tokens (the commercial TI does); the
        # contract rule under test stays: an unknown token serializes nothing,
        # a stated one round trips verbatim.
        for encoded_wall in json.loads(canonical_json(model))["walls"]:
            if encoded_wall.get("construction") is None:
                assert "construction" not in encoded_wall
            else:
                assert encoded_wall["construction"] == next(
                    wall.construction
                    for wall in model.walls
                    if wall.id == encoded_wall["id"]
                )
        checked += 1
    assert checked == len(model_fixtures) + len(valid_golden) > 0


def test_json_schema_accepts_tokens_rejects_unknown_and_allows_absence() -> None:
    validator = _schema_validator()
    base = _model(_wall(id="wall:a", construction="glazed")).to_dict()
    assert list(validator.iter_errors(base)) == []

    for token in sorted(WALL_CONSTRUCTION_TOKENS):
        document = json.loads(json.dumps(base))
        document["walls"][0]["construction"] = token
        assert list(validator.iter_errors(document)) == []

    absent = json.loads(json.dumps(base))
    del absent["walls"][0]["construction"]
    assert list(validator.iter_errors(absent)) == []

    null = json.loads(json.dumps(base))
    null["walls"][0]["construction"] = None
    assert list(validator.iter_errors(null)) == []

    for unknown in ("glass", "brick", 7, True):
        document = json.loads(json.dumps(base))
        document["walls"][0]["construction"] = unknown
        assert list(validator.iter_errors(document)), unknown


def test_ifc_round_trip_preserves_construction_through_canonical_pset() -> None:
    model = _model(
        _wall(id="wall:glazed", construction="glazed"),
        _wall(id="wall:plain"),
    )
    result = round_trip(model)
    validate_model(result)
    walls = {wall.id: wall for wall in result.walls}
    assert walls["wall:glazed"].construction == "glazed"
    assert walls["wall:plain"].construction is None
