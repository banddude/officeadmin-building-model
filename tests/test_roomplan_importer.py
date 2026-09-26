from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import stat
import subprocess
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
    assert len(south.centerline.points) > 2
    assert all(point.z == pytest.approx(3.2) for point in south.centerline.points)
    assert south.centerline.points[0].x == pytest.approx(-2.0)
    assert south.centerline.points[-1].x == pytest.approx(2.0)
    assert south.centerline.points[0].y == pytest.approx(-1.5)
    assert south.centerline.points[-1].y == pytest.approx(-1.5)
    assert max(point.y for point in south.centerline.points) > -1.2

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
    wall.pop("curve", None)
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
    assert "curve" not in imported.attributes["roomplan"]


def test_roomplan_curve_is_tessellated_into_canonical_wall_centerline() -> None:
    source = _source()
    source_wall = next(
        item for item in source["walls"] if item["identifier"] == "wall-south"
    )

    model = import_captured_room(source, source_id="fixture")
    wall = _by_source_id(model.walls)["wall-south"]

    assert len(wall.centerline.points) > 2
    assert wall.centerline.points[0].x == pytest.approx(-2.0)
    assert wall.centerline.points[-1].x == pytest.approx(2.0)
    assert wall.centerline.points[0].y == pytest.approx(-1.5)
    assert wall.centerline.points[-1].y == pytest.approx(-1.5)
    assert max(point.y for point in wall.centerline.points) > -1.2
    assert wall.attributes["roomplan"]["curve"] == source_wall["curve"]


def test_orphan_opening_host_inference_uses_every_polyline_segment() -> None:
    source = _source()
    wall = next(item for item in source["walls"] if item["identifier"] == "wall-south")
    wall.pop("curve", None)
    wall["polygonCorners"] = [
        [-2.0, -1.4, 0.0],
        [0.0, -1.4, -1.0],
        [2.0, -1.4, 0.0],
        [2.0, 1.4, 0.0],
        [0.0, 1.4, -1.0],
        [-2.0, 1.4, 0.0],
    ]
    source["walls"] = [wall]
    source["doors"] = []
    source["windows"] = []

    opening = source["openings"][0]
    opening["transform"][12] = 0.0
    opening["transform"][13] = 4.25
    opening["transform"][14] = 0.5
    opening.pop("parentIdentifier", None)
    source["openings"] = [opening]

    model = import_captured_room(source, source_id="fixture")
    imported = _by_source_id(model.openings)["opening-east"]

    assert imported.host_id == _by_source_id(model.walls)["wall-south"].id
    assert imported.attributes["roomplan"]["host_inference"]["distance_m"] == pytest.approx(
        0.0
    )


def test_orphan_opening_host_inference_rejects_equidistant_corner_ambiguity() -> None:
    source = _source()
    south = next(item for item in source["walls"] if item["identifier"] == "wall-south")
    east = next(item for item in source["walls"] if item["identifier"] == "wall-east")
    south.pop("curve", None)
    source["walls"] = [south, east]
    source["doors"] = []
    source["windows"] = []

    opening = source["openings"][0]
    opening["transform"][12] = 2.0
    opening["transform"][13] = 4.25
    opening["transform"][14] = 1.5
    opening.pop("parentIdentifier", None)
    source["openings"] = [opening]

    with pytest.raises(RoomPlanImportError, match="host wall is ambiguous"):
        import_captured_room(source, source_id="fixture")


def test_derived_level_and_space_confidence_follow_low_floor_evidence() -> None:
    source = _source()
    source["floors"][0]["confidence"] = {"low": {}}

    model = import_captured_room(source, source_id="fixture")
    level = model.levels[0]
    space = model.spaces[0]

    assert level.confidence == pytest.approx(0.33)
    assert level.provenance[0].confidence == pytest.approx(0.33)
    assert level.attributes["roomplan"]["elevation_confidence"] == pytest.approx(0.33)
    assert space.confidence == pytest.approx(0.33)
    assert space.provenance[0].confidence == pytest.approx(0.33)


def test_derived_level_confidence_tracks_medium_wall_evidence() -> None:
    source = _source()
    east = next(item for item in source["walls"] if item["identifier"] == "wall-east")
    source["walls"] = [east]
    source["floors"] = []
    source["doors"] = []
    source["windows"] = []
    source["openings"] = []
    source["objects"] = []

    model = import_captured_room(source, source_id="fixture")
    level = model.levels[0]

    assert level.attributes["roomplan"]["elevation_method"] == "wall-base"
    assert level.confidence == pytest.approx(0.66)
    assert level.provenance[0].confidence == pytest.approx(0.66)


def test_default_zero_level_elevation_has_zero_derived_confidence() -> None:
    source = _source()
    for collection in ("walls", "floors", "doors", "windows", "openings", "objects"):
        source[collection] = []

    model = import_captured_room(source, source_id="fixture")
    level = model.levels[0]

    assert level.elevation_m == pytest.approx(0.0)
    assert level.attributes["roomplan"]["elevation_method"] == "default-zero"
    assert level.confidence == pytest.approx(0.0)
    assert level.provenance[0].confidence == pytest.approx(0.0)


def test_inferred_opening_host_confidence_is_bounded_by_host_wall() -> None:
    source = _source()
    opening = source["openings"][0]
    opening["confidence"] = {"high": {}}

    model = import_captured_room(source, source_id="fixture")
    imported = _by_source_id(model.openings)["opening-east"]

    assert imported.attributes["roomplan"]["host_inferred"] is True
    assert imported.attributes["roomplan"]["source_confidence_value"] == pytest.approx(1.0)
    assert imported.confidence == pytest.approx(0.66)
    assert imported.provenance[0].confidence == pytest.approx(0.66)
    assert (
        imported.attributes["roomplan"]["host_inference"]["confidence_rule"]
        == "minimum-of-opening-and-inferred-host-wall-confidence"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source.__setitem__("identifier", "   "), "identifier"),
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


BUNDLE_FIXTURE = ROOT / "fixtures" / "roomplan" / "bundle-v3-envelope-room.json"


def test_real_bundle_envelope_imports_with_a_caller_supplied_identity() -> None:
    """A CapturedRoom exported inside a scan bundle has to load.

    This fixture carries the envelope a real Bundle v3 export actually has:
    `coreModel`, `referenceOriginTransform`, `sections`, and **no top-level
    identifier**. The capture's identity lives in the bundle around the room,
    not in the room document.

    The importer previously refused that outright, so the scan lane had only
    ever run against a fixture that happened to carry an identifier while the
    real production format failed to load at all.

    Identity is still required and still never invented: the caller's
    `source_id` supplies it.
    """
    assert "identifier" not in json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))

    model = load_captured_room(BUNDLE_FIXTURE, source_id="fixture:bundle-v3-envelope")
    assert model.walls
    assert model.spaces
    assert model.openings

    # Stable ids key off the supplied identity, so two imports agree exactly.
    repeated = load_captured_room(
        BUNDLE_FIXTURE, source_id="fixture:bundle-v3-envelope"
    )
    assert model.to_dict() == repeated.to_dict()


def test_bundle_envelope_with_no_identity_at_all_is_refused() -> None:
    """With neither a stated identifier nor a source_id there is no identity.

    Stable entity ids derive from it, so inventing one would silently produce a
    different model on every run.
    """
    document = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with pytest.raises(RoomPlanImportError, match="source_id is required"):
        import_captured_room(document)


def test_a_stated_but_blank_room_identifier_is_still_refused() -> None:
    """Absent is not the same as present-and-empty.

    An envelope that states no identifier is a real export shape. A document
    that states an empty one is malformed, and must not quietly fall back to
    the caller's source_id.
    """
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["identifier"] = "   "
    with pytest.raises(RoomPlanImportError, match="identifier"):
        import_captured_room(document, source_id="fixture:blank-identifier")


@pytest.mark.parametrize("stated", [None, "", "   ", 0, False, []])
def test_a_stated_but_malformed_room_identifier_is_refused(stated: object) -> None:
    """Stating a bad identifier is not the same as stating none.

    `null` is the case that nearly slipped through: reading the field with
    `dict.get()` returns `None` both for a bundle envelope that omits the key
    and for a document that states `"identifier": null`. Only the first is a
    real export shape. Treating the second as absent would let the caller's
    `source_id` silently paper over a malformed document, so absence is decided
    by the key and every stated value must be a non-empty string.
    """
    document = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    document["identifier"] = stated
    with pytest.raises(RoomPlanImportError, match="must be a non-empty string"):
        import_captured_room(document, source_id="fixture:malformed-identifier")


_ENVELOPE_CORE_MODEL = '  "coreModel": "BUNDLE-V3-CORE-MODEL-BLOB-PLACEHOLDER",\n'
_ENVELOPE_TRANSFORM = (
    '  "referenceOriginTransform": [\n'
    '    0.0,\n    0.0,\n    1.0,\n    0.0,\n'
    '    0.0,\n    1.0,\n    0.0,\n    0.0,\n'
    '    -1.0,\n    0.0,\n    0.0,\n    0.0,\n'
    '    0.5,\n    0.0,\n    0.25,\n    1.0\n'
    '  ],\n'
)


def _build_envelope_fixture(synthetic_text: str) -> str:
    """The envelope fixture, built from the synthetic fixture by textual surgery.

    This IS the documented construction in `fixtures/README.md`, made
    executable: remove the synthetic file's top-level `identifier`, insert the
    placeholder `coreModel`, and insert the hand-written transform before
    `sections`. Nothing is parsed and re-dumped, which is what once silently
    re-sorted a nested object's keys.

    To regenerate the fixture after a deliberate change to the synthetic one:
        BUNDLE_FIXTURE.write_text(_build_envelope_fixture(FIXTURE.read_text()))
    """
    lines = synthetic_text.splitlines(keepends=True)
    kept = [line for line in lines if not re.fullmatch(r'  "identifier": "[^"\n]*",\n', line)]
    assert len(lines) - len(kept) == 1, "expected exactly one top-level identifier line"
    text = "".join(kept)
    assert text.startswith("{\n")
    text = "{\n" + _ENVELOPE_CORE_MODEL + text[2:]
    marker = '  "sections": ['
    assert text.count(marker) == 1
    return text.replace(marker, _ENVELOPE_TRANSFORM + marker, 1)


def _first_difference_line(actual: bytes, expected: bytes) -> int:
    offset = next(
        (index for index, (a, b) in enumerate(zip(actual, expected)) if a != b),
        min(len(actual), len(expected)),
    )
    return actual[:offset].count(b"\n") + 1


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON key {key!r}: the parser would keep only the last value"
        result[key] = value
    return result


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _assert_superproject_blob(repo_root: Path, path: Path, raw: bytes) -> None:
    """Protected bytes must be ordinary files in this repository's Git tree."""
    try:
        relative = path.relative_to(repo_root)
    except ValueError:
        # Mutation tests use standalone temporary files. The real three trust
        # roots are under ROOT and must always pass the Git object checks.
        return
    assert relative.parts, f"{path}: protected file cannot be the repository root"
    for parent in reversed(relative.parents):
        if parent == Path("."):
            continue
        entry = _git_bytes(repo_root, "ls-tree", "-z", "HEAD", "--", parent.as_posix())
        assert entry.startswith(b"040000 tree ") and entry.endswith(
            b"\t" + parent.as_posix().encode() + b"\0"
        ), f"{parent}: protected path ancestor must be a normal superproject tree"
        exact_index_entry = next(
            (
                entry
                for entry in _git_bytes(
                    repo_root, "ls-files", "-s", "-z", "--", parent.as_posix()
                ).split(b"\0")
                if entry.endswith(b"\t" + parent.as_posix().encode())
            ),
            b"",
        )
        assert not exact_index_entry, (
            f"{parent}: protected path ancestor cannot be an indexed symlink or gitlink"
        )
    name = relative.as_posix()
    tree_entry = _git_bytes(repo_root, "ls-tree", "-z", "HEAD", "--", name)
    index_entry = _git_bytes(repo_root, "ls-files", "-s", "-z", "--", name)
    assert tree_entry.startswith(b"100644 blob ") and tree_entry.endswith(
        b"\t" + name.encode() + b"\0"
    ), f"{name}: protected file must be a normal superproject blob in HEAD"
    assert index_entry.startswith(b"100644 ") and index_entry.endswith(
        b"\t" + name.encode() + b"\0"
    ), f"{name}: protected file must be a normal superproject blob in the index"
    tree_oid = tree_entry.split(b"\t", 1)[0].split()[-1].decode()
    index_oid = index_entry.split(b"\t", 1)[0].split()[1].decode()
    assert tree_oid == index_oid, f"{name}: index blob differs from HEAD"
    assert raw == _git_bytes(repo_root, "cat-file", "blob", tree_oid), (
        f"{name}: worktree bytes differ from the reviewed superproject blob"
    )


def _assert_envelope_fixture_file_is_synthetic(
    bundle_path: Path,
    synthetic_path: Path,
    source_path: Path | None = None,
    *,
    repo_root: Path = ROOT,
) -> None:
    """Raise unless the fixture's BYTES ON DISK are exactly its documented construction.

    Compares bytes, never text. A text read -- `Path.read_text` included --
    applies universal-newline translation, turning CRLF and a lone CR into LF
    before any comparison runs. So line endings can carry data that no
    text-level check will ever see: an independent sweep encoded a stand-in
    measurement one bit per line (LF for 0, CRLF for 1) into this fixture, and
    a text-comparing version of this guard passed it.

    Every byte a reviewer could not see in a diff is refused in both JSON files
    and the Python source file containing the trusted envelope constants:
    only LF and printable ASCII are allowed, with no trailing whitespace. The
    construction below reproduces the synthetic fixture faithfully, so a
    channel planted in `captured-room-3d.json` would flow into the envelope
    fixture and still pass the byte comparison. Checking the synthetic file for
    them narrows its trust boundary to what a reviewer CAN see: its JSON values
    and visible formatting.

    Each path and every ancestor must have ordinary file/directory types and
    normal superproject blob/tree objects, so a symlink or initialized gitlink
    cannot redirect a protected path to bytes stored outside the reviewed Git
    tree. JSON keys must be unique at every depth, so the parser cannot
    discard a visible first value.
    The remaining semantic trust roots are the synthetic values and the two
    envelope constants; a reviewer must judge those values themselves.
    """
    source_path = source_path or Path(__file__)
    files = []
    for path in (bundle_path, synthetic_path, source_path):
        assert path.is_absolute() and ".." not in path.parts, (
            f"{path}: protected path must be absolute and free of parent traversal"
        )
        for parent in path.parents:
            assert stat.S_ISDIR(parent.lstat().st_mode), (
                f"{parent}: protected path ancestor must be a directory, not a symlink or special file"
            )
        assert stat.S_ISREG(path.lstat().st_mode), (
            f"{path.name}: must be a regular file, not a symlink or special file"
        )
        files.append((path, path.read_bytes()))
    for path, raw in files:
        _assert_superproject_blob(repo_root, path, raw)
        assert b"\r" not in raw, (
            f"{path.name}: contains a carriage return; line endings are translated "
            "before any text comparison sees them, so they can carry data unseen"
        )
        assert raw.isascii(), (
            f"{path.name}: contains non-ASCII bytes; a zero-width character renders "
            "as nothing in a diff, so it can carry data a reviewer never sees"
        )
        assert b"\t" not in raw, (
            f"{path.name}: contains a tab; a tab-or-spaces choice per line is a "
            "channel of one bit per line"
        )
        hidden = next((byte for byte in raw if byte != 10 and not 32 <= byte <= 126), None)
        assert hidden is None, (
            f"{path.name}: contains a non-printable byte 0x{hidden:02x}; "
            "only LF and printable ASCII are allowed"
        )
        trailing = next(
            (n for n, line in enumerate(raw.split(b"\n"), 1) if line != line.rstrip(b" ")),
            None,
        )
        assert trailing is None, (
            f"{path.name}: trailing whitespace on line {trailing}, which most diffs "
            "do not show"
        )
    bundle = files[0][1]
    synthetic = files[1][1]
    for raw in (bundle, synthetic):
        json.loads(raw, object_pairs_hook=_unique_json_pairs)
    expected = _build_envelope_fixture(synthetic.decode("utf-8")).encode("utf-8")
    if bundle != expected:
        raise AssertionError(
            f"{bundle_path.name} is not byte-for-byte its documented construction; "
            f"first difference on line {_first_difference_line(bundle, expected)}"
        )


def test_the_bundle_envelope_fixture_carries_no_captured_measurements() -> None:
    """The envelope shape is real; every value in it must be synthetic.

    This fixture exists because the real Bundle v3 KEY LAYOUT differs from
    what the importer accepted. The layout is the only thing taken from a real
    export. Committing an actual capture's transform, section centre or room
    dimensions to a public repository would leak the geometry of someone's
    home.

    The failure mode this guards is a future edit pasting real coordinates in
    to "make the fixture more realistic", which reads as an improvement and is
    a disclosure. The two regression tests below run every way found to smuggle
    a value past an earlier version of this guard through the same disk path.
    """
    _assert_envelope_fixture_file_is_synthetic(BUNDLE_FIXTURE, FIXTURE)


def test_the_fixture_guard_rejects_symlinks_to_approved_bytes(tmp_path: Path) -> None:
    """The tracked path must contain the JSON bytes, not a symlink target name."""
    target = tmp_path / "bundle-v3-envelope-987.1234567890123.json"
    target.write_bytes(BUNDLE_FIXTURE.read_bytes())
    bundle = tmp_path / BUNDLE_FIXTURE.name
    bundle.symlink_to(target.name)
    with pytest.raises(AssertionError, match="must be a regular file"):
        _assert_envelope_fixture_file_is_synthetic(bundle, FIXTURE)

    bundle.unlink()
    bundle.write_bytes(BUNDLE_FIXTURE.read_bytes())
    synthetic = tmp_path / FIXTURE.name
    synthetic.symlink_to(FIXTURE)
    with pytest.raises(AssertionError, match="must be a regular file"):
        _assert_envelope_fixture_file_is_synthetic(bundle, synthetic)


@pytest.mark.parametrize("protected", ["bundle", "synthetic", "source"])
def test_the_fixture_guard_rejects_symlinked_parent_directories(
    tmp_path: Path, protected: str
) -> None:
    """A regular child through a symlinked directory has no blob at that Git path."""
    sources = [BUNDLE_FIXTURE, FIXTURE, Path(__file__)]
    actual = tmp_path / "approved"
    actual.mkdir()
    link = tmp_path / "protected"
    link.symlink_to(actual.name, target_is_directory=True)
    paths = list(sources)
    selected = {"bundle": 0, "synthetic": 1, "source": 2}[protected]
    (actual / sources[selected].name).write_bytes(sources[selected].read_bytes())
    paths[selected] = link / sources[selected].name
    assert stat.S_ISREG(paths[selected].lstat().st_mode)
    assert link.is_symlink()
    with pytest.raises(AssertionError, match="ancestor must be a directory"):
        _assert_envelope_fixture_file_is_synthetic(*paths)


def test_the_fixture_guard_rejects_an_initialized_gitlink_ancestor(tmp_path: Path) -> None:
    """An ordinary directory on disk may still be a gitlink in the PR tree."""
    submodule = tmp_path / "source-repo"
    submodule.mkdir()
    _git_bytes(submodule, "init", "-q")
    (submodule / "approved.txt").write_text("synthetic only\n", encoding="ascii")
    _git_bytes(submodule, "add", ".")
    _git_bytes(submodule, "-c", "user.name=Fixture Test", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic")
    submodule_oid = _git_bytes(submodule, "rev-parse", "HEAD").decode().strip()

    superproject = tmp_path / "superproject"
    fixture_dir = superproject / "fixtures" / "roomplan"
    test_dir = superproject / "tests"
    fixture_dir.mkdir(parents=True)
    test_dir.mkdir()
    bundle = fixture_dir / BUNDLE_FIXTURE.name
    synthetic = fixture_dir / FIXTURE.name
    source = test_dir / Path(__file__).name
    for target, original in (
        (bundle, BUNDLE_FIXTURE),
        (synthetic, FIXTURE),
        (source, Path(__file__)),
    ):
        target.write_bytes(original.read_bytes())
    _git_bytes(superproject, "init", "-q")
    _git_bytes(superproject, "add", ".")
    _git_bytes(superproject, "-c", "user.name=Fixture Test", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "ordinary files")
    _git_bytes(superproject, "rm", "-r", "--cached", "fixtures/roomplan")
    _git_bytes(superproject, "update-index", "--add", "--cacheinfo", f"160000,{submodule_oid},fixtures/roomplan")
    _git_bytes(superproject, "-c", "user.name=Fixture Test", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "gitlink ancestor")

    assert fixture_dir.is_dir() and bundle.is_file() and synthetic.is_file()
    assert _git_bytes(superproject, "ls-tree", "HEAD", "--", "fixtures/roomplan").startswith(
        b"160000 commit "
    )
    with pytest.raises(AssertionError, match="ancestor must be a normal superproject tree"):
        _assert_envelope_fixture_file_is_synthetic(
            bundle, synthetic, source, repo_root=superproject
        )


def test_the_fixture_guard_checks_its_trusted_source_bytes(tmp_path: Path) -> None:
    """Mixed source line endings preserve Python semantics but can carry bits."""
    raw = Path(__file__).read_bytes()
    at = raw.index(b"_ENVELOPE_CORE_MODEL = ")
    line_end = raw.index(b"\n", at)
    mutated = raw[:line_end] + b"\r" + raw[line_end:]
    source = tmp_path / Path(__file__).name
    source.write_bytes(mutated)
    assert source.read_text(encoding="utf-8") == Path(__file__).read_text(encoding="utf-8")
    compile(mutated, str(source), "exec")
    with pytest.raises(AssertionError, match="test_roomplan_importer.py: contains a carriage return"):
        _assert_envelope_fixture_file_is_synthetic(BUNDLE_FIXTURE, FIXTURE, source)


def test_the_fixture_guard_rejects_form_feed_in_trusted_source(tmp_path: Path) -> None:
    """Python accepts invisible form-feed before a trust-root assignment."""
    raw = Path(__file__).read_bytes()
    mutated = raw.replace(b"_ENVELOPE_CORE_MODEL = ", b"\x0c_ENVELOPE_CORE_MODEL = ", 1)
    assert mutated != raw
    compile(mutated, str(tmp_path / "source.py"), "exec")
    source = tmp_path / Path(__file__).name
    source.write_bytes(mutated)
    with pytest.raises(AssertionError, match="contains a non-printable byte 0x0c"):
        _assert_envelope_fixture_file_is_synthetic(BUNDLE_FIXTURE, FIXTURE, source)


def test_the_fixture_guard_rejects_duplicate_keys_in_the_synthetic_root(tmp_path: Path) -> None:
    """Regeneration can preserve duplicate keys while the parser discards one."""
    raw = FIXTURE.read_bytes()
    duplicated = raw.replace(b'  "story": 1,\n', b'  "story": 1,\n  "story": 1,\n', 1)
    assert duplicated != raw
    json.loads(duplicated)
    synthetic = tmp_path / FIXTURE.name
    synthetic.write_bytes(duplicated)
    bundle = tmp_path / BUNDLE_FIXTURE.name
    bundle.write_bytes(_build_envelope_fixture(duplicated.decode("utf-8")).encode("utf-8"))
    with pytest.raises(AssertionError, match="duplicate JSON key 'story'"):
        _assert_envelope_fixture_file_is_synthetic(bundle, synthetic)


def _top_level_span_bounds(text: str, key: str) -> tuple[int, int]:
    start = text.index(f'  "{key}": ')
    depth = 0
    started = False
    for index in range(start, len(text)):
        char = text[index]
        if char in "[{":
            depth += 1
            started = True
        elif char in "]}":
            depth -= 1
            if started and depth == 0:
                return start, index + 1
    raise AssertionError(f"unterminated value for {key}")


def _poisoned_walls(text: str) -> list[object]:
    """The fixture's walls with one scalar replaced by a capture-looking value."""
    walls = json.loads(text)["walls"]
    walls[0]["curve"]["center"][1] = 987.1234567890123
    return walls


def _evade_by_duplicate_top_level_key(text: str) -> str:
    # Review 3's reproduction: a clean `walls` stated first, a poisoned `walls`
    # stated second. The parser loads the second; a first-occurrence scan
    # inspects the first.
    start, end = _top_level_span_bounds(text, "walls")
    poisoned = '  "walls": ' + json.dumps(_poisoned_walls(text), indent=2).replace("\n", "\n  ")
    return text[:end] + ",\n" + poisoned + text[end:]


def _evade_by_nested_decoy(text: str) -> str:
    # A clean `walls` hidden inside objects[0] at top-level indentation, with the
    # real top-level `walls` poisoned and minified so no textual scan counts it.
    start, end = _top_level_span_bounds(text, "walls")
    clean_span = text[start:end]
    rest = text[:start] + text[end:].replace(",\n", "\n", 1)
    anchor = '  "objects": [\n    {\n'
    at = rest.index(anchor) + len(anchor)
    rest = rest[:at] + clean_span + ",\n" + rest[at:]
    minified = json.dumps(_poisoned_walls(text), separators=(",", ":"))
    marker = '"coreModel": "BUNDLE-V3-CORE-MODEL-BLOB-PLACEHOLDER",'
    return rest.replace(marker, marker[:-1] + ',"walls":' + minified + ",", 1)


def _evade_by_moving_a_synthetic_scalar(text: str) -> str:
    # Review 2's reproduction: an existing high-precision synthetic scalar moved
    # into the transform passed the original global whitelist.
    return text.replace("    0.25,", "    4.58257569495584,", 1)


def _evade_by_new_precise_value(text: str) -> str:
    return text.replace("    0.25,", "    0.123456789,", 1)


def _evade_by_reserializing(text: str) -> str:
    # Same values, keys re-sorted: how the byte-identity claim broke originally.
    return json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def _evade_by_digits_past_double_precision(text: str) -> str:
    # Found by the author's own sweep after review 3: `float()` rounds these
    # trailing digits away, so every parsed comparison sees exactly 0.25 while
    # the public text carries an arbitrary digit string.
    return text.replace("    0.25,", "    0.2500000000000000000012345678,", 1)


def _evade_by_exponent_spelling(text: str) -> str:
    # Same value, different text. Leaks nothing on its own, but proves the
    # envelope fields were compared as values and never as bytes.
    return text.replace("    0.25,", "    2.5e-1,", 1)


def _evade_by_boolean_for_one(text: str) -> str:
    # `True == 1.0` in Python, so a parsed comparison accepts it.
    return text.replace("    1.0\n  ],", "    true\n  ],", 1)


@pytest.mark.parametrize(
    "evasion",
    [
        _evade_by_duplicate_top_level_key,
        _evade_by_nested_decoy,
        _evade_by_moving_a_synthetic_scalar,
        _evade_by_new_precise_value,
        _evade_by_reserializing,
        _evade_by_digits_past_double_precision,
        _evade_by_exponent_spelling,
        _evade_by_boolean_for_one,
    ],
    ids=lambda evasion: evasion.__name__.removeprefix("_evade_by_"),
)
def test_the_fixture_guard_catches_every_known_evasion(evasion, tmp_path: Path) -> None:
    """Every content-level way found to smuggle a value past this guard stays refused.

    Each mutation is written to disk and run through the guard's real file-reading
    path, so a gap between the bytes on disk and the text the guard compares
    cannot hide here. Each is asserted to be valid JSON, so the refusal comes from
    the guard and not from a parse error that would be refused for an unrelated
    reason.
    """
    original = BUNDLE_FIXTURE.read_bytes().decode("utf-8")
    mutated = evasion(original)
    assert mutated != original, "the evasion did not change the fixture"
    json.loads(mutated)

    bundle = tmp_path / BUNDLE_FIXTURE.name
    bundle.write_bytes(mutated.encode("utf-8"))
    with pytest.raises(AssertionError):
        _assert_envelope_fixture_file_is_synthetic(bundle, FIXTURE)


_STAND_IN_PAYLOAD = b"SYNTHETIC"


def _bits(payload: bytes) -> list[int]:
    return [(byte >> shift) & 1 for byte in payload for shift in range(7, -1, -1)]


def _smuggle_bits_in_crlf(raw: bytes) -> bytes:
    # The sweep's reproduction shape: one bit per line, LF for 0 and CRLF for 1.
    lines = raw.split(b"\n")
    for index, bit in enumerate(_bits(_STAND_IN_PAYLOAD)):
        if bit:
            lines[index] += b"\r"
    return b"\n".join(lines)


def _smuggle_with_lone_cr(raw: bytes) -> bytes:
    # A lone CR also reads as a newline. Git's own text normalisation converts
    # CRLF but leaves a lone CR alone, so a .gitattributes rule would not stop it.
    lines = raw.split(b"\n")
    for index in range(0, 30, 3):
        lines[index] += b"\r"
    return b"\n".join(lines)


def _save_with_windows_line_endings(raw: bytes) -> bytes:
    # The well-meaning case, not an attack: an editor that saves CRLF throughout.
    return raw.replace(b"\n", b"\r\n")


def _prefix_byte_order_mark(raw: bytes) -> bytes:
    return b"\xef\xbb\xbf" + raw


@pytest.mark.parametrize(
    ("mutation", "hidden_from_text_reads"),
    [
        (_smuggle_bits_in_crlf, True),
        (_smuggle_with_lone_cr, True),
        (_save_with_windows_line_endings, True),
        (_prefix_byte_order_mark, False),
    ],
    ids=lambda value: value.__name__.lstrip("_") if callable(value) else None,
)
def test_the_fixture_guard_compares_bytes_not_translated_text(
    mutation, hidden_from_text_reads: bool, tmp_path: Path
) -> None:
    """Byte-level changes that a text comparison cannot see are refused.

    Found by an adversarial sweep of the previous head: `Path.read_text` turns
    CRLF and a lone CR into LF, so a guard comparing text passed a fixture whose
    line endings carried an encoded payload. For each mutation marked hidden,
    this asserts the mutated file really does read back as identical text --
    which is exactly why a text-comparing guard could not catch it -- and then
    that the byte-level guard refuses it. The byte-order mark is not hidden from
    a text read; it is here because it is refused by the same byte check.
    """
    original = BUNDLE_FIXTURE.read_bytes()
    mutated = mutation(original)
    assert mutated != original, "the mutation did not change the fixture"

    bundle = tmp_path / BUNDLE_FIXTURE.name
    bundle.write_bytes(mutated)
    if hidden_from_text_reads:
        assert bundle.read_text(encoding="utf-8") == BUNDLE_FIXTURE.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_envelope_fixture_file_is_synthetic(bundle, FIXTURE)


def _plant_zero_width_character(raw: bytes) -> bytes:
    return raw.replace(b'"kitchen"', '"kitchen\u200b"'.encode("utf-8"), 1)


def _plant_bits_in_trailing_spaces(raw: bytes) -> bytes:
    lines = raw.split(b"\n")
    for index, bit in enumerate(_bits(_STAND_IN_PAYLOAD)):
        if bit:
            lines[index] += b" "
    return b"\n".join(lines)


def _plant_a_tab_in_the_indentation(raw: bytes) -> bytes:
    lines = raw.split(b"\n")
    lines[3] = b"\t" + lines[3].removeprefix(b"  ")
    return b"\n".join(lines)


@pytest.mark.parametrize(
    ("channel", "refusal"),
    [
        (_smuggle_bits_in_crlf, "carriage return"),
        (_plant_zero_width_character, "non-ASCII"),
        (_plant_bits_in_trailing_spaces, "trailing whitespace"),
        (_plant_a_tab_in_the_indentation, "a tab"),
    ],
    ids=lambda value: value.__name__.lstrip("_") if callable(value) else None,
)
def test_a_channel_planted_in_the_synthetic_fixture_is_refused_too(
    channel, refusal: str, tmp_path: Path
) -> None:
    """A channel planted upstream cannot pass by being reproduced faithfully.

    The envelope fixture is built from the synthetic one byte for byte, so
    anything written into `captured-room-3d.json` and carried through a
    regenerated envelope fixture satisfies the byte comparison on its own. This
    asserts that is really so for each channel -- the regenerated fixture IS its
    construction -- and that the check on the synthetic file refuses it, by the
    specific check named.
    """
    synthetic_raw = channel(FIXTURE.read_bytes())
    assert synthetic_raw != FIXTURE.read_bytes(), "the channel did not change the file"
    json.loads(synthetic_raw.decode("utf-8"))
    synthetic = tmp_path / FIXTURE.name
    synthetic.write_bytes(synthetic_raw)
    bundle = tmp_path / BUNDLE_FIXTURE.name
    bundle.write_bytes(_build_envelope_fixture(synthetic_raw.decode("utf-8")).encode("utf-8"))

    with pytest.raises(AssertionError, match=refusal):
        _assert_envelope_fixture_file_is_synthetic(bundle, synthetic)


def test_an_omitted_identifier_key_is_the_only_accepted_absence() -> None:
    """The envelope case and the malformed case must not converge.

    Pinned alongside the parametrised refusal above: with the key gone the
    caller's identity is accepted, and it is accepted for no other reason.
    """
    document = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    assert "identifier" not in document
    model = import_captured_room(document, source_id="fixture:omitted-identifier")
    assert model.walls


def test_a_file_path_is_never_used_as_capture_identity(tmp_path: Path) -> None:
    """Where a file sits is not what the capture IS.

    `load_captured_room` has always defaulted its provenance source to the file
    path. Once the envelope fix let `source_id` supply IDENTITY, that harmless
    fallback quietly became an identity fallback: a bundle-envelope capture
    loaded without a `source_id` would take the path as its identity, so the
    same bytes at two paths produced different stable entity ids.

    Identity and description are now separate. The path still describes where
    the document was read from; it can never seed an id.
    """
    payload = BUNDLE_FIXTURE.read_text(encoding="utf-8")
    first = tmp_path / "one" / "room.json"
    second = tmp_path / "two" / "renamed.json"
    for target in (first, second):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")

    # No stated identifier and no caller identity: refuse, do not fall back.
    with pytest.raises(RoomPlanImportError, match="source_id is required"):
        load_captured_room(first)

    # The same capture, named by the caller, is the same model wherever it sits.
    left = load_captured_room(first, source_id="capture:identical")
    right = load_captured_room(second, source_id="capture:identical")
    assert left.to_dict() == right.to_dict()
    assert [w.id for w in left.walls] == [w.id for w in right.walls]


def test_a_stated_identifier_still_records_the_file_in_provenance(
    tmp_path: Path,
) -> None:
    """Separating identity from description must not lose the description.

    A document that states its own identifier has always been loadable without
    a `source_id`, with the file path recorded as the provenance source. That
    behaviour is unchanged.
    """
    model = load_captured_room(FIXTURE)
    assert model.walls
    assert any(
        record.source_id == str(FIXTURE)
        for record in model.walls[0].provenance
    )


# --- Capture envelope digests (#90) -----------------------------------------
#
# A real Bundle v3 capture carries `coreModel` (an opaque, unbounded blob) and
# `referenceOriginTransform` (the capture's world placement) on the room
# document. The importer does not interpret either, so it no longer copies
# either: both are recorded as digests. Every value used here is synthetic and
# invented for these tests; none of it comes from a real capture.

_CORE_MARKER = "SYNTHETIC-CORE-7Q4Z9B-MARKER"
_BIG_BLOB_MARKER = "SYNTHETIC-BIGBLOB-3KQ8V-MARKER"


def _envelope_source() -> dict:
    """The synthetic room plus a fake 10 kB coreModel and a 16-number transform."""
    source = _source()
    filler = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 300
    blob = (_CORE_MARKER + filler)[:10240]
    assert len(blob) == 10240 and _CORE_MARKER in blob
    source["coreModel"] = blob
    # Every value is an exact binary fraction, so the JSON text of the model is
    # stable and the distinctive member can be searched for as a substring.
    source["referenceOriginTransform"] = [
        0.5,
        -1.25,
        0.75,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        -0.75,
        0.0,
        0.5,
        0.0,
        2.5,
        424242.75,
        -0.25,
        1.0,
    ]
    return source


def _expected_digest(value: object) -> dict:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "present": True,
        "size_bytes": len(canonical),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def test_named_envelope_keys_are_digested_and_never_copied() -> None:
    source = _envelope_source()
    model = import_captured_room(source, source_id="fixture:envelope")

    model_json = model.to_json()
    assert _CORE_MARKER not in model_json
    assert "424242.75" not in model_json
    assert "extra_fields" not in model.attributes["roomplan"]

    envelope = model.attributes["roomplan"]["envelope"]
    assert envelope["coreModel"] == _expected_digest(source["coreModel"])
    assert envelope["coreModel"]["size_bytes"] >= 10240
    assert envelope["referenceOriginTransform"] == _expected_digest(
        source["referenceOriginTransform"]
    )


def test_small_unknown_top_level_key_still_passes_through() -> None:
    source = _source()
    source["scanNote"] = {"quality": "synthetic", "count": 3}

    model = import_captured_room(source, source_id="fixture:envelope")

    assert model.attributes["roomplan"]["extra_fields"]["scanNote"] == {
        "quality": "synthetic",
        "count": 3,
    }
    assert "envelope" not in model.attributes["roomplan"]


def test_oversized_unknown_top_level_key_is_digested() -> None:
    source = _source()
    source["bigBlob"] = _BIG_BLOB_MARKER + "x" * 5000

    model = import_captured_room(source, source_id="fixture:envelope")

    roomplan = model.attributes["roomplan"]
    assert roomplan["envelope"]["bigBlob"] == _expected_digest(source["bigBlob"])
    assert roomplan["envelope_digested_keys"] == ["bigBlob"]
    assert "extra_fields" not in roomplan
    assert _BIG_BLOB_MARKER not in model.to_json()


def test_envelope_passthrough_size_limit_is_4096_bytes_exclusive() -> None:
    source = _source()
    # Two quotes wrap each string, so a 4094-character value is exactly 4096
    # canonical bytes and passes through; one more character tips it over.
    source["atLimit"] = "y" * 4094
    source["pastLimit"] = "z" * 4095

    model = import_captured_room(source, source_id="fixture:envelope")

    roomplan = model.attributes["roomplan"]
    assert roomplan["extra_fields"]["atLimit"] == "y" * 4094
    assert roomplan["envelope"]["pastLimit"] == _expected_digest("z" * 4095)
    assert roomplan["envelope_digested_keys"] == ["pastLimit"]


def test_envelope_digests_are_deterministic() -> None:
    source = _envelope_source()
    source["bigBlob"] = _BIG_BLOB_MARKER + "x" * 5000

    first = import_captured_room(copy.deepcopy(source), source_id="fixture:envelope")
    second = import_captured_room(copy.deepcopy(source), source_id="fixture:envelope")

    assert first.to_json() == second.to_json()


def test_ifc_export_does_not_carry_the_raw_envelope(tmp_path: Path) -> None:
    """The IFC adapter embeds model attributes, so the envelope must be gone.

    A raw `coreModel` used to ride into the canonical model through
    `extra_fields` and from there into every entity shadow the IFC adapter
    writes as `CanonicalJson` pset text. The written file must not carry it.
    """
    from oabm.ifc import to_ifc

    model = import_captured_room(_envelope_source(), source_id="fixture:envelope")

    destination = tmp_path / "envelope-model.ifc"
    to_ifc(model, str(destination))
    written = destination.read_text(encoding="utf-8", errors="replace")

    assert _CORE_MARKER not in written
    assert "424242.75" not in written
    # The digest itself stays auditable in the embedded model attributes.
    assert '"sha256"' in written
