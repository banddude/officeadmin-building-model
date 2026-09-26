from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.ifc import round_trip, to_ifc
from oabm.model import BuildingModel, Point3, Polyline3D, stable_id
from oabm.qa import (
    GoldenFixtureError,
    canonical_digest,
    iter_entities,
    load_golden_cases,
    load_golden_model,
    validate_golden_case,
    validate_golden_suite,
    validate_lane_model,
    validate_public_fixture_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"

EXPECTED_VALID_CASES = {
    "rectangular-room",
    "door-obstruction",
    "panel-to-evse",
    "elevation-change",
    "multiple-valid-routes",
    "impossible-route",
    "two-level-building",
    "synthetic-garage",
}


def _case(name: str):
    return next(case for case in load_golden_cases(GOLDEN_ROOT) if case.name == name)


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _polygon_area_xy(points) -> float:
    return abs(
        sum(
            a.x * b.y - b.x * a.y
            for a, b in zip(points, (*points[1:], points[0]))
        )
    ) / 2.0


def _length(points) -> float:
    return sum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(points, points[1:])
    )


def _point_tuple(point) -> tuple[float, float, float]:
    return (point.x, point.y, point.z)


def test_manifest_owns_all_required_issue_10_cases() -> None:
    cases = load_golden_cases(GOLDEN_ROOT)
    valid = {case.name for case in cases if case.valid}
    invalid = {case.name for case in cases if not case.valid}

    assert valid == EXPECTED_VALID_CASES
    assert len(invalid) == 10
    assert all(case.path.is_relative_to(GOLDEN_ROOT) for case in cases)


def test_valid_golden_documents_validate_against_portable_json_schema() -> None:
    validator = _schema_validator()
    for case in load_golden_cases(GOLDEN_ROOT):
        if not case.valid:
            continue
        document = json.loads(case.path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        assert not errors, f"{case.name}: " + "\n".join(error.message for error in errors)


def test_full_golden_suite_validates_expected_successes_and_failures() -> None:
    results = validate_golden_suite(GOLDEN_ROOT)
    assert set(results) == {case.name for case in load_golden_cases(GOLDEN_ROOT)}
    for case in load_golden_cases(GOLDEN_ROOT):
        if case.valid:
            assert results[case.name] == case.sha256
        else:
            assert results[case.name] is None


@pytest.mark.parametrize("case", [c for c in load_golden_cases(GOLDEN_ROOT) if c.valid], ids=lambda c: c.name)
def test_valid_fixture_handoff_is_strict_stable_and_repeatable(case) -> None:
    first = load_golden_model(case)
    second = BuildingModel.from_json(case.path.read_text(encoding="utf-8"))

    first_ids = tuple(entity.id for entity in iter_entities(first))
    second_ids = tuple(entity.id for entity in iter_entities(second))
    assert first_ids == second_ids
    assert validate_lane_model(first) == case.sha256
    assert validate_lane_model(second) == case.sha256
    assert canonical_digest(first) == canonical_digest(second) == case.sha256
    assert BuildingModel.from_json(first.to_json(indent=None)).to_dict() == first.to_dict()


def test_stable_id_helper_is_repeatable_and_kind_namespaced() -> None:
    wall_a = stable_id("wall", "golden:source-wall-17")
    wall_b = stable_id("wall", "golden:source-wall-17")
    device = stable_id("device", "golden:source-wall-17")
    assert wall_a == wall_b
    assert wall_a.startswith("wall:")
    assert wall_a != device


def test_rectangular_room_has_known_5_by_4_by_3_geometry() -> None:
    model = load_golden_model(_case("rectangular-room"))
    space = model.spaces[0]
    assert _polygon_area_xy(space.footprint.points) == pytest.approx(20.0)
    assert space.height_m == pytest.approx(3.0)
    assert sorted(_length(wall.centerline.points) for wall in model.walls) == [4.0, 4.0, 5.0, 5.0]
    assert {point.z for point in space.footprint.points} == {0.0}
    assert {point.z for point in model.ceilings[0].footprint.points} == {3.0}


def test_door_obstruction_preserves_opening_host_and_hard_keepout() -> None:
    model = load_golden_model(_case("door-obstruction"))
    opening = model.openings[0]
    constraint = model.route_constraints[0]
    assert opening.host_id == "wall:door-room-south"
    assert opening.opening_type == "door"
    assert constraint.constraint_type == "no-go"
    assert constraint.hard is True
    assert constraint.level_id == opening_host_level(model, opening.host_id)
    assert set(constraint.applies_to) == {"emt", "pvc", "tray", "cable"}


def opening_host_level(model: BuildingModel, host_id: str) -> str:
    host = next(item for item in (*model.walls, *model.slabs, *model.ceilings) if item.id == host_id)
    return host.level_id


def test_panel_to_evse_exercises_connectivity_circuit_conductors_and_confidence() -> None:
    model = load_golden_model(_case("panel-to-evse"))
    start, end = model.ports
    route = model.routes[0]
    circuit = model.circuits[0]

    assert start.connected_port_ids == (end.id,)
    assert end.connected_port_ids == (start.id,)
    assert route.start_port_id == start.id
    assert route.end_port_id == end.id
    assert _point_tuple(route.centerline.points[0]) == _point_tuple(start.pose.position)
    assert _point_tuple(route.centerline.points[-1]) == _point_tuple(end.pose.position)
    assert circuit.source_port_id == start.id
    assert circuit.load_port_ids == (end.id,)
    assert circuit.route_ids == (route.id,)
    assert {conductor.circuit_id for conductor in model.conductors} == {circuit.id}
    assert {conductor.role for conductor in model.conductors} == {"line", "equipment-ground"}
    assert all(conductor.route_ids == (route.id,) for conductor in model.conductors)
    assert model.electrical_devices[0].confidence == pytest.approx(0.99)
    assert model.routes[0].confidence == pytest.approx(0.98)
    assert model.provenance[0].confidence == pytest.approx(0.99)


def test_elevation_change_has_ordered_fittings_at_route_vertices() -> None:
    model = load_golden_model(_case("elevation-change"))
    route = model.routes[0]
    fittings = {fitting.id: fitting for fitting in model.route_fittings}
    interior = [_point_tuple(point) for point in route.centerline.points[1:-1]]
    ordered_positions = [_point_tuple(fittings[fid].pose.position) for fid in route.fitting_ids]

    assert [point.z for point in route.centerline.points] == [0.8, 2.4, 2.4, 1.4]
    assert ordered_positions == interior
    assert all(fittings[fid].route_id == route.id for fid in route.fitting_ids)
    assert all(fittings[fid].angle_radians == pytest.approx(math.pi / 2) for fid in route.fitting_ids)


def test_multiple_valid_routes_share_endpoints_but_keep_distinct_geometry() -> None:
    model = load_golden_model(_case("multiple-valid-routes"))
    north, south = model.routes
    assert north.start_port_id == south.start_port_id
    assert north.end_port_id == south.end_port_id
    assert north.centerline.points != south.centerline.points
    assert max(point.y for point in north.centerline.points) > 3.0
    assert min(point.y for point in south.centerline.points) < 1.0
    assert model.obstacles[0].obstacle_type == "hard"


def test_impossible_route_is_input_only_and_does_not_fabricate_route_output() -> None:
    model = load_golden_model(_case("impossible-route"))
    assert len(model.ports) == 2
    assert model.routes == ()
    assert len(model.route_constraints) == 1
    barrier = model.route_constraints[0]
    assert barrier.hard is True
    assert barrier.constraint_type == "no-go"
    assert barrier.geometry.size.y == pytest.approx(4.0)
    assert barrier.geometry.size.z == pytest.approx(3.0)


def test_two_level_building_keeps_real_z_geometry_and_cross_level_route() -> None:
    model = load_golden_model(_case("two-level-building"))
    assert [level.elevation_m for level in model.levels] == [0.0, 3.0]
    assert {point.z for point in model.spaces[0].footprint.points} == {0.0}
    assert {point.z for point in model.spaces[1].footprint.points} == {3.0}
    route = model.routes[0]
    assert min(point.z for point in route.centerline.points) == pytest.approx(1.5)
    assert max(point.z for point in route.centerline.points) == pytest.approx(4.2)
    assert model.openings[0].host_id == "slab:two-upper"


def test_synthetic_garage_fixture_covers_gate_b_interfaces_without_implementing_them() -> None:
    model = load_golden_model(_case("synthetic-garage"))
    route = model.routes[0]
    fitting_by_id = {fitting.id: fitting for fitting in model.route_fittings}
    interior = {_point_tuple(point) for point in route.centerline.points[1:-1]}

    assert model.openings[0].opening_type == "door"
    assert model.obstacles[0].obstacle_type == "hard"
    assert model.route_constraints[0].constraint_type == "preferred-corridor"
    assert model.route_constraints[0].hard is False
    assert all(_point_tuple(fitting_by_id[fid].pose.position) in interior for fid in route.fitting_ids)
    assert model.circuits[0].route_ids == (route.id,)
    assert all(conductor.route_ids == (route.id,) for conductor in model.conductors)
    assert {conductor.role for conductor in model.conductors} == {"line", "equipment-ground"}


def test_invalid_cases_reject_at_canonical_boundary_with_declared_reason() -> None:
    invalid = [case for case in load_golden_cases(GOLDEN_ROOT) if not case.valid]
    assert invalid
    for case in invalid:
        assert validate_golden_case(case) is None


def test_negative_suite_contains_schema_and_cross_reference_failures() -> None:
    validator = _schema_validator()
    cases = {case.name: case for case in load_golden_cases(GOLDEN_ROOT) if not case.valid}

    # Reference/connectivity/geometry failures are intentionally valid JSON shapes so
    # they prove Python cross-reference and geometric validation adds value beyond schema.
    for name in (
        "invalid-missing-reference",
        "invalid-asymmetric-connectivity",
        "invalid-route-endpoint-mismatch",
        "invalid-fitting-ownership-mismatch",
        "invalid-duplicate-id",
        "invalid-duplicate-polyline-point",
    ):
        document = json.loads(cases[name].path.read_text(encoding="utf-8"))
        assert list(validator.iter_errors(document)) == [], name

    # Portable schema independently rejects shape/range/unit errors.
    for name in (
        "invalid-confidence-out-of-range",
        "invalid-noncanonical-units",
        "invalid-unknown-field",
        "invalid-provenance-source-id-empty",
    ):
        document = json.loads(cases[name].path.read_text(encoding="utf-8"))
        assert list(validator.iter_errors(document)), name


def test_public_fixture_provenance_validator_rejects_unmarked_data() -> None:
    model = load_golden_model(_case("panel-to-evse"))
    altered_device = replace(model.electrical_devices[0], provenance=())
    altered = replace(model, electrical_devices=(altered_device,))
    with pytest.raises(GoldenFixtureError, match="must carry synthetic provenance"):
        validate_public_fixture_provenance(altered)


def test_digest_changes_when_a_valid_model_changes() -> None:
    model = load_golden_model(_case("elevation-change"))
    route = model.routes[0]
    points = list(route.centerline.points)
    points[1] = Point3(x=0.5, y=1.2, z=2.4)
    changed_route = replace(route, centerline=Polyline3D(points=tuple(points)))
    changed_fitting = replace(model.route_fittings[0], pose=replace(model.route_fittings[0].pose, position=points[1]))
    changed = replace(model, routes=(changed_route,), route_fittings=(changed_fitting, model.route_fittings[1]))

    assert validate_lane_model(changed) != canonical_digest(model)


@pytest.mark.parametrize("name", sorted(EXPECTED_VALID_CASES))
def test_every_golden_case_survives_the_ifc_export_path(name: str) -> None:
    """Assert the golden suite through to_ifc, not only as canonical JSON.

    The suite used to validate every fixture as a document and never export
    one. That let panel-to-evse -- a case whose stated purpose is port
    connectivity -- round trip through IFC with connected_port_ids silently
    emptied, because nothing in the suite ever called the adapter.
    """

    model = load_golden_model(_case(name))

    assert round_trip(model).to_dict() == model.to_dict()


@pytest.mark.parametrize("name", sorted(EXPECTED_VALID_CASES))
def test_every_golden_case_exports_valid_ifc4(name: str) -> None:
    """EXPRESS where-rules, not just attribute typing.

    ifcopenshell.validate without express_rules=True reports nothing for a
    product that carries geometry with no ObjectPlacement.
    """

    import ifcopenshell.validate

    ifc = to_ifc(load_golden_model(_case(name)))
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)

    assert logger.statements == []
