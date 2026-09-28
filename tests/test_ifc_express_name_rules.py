"""Strict EXPRESS validation for contract-optional canonical names.

The canonical contract lets ``name`` be None on every entity, but IFC4 gives
``IfcProject`` (WR1) and ``IfcBuildingElementProxy`` (WR1) an ``exists(Name)``
where-rule, and ``from_ifc`` deliberately reads ``Name`` back as the canonical
name so a Bonsai rename flows into the model. A nameless model or obstacle
would therefore export EXPRESS-invalid IFC or import with a fabricated name,
so ``to_ifc`` refuses both fail-closed. Every model the adapter does export
validates clean under ``ifcopenshell.validate(express_rules=True)``. Every
dimension, coordinate and identifier here is synthetic and invented for these
tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import ifcopenshell.validate
import pytest

from oabm.ifc import (
    IfcAdapterError,
    canonical_id_to_ifc_guid,
    round_trip,
    to_ifc,
)
from oabm.model import (
    Box3D,
    BuildingModel,
    Ceiling,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Obstacle,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Route,
    RouteConstraint,
    RouteFitting,
    Size3,
    Slab,
    Space,
    Vector3,
    Wall,
)

MODEL_ID = "model:name-rules-synth"
MODEL_NAME = "Synthetic name rules"
FOOTPRINT = Polygon3D(
    points=(Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0), Point3(x=4, y=3, z=0), Point3(x=0, y=3, z=0))
)


def _synthetic_model() -> BuildingModel:
    """One named entity of every exported class, circuits and conductors too."""

    return BuildingModel(
        model_id=MODEL_ID,
        name=MODEL_NAME,
        levels=(Level(id="level:x", name="Storey", elevation_m=0.0),),
        spaces=(Space(id="space:x", name="Room", level_id="level:x", footprint=FOOTPRINT, height_m=2.7),),
        walls=(
            Wall(
                id="wall:x",
                name="Party wall",
                level_id="level:x",
                centerline=Polyline3D(points=(Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0))),
                thickness_m=0.2,
                height_m=2.7,
            ),
        ),
        slabs=(Slab(id="slab:x", name="Plate", level_id="level:x", footprint=FOOTPRINT, thickness_m=0.1),),
        ceilings=(
            Ceiling(
                id="ceiling:x",
                name="Ceiling",
                level_id="level:x",
                footprint=Polygon3D(
                    points=tuple(Point3(x=p.x, y=p.y, z=2.7) for p in FOOTPRINT.points)
                ),
                thickness_m=0.02,
            ),
        ),
        openings=(
            Opening(
                id="opening:x",
                name="Door",
                host_id="wall:x",
                opening_type="door",
                pose=Pose(position=Point3(x=1.0, y=0.0, z=1.05)),
                size=Size3(x=0.9, y=0.2, z=2.1),
            ),
        ),
        obstacles=(
            Obstacle(
                id="obstacle:x",
                name="Casework",
                level_id="level:x",
                obstacle_type="casework",
                geometry=Box3D(pose=Pose(position=Point3(x=2, y=2, z=0.4)), size=Size3(x=0.8, y=0.6, z=0.8)),
            ),
        ),
        route_constraints=(RouteConstraint(id="constraint:x", constraint_type="no_go", geometry=FOOTPRINT),),
        electrical_devices=(
            ElectricalDevice(
                id="device:x",
                name="Receptacle",
                level_id="level:x",
                device_type="receptacle_duplex",
                pose=Pose(position=Point3(x=1, y=1, z=0.3)),
                size=Size3(x=0.1, y=0.05, z=0.12),
            ),
        ),
        electrical_equipment=(
            ElectricalEquipment(
                id="equipment:x",
                name="Panel",
                level_id="level:x",
                equipment_type="panelboard",
                pose=Pose(position=Point3(x=3, y=1, z=1.0)),
                size=Size3(x=0.6, y=0.2, z=1.2),
            ),
        ),
        ports=(
            Port(
                id="port:source",
                owner_id="equipment:x",
                domain="power",
                role="source",
                pose=Pose(position=Point3(x=3, y=1, z=1.4)),
                direction=Vector3(x=0, y=0, z=1),
            ),
            Port(
                id="port:load",
                owner_id="equipment:x",
                domain="power",
                role="load",
                pose=Pose(position=Point3(x=3, y=1, z=0.6)),
                direction=Vector3(x=0, y=0, z=-1),
            ),
        ),
        routes=(
            Route(
                id="route:x",
                route_type="emt",
                start_port_id="port:source",
                end_port_id="port:load",
                centerline=Polyline3D(
                    points=(
                        Point3(x=3, y=1, z=1.4),
                        Point3(x=3, y=2, z=1.4),
                        Point3(x=3, y=2, z=0.6),
                        Point3(x=3, y=1, z=0.6),
                    )
                ),
                nominal_diameter_m=0.0209,
                fitting_ids=("fitting:x",),
            ),
        ),
        route_fittings=(
            RouteFitting(
                id="fitting:x",
                route_id="route:x",
                fitting_type="elbow-90",
                pose=Pose(position=Point3(x=3, y=2, z=1.4)),
                angle_radians=1.5708,
            ),
        ),
        circuits=(
            Circuit(
                id="circuit:x",
                source_port_id="port:source",
                load_port_ids=("port:load",),
                route_ids=("route:x",),
            ),
        ),
        conductors=(
            Conductor(id="conductor:x", circuit_id="circuit:x", role="hot", size="12", count=2,
                      route_ids=("route:x",)),
        ),
    )


def test_nameless_model_is_refused_fail_closed() -> None:
    with pytest.raises(IfcAdapterError, match=r"IfcProject.*model:name-rules-synth"):
        to_ifc(replace(_synthetic_model(), name=None))


def test_nameless_obstacle_is_refused_and_names_the_canonical_id() -> None:
    model = replace(
        _synthetic_model(),
        obstacles=(replace(_synthetic_model().obstacles[0], name=None),),
    )
    with pytest.raises(IfcAdapterError, match=r"obstacle:x"):
        to_ifc(model)


def test_named_export_has_zero_strict_statements(tmp_path: Path) -> None:
    ifc = to_ifc(_synthetic_model(), tmp_path / "named.ifc")
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []
    assert ifc.by_type("IfcProject")[0].Name == MODEL_NAME
    proxy = ifc.by_guid(canonical_id_to_ifc_guid("obstacle:x"))
    assert proxy.is_a("IfcBuildingElementProxy")
    assert proxy.Name == "Casework"


def test_named_export_round_trips_and_is_byte_deterministic(tmp_path: Path) -> None:
    model = _synthetic_model()
    first = tmp_path / "first.ifc"
    second = tmp_path / "second.ifc"
    to_ifc(model, first)
    to_ifc(model, second)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    assert round_trip(model).to_dict() == model.to_dict()


@pytest.mark.parametrize(
    "canonical_id",
    ["wall:x", "slab:x", "ceiling:x", "space:x", "opening:x", "device:x", "equipment:x", "obstacle:x"],
)
def test_every_geometry_class_keeps_a_non_empty_body(canonical_id: str) -> None:
    ifc = to_ifc(_synthetic_model())
    product = ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))
    assert product.Representation is not None, canonical_id
    body = [
        representation
        for representation in product.Representation.Representations
        if representation.RepresentationIdentifier == "Body"
    ]
    assert len(body) == 1, canonical_id
    assert body[0].Items, canonical_id
