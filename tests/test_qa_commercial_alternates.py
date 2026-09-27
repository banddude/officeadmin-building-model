"""Cross-exporter agreement over one canonical commercial TI golden model.

The ``commercial-ti-alternates`` golden fixture (synthetic, public safe)
carries a base scope, a caller-declared ALTERNATES bid group marked with the
fixture-only ``alternate`` attribute, and canonical glazed/framed wall tokens.
The same group ids go to the quantities, IFC and GLB lanes; these tests prove
the lanes agree over that one model: the same members, the same alternate
devices, and glazed walls recognized everywhere they are drawn.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import ifcopenshell.validate
import pytest

from oabm.exports import to_glb
from oabm.ifc import canonical_id_to_ifc_guid, to_ifc
from oabm.quantities import extract_quantities
from oabm.qa import (
    canonical_digest,
    load_golden_cases,
    load_golden_model,
    validate_public_fixture_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
CASE_NAME = "commercial-ti-alternates"
CANONICAL_PSET = "OABM_Canonical"
ADAPTER_PSET = "OABM_Adapter"

# Known answers (metres), straight from the fixture geometry.
ALT_ROUTE_LENGTH_M = 5.3
BASE_ROUTE_LENGTH_M = 45.1
ALT_CONDUCTOR_LENGTH_M = 5.3
C1_LENGTH_M = 1.9 + 7.1 + 8.6
C2_LENGTH_M = 11.0 + 3.4 + 4.5 + 8.6

ALT_DEVICE_IDS = frozenset({
    "device:ti-rec-a1",
    "device:ti-rec-a2",
    "device:ti-lum-a1",
})
ALT_MEMBER_IDS = ALT_DEVICE_IDS | {
    "route:ti-r8",
    "conductor:ti-c3-l1",
    "conductor:ti-c3-n",
    "conductor:ti-c3-egc",
}

# The IFC lane expands the route member through its segment products (the
# route system container is not a selectable product) and adds the fitting.
IFC_MEMBER_KEYS = (
    ALT_MEMBER_IDS
    - {"route:ti-r8"}
    | {"fitting:ti-r8-bend", "route:ti-r8#segment:0", "route:ti-r8#segment:1"}
)

# The GLB lane expands the route member through its drawn wire nodes.
GLB_CHILDREN = {
    "device:ti-rec-a1",
    "device:ti-rec-a2",
    "device:ti-lum-a1",
    "route:ti-r8",
    "conductor:ti-c3-l1#route:ti-r8#0",
    "conductor:ti-c3-n#route:ti-r8#0",
    "conductor:ti-c3-egc#route:ti-r8#0",
}


def _case():
    return next(case for case in load_golden_cases(GOLDEN_ROOT) if case.name == CASE_NAME)


def _model():
    return load_golden_model(_case())


def _alt_group(model) -> dict[str, tuple[str, ...]]:
    """The caller's bid group, read back from the fixture's own attribute."""
    members = {
        *(entity.id for entity in model.electrical_devices if entity.attributes.get("alternate")),
        *(entity.id for entity in model.routes if entity.attributes.get("alternate")),
        *(entity.id for entity in model.conductors if entity.attributes.get("alternate")),
    }
    return {"ALTERNATES": tuple(sorted(members))}


def _props(item, name: str) -> dict | None:
    for rel in getattr(item, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == name:
            return {
                prop.Name: getattr(prop.NominalValue, "wrappedValue", prop.NominalValue)
                for prop in definition.HasProperties
                if prop.is_a("IfcPropertySingleValue")
            }
    return None


def _canonical_key(product) -> str | None:
    """Map an IFC product back to its canonical id or adapter segment key."""
    canonical = _props(product, CANONICAL_PSET)
    if canonical is not None:
        return str(json.loads(str(canonical["CanonicalJson"]))["id"])
    adapter = _props(product, ADAPTER_PSET)
    if adapter is not None and adapter.get("Role") == "route-segment":
        return f"{adapter['RouteId']}#segment:{int(adapter['SegmentIndex'])}"
    return None


def _parse_glb(path: Path) -> dict:
    data = path.read_bytes()
    json_length, _json_type = struct.unpack_from("<II", data, 12)
    return json.loads(data[20:20 + json_length])


def _by_name(gltf: dict) -> dict[str, int]:
    return {node["name"]: index for index, node in enumerate(gltf["nodes"])}


def _node_material(gltf: dict, names: dict[str, int], name: str) -> dict:
    node = gltf["nodes"][names[name]]
    primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
    return gltf["materials"][primitive["material"]]


def test_fixture_is_synthetic_stable_and_carries_the_tokens() -> None:
    case = _case()
    model = _model()
    validate_public_fixture_provenance(model)
    assert canonical_digest(model) == case.sha256

    tokens = [wall.construction for wall in model.walls]
    assert tokens.count("glazed") == 2
    assert tokens.count("framed") == 1
    assert tokens.count(None) == 5


def test_alternates_group_membership_reads_back_from_the_attribute() -> None:
    model = _model()
    assert set(_alt_group(model)["ALTERNATES"]) == ALT_MEMBER_IDS
    # The fixture marks the whole alternate scope; the group the caller names
    # carries the quantity-bearing members: devices, the route, conductors.
    scope = ALT_MEMBER_IDS | {"fitting:ti-r8-bend", "circuit:ti-alt-1"}
    marked = set()
    for collection in (
        model.electrical_devices,
        model.routes,
        model.route_fittings,
        model.circuits,
        model.conductors,
    ):
        marked.update(entity.id for entity in collection if entity.attributes.get("alternate"))
    assert marked == scope


def test_quantities_alternates_and_base_lines_hit_the_known_answers() -> None:
    model = _model()
    report = extract_quantities(model, groups=_alt_group(model))

    alt = {
        (item.category, item.item_type): item.quantity
        for item in report.items
        if item.group == "ALTERNATES"
    }
    assert alt[("device", "receptacle")] == pytest.approx(2.0)
    assert alt[("device", "luminaire")] == pytest.approx(1.0)
    assert alt[("route_length", "emt")] == pytest.approx(ALT_ROUTE_LENGTH_M)
    assert alt[("conductor_length", "line")] == pytest.approx(ALT_CONDUCTOR_LENGTH_M)
    assert alt[("conductor_length", "neutral")] == pytest.approx(ALT_CONDUCTOR_LENGTH_M)
    assert alt[("conductor_length", "equipment-ground")] == pytest.approx(ALT_CONDUCTOR_LENGTH_M)
    assert alt[("fitting", "elbow-90")] == pytest.approx(1.0)
    assert ("equipment", "panelboard") not in alt

    base = {
        (item.category, item.item_type): item.quantity
        for item in report.items
        if item.group is None
    }
    assert base[("device", "receptacle")] == pytest.approx(4.0)
    assert base[("device", "luminaire")] == pytest.approx(3.0)
    assert base[("equipment", "panelboard")] == pytest.approx(1.0)
    assert base[("route_length", "emt")] == pytest.approx(BASE_ROUTE_LENGTH_M)
    # Two line conductors per base circuit ride every home run of the circuit.
    assert base[("conductor_length", "line")] == pytest.approx(2.0 * (C1_LENGTH_M + C2_LENGTH_M))
    assert base[("conductor_length", "equipment-ground")] == pytest.approx(C1_LENGTH_M + C2_LENGTH_M)
    assert ("conductor_length", "neutral") not in base

    assert report.group_summary == (("ALTERNATES", 7, 7),)
    assert report.unmatched_group_ids == 0


def test_quantities_base_plus_alternates_equal_the_ungrouped_takeoff() -> None:
    model = _model()
    plain = extract_quantities(model)
    grouped = extract_quantities(model, groups=_alt_group(model))

    def totals(items):
        sums_by_key: dict = {}
        for item in items:
            key = (item.category, item.item_type, tuple(item.variant), item.unit)
            sums_by_key[key] = sums_by_key.get(key, 0.0) + item.quantity
        return sums_by_key

    base_sums = totals(item for item in grouped.items if item.group is None)
    alt_sums = totals(item for item in grouped.items if item.group is not None)
    plain_sums = totals(plain.items)

    assert set(base_sums) | set(alt_sums) == set(plain_sums)
    for key, quantity in plain_sums.items():
        assert base_sums.get(key, 0.0) + alt_sums.get(key, 0.0) == pytest.approx(quantity)


def test_ifc_group_members_are_exactly_the_alternate_scope(tmp_path: Path) -> None:
    model = _model()
    ifc = to_ifc(model, tmp_path / "ti.ifc", groups=_alt_group(model))

    groups = [item for item in ifc.by_type("IfcGroup") if item.Name == "ALTERNATES"]
    assert len(groups) == 1
    group = groups[0]
    assignments = [
        rel for rel in ifc.by_type("IfcRelAssignsToGroup") if rel.RelatingGroup == group
    ]
    assert len(assignments) == 1

    keys = [_canonical_key(product) for product in assignments[0].RelatedObjects]
    assert None not in keys
    assert set(keys) == IFC_MEMBER_KEYS


def test_glb_group_hides_alternates_and_carries_exactly_the_members(tmp_path: Path) -> None:
    model = _model()
    to_glb(
        model,
        tmp_path / "ti.glb",
        groups=_alt_group(model),
        hidden_groups=("ALTERNATES",),
    )
    gltf = _parse_glb(tmp_path / "ti.glb")
    names = _by_name(gltf)
    group_node = gltf["nodes"][names["group:ALTERNATES"]]

    children = {gltf["nodes"][index]["name"] for index in group_node["children"]}
    assert children == GLB_CHILDREN
    assert group_node["extras"]["hidden_by_default"] is True
    assert group_node["extras"]["members"] == len(GLB_CHILDREN)
    assert group_node["extensions"] == {"KHR_node_visibility": {"visible": False}}
    assert gltf["extensionsUsed"] == ["KHR_node_visibility"]
    assert "extensionsRequired" not in gltf


def test_glazed_walls_draw_as_glass_and_every_token_travels(tmp_path: Path) -> None:
    model = _model()
    to_glb(model, tmp_path / "ti.glb", groups=_alt_group(model))
    gltf = _parse_glb(tmp_path / "ti.glb")
    names = _by_name(gltf)

    key = "construction"
    glazed = ("wall:ti-south", "wall:ti-conf-glass")
    material_names = set()
    for wall_id in glazed:
        node = gltf["nodes"][names[wall_id]]
        mesh = gltf["meshes"][node["mesh"]]
        assert node["extras"][key] == "glazed"
        assert mesh["extras"] == {key: "glazed"}
        material_names.add(_node_material(gltf, names, wall_id)["name"])
    assert material_names == {"wall-glazed"}
    # Both glazed walls share exactly one glass material.
    assert sum(1 for m in gltf["materials"] if m["name"] == "wall-glazed") == 1
    glass = next(m for m in gltf["materials"] if m["name"] == "wall-glazed")
    assert glass["pbrMetallicRoughness"]["baseColorFactor"] == pytest.approx(
        [0.70, 0.82, 0.88, 0.35]
    )
    assert glass["alphaMode"] == "BLEND"
    assert glass["doubleSided"] is True

    framed_node = gltf["nodes"][names["wall:ti-lobby"]]
    framed_material = _node_material(gltf, names, "wall:ti-lobby")
    assert framed_node["extras"][key] == "framed"
    assert gltf["meshes"][framed_node["mesh"]]["extras"] == {key: "framed"}
    assert framed_material["name"] == "wall"
    assert framed_material["name"] != "wall-glazed"

    plain_node = gltf["nodes"][names["wall:ti-east"]]
    assert key not in plain_node["extras"]
    assert "extras" not in gltf["meshes"][plain_node["mesh"]]


def test_alternate_device_ids_agree_across_all_three_outputs(tmp_path: Path) -> None:
    model = _model()
    group = _alt_group(model)

    report = extract_quantities(model, groups=group)
    quantity_devices = set()
    for item in report.items:
        if item.group == "ALTERNATES" and item.category == "device":
            quantity_devices.update(item.source_entity_ids)

    ifc = to_ifc(model, tmp_path / "ti.ifc", groups=group)
    group_item = next(item for item in ifc.by_type("IfcGroup") if item.Name == "ALTERNATES")
    assignment = next(
        rel for rel in ifc.by_type("IfcRelAssignsToGroup") if rel.RelatingGroup == group_item
    )
    model_device_ids = {device.id for device in model.electrical_devices}
    ifc_devices = {
        key
        for key in (_canonical_key(product) for product in assignment.RelatedObjects)
        if key in model_device_ids
    }

    to_glb(model, tmp_path / "ti.glb", groups=group)
    gltf = _parse_glb(tmp_path / "ti.glb")
    child_names = {node["name"] for node in gltf["nodes"]}
    glb_devices = set(ALT_DEVICE_IDS & child_names)

    assert quantity_devices == ifc_devices == glb_devices == set(ALT_DEVICE_IDS)


def test_each_export_is_deterministic(tmp_path: Path) -> None:
    model = _model()
    group = _alt_group(model)

    first = extract_quantities(model, groups=group).to_json()
    second = extract_quantities(model, groups=group).to_json()
    assert first == second

    ifc_first = tmp_path / "ifc-first.ifc"
    ifc_second = tmp_path / "ifc-second.ifc"
    to_ifc(model, ifc_first, groups=group)
    to_ifc(model, ifc_second, groups=group)
    assert ifc_first.read_bytes() == ifc_second.read_bytes()

    glb_first = tmp_path / "glb-first.glb"
    glb_second = tmp_path / "glb-second.glb"
    to_glb(model, glb_first, groups=group, hidden_groups=("ALTERNATES",))
    to_glb(model, glb_second, groups=group, hidden_groups=("ALTERNATES",))
    assert glb_first.read_bytes() == glb_second.read_bytes()


def _product(ifc, canonical_id: str):
    return ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))


def _body_items(product) -> tuple:
    for shape in product.Representation.Representations:
        if shape.RepresentationIdentifier == "Body":
            return shape.Items
    raise AssertionError(f"{product.is_a()} {product.GlobalId} has no Body representation")


def test_ifc_glass_materials_and_style_coexist_with_the_group(tmp_path: Path) -> None:
    """The #167 material and style, over the golden fixture, with the
    caller group in the same file: the glazed walls relate the one Glass
    material and carry the one shared translucent style, the framed wall
    relates Framed partition and nothing else is styled."""

    model = _model()
    ifc = to_ifc(model, tmp_path / "ti.ifc", groups=_alt_group(model))

    # Materials: exactly the tokens this fixture states.
    materials = ifc.by_type("IfcMaterial")
    assert [(material.Name, material.Category) for material in materials] == [
        ("Framed partition", "framing"),
        ("Glass", "glass"),
    ]
    rels = {
        rel.RelatingMaterial.Name: rel for rel in ifc.by_type("IfcRelAssociatesMaterial")
    }
    assert set(rels) == {"Framed partition", "Glass"}
    assert sorted(_canonical_key(item) for item in rels["Glass"].RelatedObjects) == [
        "wall:ti-conf-glass",
        "wall:ti-south",
    ]
    assert [_canonical_key(item) for item in rels["Framed partition"].RelatedObjects] == [
        "wall:ti-lobby"
    ]
    # A tokenless wall carries no material association.
    plain = _product(ifc, "wall:ti-east")
    assert not any(
        rel.is_a("IfcRelAssociatesMaterial") for rel in plain.HasAssociations
    )

    # The glass style: one shared surface style on exactly the two glazed
    # bodies, and no other product styled anywhere in the file.
    styles = ifc.by_type("IfcSurfaceStyle")
    assert len(styles) == 1
    assert styles[0].Name == "OABM Glazed"
    (rendering,) = [
        item for item in styles[0].Styles if item.is_a("IfcSurfaceStyleRendering")
    ]
    assert rendering.Transparency == pytest.approx(0.65)
    (red, green, blue) = (
        rendering.SurfaceColour.Red,
        rendering.SurfaceColour.Green,
        rendering.SurfaceColour.Blue,
    )
    assert (red, green, blue) == pytest.approx((0.80, 0.86, 0.90))

    glazed_items = []
    for wall_id in ("wall:ti-south", "wall:ti-conf-glass"):
        items = _body_items(_product(ifc, wall_id))
        assert len(items) == 1
        glazed_items.append(items[0])
        for item in items:
            styled = item.StyledByItem
            assert len(styled) == 1
            assert list(styled[0].Styles) == [styles[0]]
    # The framed wall and every other product go unstyled.
    for styled_item in ifc.by_type("IfcStyledItem"):
        assert styled_item.Item in glazed_items

    # Group and material relationships coexist in this one file.
    assert any(item.Name == "ALTERNATES" for item in ifc.by_type("IfcGroup"))

    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []
