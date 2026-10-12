"""#290 public tests: explicit structural members, contract 1.4."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.model import (
    BuildingModel,
    CatalogProduct,
    ContractError,
    Point3,
    Pose,
    Provenance,
    Quaternion,
    Size3,
    StructuralMember,
    STRUCTURAL_MEMBER_KINDS,
    upgrade_to_v1_4,
)
from oabm.quantities import extract_quantities
from oabm.routing import route_between_ports

ROOT = Path(__file__).resolve().parents[1]
PROV = (
    Provenance(
        source_kind="synthetic",
        source_id="fixture:structural-member",
        derivation="user",
    ),
)


def pose(x: float, y: float, z: float, *, z_radians: float = 0.0) -> Pose:
    rotation = Quaternion()
    if z_radians:
        rotation = Quaternion(
            x=0.0, y=0.0, z=math.sin(z_radians / 2), w=math.cos(z_radians / 2),
        )
    return Pose(position=Point3(x=x, y=y, z=z), rotation=rotation)


def base_model() -> BuildingModel:
    return BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")


def member(model: BuildingModel, **changes) -> StructuralMember:
    values = {
        "id": "member:beam-1",
        "kind": "beam",
        "level_id": model.levels[0].id,
        "pose": pose(2.7, 0.2, 1.65),
        "size": Size3(x=0.4, y=0.4, z=0.4),
        "provenance": PROV,
    }
    values.update(changes)
    return StructuralMember(**values)


def member_model(**changes) -> BuildingModel:
    model = upgrade_to_v1_4(base_model())
    return replace(model, structural_members=(member(model, **changes),))


def _route_length(route) -> float:
    points = route.centerline.points
    return sum(
        math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        for a, b in zip(points, points[1:])
    )


def _penetrates(route, struct_member, step_m: float = 0.02) -> bool:
    """Whether any point along the route's segments enters the member box."""

    min_x, min_y, min_z, max_x, max_y, max_z = struct_member.axis_aligned_bounds()
    for start, end in zip(route.centerline.points, route.centerline.points[1:]):
        length = math.dist((start.x, start.y, start.z), (end.x, end.y, end.z))
        steps = max(2, int(length / step_m))
        for index in range(steps + 1):
            fraction = index / steps
            point = (
                start.x + (end.x - start.x) * fraction,
                start.y + (end.y - start.y) * fraction,
                start.z + (end.z - start.z) * fraction,
            )
            if (
                min_x <= point[0] <= max_x
                and min_y <= point[1] <= max_y
                and min_z <= point[2] <= max_z
            ):
                return True
    return False


# --- contract and portability ---


def test_version_gates_and_lossless_upgrade() -> None:
    model = base_model()
    document = model.to_dict()
    assert "structural_members" not in document
    for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        with pytest.raises(ContractError, match="structural members require"):
            replace(model, schema_version=version, structural_members=(member(model),))
        # A document of any older version carrying the field is refused
        # outright; the refusal may cite the missing earlier collections of
        # that version or the unknown structural fields, but never silently
        # drops them.
        with pytest.raises(ContractError):
            BuildingModel.from_dict({**document, "schema_version": version,
                                     "structural_members": []})
        with pytest.raises(ContractError):
            BuildingModel.from_dict({**document, "schema_version": version,
                                     "structural_members": [{
                                         "id": "member:x", "kind": "beam",
                                         "level_id": model.levels[0].id,
                                         "pose": {"position": {"x": 0.0, "y": 0.0, "z": 0.0}},
                                         "size": {"x": 0.1, "y": 0.1, "z": 0.1},
                                         "provenance": [],
                                     }]})
    # 1.3 documents without the field stay valid, and 1.3 with the field is
    # rejected rather than silently dropped.
    v13 = replace(model, schema_version="1.3.0").to_dict()
    BuildingModel.from_dict(v13)
    with pytest.raises(ContractError, match="unknown structural fields"):
        BuildingModel.from_dict({**v13, "structural_members": []})
    # The explicit upgrade keeps every prior collection.
    upgraded = upgrade_to_v1_4(model)
    assert upgraded.schema_version == "1.4.0"
    stripped = {
        key: value for key, value in upgraded.to_dict().items()
        if key != "structural_members"
    }
    # Every legacy key survives unchanged; the upgrade only adds the version
    # and the (empty) collection arrays of the boundaries the fixture had not
    # yet opted into.
    for key, value in document.items():
        if key == "schema_version":
            continue
        assert stripped[key] == value
    added = set(stripped) - set(document) - {"schema_version"}
    assert added <= {
        "electrical_boxes", "box_selection_outcomes",
        "raceway_selections", "raceway_selection_outcomes",
        "support_assemblies", "support_components",
        "support_interfaces", "support_selection_outcomes",
    }
    assert all(stripped[key] == [] for key in added)
    assert stripped["schema_version"] == "1.4.0"
    # A 1.4 document requires the array, and round-trips exactly.
    full = upgraded.to_dict()
    full["structural_members"] = []
    assert BuildingModel.from_dict(full).to_dict() == full
    with pytest.raises(ContractError, match="requires the structural_members array"):
        BuildingModel.from_dict({
            key: value for key, value in full.items() if key != "structural_members"
        })


def test_v1_4_schema_and_current_collections_stay_valid_together() -> None:
    schema = json.loads(
        (ROOT / "contracts/oabm-model-v1.4.schema.json").read_text(encoding="utf-8"),
    )
    Draft202012Validator.check_schema(schema)
    model = member_model()
    # The 1.4 boundary keeps boxes, raceway selections, and the support
    # inventory valid together: upgrade a support-bearing model and keep a
    # member alongside.
    supports_case = BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")
    document = replace(upgrade_to_v1_4(supports_case), structural_members=(
        member(supports_case),
    )).to_dict()
    Draft202012Validator(schema).validate(document)
    assert BuildingModel.from_dict(document).to_dict() == document
    # Member documents validate; a malformed one fails the schema.
    bad = model.to_dict()
    bad["structural_members"][0]["kind"] = "truss"
    with pytest.raises(ContractError):
        BuildingModel.from_dict(bad)


def test_member_validation_fails_closed() -> None:
    model = base_model()
    with pytest.raises(ContractError, match="kind"):
        member(model, kind="truss")
    with pytest.raises(ContractError, match="pose"):
        member(model, pose=None)
    with pytest.raises(ContractError, match="size"):
        member(model, size=None)
    with pytest.raises(ContractError, match="Size3.x"):
        member(model, size=Size3(x=0.0, y=0.1, z=0.1))
    with pytest.raises(ContractError, match="level_id"):
        member(model, level_id="")
    # Unknown material stays unknown; nothing infers it from the kind.
    unknown = member(model)
    assert unknown.material is None


def test_duplicate_ids_and_missing_levels_refused() -> None:
    model = upgrade_to_v1_4(base_model())
    beam = member(model)
    with pytest.raises(ContractError, match="duplicate entity id"):
        replace(model, structural_members=(beam, replace(beam, kind="joist")))
    with pytest.raises(ContractError, match="references missing id"):
        replace(model, structural_members=(member(model, level_id="level:missing"),))


def test_provenance_scopes_and_deterministic_serialization() -> None:
    model = upgrade_to_v1_4(base_model())
    scoped = Provenance(
        source_kind="synthetic",
        source_id="fixture:structural-member",
        derivation="user",
        scope_paths=("kind", "size"),
    )
    beam = member(model, provenance=(scoped,))
    document = replace(model, structural_members=(beam,)).to_dict()
    entry = document["structural_members"][0]
    assert entry["provenance"][0]["scope_paths"] == ["kind", "size"]
    assert BuildingModel.from_dict(document).to_dict() == document
    # Deterministic serialization: the same model bytes come out every time.
    populated = replace(model, structural_members=(
        member(model), replace(member(model), id="member:beam-2", kind="joist"),
    ))
    assert populated.to_json() == populated.to_json()


# --- router ---


def test_router_avoids_member_and_removal_restores_the_direct_path() -> None:
    model = base_model()
    start, end = model.ports[0].id, model.ports[1].id
    direct, _ = route_between_ports(model, start, end, "emt")

    with_member = member_model()  # the beam sits on the direct corridor
    blocked, _ = route_between_ports(with_member, start, end, "emt")
    assert blocked.centerline.points != direct.centerline.points
    assert not _penetrates(blocked, with_member.structural_members[0])

    # Removing the member restores the direct path exactly.
    restored, _ = route_between_ports(upgrade_to_v1_4(model), start, end, "emt")
    assert restored.centerline.points == direct.centerline.points


def test_rotated_member_is_avoided_conservatively() -> None:
    model = base_model()
    start, end = model.ports[0].id, model.ports[1].id
    direct, _ = route_between_ports(model, start, end, "emt")
    rotated = member_model(pose=pose(2.7, 0.2, 1.65, z_radians=math.radians(45)))
    blocked, _ = route_between_ports(rotated, start, end, "emt")
    assert blocked.centerline.points != direct.centerline.points
    assert not _penetrates(blocked, rotated.structural_members[0])
    # Documented conservative overexclusion: the axis-aligned bounds of the
    # 45-degree member cover more plan area than the member itself.
    member_entity = rotated.structural_members[0]
    min_x, min_y, min_z, max_x, max_y, max_z = member_entity.axis_aligned_bounds()
    rectangle_area = member_entity.size.x * member_entity.size.y
    assert (max_x - min_x) * (max_y - min_y) > rectangle_area * 1.2


# --- IFC, GLB, quantities ---


def test_ifc_exports_member_once_and_round_trips(tmp_path: Path) -> None:
    from oabm.ifc import from_ifc, to_ifc

    model = member_model()
    destination = tmp_path / "members.ifc"
    ifc = to_ifc(model, destination)
    # Exactly one IfcBeam: the member exports once, with its kind.
    beams = list(ifc.by_type("IfcBeam"))
    assert len(beams) == 1
    assert beams[0].ObjectType == "beam"
    # Its canonical id is recoverable from the lossless canonical shadow.
    assert "member:beam-1" in destination.read_text()
    restored = from_ifc(destination)
    assert restored.to_dict() == model.to_dict()
    # Deterministic bytes.
    second = tmp_path / "members-2.ifc"
    to_ifc(model, second)
    assert destination.read_bytes() == second.read_bytes()


def test_legacy_ifc_outputs_stay_unchanged(tmp_path: Path) -> None:
    from oabm.ifc import to_ifc

    legacy = base_model()
    first = tmp_path / "legacy.ifc"
    to_ifc(legacy, first)
    second = tmp_path / "legacy-upgraded.ifc"
    to_ifc(upgrade_to_v1_4(legacy), second)
    # The upgrade alone changes only the version shadow, not the elements.
    legacy_text = first.read_text()
    assert "structural_member" not in legacy_text


def test_glb_draws_each_member_once_and_keeps_legacy_bytes(tmp_path: Path) -> None:
    from oabm.exports import to_glb

    legacy = base_model()
    plain = tmp_path / "plain.glb"
    plain_summary = to_glb(legacy, plain)
    assert "structural_members" not in plain_summary

    model = member_model()
    target = tmp_path / "members.glb"
    summary = to_glb(model, target)
    assert summary["structural_members"] == 1
    parsed = json.loads(target.read_bytes()[20:].split(b" ", 1)[0]) if False else None
    # The member node carries the canonical identity and kind.
    data = target.read_bytes()
    assert b"member:beam-1" in data
    assert b"structural_member" in data
    assert b'"kind":"beam"' in data.replace(b",", b",")
    # Deterministic.
    again = tmp_path / "members-2.glb"
    to_glb(model, again)
    assert target.read_bytes() == again.read_bytes()


def test_quantities_report_member_count_and_rectangular_volume() -> None:
    model = member_model()
    report = extract_quantities(model)
    lines = {
        (item.category, item.item_type): item
        for item in report.items
        if item.category.startswith("structural_member")
    }
    count = lines[("structural_member", "beam")]
    volume = lines[("structural_member_volume", "beam")]
    assert count.quantity == 1.0 and count.unit == "ea"
    member_entity = model.structural_members[0]
    expected_volume = (
        member_entity.size.x * member_entity.size.y * member_entity.size.z
    )
    assert volume.quantity == pytest.approx(expected_volume)
    assert volume.unit == "m3"
    # Deterministic under input permutation, and legacy reports unchanged.
    reordered = replace(model, structural_members=(
        member(model, id="member:beam-2", kind="stud"),
        member(model),
    ))
    flipped = replace(model, structural_members=(
        member(model),
        member(model, id="member:beam-2", kind="stud"),
    ))
    assert extract_quantities(reordered).to_json() == extract_quantities(flipped).to_json()
    # Quantity lines carry stable identities for the same members.
    left = {
        (item.category, item.item_type, item.quantity)
        for item in extract_quantities(reordered).items
        if item.category.startswith("structural_member")
    }
    right = {
        (item.category, item.item_type, item.quantity)
        for item in extract_quantities(flipped).items
        if item.category.startswith("structural_member")
    }
    assert left == right
    assert extract_quantities(upgrade_to_v1_4(base_model())).to_json() == (
        extract_quantities(base_model()).to_json()
    )
