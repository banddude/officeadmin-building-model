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
    Ceiling,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    Slab,
    Vector3,
    Wall,
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
    # Conductors ride their routes as extra wire nodes; every entity node name
    # is still present exactly once.
    wire_names = {name for name in names if name.startswith("conductor:") and "#route:" in name}
    expected_wire_names = {
        f"{conductor.id}#route:{route_id.removeprefix('route:')}#{index}"
        for conductor in model.conductors
        for route_id in conductor.route_ids
        for index in range(conductor.count)
    }
    assert wire_names == expected_wire_names
    assert set(names) - wire_names == expected_ids
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
    assert summary["conductor_wires"] == 3
    assert summary["nodes"] == 12
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
    assert len(gltf.nodes) == 12


def _wire_nodes(parsed: dict) -> dict[str, dict]:
    return {
        node["name"]: node
        for node in parsed["gltf"]["nodes"]
        if node["name"].startswith("conductor:") and "#route:" in node["name"]
    }


def _distance_to_polyline(
    point: tuple[float, float, float], points: tuple[Point3, ...]
) -> float:
    best = float("inf")
    for start, end in zip(points, points[1:]):
        segment = (end.x - start.x, end.y - start.y, end.z - start.z)
        length_squared = sum(component * component for component in segment)
        if length_squared <= 0.0:
            continue
        t = (
            (point[0] - start.x) * segment[0]
            + (point[1] - start.y) * segment[1]
            + (point[2] - start.z) * segment[2]
        ) / length_squared
        t = max(0.0, min(1.0, t))
        nearest = (
            start.x + t * segment[0],
            start.y + t * segment[1],
            start.z + t * segment[2],
        )
        best = min(best, math.dist(point, nearest))
    return best


def test_wire_count_names_and_extras_match_conductors(tmp_path: Path) -> None:
    model = _golden_garage()
    summary = to_glb(model, tmp_path / "garage.glb")
    assert summary["conductor_wires"] == 3

    parsed = _parse_glb(tmp_path / "garage.glb")
    wires = _wire_nodes(parsed)
    assert len(wires) == 3
    by_id = {conductor.id: conductor for conductor in model.conductors}
    for name, node in wires.items():
        extras = node["extras"]
        conductor = by_id[extras["canonical_id"]]
        assert extras["circuit_id"] == conductor.circuit_id
        assert extras["role"] == conductor.role
        assert extras["size"] == conductor.size
        assert extras["route_id"] in conductor.route_ids
        assert node["name"] == (
            f"{conductor.id}#route:{extras['route_id'].removeprefix('route:')}#0"
        )
        assert name.startswith("conductor:")

    # The route node keeps its conductor list alongside the new wire nodes.
    route_node = parsed["gltf"]["nodes"][_by_name(parsed)["route:garage-panel-evse"]]
    assert len(route_node["extras"]["conductors"]) == 3


def test_every_wire_vertex_stays_inside_the_conduit(tmp_path: Path) -> None:
    model = _golden_garage()
    route = model.routes[0]
    bound = route.nominal_diameter_m / 2.0
    parsed = _parse_glb(_export_garage(tmp_path))

    for name, node in _wire_nodes(parsed).items():
        vertices = _node_positions(parsed, _by_name(parsed)[name])
        # glTF +Y-up back to canonical +Z-up.
        canonical = [(x, -z, y) for x, y, z in vertices]
        for vertex in canonical:
            assert _distance_to_polyline(vertex, route.centerline.points) <= bound + 1e-6, name


def _wire_model(conductor: Conductor, diameter: float | None) -> BuildingModel:
    owner = ElectricalDevice(
        id="device:wires-panel",
        device_type="panel",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
    )
    def port(port_id: str, position: Point3) -> Port:
        return Port(
            id=port_id,
            owner_id="device:wires-panel",
            domain="electrical",
            role="source",
            pose=Pose(position=position),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
        )
    route = Route(
        id="route:wires-1",
        route_type="emt",
        start_port_id="port:wires-a",
        end_port_id="port:wires-b",
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=1.0),
            Point3(x=2.0, y=0.0, z=1.0),
            Point3(x=2.0, y=3.0, z=1.0),
        )),
        nominal_diameter_m=diameter,
    )
    return BuildingModel(
        model_id="model:glb-wires-synth",
        electrical_devices=(owner,),
        ports=(
            port("port:wires-a", Point3(x=0.0, y=0.0, z=1.0)),
            port("port:wires-b", Point3(x=2.0, y=3.0, z=1.0)),
        ),
        circuits=(Circuit(
            id="circuit:wires-1",
            source_port_id="port:wires-a",
            load_port_ids=("port:wires-b",),
            route_ids=("route:wires-1",),
        ),),
        routes=(route,),
        conductors=(conductor,),
    )


def test_conductor_count_two_gives_two_wire_nodes(tmp_path: Path) -> None:
    model = _wire_model(
        Conductor(
            id="conductor:wires-1",
            circuit_id="circuit:wires-1",
            role="phase",
            count=2,
            route_ids=("route:wires-1",),
        ),
        diameter=0.021,
    )
    to_glb(model, tmp_path / "wires.glb")
    parsed = _parse_glb(tmp_path / "wires.glb")
    assert sorted(_wire_nodes(parsed)) == [
        "conductor:wires-1#route:wires-1#0",
        "conductor:wires-1#route:wires-1#1",
    ]


def test_route_without_diameter_still_draws_its_wires(tmp_path: Path) -> None:
    model = _wire_model(
        Conductor(
            id="conductor:wires-1",
            circuit_id="circuit:wires-1",
            role="neutral",
            count=1,
            route_ids=("route:wires-1",),
        ),
        diameter=None,
    )
    summary = to_glb(model, tmp_path / "wires.glb")
    assert summary["conductor_wires"] == 1
    parsed = _parse_glb(tmp_path / "wires.glb")
    assert sorted(_wire_nodes(parsed)) == ["conductor:wires-1#route:wires-1#0"]


def test_wire_colours_follow_role_convention(tmp_path: Path) -> None:
    to_glb(_golden_garage(), tmp_path / "garage.glb")
    parsed = _parse_glb(tmp_path / "garage.glb")

    def _material(name: str) -> dict:
        node = parsed["gltf"]["nodes"][_by_name(parsed)[name]]
        primitive = parsed["gltf"]["meshes"][node["mesh"]]["primitives"][0]
        return parsed["gltf"]["materials"][primitive["material"]]

    def _material_name(name: str) -> str:
        return _material(name)["name"]

    def _colour(name: str) -> tuple[float, float, float]:
        return tuple(_material(name)["pbrMetallicRoughness"]["baseColorFactor"][:3])

    ground = "conductor:garage-egc#route:garage-panel-evse#0"
    phase_one = "conductor:garage-l1#route:garage-panel-evse#0"
    phase_two = "conductor:garage-l2#route:garage-panel-evse#0"

    # The ground wire is green; phases cycle black then red by circuit order.
    assert _material_name(ground) == "wire-ground"
    red, green, blue = _colour(ground)
    assert green > red and green > blue
    assert _material_name(phase_one) == "wire-phase-1"
    assert _material_name(phase_two) == "wire-phase-2"
    red, green, blue = _colour(phase_two)
    assert red > green and red > blue


def test_inferred_conductor_wire_draws_translucent(tmp_path: Path) -> None:
    model = _golden_garage()
    conductor = next(c for c in model.conductors if c.id == "conductor:garage-egc")
    inferred = replace(
        model,
        conductors=tuple(
            (
                replace(conductor, provenance=(replace(conductor.provenance[0], derivation="inferred"),))
                if item.id == conductor.id
                else item
            )
            for item in model.conductors
        ),
    )
    to_glb(inferred, tmp_path / "inferred.glb")
    parsed = _parse_glb(tmp_path / "inferred.glb")
    node = parsed["gltf"]["nodes"][_by_name(parsed)["conductor:garage-egc#route:garage-panel-evse#0"]]
    material = parsed["gltf"]["materials"][parsed["gltf"]["meshes"][node["mesh"]]["primitives"][0]["material"]]
    assert material["alphaMode"] == "BLEND"
    assert material["pbrMetallicRoughness"]["baseColorFactor"][3] < 1.0


def _wall_model(
    *,
    wall_height: float | None,
    level_height: float | None,
    elevation: float,
) -> BuildingModel:
    """One straight 4 m wall, 0.2 m thick, on one level.

    ``wall_height`` of ``None`` simulates a producer that omitted the wall's
    canonical height: the contract requires a positive one, so the check is
    undone after construction to reach the exporter's fallback path.
    """

    level = Level(id="level:main", elevation_m=elevation, height_m=level_height)
    wall = Wall(
        id="wall:south-run",
        level_id=level.id,
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=elevation),
            Point3(x=4.0, y=0.0, z=elevation),
        )),
        thickness_m=0.2,
        height_m=2.7,
    )
    if wall_height is None:
        object.__setattr__(wall, "height_m", None)
    return BuildingModel(model_id="model:glb-wall-synth", levels=(level,), walls=(wall,))


def _wall_vertex_ranges(parsed: dict) -> tuple[dict[str, float], dict[str, dict]]:
    vertices = _node_positions(parsed, _by_name(parsed)["wall:south-run"])
    node = parsed["gltf"]["nodes"][_by_name(parsed)["wall:south-run"]]
    spans = {
        axis: max(vertex[axis] for vertex in vertices) - min(vertex[axis] for vertex in vertices)
        for axis in range(3)
    }
    bounds = {
        axis: (min(vertex[axis] for vertex in vertices), max(vertex[axis] for vertex in vertices))
        for axis in range(3)
    }
    return spans, {"extras": node["extras"], "bounds": bounds}


def test_wall_extrudes_to_its_own_height(tmp_path: Path) -> None:
    model = _wall_model(wall_height=2.7, level_height=None, elevation=0.0)
    target = tmp_path / "walls.glb"
    to_glb(model, target)
    parsed = _parse_glb(target)

    spans, info = _wall_vertex_ranges(parsed)
    # glTF +Y is canonical +Z: the wall rises from the floor to its height.
    (y_bottom, y_top) = info["bounds"][1]
    assert y_bottom == pytest.approx(0.0, abs=1e-6)
    assert y_top == pytest.approx(2.7, abs=1e-6)
    # Plan footprint: 4 m along canonical x, 0.2 m across it.
    assert spans[0] == pytest.approx(4.0, abs=1e-6)
    assert spans[2] == pytest.approx(0.2, abs=1e-6)
    assert info["extras"]["height_source"] == "wall"


def test_wall_on_elevated_level_keeps_its_base(tmp_path: Path) -> None:
    model = _wall_model(wall_height=2.7, level_height=3.05, elevation=-3.0)
    to_glb(model, tmp_path / "walls.glb")
    parsed = _parse_glb(tmp_path / "walls.glb")

    _spans, info = _wall_vertex_ranges(parsed)
    y_bottom, y_top = info["bounds"][1]
    assert y_bottom == pytest.approx(-3.0, abs=1e-6)
    assert y_top == pytest.approx(-0.3, abs=1e-6)
    assert info["extras"]["height_source"] == "wall"


def test_wall_without_height_uses_the_level_height(tmp_path: Path) -> None:
    model = _wall_model(wall_height=None, level_height=3.05, elevation=0.0)
    to_glb(model, tmp_path / "walls.glb")
    parsed = _parse_glb(tmp_path / "walls.glb")

    _spans, info = _wall_vertex_ranges(parsed)
    y_bottom, y_top = info["bounds"][1]
    assert y_bottom == pytest.approx(0.0, abs=1e-6)
    assert y_top == pytest.approx(3.05, abs=1e-6)
    assert info["extras"]["height_source"] == "level"


def test_wall_with_no_height_anywhere_stays_a_flat_strip(tmp_path: Path) -> None:
    model = _wall_model(wall_height=None, level_height=None, elevation=0.0)
    to_glb(model, tmp_path / "walls.glb")
    parsed = _parse_glb(tmp_path / "walls.glb")

    _spans, info = _wall_vertex_ranges(parsed)
    y_bottom, y_top = info["bounds"][1]
    assert y_bottom == pytest.approx(0.0, abs=1e-6)
    assert y_top == pytest.approx(0.0, abs=1e-6)
    assert info["extras"]["height_source"] == "none"


def test_wall_export_is_deterministic(tmp_path: Path) -> None:
    model = _wall_model(wall_height=2.7, level_height=3.05, elevation=-3.0)
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    to_glb(model, first)
    to_glb(model, second)
    assert first.read_bytes() == second.read_bytes()


# --- Display options: reference planes, dimmed entities, low-voltage colour,
# --- names in extras. Every option is keyword-only and defaults off; with all
# --- options at their defaults the bytes are identical to a plain export.


def _plane_model(
    *,
    levels: tuple[tuple[float, float | None], ...] = ((0.0, 2.7),),
    slab_on_levels: tuple[int, ...] = (),
    ceiling_on_levels: tuple[int, ...] = (),
) -> BuildingModel:
    """Synthetic plan: per level one wall and one posed device, plus an
    optional canonical slab or ceiling on the levels named by index.

    Levels sit 10 m apart in plan so their bounding boxes never touch. Level
    confidence is a distinctive 0.75 so the extras assertion proves the value
    really travelled.
    """

    level_objs: list[Level] = []
    walls: list[Wall] = []
    devices: list[ElectricalDevice] = []
    slabs: list[Slab] = []
    ceilings: list[Ceiling] = []
    for index, (elevation, height) in enumerate(levels):
        level_id = f"level:ref-{index}"
        level_objs.append(Level(
            id=level_id, elevation_m=elevation, height_m=height, confidence=0.75,
        ))
        walls.append(Wall(
            id=f"wall:ref-{index}",
            level_id=level_id,
            centerline=Polyline3D(points=(
                Point3(x=10.0 * index, y=0.0, z=elevation),
                Point3(x=10.0 * index + 4.0, y=0.0, z=elevation),
            )),
            thickness_m=0.15,
            height_m=2.4,
        ))
        devices.append(ElectricalDevice(
            id=f"device:ref-{index}",
            device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=10.0 * index + 1.5, y=2.5, z=elevation + 0.3)),
            level_id=level_id,
        ))
        if index in slab_on_levels:
            slabs.append(Slab(
                id=f"slab:ref-{index}",
                level_id=level_id,
                footprint=Polygon3D(points=(
                    Point3(x=10.0 * index - 1.0, y=-1.0, z=elevation - 0.1),
                    Point3(x=10.0 * index + 5.0, y=-1.0, z=elevation - 0.1),
                    Point3(x=10.0 * index + 5.0, y=3.5, z=elevation - 0.1),
                    Point3(x=10.0 * index - 1.0, y=3.5, z=elevation - 0.1),
                )),
                thickness_m=0.1,
            ))
        if index in ceiling_on_levels:
            ceilings.append(Ceiling(
                id=f"ceiling:ref-{index}",
                level_id=level_id,
                footprint=Polygon3D(points=(
                    Point3(x=10.0 * index - 1.0, y=-1.0, z=elevation + 2.4),
                    Point3(x=10.0 * index + 5.0, y=-1.0, z=elevation + 2.4),
                    Point3(x=10.0 * index + 5.0, y=3.5, z=elevation + 2.4),
                    Point3(x=10.0 * index - 1.0, y=3.5, z=elevation + 2.4),
                )),
            ))
    return BuildingModel(
        model_id="model:glb-planes-synth",
        levels=tuple(level_objs),
        walls=tuple(walls),
        slabs=tuple(slabs),
        ceilings=tuple(ceilings),
        electrical_devices=tuple(devices),
    )


def _node_material(parsed: dict, node_name: str) -> dict:
    node = parsed["gltf"]["nodes"][_by_name(parsed)[node_name]]
    primitive = parsed["gltf"]["meshes"][node["mesh"]]["primitives"][0]
    return parsed["gltf"]["materials"][primitive["material"]]


def _default_options() -> dict:
    return {
        "reference_planes": False,
        "reference_floor_alpha": 0.25,
        "reference_ceiling_alpha": 0.08,
        "reference_margin_m": 0.5,
        "dimmed_ids": (),
        "dimmed_alpha": 0.3,
        "dimmed_color": (0.62, 0.62, 0.62),
    }


def test_all_options_at_defaults_give_plain_bytes(tmp_path: Path) -> None:
    model = _plane_model(levels=((0.0, 2.7), (3.0, 2.6)))
    to_glb(model, tmp_path / "plain.glb")
    to_glb(model, tmp_path / "defaults.glb", **_default_options())
    assert (tmp_path / "plain.glb").read_bytes() == (tmp_path / "defaults.glb").read_bytes()


def test_reference_planes_one_level_geometry_and_materials(tmp_path: Path) -> None:
    model = _plane_model()
    options = _default_options() | {"reference_planes": True}
    summary = to_glb(model, tmp_path / "planes.glb", **options)
    parsed = _parse_glb(tmp_path / "planes.glb")
    assert summary["reference_planes"] == 2

    # The wall spans x 0..4, the device sits at (1.5, 2.5): the plan bbox is
    # (0, 0)..(4, 2.5), so with the 0.5 m margin the quads span
    # (-0.5, -0.5)..(4.5, 3.0) in plan. glTF: y is height, z is -plan-y.
    floor = _node_positions(parsed, _by_name(parsed)["reference:floor#level:ref-0"])
    ceiling = _node_positions(parsed, _by_name(parsed)["reference:ceiling#level:ref-0"])
    assert len(floor) == 6 and len(ceiling) == 6  # two triangles each
    # Positions are float32-packed, so assertions read at float32 precision.
    assert all(point[1] == pytest.approx(0.0, abs=1e-6) for point in floor)
    assert all(point[1] == pytest.approx(2.7, abs=1e-6) for point in ceiling)
    for quad in (floor, ceiling):
        assert min(point[0] for point in quad) == pytest.approx(-0.5, abs=1e-6)
        assert max(point[0] for point in quad) == pytest.approx(4.5, abs=1e-6)
        assert min(point[2] for point in quad) == pytest.approx(-3.0, abs=1e-6)
        assert max(point[2] for point in quad) == pytest.approx(0.5, abs=1e-6)

    floor_material = _node_material(parsed, "reference:floor#level:ref-0")
    ceiling_material = _node_material(parsed, "reference:ceiling#level:ref-0")
    assert floor_material["name"] == "reference-floor"
    assert ceiling_material["name"] == "reference-ceiling"
    assert floor_material["alphaMode"] == "BLEND"
    assert ceiling_material["alphaMode"] == "BLEND"
    assert floor_material["pbrMetallicRoughness"]["baseColorFactor"][3] == pytest.approx(0.25)
    assert ceiling_material["pbrMetallicRoughness"]["baseColorFactor"][3] == pytest.approx(0.08)

    floor_extras = parsed["gltf"]["nodes"][_by_name(parsed)["reference:floor#level:ref-0"]]["extras"]
    assert floor_extras["reference_plane"] == "floor"
    assert floor_extras["canonical"] is False
    assert floor_extras["source"] == "level elevation_m"
    assert floor_extras["extent"] == "plan bbox of the level's contents + margin"
    assert floor_extras["margin_m"] == pytest.approx(0.5)
    assert floor_extras["alpha"] == pytest.approx(0.25)
    assert floor_extras["level_id"] == "level:ref-0"
    assert floor_extras["level_confidence"] == pytest.approx(0.75)
    ceiling_extras = parsed["gltf"]["nodes"][_by_name(parsed)["reference:ceiling#level:ref-0"]]["extras"]
    assert ceiling_extras["reference_plane"] == "ceiling"
    assert ceiling_extras["canonical"] is False
    assert ceiling_extras["source"] == "level elevation_m + height_m"
    assert ceiling_extras["alpha"] == pytest.approx(0.08)


def test_reference_planes_two_levels_each_at_its_own_elevation(tmp_path: Path) -> None:
    model = _plane_model(levels=((0.0, 2.7), (3.0, 2.6)))
    summary = to_glb(model, tmp_path / "planes.glb", **(_default_options() | {"reference_planes": True}))
    parsed = _parse_glb(tmp_path / "planes.glb")
    assert summary["reference_planes"] == 4

    heights = {}
    for kind in ("floor", "ceiling"):
        for level_index in (0, 1):
            positions = _node_positions(parsed, _by_name(parsed)[f"reference:{kind}#level:ref-{level_index}"])
            # float32-packed positions: compare within packing precision.
            heights[f"{kind}-{level_index}"] = (
                min(point[1] for point in positions),
                max(point[1] for point in positions),
            )
    assert heights["floor-0"] == pytest.approx((0.0, 0.0), abs=1e-6)
    assert heights["ceiling-0"] == pytest.approx((2.7, 2.7), abs=1e-6)
    assert heights["floor-1"] == pytest.approx((3.0, 3.0), abs=1e-6)
    assert heights["ceiling-1"] == pytest.approx((5.6, 5.6), abs=1e-6)


def test_reference_planes_skip_floor_where_a_canonical_slab_exists(tmp_path: Path) -> None:
    model = _plane_model(slab_on_levels=(0,))
    summary = to_glb(model, tmp_path / "planes.glb", **(_default_options() | {"reference_planes": True}))
    parsed = _parse_glb(tmp_path / "planes.glb")
    names = _by_name(parsed)
    assert summary["reference_planes"] == 1
    assert not any(name.startswith("reference:floor#") for name in names)
    assert "reference:ceiling#level:ref-0" in names


def test_reference_planes_skip_ceiling_where_a_canonical_ceiling_exists(tmp_path: Path) -> None:
    model = _plane_model(ceiling_on_levels=(0,))
    summary = to_glb(model, tmp_path / "planes.glb", **(_default_options() | {"reference_planes": True}))
    parsed = _parse_glb(tmp_path / "planes.glb")
    names = _by_name(parsed)
    assert summary["reference_planes"] == 1
    assert "reference:floor#level:ref-0" in names
    assert not any(name.startswith("reference:ceiling#") for name in names)


def test_reference_planes_without_level_height_explain_in_extras(tmp_path: Path) -> None:
    model = _plane_model(levels=((0.0, None),))
    summary = to_glb(model, tmp_path / "planes.glb", **(_default_options() | {"reference_planes": True}))
    parsed = _parse_glb(tmp_path / "planes.glb")
    names = _by_name(parsed)
    assert summary["reference_planes"] == 1
    assert "reference:floor#level:ref-0" in names
    assert not any(name.startswith("reference:ceiling#") for name in names)
    floor_extras = parsed["gltf"]["nodes"][_by_name(parsed)["reference:floor#level:ref-0"]]["extras"]
    assert floor_extras["ceiling"] == "no level height"


def test_all_options_on_export_is_deterministic_and_valid(tmp_path: Path) -> None:
    model = _plane_model(levels=((0.0, 2.7), (3.0, 2.6)))
    options = _default_options() | {
        "reference_planes": True,
        "reference_floor_alpha": 0.4,
        "reference_ceiling_alpha": 0.1,
        "reference_margin_m": 0.75,
        "dimmed_ids": ("device:ref-0", "device:ref-1", "device:missing"),
        "dimmed_alpha": 0.2,
        "dimmed_color": (0.5, 0.5, 0.55),
    }
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    summary = to_glb(model, first, **options)
    to_glb(model, second, **options)
    assert first.read_bytes() == second.read_bytes()
    # _parse_glb asserts the GLB header and chunk lengths are consistent.
    parsed = _parse_glb(first)
    assert summary["reference_planes"] == 4
    assert summary["dimmed"] == 2  # the unknown id matches nothing
    assert len(parsed["gltf"]["nodes"]) == summary["nodes"]


def _dimmed_model() -> BuildingModel:
    """Two same-type devices, one low-voltage device, a panel equipment with
    two ports, and one route between them: enough to dim a device, equipment
    and route while a same-type device stays undimmed."""

    def device(device_id: str, x: float, device_type: str = "receptacle_duplex") -> ElectricalDevice:
        return ElectricalDevice(
            id=device_id,
            device_type=device_type,
            pose=Pose(position=Point3(x=x, y=2.0, z=0.3)),
            level_id="level:dim",
        )

    equipment = ElectricalEquipment(
        id="equip:dim-panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
        level_id="level:dim",
    )

    def port(port_id: str, position: Point3) -> Port:
        return Port(
            id=port_id,
            owner_id="equip:dim-panel",
            domain="electrical",
            role="source",
            pose=Pose(position=position),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
        )

    return BuildingModel(
        model_id="model:glb-dim-synth",
        levels=(Level(id="level:dim", elevation_m=0.0, height_m=2.7),),
        electrical_devices=(
            device("device:dim-keep", 4.0),
            device("device:dim-drop", 6.0),
            device("device:dim-data", 8.0, "data_outlet"),
        ),
        electrical_equipment=(equipment,),
        ports=(
            port("port:dim-a", Point3(x=0.0, y=0.0, z=1.0)),
            port("port:dim-b", Point3(x=3.0, y=1.5, z=1.0)),
        ),
        circuits=(Circuit(
            id="circuit:dim-1",
            source_port_id="port:dim-a",
            load_port_ids=("port:dim-b",),
            route_ids=("route:dim-1",),
        ),),
        routes=(Route(
            id="route:dim-1",
            route_type="emt",
            start_port_id="port:dim-a",
            end_port_id="port:dim-b",
            centerline=Polyline3D(points=(
                Point3(x=0.0, y=0.0, z=1.0),
                Point3(x=3.0, y=1.5, z=1.0),
            )),
            nominal_diameter_m=0.021,
        ),),
    )


def test_dimmed_device_gets_caller_grey_blend_and_extras(tmp_path: Path) -> None:
    color = (0.4, 0.4, 0.45)
    options = _default_options() | {
        "dimmed_ids": ("device:dim-drop", "device:dim-data"),
        "dimmed_alpha": 0.25,
        "dimmed_color": color,
    }
    summary = to_glb(_dimmed_model(), tmp_path / "dim.glb", **options)
    parsed = _parse_glb(tmp_path / "dim.glb")

    dropped = _node_material(parsed, "device:dim-drop")
    assert dropped["name"] == "outlet-dimmed"
    assert dropped["alphaMode"] == "BLEND"
    assert dropped["pbrMetallicRoughness"]["baseColorFactor"] == pytest.approx([*color, 0.25])
    node = parsed["gltf"]["nodes"][_by_name(parsed)["device:dim-drop"]]
    assert node["extras"]["display"] == "dimmed (caller-supplied)"

    # The dimmed style wins over the low-voltage teal.
    data = _node_material(parsed, "device:dim-data")
    assert data["name"] == "low_voltage-dimmed"
    assert data["pbrMetallicRoughness"]["baseColorFactor"] == pytest.approx([*color, 0.25])

    # A non-dimmed device of the same type keeps its class colour, opaque.
    kept = _node_material(parsed, "device:dim-keep")
    assert kept["name"] == "outlet"
    assert "alphaMode" not in kept
    red, green, blue = kept["pbrMetallicRoughness"]["baseColorFactor"][:3]
    assert red > green and red > blue
    assert kept["pbrMetallicRoughness"]["baseColorFactor"][3] == 1.0
    assert summary["dimmed"] == 2


def test_dimmed_route_and_equipment_go_grey(tmp_path: Path) -> None:
    options = _default_options() | {
        "dimmed_ids": ("route:dim-1", "equip:dim-panel"),
    }
    summary = to_glb(_dimmed_model(), tmp_path / "dim.glb", **options)
    parsed = _parse_glb(tmp_path / "dim.glb")

    route = _node_material(parsed, "route:dim-1")
    assert route["name"] == "route-dimmed"
    assert route["alphaMode"] == "BLEND"
    red, green, blue = route["pbrMetallicRoughness"]["baseColorFactor"][:3]
    assert red == green == pytest.approx(0.62)
    assert blue == pytest.approx(0.62)
    assert route["pbrMetallicRoughness"]["baseColorFactor"][3] == pytest.approx(0.3)

    panel = _node_material(parsed, "equip:dim-panel")
    assert panel["name"] == "panel-dimmed"
    assert summary["dimmed"] == 2


def test_dimmed_count_only_counts_matched_ids(tmp_path: Path) -> None:
    options = _default_options() | {
        "dimmed_ids": (
            "device:dim-drop",
            "device:does-not-exist",
            "route:not-here",
            "equip:dim-panel",
        ),
    }
    summary = to_glb(_dimmed_model(), tmp_path / "dim.glb", **options)
    assert summary["dimmed"] == 2
    parsed = _parse_glb(tmp_path / "dim.glb")
    assert "display" not in parsed["gltf"]["nodes"][_by_name(parsed)["device:dim-keep"]]["extras"]


def _low_voltage_model() -> BuildingModel:
    low_voltage_types = (
        "data_outlet", "catv_outlet", "telephone_outlet",
        "junction_box_data", "speaker", "access_control_device",
    )
    return BuildingModel(
        model_id="model:glb-lowvolt-synth",
        electrical_devices=tuple(
            ElectricalDevice(
                id=f"device:lv-{device_type}",
                device_type=device_type,
                pose=Pose(position=Point3(x=1.0, y=2.0, z=3.0)),
            )
            for device_type in low_voltage_types
        ),
    )


def test_low_voltage_types_draw_teal_with_unchanged_geometry(tmp_path: Path) -> None:
    to_glb(_low_voltage_model(), tmp_path / "lowvoltage.glb")
    parsed = _parse_glb(tmp_path / "lowvoltage.glb")
    gltf = parsed["gltf"]

    vertex_counts = {
        "device:lv-data_outlet": 36,        # outlet box, as before
        "device:lv-catv_outlet": 36,        # outlet box, as before
        "device:lv-telephone_outlet": 36,   # outlet box, as before
        "device:lv-junction_box_data": 36,  # other box, as before
        "device:lv-speaker": 36,            # other box, as before
        "device:lv-access_control_device": 120,  # sensor cylinder, as before
    }
    for name, node in ((node["name"], node) for node in gltf["nodes"]):
        primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
        accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
        assert accessor["count"] == vertex_counts[name], name
        material = gltf["materials"][primitive["material"]]
        assert material["name"] == "low_voltage", name
        red, green, blue = material["pbrMetallicRoughness"]["baseColorFactor"][:3]
        assert (red, green, blue) == pytest.approx((0.10, 0.65, 0.70)), name


def test_combination_outlet_stays_an_outlet(tmp_path: Path) -> None:
    model = BuildingModel(
        model_id="model:glb-combo-synth",
        electrical_devices=(ElectricalDevice(
            id="device:combo",
            device_type="combination_outlet",
            pose=Pose(position=Point3(x=1.0, y=2.0, z=3.0)),
        ),),
    )
    to_glb(model, tmp_path / "combo.glb")
    parsed = _parse_glb(tmp_path / "combo.glb")
    material = _node_material(parsed, "device:combo")
    assert material["name"] == "outlet"
    red, green, blue = material["pbrMetallicRoughness"]["baseColorFactor"][:3]
    assert red > green and red > blue


def test_named_entities_carry_name_in_extras(tmp_path: Path) -> None:
    model = _dimmed_model()
    named = replace(
        model,
        electrical_devices=(
            replace(model.electrical_devices[0], name="receptacle at the bench"),
            *model.electrical_devices[1:],
        ),
        electrical_equipment=(
            replace(model.electrical_equipment[0], name="main panelboard"),
        ),
        routes=(
            replace(model.routes[0], name="low voltage stub-up, cabling by others"),
        ),
    )
    to_glb(named, tmp_path / "named.glb")
    parsed = _parse_glb(tmp_path / "named.glb")
    nodes = {node["name"]: node for node in parsed["gltf"]["nodes"]}
    assert nodes["device:dim-keep"]["extras"]["name"] == "receptacle at the bench"
    assert nodes["equip:dim-panel"]["extras"]["name"] == "main panelboard"
    assert nodes["route:dim-1"]["extras"]["name"] == "low voltage stub-up, cabling by others"
    # Entities without a name claim none.
    assert "name" not in nodes["device:dim-drop"]["extras"]

    wall_model = _wall_model(wall_height=2.7, level_height=None, elevation=0.0)
    wall_model = replace(
        wall_model,
        walls=(replace(wall_model.walls[0], name="south run"),),
    )
    to_glb(wall_model, tmp_path / "wall.glb")
    wall_parsed = _parse_glb(tmp_path / "wall.glb")
    wall_node = wall_parsed["gltf"]["nodes"][_by_name(wall_parsed)["wall:south-run"]]
    assert wall_node["extras"]["name"] == "south run"


# --- Caller-emphasized entities. Emphasis is the full-colour counterpart of
# --- dimming: the caller picks the ids, the exporter only refuses to fade
# --- them, and an id in both sets stays dimmed.


def _emphasis_model() -> BuildingModel:
    """A plan-derived (inferred) device and an observed device of the same
    type, a panel equipment and one route between its ports: enough to
    emphasize a device, an equipment and a route while the other device of
    the same type keeps the default look. The inferred provenance record
    stands in for any device whose mounting height is a rule, which is the
    case emphasis exists for."""

    def device(device_id: str, x: float, provenance: tuple = ()) -> ElectricalDevice:
        return ElectricalDevice(
            id=device_id,
            device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=x, y=2.0, z=0.3)),
            level_id="level:emph",
            provenance=provenance,
        )

    equipment = ElectricalEquipment(
        id="equip:emph-panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
        level_id="level:emph",
    )

    def port(port_id: str, position: Point3) -> Port:
        return Port(
            id=port_id,
            owner_id="equip:emph-panel",
            domain="electrical",
            role="source",
            pose=Pose(position=position),
            direction=Vector3(x=1.0, y=0.0, z=0.0),
        )

    return BuildingModel(
        model_id="model:glb-emphasis-synth",
        levels=(Level(id="level:emph", elevation_m=0.0, height_m=2.7),),
        electrical_devices=(
            device(
                "device:emph-inferred",
                4.0,
                (Provenance(
                    source_kind="synthetic",
                    source_id="sheet:synth-emphasis",
                    derivation="inferred",
                ),),
            ),
            device("device:emph-plain", 6.0),
        ),
        electrical_equipment=(equipment,),
        ports=(
            port("port:emph-a", Point3(x=0.0, y=0.0, z=1.0)),
            port("port:emph-b", Point3(x=3.0, y=1.5, z=1.0)),
        ),
        circuits=(Circuit(
            id="circuit:emph-1",
            source_port_id="port:emph-a",
            load_port_ids=("port:emph-b",),
            route_ids=("route:emph-1",),
        ),),
        routes=(Route(
            id="route:emph-1",
            route_type="emt",
            start_port_id="port:emph-a",
            end_port_id="port:emph-b",
            centerline=Polyline3D(points=(
                Point3(x=0.0, y=0.0, z=1.0),
                Point3(x=3.0, y=1.5, z=1.0),
            )),
            nominal_diameter_m=0.021,
        ),),
    )


def test_emphasized_inferred_device_draws_opaque_in_class_colour(tmp_path: Path) -> None:
    summary = to_glb(
        _emphasis_model(),
        tmp_path / "emph.glb",
        emphasized_ids=("device:emph-inferred",),
    )
    parsed = _parse_glb(tmp_path / "emph.glb")

    material = _node_material(parsed, "device:emph-inferred")
    assert material["name"] == "outlet-emphasized"
    assert "alphaMode" not in material
    red, green, blue = material["pbrMetallicRoughness"]["baseColorFactor"][:3]
    assert red > green and red > blue  # the outlet class colour, not grey
    assert material["pbrMetallicRoughness"]["baseColorFactor"][3] == 1.0

    extras = parsed["gltf"]["nodes"][_by_name(parsed)["device:emph-inferred"]]["extras"]
    assert extras["display"] == "emphasized (caller-supplied)"
    assert extras["derivation"] == "inferred"
    assert summary["emphasized"] == 1
    assert summary["dimmed"] == 0


def test_unemphasized_inferred_device_keeps_translucent_look(tmp_path: Path) -> None:
    to_glb(_emphasis_model(), tmp_path / "default.glb")
    parsed = _parse_glb(tmp_path / "default.glb")

    material = _node_material(parsed, "device:emph-inferred")
    assert material["name"] == "outlet-inferred"
    assert material["alphaMode"] == "BLEND"
    assert material["pbrMetallicRoughness"]["baseColorFactor"][3] < 1.0
    extras = parsed["gltf"]["nodes"][_by_name(parsed)["device:emph-inferred"]]["extras"]
    assert extras["derivation"] == "inferred"
    assert "display" not in extras


def test_id_in_both_sets_draws_dimmed(tmp_path: Path) -> None:
    summary = to_glb(
        _emphasis_model(),
        tmp_path / "both.glb",
        dimmed_ids=("device:emph-inferred",),
        emphasized_ids=("device:emph-inferred",),
    )
    parsed = _parse_glb(tmp_path / "both.glb")

    material = _node_material(parsed, "device:emph-inferred")
    assert material["name"] == "outlet-dimmed"
    assert material["alphaMode"] == "BLEND"
    extras = parsed["gltf"]["nodes"][_by_name(parsed)["device:emph-inferred"]]["extras"]
    assert extras["display"] == "dimmed (caller-supplied)"
    assert summary["dimmed"] == 1
    assert summary["emphasized"] == 0


def test_emphasized_equipment_and_route_draw_opaque(tmp_path: Path) -> None:
    summary = to_glb(
        _emphasis_model(),
        tmp_path / "route.glb",
        emphasized_ids=("equip:emph-panel", "route:emph-1"),
    )
    parsed = _parse_glb(tmp_path / "route.glb")

    route = _node_material(parsed, "route:emph-1")
    assert route["name"] == "route-emphasized"
    assert "alphaMode" not in route
    assert route["pbrMetallicRoughness"]["baseColorFactor"][3] == 1.0
    route_extras = parsed["gltf"]["nodes"][_by_name(parsed)["route:emph-1"]]["extras"]
    assert route_extras["display"] == "emphasized (caller-supplied)"

    panel = _node_material(parsed, "equip:emph-panel")
    assert panel["name"] == "panel-emphasized"
    assert "alphaMode" not in panel
    assert summary["emphasized"] == 2


def test_empty_emphasis_option_gives_plain_bytes(tmp_path: Path) -> None:
    model = _emphasis_model()
    to_glb(model, tmp_path / "plain.glb")
    to_glb(model, tmp_path / "empty-option.glb", emphasized_ids=())
    assert (tmp_path / "plain.glb").read_bytes() == (tmp_path / "empty-option.glb").read_bytes()


def test_emphasized_export_is_deterministic(tmp_path: Path) -> None:
    model = _emphasis_model()
    options: dict = {
        "dimmed_ids": ("device:emph-plain",),
        "emphasized_ids": ("device:emph-inferred", "equip:emph-panel", "route:emph-1"),
    }
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    summary = to_glb(model, first, **options)
    to_glb(model, second, **options)
    assert first.read_bytes() == second.read_bytes()
    assert summary["emphasized"] == 3
    assert summary["dimmed"] == 1
