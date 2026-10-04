"""Synthetic non-axis-aligned IFC interchange; no private capture fixtures."""
import copy
import math
from pathlib import Path

import pytest

from oabm.ifc import from_ifc, round_trip, round_trip_differences, to_ifc
from oabm.model import BuildingModel

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/model/v1/garage-route.json"


def model():
    return BuildingModel.load(FIXTURE)


def changed(original, mutate):
    data = copy.deepcopy(original.to_dict())
    mutate(data)
    return BuildingModel.from_dict(data)


def test_non_axis_rotation_is_equivalent_in_memory_and_step_without_rewriting_json(tmp_path):
    original = changed(model(), lambda d: d["electrical_devices"][0]["pose"].update(
        rotation={"x": 0.0, "y": 0.0, "z": math.sin(1.137), "w": math.cos(1.137)}))
    saved = original.to_json()
    restored = round_trip(original)
    assert restored.to_dict() != original.to_dict()  # actual floating-point repro
    assert round_trip_differences(original, restored) == ()
    path = tmp_path / "rotated.ifc"
    to_ifc(original, path)
    assert round_trip_differences(original, from_ifc(path)) == ()
    assert original.to_json() == saved


@pytest.mark.parametrize("axis", [(1, 2, 3), (2, -3, 5), (-7, 2, 1)])
def test_arbitrary_three_axis_rotations(axis):
    length = math.sqrt(sum(v*v for v in axis))
    q = dict(zip(("x", "y", "z"), (v / length * math.sin(0.731) for v in axis)))
    q["w"] = math.cos(0.731)
    original = changed(model(), lambda d: d["electrical_devices"][0]["pose"].update(rotation=q))
    assert round_trip_differences(original, round_trip(original)) == ()


def test_negative_elevation_fitting_round_trip():
    original = changed(model(), lambda d: d["route_fittings"][0]["pose"]["position"].update(z=-3.123456789012345))
    assert round_trip_differences(original, round_trip(original)) == ()


def test_quaternion_sign_is_whole_vector_equivalence_not_per_component():
    original = changed(model(), lambda d: d["electrical_devices"][0]["pose"].update(
        rotation={"x": .5, "y": .5, "z": .5, "w": .5}))
    negative = changed(original, lambda d: d["electrical_devices"][0]["pose"].update(
        rotation={"x": -.5, "y": -.5, "z": -.5, "w": -.5}))
    assert round_trip_differences(original, negative) == ()
    different = changed(original, lambda d: d["electrical_devices"][0]["pose"]["rotation"].update(x=-.5))
    assert round_trip_differences(original, different) == ("/electrical_devices/0/pose/rotation",)


@pytest.mark.parametrize("delta,expected", [(5e-10, ()), (2e-9, ("/electrical_devices/0/pose/position/x",))])
def test_position_tolerance_is_bounded(delta, expected):
    original = model()
    def move(d):
        d["electrical_devices"][0]["pose"]["position"]["x"] += delta
    assert round_trip_differences(original, changed(original, move)) == expected


def test_no_relative_tolerance_at_large_coordinates():
    original = changed(model(), lambda d: d["electrical_devices"][0]["pose"]["position"].update(x=1e8))
    moved = changed(original, lambda d: d["electrical_devices"][0]["pose"]["position"].update(x=1e8 + .001))
    assert round_trip_differences(original, moved) == ("/electrical_devices/0/pose/position/x",)


@pytest.mark.parametrize("field", ["confidence", "attributes", "name"])
def test_non_geometric_fields_remain_exact(field):
    original = model()
    def alter(d):
        device = d["electrical_devices"][0]
        if field == "confidence": device[field] -= 1e-13
        elif field == "attributes": device[field]["tiny"] = 1e-13
        else: device[field] = "Changed name"
    assert round_trip_differences(original, changed(original, alter))


def test_attributes_named_like_geometry_do_not_receive_tolerance():
    original = changed(model(), lambda d: d["attributes"].update(pose={"position":{"x":0.0}}, enabled=True))
    other = changed(original, lambda d: d["attributes"]["pose"]["position"].update(x=1e-13))
    assert round_trip_differences(original, other) == ("/attributes/pose/position/x",)
    boolean = changed(original, lambda d: d["attributes"].update(enabled=1))
    assert round_trip_differences(original, boolean) == ("/attributes/enabled",)


def test_wall_dimensions_remain_exact_and_order_is_not_normalized():
    original = model()
    thicker = changed(original, lambda d: d["walls"][0].update(thickness_m=d["walls"][0]["thickness_m"] + 1e-13))
    assert round_trip_differences(original, thicker) == ("/walls/0/thickness_m",)
    reversed_walls = changed(original, lambda d: d["walls"].reverse())
    assert round_trip_differences(original, reversed_walls)


@pytest.mark.parametrize("collection,field", [
    ("levels", ("elevation_m",)),
    ("walls", ("centerline", "points", 0, "x")),
    ("routes", ("centerline", "points", 1, "x")),
    ("route_fittings", ("pose", "position", "z")),
    ("ports", ("pose", "position", "z")),
])
def test_each_native_geometry_path_has_only_absolute_linear_slack(collection, field):
    original = model()
    def alter(delta):
        def edit(d):
            node = d[collection][0]
            for key in field[:-1]: node = node[key]
            node[field[-1]] += delta
        return changed(original, edit)
    assert round_trip_differences(original, alter(5e-10)) == ()
    assert round_trip_differences(original, alter(2e-9)) == (
        '/' + '/'.join(str(k) for k in (collection, 0, *field)),)


def test_actual_ifc_native_move_is_returned_and_reported_not_snapped_to_shadow():
    import ifcopenshell.api.geometry
    import ifcopenshell.util.placement
    from oabm.ifc import canonical_id_to_ifc_guid
    original = model()
    ifc = to_ifc(original)
    device = ifc.by_guid(canonical_id_to_ifc_guid(original.openings[0].id))
    matrix = ifcopenshell.util.placement.get_local_placement(device.ObjectPlacement)
    matrix[0, 3] += 1e-6
    ifcopenshell.api.geometry.edit_object_placement(ifc, product=device, matrix=matrix, is_si=True)
    edited = from_ifc(ifc)
    assert edited.openings[0].pose.position.x != original.openings[0].pose.position.x
    assert '/openings/0/pose/position/x' in round_trip_differences(original, edited)
