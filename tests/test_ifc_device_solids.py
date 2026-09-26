"""Known-answer tests for native Body solids on electrical devices, equipment
and obstacles, and for the recorded no-Body decisions on conductors and route
fittings.

Issue #85 part B. The ``Axis`` curve stays the only geometry ``from_ifc``
reads back, so every case here must round trip exactly and validate strictly
with EXPRESS rules on. Every dimension, coordinate and identifier is
synthetic and invented for these tests; the repository's golden fixtures are
synthetic too.
"""

from __future__ import annotations

import math
from pathlib import Path

import ifcopenshell.geom
import ifcopenshell.util.shape
import ifcopenshell.validate
import numpy as np
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, round_trip, to_ifc
from oabm.model import (
    Box3D,
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Obstacle,
    Point3,
    Polyline3D,
    Pose,
    Quaternion,
    Size3,
)
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
GARAGE_ROUTE = ROOT / "fixtures" / "model" / "v1" / "garage-route.json"

# Known-answer device: 0.10 x 0.05 x 0.12 m, yawed 90 degrees about Z.
DEVICE_SIZE_M = Size3(x=0.10, y=0.05, z=0.12)
DEVICE_VOLUME_M3 = 0.10 * 0.05 * 0.12
DEVICE_POSITION_M = Point3(x=1.0, y=2.0, z=1.5)
YAW_RADIANS = math.pi / 2.0

EQUIPMENT_SIZE_M = Size3(x=0.4, y=0.15, z=0.8)
EQUIPMENT_VOLUME_M3 = 0.4 * 0.15 * 0.8
EQUIPMENT_POSITION_M = Point3(x=0.5, y=-1.0, z=1.1)

OBSTACLE_SIZE_M = Size3(x=0.6, y=0.2, z=0.3)
OBSTACLE_VOLUME_M3 = 0.6 * 0.2 * 0.3
OBSTACLE_POSITION_M = Point3(x=-1.0, y=0.5, z=2.0)

VOLUME_TOLERANCE_M3 = 1e-9
TOLERANCE_M = 1e-6

CONDUCTOR_REASON = (
    "conductor is represented by its route's conduit solid; see route_ids"
)
FITTING_REASON = "fitting is a placement-only occurrence on its conduit run"

_VALID_GOLDEN_CASES = sorted(
    case.name for case in load_golden_cases(GOLDEN_ROOT) if case.valid
)


def _settings() -> ifcopenshell.geom.settings:
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    return settings


def _triangulated_product(ifc, canonical_id: str):
    product = ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))
    shape = ifcopenshell.geom.create_shape(_settings(), product)
    verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
    return product, ifcopenshell.util.shape.get_volume(shape.geometry), verts


def _has_body(product) -> bool:
    if product.Representation is None:
        return False
    return any(
        representation.RepresentationIdentifier == "Body"
        for representation in product.Representation.Representations
    )


def _representation_identifiers(product) -> set[str]:
    if product.Representation is None:
        return set()
    return {
        representation.RepresentationIdentifier
        for representation in product.Representation.Representations
    }


def _adapter_pset(product) -> dict:
    for rel in product.IsDefinedBy:
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if not definition.is_a("IfcPropertySet") or definition.Name != "OABM_Adapter":
            continue
        return {
            prop.Name: (prop.NominalValue.wrappedValue if prop.NominalValue else None)
            for prop in definition.HasProperties
        }
    raise AssertionError("product carries no OABM_Adapter property set")


def _yawed_pose(position: Point3) -> Pose:
    half = math.sin(YAW_RADIANS / 2.0)
    return Pose(
        position=position,
        rotation=Quaternion(x=0.0, y=0.0, z=half, w=math.cos(YAW_RADIANS / 2.0)),
    )


def _synthetic_model(
    *,
    device_size: Size3 | None = DEVICE_SIZE_M,
    equipment_size: Size3 | None = EQUIPMENT_SIZE_M,
    obstacle: Obstacle | None = None,
) -> BuildingModel:
    level = Level(id="level:synth", name=None, elevation_m=0.0)
    device = ElectricalDevice(
        id="device:synth",
        name=None,
        device_type="evse",
        level_id="level:synth",
        pose=_yawed_pose(DEVICE_POSITION_M),
        size=device_size,
    )
    equipment = ElectricalEquipment(
        id="equip:synth",
        name=None,
        equipment_type="panel",
        level_id="level:synth",
        pose=Pose(position=EQUIPMENT_POSITION_M),
        size=equipment_size,
    )
    return BuildingModel(
        model_id="model:device-solids-synth",
        name="Synthetic device solids",
        levels=(level,),
        electrical_equipment=(equipment,),
        electrical_devices=(device,),
        obstacles=(obstacle,) if obstacle is not None else (),
    )


def _box_obstacle() -> Obstacle:
    return Obstacle(
        id="obstacle:synth",
        name="Synthetic soffit blockout",
        level_id="level:synth",
        geometry=Box3D(
            pose=Pose(position=OBSTACLE_POSITION_M),
            size=OBSTACLE_SIZE_M,
        ),
    )


def _polyline_obstacle() -> Obstacle:
    return Obstacle(
        id="obstacle:synth",
        name="Synthetic clearance path",
        level_id="level:synth",
        geometry=Polyline3D(
            points=(
                Point3(x=0.0, y=0.0, z=1.0),
                Point3(x=1.5, y=1.0, z=1.0),
            )
        ),
    )


def test_device_body_volume_and_rotated_world_bbox_match_canonical() -> None:
    ifc = to_ifc(_synthetic_model())
    product, volume, verts = _triangulated_product(ifc, "device:synth")

    # 0.10 x 0.05 x 0.12 m = 0.0006 m^3, independent of the yaw.
    assert volume == pytest.approx(DEVICE_VOLUME_M3, abs=VOLUME_TOLERANCE_M3)

    # Yaw 90 degrees maps local +X onto world +Y, so the footprint swaps.
    assert np.allclose(
        verts.min(axis=0),
        (
            DEVICE_POSITION_M.x - DEVICE_SIZE_M.y / 2.0,
            DEVICE_POSITION_M.y - DEVICE_SIZE_M.x / 2.0,
            DEVICE_POSITION_M.z - DEVICE_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert np.allclose(
        verts.max(axis=0),
        (
            DEVICE_POSITION_M.x + DEVICE_SIZE_M.y / 2.0,
            DEVICE_POSITION_M.y + DEVICE_SIZE_M.x / 2.0,
            DEVICE_POSITION_M.z + DEVICE_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert _has_body(product)
    properties = _adapter_pset(product)
    assert properties["Body"] == "yes"


def test_equipment_with_size_gets_a_body() -> None:
    ifc = to_ifc(_synthetic_model())
    product, volume, verts = _triangulated_product(ifc, "equip:synth")

    assert _has_body(product)
    assert product.ObjectPlacement is not None
    assert volume == pytest.approx(EQUIPMENT_VOLUME_M3, abs=VOLUME_TOLERANCE_M3)
    assert np.allclose(
        verts.min(axis=0),
        (
            EQUIPMENT_POSITION_M.x - EQUIPMENT_SIZE_M.x / 2.0,
            EQUIPMENT_POSITION_M.y - EQUIPMENT_SIZE_M.y / 2.0,
            EQUIPMENT_POSITION_M.z - EQUIPMENT_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert np.allclose(
        verts.max(axis=0),
        (
            EQUIPMENT_POSITION_M.x + EQUIPMENT_SIZE_M.x / 2.0,
            EQUIPMENT_POSITION_M.y + EQUIPMENT_SIZE_M.y / 2.0,
            EQUIPMENT_POSITION_M.z + EQUIPMENT_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert _adapter_pset(product)["Body"] == "yes"


def test_device_without_size_gets_no_body_and_carries_reason() -> None:
    ifc = to_ifc(_synthetic_model(device_size=None))
    product = ifc.by_guid(canonical_id_to_ifc_guid("device:synth"))

    assert not _has_body(product)
    properties = _adapter_pset(product)
    assert properties["Body"] == "no"
    assert properties["BodyReason"] == "no canonical size"


def test_box_obstacle_body_sits_on_its_canonical_pose() -> None:
    ifc = to_ifc(_synthetic_model(obstacle=_box_obstacle()))
    product, volume, verts = _triangulated_product(ifc, "obstacle:synth")

    assert _has_body(product)
    assert product.ObjectPlacement is not None
    assert volume == pytest.approx(OBSTACLE_VOLUME_M3, abs=VOLUME_TOLERANCE_M3)
    assert np.allclose(
        verts.min(axis=0),
        (
            OBSTACLE_POSITION_M.x - OBSTACLE_SIZE_M.x / 2.0,
            OBSTACLE_POSITION_M.y - OBSTACLE_SIZE_M.y / 2.0,
            OBSTACLE_POSITION_M.z - OBSTACLE_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert np.allclose(
        verts.max(axis=0),
        (
            OBSTACLE_POSITION_M.x + OBSTACLE_SIZE_M.x / 2.0,
            OBSTACLE_POSITION_M.y + OBSTACLE_SIZE_M.y / 2.0,
            OBSTACLE_POSITION_M.z + OBSTACLE_SIZE_M.z / 2.0,
        ),
        atol=TOLERANCE_M,
    )
    assert _adapter_pset(product)["Body"] == "yes"


def test_obstacle_without_volumetric_extent_gets_no_body_and_reason() -> None:
    ifc = to_ifc(_synthetic_model(obstacle=_polyline_obstacle()))
    product = ifc.by_guid(canonical_id_to_ifc_guid("obstacle:synth"))

    assert not _has_body(product)
    properties = _adapter_pset(product)
    assert properties["Body"] == "no"
    assert properties["BodyReason"] == (
        "obstacle geometry is a polyline3d; no canonical volumetric extent"
    )


def test_conductors_and_fittings_record_their_representation() -> None:
    # Public synthetic fixture shared with the round-trip suite.
    ifc = to_ifc(BuildingModel.load(GARAGE_ROUTE))

    for canonical_id in ("conductor:l1", "conductor:l2", "conductor:egc"):
        product = ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))
        assert not _has_body(product), canonical_id
        properties = _adapter_pset(product)
        assert properties["Body"] == "no", canonical_id
        assert properties["BodyReason"] == CONDUCTOR_REASON, canonical_id
        # The route centerline Axis is untouched; no solid was added.
        assert _representation_identifiers(product) == {"Axis"}, canonical_id

    for canonical_id in ("fitting:rise-elbow", "fitting:drop-elbow"):
        product = ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))
        assert not _has_body(product), canonical_id
        properties = _adapter_pset(product)
        assert properties["Body"] == "no", canonical_id
        assert properties["BodyReason"] == FITTING_REASON, canonical_id


@pytest.mark.parametrize(
    "case_name", [*_VALID_GOLDEN_CASES, "synthetic-suite", "synthetic-no-size"]
)
def test_exports_pass_strict_express_validation(case_name: str) -> None:
    if case_name == "synthetic-suite":
        model = _synthetic_model(obstacle=_box_obstacle())
    elif case_name == "synthetic-no-size":
        model = _synthetic_model(
            device_size=None, equipment_size=None, obstacle=_polyline_obstacle()
        )
    else:
        case = next(c for c in load_golden_cases(GOLDEN_ROOT) if c.name == case_name)
        model = load_golden_model(case)
    ifc = to_ifc(model)
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    errors = [
        statement for statement in logger.statements if statement.get("level") == "error"
    ]
    assert errors == []


@pytest.mark.parametrize("case_name", [*_VALID_GOLDEN_CASES, "synthetic-suite"])
def test_round_trip_is_exact(case_name: str) -> None:
    if case_name == "synthetic-suite":
        model = _synthetic_model(obstacle=_box_obstacle())
    else:
        case = next(c for c in load_golden_cases(GOLDEN_ROOT) if c.name == case_name)
        model = load_golden_model(case)
    assert round_trip(model).to_dict() == model.to_dict()


def test_body_geometry_is_identical_across_two_exports() -> None:
    model = _synthetic_model(obstacle=_box_obstacle())
    first = to_ifc(model)
    second = to_ifc(model)
    for canonical_id in ("device:synth", "equip:synth", "obstacle:synth"):
        shape_first = ifcopenshell.geom.create_shape(
            _settings(), first.by_guid(canonical_id_to_ifc_guid(canonical_id))
        )
        shape_second = ifcopenshell.geom.create_shape(
            _settings(), second.by_guid(canonical_id_to_ifc_guid(canonical_id))
        )
        verts_first = np.round(
            np.asarray(shape_first.geometry.verts, dtype=float).reshape(-1, 3), 9
        )
        verts_second = np.round(
            np.asarray(shape_second.geometry.verts, dtype=float).reshape(-1, 3), 9
        )
        assert verts_first.shape == verts_second.shape, canonical_id
        assert np.array_equal(verts_first, verts_second), canonical_id
