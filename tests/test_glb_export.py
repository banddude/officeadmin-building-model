from __future__ import annotations

import json
import math
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.exports import to_glb
from oabm.exports.gltf import to_glb as to_glb_impl
from oabm.model import (
    BuildingModel,
    ElectricalDevice,
    Point3,
    Pose,
)
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942

_EXPECTED_KIND_PREFIXES = {
    "wall:": 4,
    "slab:": 1,
    "space:": 1,
    "device:": 1,
    "equip:": 1,
    "route:": 1,
}


def _golden_garage() -> BuildingModel:
    case = next(case for case in load_golden_cases(GOLDEN_ROOT) if case.name == "synthetic-garage")
    return load_golden_model(case)


def _export_garage(tmp_path: Path) -> Path:
    target = tmp_path / "garage.glb"
    to_glb(_golden_garage(), target)
    return target


def _parse_glb(path: Path) -> dict:
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<III", data, 0)
    assert magic == _GLB_MAGIC
    assert version == 2
    assert total == len(data)
    json_length, json_type = struct.unpack_from("<II", data, 12)
    assert json_type == _CHUNK_JSON
    gltf = json.loads(data[20:20 + json_length])
    parsed: dict = {"gltf": gltf, "bin": b""}
    offset = 20 + json_length
    if offset < total:
        bin_length, bin_type = struct.unpack_from("<II", data, offset)
        assert bin_type == _CHUNK_BIN
        assert offset + 8 + bin_length == total
        parsed["bin"] = data[offset + 8:offset + 8 + bin_length]
    return parsed


def _node_positions(parsed: dict, node_index: int) -> list[tuple[float, float, float]]:
    gltf = parsed["gltf"]
    primitive = gltf["meshes"][gltf["nodes"][node_index]["mesh"]]["primitives"][0]
    accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
    view = gltf["bufferViews"][accessor["bufferView"]]
    values = struct.unpack_from(f"<{accessor['count'] * 3}f", parsed["bin"], view["byteOffset"])
    return list(zip(values[0::3], values[1::3], values[2::3]))


def _by_name(parsed: dict) -> dict[str, int]:
    return {node["name"]: index for index, node in enumerate(parsed["gltf"]["nodes"])}


def test_glb_header_and_chunks_are_valid(tmp_path: Path) -> None:
    parsed = _parse_glb(_export_garage(tmp_path))
    gltf = parsed["gltf"]
    assert gltf["asset"]["version"] == "2.0"
    assert gltf["scene"] == 0
    assert len(gltf["scenes"]) == 1
    # BIN chunk content length is exactly the declared buffer length.
    assert gltf["buffers"][0]["byteLength"] == len(parsed["bin"])


def test_node_counts_and_names_per_kind(tmp_path: Path) -> None:
    model = _golden_garage()
    target = tmp_path / "garage.glb"
    to_glb(model, target)
    parsed = _parse_glb(target)
    names = [node["name"] for node in parsed["gltf"]["nodes"]]

    expected_ids = {
        entity.id
        for collection in (
            model.walls, model.slabs, model.spaces,
            model.electrical_devices, model.electrical_equipment, model.routes,
        )
        for entity in collection
    }
    assert set(names) == expected_ids
    assert len(names) == len(set(names))

    for prefix, count in _EXPECTED_KIND_PREFIXES.items():
        matching = [name for name in names if name.startswith(prefix)]
        assert len(matching) == count, prefix

    # Every exported node carries geometry.
    assert all(node["mesh"] is not None for node in parsed["gltf"]["nodes"])


def test_buffer_lengths_match_accessors(tmp_path: Path) -> None:
    parsed = _parse_glb(_export_garage(tmp_path))
    gltf = parsed["gltf"]
    buffer_length = gltf["buffers"][0]["byteLength"]

    for view in gltf["bufferViews"]:
        assert view["byteOffset"] % 4 == 0
        assert view["byteOffset"] + view["byteLength"] <= buffer_length

    for accessor in gltf["accessors"]:
        assert accessor["type"] == "VEC3"
        assert accessor["componentType"] == 5126
        view = gltf["bufferViews"][accessor["bufferView"]]
        assert accessor["count"] * 12 == view["byteLength"]
        for bound in (*accessor["min"], *accessor["max"]):
            assert math.isfinite(bound)

    referenced = [
        primitive["attributes"]["POSITION"]
        for mesh in gltf["meshes"]
        for primitive in mesh["primitives"]
    ]
    assert sorted(referenced) == list(range(len(gltf["accessors"])))
    assert all(
        0 <= primitive["material"] < len(gltf["materials"])
        for mesh in gltf["meshes"]
        for primitive in mesh["primitives"]
    )


def test_device_node_sits_at_model_position_after_axis_conversion(tmp_path: Path) -> None:
    model = _golden_garage()
    parsed = _parse_glb(_export_garage(tmp_path))
    index = _by_name(parsed)["device:garage-evse"]

    position = model.electrical_devices[0].pose.position
    expected = (position.x, position.z, -position.y)
    assert parsed["gltf"]["nodes"][index]["translation"] == pytest.approx(expected)


def test_route_tube_follows_segment_endpoints(tmp_path: Path) -> None:
    model = _golden_garage()
    route = model.routes[0]
    parsed = _parse_glb(_export_garage(tmp_path))
    vertices = _node_positions(parsed, _by_name(parsed)[route.id])

    radius = route.nominal_diameter_m / 2.0
    points = list(route.centerline.points)
    midpoints = [
        Point3(x=(a.x + b.x) / 2, y=(a.y + b.y) / 2, z=(a.z + b.z) / 2)
        for a, b in zip(route.centerline.points, route.centerline.points[1:])
    ]

    # Every centerline vertex is ringed by tube vertices exactly one radius away.
    for point in points:
        gltf_point = (point.x, point.z, -point.y)
        nearest = min(math.dist(vertex, gltf_point) for vertex in vertices)
        assert nearest == pytest.approx(radius, abs=1e-4), point

    # The whole polyline, midpoints included, stays inside the tube's bounds.
    mins = [min(vertex[axis] for vertex in vertices) for axis in range(3)]
    maxs = [max(vertex[axis] for vertex in vertices) for axis in range(3)]
    for point in (*points, *midpoints):
        gltf_point = (point.x, point.z, -point.y)
        for axis in range(3):
            assert mins[axis] - 1e-6 <= gltf_point[axis] <= maxs[axis] + 1e-6, point


def test_extras_carry_canonical_data(tmp_path: Path) -> None:
    model = _golden_garage()
    parsed = _parse_glb(_export_garage(tmp_path))
    nodes = parsed["gltf"]["nodes"]
    by_name = {node["name"]: node for node in nodes}

    route = model.routes[0]
    route_extras = by_name[route.id]["extras"]
    expected_length = math.fsum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(route.centerline.points, route.centerline.points[1:])
    )
    assert route_extras["canonical_type"] == "emt"
    assert route_extras["raceway"] == "emt"
    assert route_extras["nominal_diameter_m"] == route.nominal_diameter_m
    assert route_extras["length_m"] == pytest.approx(expected_length, abs=1e-6)
    assert route_extras["level"] == "level:garage-ground"
    assert len(route_extras["conductors"]) == len(model.conductors)
    assert all("size" in conductor or conductor["role"] for conductor in route_extras["conductors"])

    device_extras = by_name["device:garage-evse"]["extras"]
    assert device_extras["canonical_type"] == "evse"
    assert device_extras["level"] == "level:garage-ground"
    # The golden fixture carries no derivation, so none is claimed.
    assert "derivation" not in device_extras
    assert "scope_status" not in device_extras

    wall = model.walls[0]
    wall_extras = by_name[wall.id]["extras"]
    assert wall_extras["canonical_type"] == "wall"
    assert wall_extras["level"] == wall.level_id


def test_export_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    summary_first = to_glb(_golden_garage(), first)
    summary_second = to_glb(_golden_garage(), second)
    assert first.read_bytes() == second.read_bytes()
    strip_path = lambda summary: {key: value for key, value in summary.items() if key != "path"}
    assert strip_path(summary_first) == strip_path(summary_second)


def test_inferred_geometry_gets_semitransparent_material(tmp_path: Path) -> None:
    model = _golden_garage()
    device = model.electrical_devices[0]
    derived_device = replace(
        device,
        provenance=(replace(device.provenance[0], derivation="inferred"),),
    )
    derived_model = replace(model, electrical_devices=(derived_device,))

    to_glb(model, tmp_path / "plain.glb")
    plain = _parse_glb(tmp_path / "plain.glb")
    to_glb(derived_model, tmp_path / "derived.glb")
    derived = _parse_glb(tmp_path / "derived.glb")

    plain_device = plain["gltf"]["nodes"][_by_name(plain)["device:garage-evse"]]
    plain_material = plain["gltf"]["materials"][
        plain["gltf"]["meshes"][plain_device["mesh"]]["primitives"][0]["material"]
    ]
    assert "alphaMode" not in plain_material
    assert plain_material["pbrMetallicRoughness"]["baseColorFactor"][3] == 1.0

    derived_device_node = derived["gltf"]["nodes"][_by_name(derived)["device:garage-evse"]]
    assert derived_device_node["extras"]["derivation"] == "inferred"
    derived_material = derived["gltf"]["materials"][
        derived["gltf"]["meshes"][derived_device_node["mesh"]]["primitives"][0]["material"]
    ]
    assert derived_material["alphaMode"] == "BLEND"
    assert derived_material["pbrMetallicRoughness"]["baseColorFactor"][3] < 1.0


def _synthetic_devices() -> BuildingModel:
    def device(device_id: str, device_type: str) -> ElectricalDevice:
        return ElectricalDevice(
            id=device_id,
            device_type=device_type,
            pose=Pose(position=Point3(x=1.0, y=2.0, z=3.0)),
        )

    return BuildingModel(
        model_id="model:glb-export-synth",
        electrical_devices=(
            device("device:synth-receptacle", "receptacle"),
            device("device:synth-luminaire", "luminaire"),
            device("device:synth-switch", "switch"),
            device("device:synth-occupancy", "occupancy_sensor"),
            device("device:synth-panelboard", "panelboard"),
            device("device:synth-unknown", "mystery_gizmo"),
        ),
    )


def test_device_primitives_and_colours_by_type(tmp_path: Path) -> None:
    target = tmp_path / "devices.glb"
    to_glb(_synthetic_devices(), target)
    parsed = _parse_glb(target)
    gltf = parsed["gltf"]

    counts = {
        "device:synth-receptacle": 36,   # outlet family: box
        "device:synth-luminaire": 120,   # flat disc: 10-sided cylinder
        "device:synth-switch": 36,       # box
        "device:synth-occupancy": 120,   # small cylinder
        "device:synth-panelboard": 36,   # box
        "device:synth-unknown": 36,      # other: box
    }
    colours = {
        "device:synth-receptacle": ("outlet", lambda r, g, b: r > g and r > b),
        "device:synth-luminaire": ("luminaire", lambda r, g, b: r > b and g > b),
        "device:synth-switch": ("switch", lambda r, g, b: b > r and b > g),
        "device:synth-occupancy": ("sensor", lambda r, g, b: g > r and g > b),
        "device:synth-panelboard": ("panel", lambda r, g, b: r == g == b),
        "device:synth-unknown": ("other", lambda r, g, b: r == g == b),
    }
    for name, node in ((node["name"], node) for node in gltf["nodes"]):
        primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
        accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
        assert accessor["count"] == counts[name], name
        material = gltf["materials"][primitive["material"]]
        assert material["name"] == colours[name][0], name
        red, green, blue = material["pbrMetallicRoughness"]["baseColorFactor"][:3]
        assert colours[name][1](red, green, blue), name
        # A posed device converts canonical +Z-up to glTF +Y-up.
        assert node["translation"] == pytest.approx((1.0, 3.0, -2.0)), name


def test_summary_reports_per_kind_counts(tmp_path: Path) -> None:
    summary = to_glb(_golden_garage(), tmp_path / "garage.glb")
    assert summary["walls"] == 4
    assert summary["slabs"] == 1
    assert summary["spaces"] == 1
    assert summary["devices"] == 1
    assert summary["equipment"] == 1
    assert summary["routes"] == 1
    assert summary["nodes"] == 9
    assert summary["bytes"] == (tmp_path / "garage.glb").stat().st_size


def test_package_reexport_matches_impl() -> None:
    assert to_glb is to_glb_impl


def test_optional_gltf_validators_accept_the_export(tmp_path: Path) -> None:
    target = tmp_path / "garage.glb"
    to_glb(_golden_garage(), target)
    try:
        import pygltflib
    except ImportError:
        pytest.skip("pygltflib not installed")
    gltf = pygltflib.GLTF2().load(str(target))
    assert gltf.asset.version == "2.0"
    assert len(gltf.nodes) == 9
