import copy
from dataclasses import replace

import pytest

from oabm.model import BuildingModel, ContractError
from oabm.qa import (
    assert_deterministic,
    assert_identity_preserved,
    canonical_fingerprint,
    discover_cases,
    entity_ids,
    get_case,
    load_case_document,
    validate_case,
    validate_schema,
)


def _valid_cases():
    return discover_cases(include_invalid=False)


@pytest.mark.parametrize("case", _valid_cases(), ids=lambda case: case.name)
def test_golden_cases_validate_and_round_trip(case) -> None:
    model = validate_case(case)
    assert model is not None
    assert canonical_fingerprint(model) == canonical_fingerprint(model.to_dict())


def test_required_scenario_inventory_is_complete() -> None:
    names = {case.name for case in discover_cases()}
    assert {
        "rectangular-room",
        "door-obstruction",
        "panel-to-evse",
        "elevation-change",
        "multiple-valid-routes",
        "impossible-route",
        "two-level-building",
        "synthetic-garage",
        "invalid-reference",
    } <= names


def test_negative_reference_fixture_passes_schema_but_fails_contract() -> None:
    case = get_case("invalid-reference")
    document = load_case_document(case)
    validate_schema(document)

    with pytest.raises(ContractError, match="references missing id"):
        BuildingModel.from_dict(document)
    assert validate_case(case) is None


def test_manifest_tag_filter_is_deterministic_and_composable() -> None:
    first = discover_cases(tags=("routing-input",), include_invalid=False)
    second = discover_cases(tags=("routing-input",), include_invalid=False)
    assert [case.name for case in first] == [case.name for case in second]
    assert {case.name for case in first} == {
        "door-obstruction",
        "impossible-route",
        "multiple-valid-routes",
    }


def test_route_and_fitting_order_are_known_answer() -> None:
    model = validate_case("elevation-change")
    assert model is not None
    route = model.routes[0]
    assert route.fitting_ids == ("fitting:rise", "fitting:turn")
    assert [point.z for point in route.centerline.points] == [0.8, 2.4, 2.4]


def test_panel_to_evse_connectivity_semantics_are_known_answer() -> None:
    model = validate_case("panel-to-evse")
    assert model is not None
    circuit = model.circuits[0]
    assert circuit.source_port_id == "port:panel-load"
    assert circuit.load_port_ids == ("port:evse-feed",)
    assert circuit.route_ids == ("route:panel-evse",)
    assert {conductor.role for conductor in model.conductors} == {
        "line",
        "equipment-ground",
    }
    ports = {port.id: port for port in model.ports}
    assert ports["port:panel-load"].connected_port_ids == ("port:evse-feed",)
    assert ports["port:evse-feed"].connected_port_ids == ("port:panel-load",)


def test_two_level_fixture_preserves_explicit_z_geometry() -> None:
    model = validate_case("two-level-building")
    assert model is not None
    assert {level.id: level.elevation_m for level in model.levels} == {
        "level:ground": 0.0,
        "level:upper": 3.2,
    }
    upper = next(space for space in model.spaces if space.id == "space:upper")
    assert {point.z for point in upper.footprint.points} == {3.2}


def test_synthetic_garage_exercises_provenance_and_nontrivial_confidence() -> None:
    model = validate_case("synthetic-garage")
    assert model is not None
    values = [
        item.confidence
        for collection in (
            model.electrical_equipment,
            model.electrical_devices,
            model.routes,
            model.route_fittings,
            model.circuits,
            model.conductors,
        )
        for item in collection
    ]
    assert min(values) < 1.0
    assert all(wall.provenance for wall in model.walls)
    assert all(route.provenance for route in model.routes)


def test_checked_in_case_is_byte_meaning_deterministic() -> None:
    case = get_case("synthetic-garage")
    first = assert_deterministic(lambda: load_case_document(case), repeats=4)
    second = BuildingModel.from_json(first.to_json(indent=None))
    assert canonical_fingerprint(first) == canonical_fingerprint(second)
    assert entity_ids(first) == entity_ids(second)


def test_identity_preservation_helper_accepts_superset_outputs() -> None:
    before = validate_case("rectangular-room")
    assert before is not None
    after = replace(before, attributes={**before.attributes, "consumer":"test"})
    assert_identity_preserved(before, after)


def test_identity_preservation_helper_rejects_removed_ids() -> None:
    before = validate_case("rectangular-room")
    assert before is not None
    after = replace(before, walls=before.walls[1:])
    with pytest.raises(AssertionError, match="lost stable IDs"):
        assert_identity_preserved(before, after)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda document: document["openings"][0].__setitem__("host_id", "wall:missing"),
            "references missing id",
        ),
        (
            lambda document: document["ports"][0].__setitem__("owner_id", "equip:missing"),
            "references missing id",
        ),
        (
            lambda document: document["routes"][0].__setitem__("start_port_id", "port:missing"),
            "references missing id",
        ),
        (
            lambda document: document["route_fittings"][0].__setitem__("route_id", "route:missing"),
            "does not match route",
        ),
        (
            lambda document: document["circuits"][0]["route_ids"].__setitem__(0, "route:missing"),
            "references missing id",
        ),
        (
            lambda document: document["conductors"][0].__setitem__("circuit_id", "circuit:missing"),
            "references missing id",
        ),
    ],
)
def test_cross_reference_mutations_are_rejected(mutator, message) -> None:
    document = copy.deepcopy(load_case_document("synthetic-garage"))
    mutator(document)
    validate_schema(document)

    with pytest.raises(ContractError, match=message):
        BuildingModel.from_dict(document)


def test_asymmetric_explicit_port_connectivity_is_rejected() -> None:
    document = copy.deepcopy(load_case_document("panel-to-evse"))
    document["ports"][1]["connected_port_ids"] = []
    validate_schema(document)

    with pytest.raises(ContractError, match="connectivity must be symmetric"):
        BuildingModel.from_dict(document)


def test_route_endpoint_alignment_is_rejected() -> None:
    document = copy.deepcopy(load_case_document("panel-to-evse"))
    document["routes"][0]["centerline"]["points"][0]["x"] += 0.5
    validate_schema(document)

    with pytest.raises(ContractError, match="must start at the start port position"):
        BuildingModel.from_dict(document)


def test_fitting_membership_order_reference_is_rejected() -> None:
    document = copy.deepcopy(load_case_document("elevation-change"))
    document["routes"][0]["fitting_ids"] = ["fitting:rise"]
    validate_schema(document)

    with pytest.raises(ContractError, match="must appear in .*fitting_ids"):
        BuildingModel.from_dict(document)


def test_strict_serialization_rejects_unknown_fields() -> None:
    document = copy.deepcopy(load_case_document("rectangular-room"))
    document["workstream_private_shape"] = {}

    with pytest.raises(ContractError, match="unknown field"):
        BuildingModel.from_dict(document)


def test_schema_rejects_noncanonical_units() -> None:
    document = copy.deepcopy(load_case_document("rectangular-room"))
    document["coordinate_system"]["length_unit"] = "ft"

    with pytest.raises(AssertionError, match="Schema validation failed"):
        validate_schema(document)
