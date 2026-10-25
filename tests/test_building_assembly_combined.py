"""Known-answer proof that separately selected presets share one physical model."""
import json
import math
import struct
from collections import Counter

from oabm.building_assemblies import (
    build_opening_insert_assembly, build_suspended_ceiling_assembly,
)
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_source
from oabm.exports.gltf import build_glb_bytes
from oabm.model import BuildingModel, WireSweep, point_on_component_surface, point_on_slab_surface
from oabm.quantities import extract_quantities


def test_combined_presets_keep_source_facts_and_one_physical_identity():
    model = synthetic_wall_inserts_ceiling_assembly()
    source = synthetic_two_opening_wall_source()
    assert model.walls == source.walls
    assert model.openings == source.openings
    assert model.levels == source.levels
    assert model.schema_version == "1.6.0"
    assert len(model.building_assemblies) == 4
    assert len(model.assembly_components) == 165
    assert len(model.slabs) == 1
    assert not model.routes
    roles = Counter(part.component_kind for part in model.assembly_components)
    assert roles == {
        "stud": 15, "cripple": 6, "bottom_plate": 2, "top_plate": 2,
        "header": 6, "sill": 1, "drywall_panel": 24,
        "door_frame": 3, "door_leaf": 1, "window_frame": 4, "window_glazing": 1,
        "tbar_main_runner": 7, "tbar_cross_runner": 32, "tile": 40, "hanger_wire": 21,
    }
    claimed = [part_id for owner in model.building_assemblies for part_id in owner.component_ids]
    ids = {part.id for part in model.assembly_components}
    assert len(claimed) == len(set(claimed)) == len(ids)
    assert set(claimed) == ids
    assert BuildingModel.from_dict(model.to_dict()).to_json() == model.to_json()
    assert synthetic_wall_inserts_ceiling_assembly().to_json() == model.to_json()
    assert callable(build_opening_insert_assembly) and callable(build_suspended_ceiling_assembly)


def test_combined_mesh_quantities_and_hanger_contacts_use_same_parts():
    model = synthetic_wall_inserts_ceiling_assembly()
    ids = {part.id for part in model.assembly_components}
    payload = build_glb_bytes(model)
    assert payload == build_glb_bytes(synthetic_wall_inserts_ceiling_assembly())
    length, chunk_type = struct.unpack_from("<II", payload, 12)
    assert chunk_type == 0x4E4F534A
    document = json.loads(payload[20:20+length])
    binary_start = 20 + length + 8
    binary = payload[binary_start:]

    def positions(node):
        # These producer meshes use world-space positions, then glTF Y-up.
        assert not any(key in node for key in ("matrix", "translation", "rotation", "scale"))
        primitive = document["meshes"][node["mesh"]]["primitives"][0]
        accessor = document["accessors"][primitive["attributes"]["POSITION"]]
        assert accessor["componentType"] == 5126 and accessor["type"] == "VEC3"
        view = document["bufferViews"][accessor["bufferView"]]
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        stride = view.get("byteStride", 12)
        return [struct.unpack_from("<3f", binary, start + index * stride)
                for index in range(accessor["count"])]
    part_nodes = [node for node in document["nodes"]
                  if node.get("extras", {}).get("canonical_type") == "building_assembly_component"]
    assert len(part_nodes) == len(ids)
    assert {node["name"] for node in part_nodes} == ids
    assert len(document["meshes"]) == len(ids) + 1  # existing deck only
    assert not any(node["name"] in {"wall:proof", "ceiling:proof"} for node in document["nodes"])
    deck_nodes = [node for node in document["nodes"] if node["name"] == "slab:proof-deck"]
    assert len(deck_nodes) == 1
    deck_y = [point[1] for point in positions(deck_nodes[0])]
    assert math.isclose(min(deck_y), 2.8, abs_tol=1e-6)
    assert math.isclose(max(deck_y), 3.0, abs_tol=1e-6)
    wire_nodes = [node for node in part_nodes if node["extras"]["component_kind"] == "hanger_wire"]
    assert len(wire_nodes) == 21
    for node in wire_nodes:
        assert math.isclose(max(point[1] for point in positions(node)), min(deck_y), abs_tol=1e-6)
    for node in part_nodes:
        primitive = document["meshes"][node["mesh"]]["primitives"][0]
        assert document["accessors"][primitive["attributes"]["POSITION"]]["count"] > 0
        assert document["accessors"][primitive["indices"]]["count"] > 0

    report = extract_quantities(model)
    counts = [item for item in report.items if item.category == "assembly_component_count"]
    assert {key for item in counts for key in item.source_entity_ids} == ids
    assert math.fsum(item.quantity for item in counts) == 165
    assert not any(item.category in {"wall_volume", "wall_face_area", "ceiling_volume"}
                   for item in report.items)
    deck = [item for item in report.items if item.category == "slab_volume"]
    assert len(deck) == 1
    assert deck[0].source_entity_ids == ("slab:proof-deck",)
    assert math.isclose(deck[0].quantity, 4.8 * 3 * .2, abs_tol=1e-9)
    parts = {part.id: part for part in model.assembly_components}
    for part in model.assembly_components:
        if not isinstance(part.geometry, WireSweep):
            continue
        attachment = part.hanger_attachment
        assert point_on_slab_surface(model.slabs[0], attachment.top_anchor)
        assert point_on_component_surface(parts[attachment.bottom_runner_id], attachment.bottom_anchor)
        assert part.geometry.centerline.points[0] == attachment.top_anchor
        assert part.geometry.centerline.points[-1] == attachment.bottom_anchor
