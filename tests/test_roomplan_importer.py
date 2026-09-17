from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.importers.roomplan import (
    RoomPlanImportError,
    import_captured_room,
    load_captured_room,
)
from oabm.model import BuildingModel, Point3, stable_id

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "roomplan" / "captured-room-3d.json"
ROOM_ID = "11111111-1111-4111-8111-111111111111"


def _source() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_source_id(entities):
    return {
        entity.provenance[0].source_element_id: entity
        for entity in entities
        if entity.provenance
    }


def test_fixture_loads_as_canonical_3d_model() -> None:
    model = load_captured_room(FIXTURE, source_id="fixture:captured-room-3d")

    assert isinstance(model, BuildingModel)
    assert model.model_id == stable_id("model", f"roomplan:{ROOM_ID}")
    assert model.coordinate_system.handedness == "right"
    assert model.coordinate_system.up_axis == "+Z"
    assert model.coordinate_system.length_unit == "m"
    assert model.coordinate_system.angle_unit == "rad"

    assert len(model.levels) == 1
    assert len(model.spaces) == 1
    assert len(model.walls) == 4
    assert len(model.slabs) == 1
    assert len(model.openings) == 3
    assert len(model.obstacles) == 1
    assert not model.routes
    assert not model.electrical_devices
    assert not model.electrical_equipment

    assert BuildingModel.from_json(model.to_json()).to_dict() == model.to_dict()


def test_imported_output_validates_against_portable_contract_schema() -> None:
    model = import_captured_room(_source(), source_id="fixture")
    schema = json.loads(
        (ROOT / "contracts" / "oabm-model-v1.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_coordinate_conversion_preserves_3d_story_elevation_and_floor_geometry() -> None:
    model = import_captured_room(_source(), source_id="fixture")
    level = model.levels[0]
    floor = model.slabs[0]

    assert level.elevation_m == pytest.approx(3.2)
    assert level.height_m == pytest.approx(2.8)
    assert sorted({point.z for point in floor.footprint.points}) == pytest.approx([3.2])
    assert {(point.x, point.y) for point in floor.footprint.points} == {
        (-2.0, -1.5),
        (-2.0, 1.5),
        (2.0, -1.5),
        (2.0, 1.5),
    }
    assert model.spaces[0].footprint == floor.footprint
    assert model.spaces[0].usage == "kitchen"


def test_walls_keep_true_3d_positions_instead_of_plan_projection() -> None:
    model = import_captured_room(_source(), source_id="fixture")
    walls = _by_source_id(model.walls)

    south = walls["wall-south"]
    assert south.height_m == pytest.approx(2.8)
    assert [point.z for point in south.centerline.points] == pytest.approx([3.2, 3.2])
    assert [point.y for point in south.centerline.points] == pytest.approx([-1.5, -1.5])

    east = walls["wall-east"]
    assert [point.x for point in east.centerline.points] == pytest.approx([2.0, 2.0])
    assert sorted(point.y for point in east.centerline.points) == pytest.approx([-1.5, 1.5])
    assert all(point.z == pytest.approx(3.2) for point in east.centerline.points)


def test_openings_keep_pose_size_host_and_confidence() -> None:
    model = import_captured_room(_source(), source_id="fixture")
    walls = _by_source_id(model.walls)
    openings = _by_source_id(model.openings)

    door = openings["door-south"]
    assert door.host_id == walls["wall-south"].id
    assert door.opening_type == "door"
    assert door.pose.position == Point3(x=0.8, y=-1.5, z=4.25)
    assert door.size.x == pytest.approx(0.9)
    assert door.size.z == pytest.approx(2.1)
    assert door.size.y == pytest.approx(walls["wall-south"].thickness_m)
    assert door.confidence == pytest.approx(1.0)

    window = openings["window-north"]
    assert window.host_id == walls["wall-north"].id
    assert window.confidence == pytest.approx(0.66)

    inferred = openings["opening-east"]
    assert inferred.host_id == walls["wall-east"].id
    assert inferred.attributes["roomplan"]["host_inferred"] is True
    assert inferred.confidence == pytest.approx(0.33)


def test_object_is_oriented_3d_obstacle_with_source_semantics() -> None:
    model = import_captured_room(_source(), source_id="fixture")
    obstacle = model.obstacles[0]

    box = obstacle.geometry
    assert box.pose.position == Point3(x=0.0, y=0.0, z=3.575)
    assert box.size.x == pytest.approx(1.2)
    assert box.size.y == pytest.approx(0.6)
    assert box.size.z == pytest.approx(0.75)
    assert obstacle.level_id == model.levels[0].id
    assert obstacle.obstacle_type == "roomplan-object:table"
    assert obstacle.confidence == pytest.approx(0.66)
    assert obstacle.attributes["roomplan"]["attributes"] == {
        "fixtureNote": "preserved source metadata"
    }


def test_native_transform_polygon_curve_and_confidence_are_preserved() -> None:
    source = _source()
    model = import_captured_room(source, source_id="capture:unit-test")
    walls = _by_source_id(model.walls)
    south = walls["wall-south"]
    source_south = next(item for item in source["walls"] if item["identifier"] == "wall-south")

    assert south.attributes["roomplan"]["transform_column_major"] == source_south["transform"]
    assert south.attributes["roomplan"]["polygon_corners_local"] == source_south["polygonCorners"]
    assert south.attributes["roomplan"]["curve"] == source_south["curve"]
    assert south.attributes["roomplan"]["completedEdges"] == [0, 1, 2, 3]
    assert south.attributes["roomplan"]["surface_thickness_inferred"] is True
    assert south.provenance[0].source_id == "capture:unit-test"
    assert south.provenance[0].source_element_id == "wall-south"
    assert south.provenance[0].attributes["roomplan_confidence"] == "high"


def test_unknown_room_and_element_fields_are_retained_as_source_metadata() -> None:
    source = _source()
    source["futureRoomField"] = {"quality": 7}
    source["walls"][0]["futureSurfaceField"] = ["a", "b"]

    model = import_captured_room(source, source_id="fixture")
    south = _by_source_id(model.walls)["wall-south"]

    assert model.attributes["roomplan"]["extra_fields"] == {
        "futureRoomField": {"quality": 7}
    }
    assert south.attributes["roomplan"]["extra_fields"] == {
        "futureSurfaceField": ["a", "b"]
    }


def test_stable_identity_and_output_are_independent_of_array_order() -> None:
    source = _source()
    reordered = copy.deepcopy(source)
    for collection in ("walls", "floors", "doors", "windows", "openings", "objects"):
        reordered[collection] = list(reversed(reordered[collection]))

    first = import_captured_room(source, source_id="fixture")
    second = import_captured_room(reordered, source_id="fixture")

    assert first.to_dict() == second.to_dict()
    assert first.to_json() == second.to_json()

    first_ids = {
        entity.provenance[0].source_element_id: entity.id
        for collection in (
            first.levels,
            first.spaces,
            first.walls,
            first.slabs,
            first.openings,
            first.obstacles,
        )
        for entity in collection
        if entity.provenance
    }
    second_ids = {
        entity.provenance[0].source_element_id: entity.id
        for collection in (
            second.levels,
            second.spaces,
            second.walls,
            second.slabs,
            second.openings,
            second.obstacles,
        )
        for entity in collection
        if entity.provenance
    }
    assert first_ids == second_ids


def test_geometry_changes_do_not_replace_native_identity() -> None:
    source = _source()
    moved = copy.deepcopy(source)
    wall = next(item for item in moved["walls"] if item["identifier"] == "wall-south")
    wall["transform"][12] += 0.25

    first = import_captured_room(source, source_id="fixture")
    second = import_captured_room(moved, source_id="fixture")
    first_wall = _by_source_id(first.walls)["wall-south"]
    second_wall = _by_source_id(second.walls)["wall-south"]

    assert first_wall.id == second_wall.id
    assert first_wall.centerline != second_wall.centerline


def test_multiple_stories_emit_distinct_levels_with_3d_elevations() -> None:
    source = _source()
    second_floor = copy.deepcopy(source["floors"][0])
    second_floor["identifier"] = "floor-upper"
    second_floor["story"] = 2
    second_floor["transform"][13] = 6.4
    source["floors"].append(second_floor)

    second_wall = copy.deepcopy(source["walls"][0])
    second_wall["identifier"] = "wall-upper"
    second_wall["story"] = 2
    second_wall["transform"][13] = 7.8
    source["walls"].append(second_wall)

    model = import_captured_room(source, source_id="fixture")
    by_story = {
        level.attributes["roomplan"]["story"]: level
        for level in model.levels
    }

    assert sorted(by_story) == [1, 2]
    assert by_story[1].elevation_m == pytest.approx(3.2)
    assert by_story[2].elevation_m == pytest.approx(6.4)
    assert by_story[2].height_m == pytest.approx(2.8)

    upper_wall = _by_source_id(model.walls)["wall-upper"]
    assert upper_wall.level_id == by_story[2].id
    assert sorted({point.z for point in upper_wall.centerline.points}) == pytest.approx([6.4])


def test_sloped_floor_polygon_remains_3d() -> None:
    source = _source()
    floor = source["floors"][0]
    angle = math.radians(80.0)
    c = math.cos(angle)
    s = math.sin(angle)
    floor["transform"] = [
        1.0, 0.0, 0.0, 0.0,
        0.0, c, s, 0.0,
        0.0, -s, c, 0.0,
        0.0, 3.2, 0.0, 1.0,
    ]

    model = import_captured_room(source, source_id="fixture")
    z_values = sorted({round(point.z, 6) for point in model.slabs[0].footprint.points})

    assert len(z_values) == 2
    assert z_values[0] < 3.2 < z_values[1]


def test_curved_or_segmented_wall_bottom_is_kept_as_polyline() -> None:
    source = _source()
    wall = next(item for item in source["walls"] if item["identifier"] == "wall-south")
    wall["polygonCorners"] = [
        [-2.0, -1.4, 0.0],
        [-1.0, -1.4, -0.2],
        [0.0, -1.4, -0.3],
        [1.0, -1.4, -0.2],
        [2.0, -1.4, 0.0],
        [2.0, 1.4, 0.0],
        [1.0, 1.4, -0.2],
        [0.0, 1.4, -0.3],
        [-1.0, 1.4, -0.2],
        [-2.0, 1.4, 0.0],
    ]

    model = import_captured_room(source, source_id="fixture")
    imported = _by_source_id(model.walls)["wall-south"]

    assert len(imported.centerline.points) == 5
    assert len({round(point.y, 3) for point in imported.centerline.points}) > 1
    assert imported.attributes["roomplan"]["curve"] == wall["curve"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source.pop("identifier"), "identifier"),
        (
            lambda source: source["doors"][0].__setitem__(
                "parentIdentifier", "wall-does-not-exist"
            ),
            "unknown parentIdentifier",
        ),
        (
            lambda source: source["walls"][0].__setitem__(
                "transform", [1.0, 0.0, 0.0]
            ),
            "16 numbers",
        ),
    ],
)
def test_invalid_source_data_fails_at_import_boundary(mutation, message: str) -> None:
    source = _source()
    mutation(source)
    with pytest.raises(RoomPlanImportError, match=message):
        import_captured_room(source, source_id="fixture")


def test_source_to_canonical_rotation_is_right_handed_and_normalized() -> None:
    source = _source()
    object_item = source["objects"][0]
    angle = math.radians(37.0)
    c = math.cos(angle)
    s = math.sin(angle)
    object_item["transform"] = [
        c, 0.0, -s, 0.0,
        0.0, 1.0, 0.0, 0.0,
        s, 0.0, c, 0.0,
        0.5, 4.0, -0.25, 1.0,
    ]

    model = import_captured_room(source, source_id="fixture")
    rotation = model.obstacles[0].geometry.pose.rotation
    norm = math.sqrt(
        rotation.x**2 + rotation.y**2 + rotation.z**2 + rotation.w**2
    )

    assert norm == pytest.approx(1.0)
    assert model.obstacles[0].geometry.pose.position == Point3(
        x=0.5, y=0.25, z=4.0
    )
