"""Legible provenance on exported IFC: the ``OABM_Provenance`` property set.

Issue #86: provenance was recoverable only by parsing the private
``OABM_Canonical.CanonicalJson`` blob, so a consumer could not tell a measured
wall from one whose thickness this tool chose. These tests pin the plain
custom property set any IFC viewer shows, the derivation rule over unscoped
records, the per-claim scopes that keep a measured wall with an assumed
thickness from collapsing into one class, strict validation, exact round trip,
and deterministic pset identity across exports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.util.element
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, round_trip, to_ifc
from oabm.model import (
    BuildingModel,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    Vector3,
    Wall,
)
from oabm.qa import iter_entities, load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"


def _provenance_psets(item: Any) -> dict[str, Any]:
    return ifcopenshell.util.element.get_psets(item).get("OABM_Provenance", {})


def _provenance_pset_entity(item: Any) -> Any:
    for rel in getattr(item, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == "OABM_Provenance":
            return definition
    raise AssertionError("product carries no OABM_Provenance property set")


def _by_canonical_id(ifc: ifcopenshell.file, canonical_id: str) -> Any:
    return ifc.by_guid(canonical_id_to_ifc_guid(canonical_id))


def _assumed_thickness_wall_model() -> BuildingModel:
    """A scan-measured wall whose thickness is the importer's default.

    The observed record is entity-wide; the inferred one is scoped to the one
    dimension this tool chose, which is exactly the case that must not read
    as wholly observed or wholly inferred.
    """

    return BuildingModel(
        model_id="model:pset-wall",
        levels=(Level(id="level:pset-wall", elevation_m=0.0, height_m=2.7),),
        walls=(Wall(
            id="wall:pset-0",
            level_id="level:pset-wall",
            centerline=Polyline3D(points=(
                Point3(x=0.0, y=0.0, z=0.0),
                Point3(x=4.0, y=0.0, z=0.0),
            )),
            thickness_m=0.15,
            height_m=2.4,
            confidence=0.92,
            provenance=(
                Provenance(
                    source_kind="scan",
                    source_id="storey-3",
                    derivation="observed",
                ),
                Provenance(
                    source_kind="importer",
                    source_id="surface-depth-default",
                    method="surface depth default",
                    derivation="inferred",
                    scope_paths=("thickness_m",),
                ),
            ),
        ),),
    )


def test_wall_with_scoped_assumption_reads_observed_with_named_claim() -> None:
    ifc = to_ifc(_assumed_thickness_wall_model())
    wall = _by_canonical_id(ifc, "wall:pset-0")
    psets = _provenance_psets(wall)

    assert psets["Derivation"] == "observed"
    assert psets["InferredClaims"] == "thickness_m"
    assert psets["UserClaims"] == ""
    assert psets["Confidence"] == pytest.approx(0.92)
    assert psets["Sources"] == "importer:surface-depth-default; scan:storey-3"
    assert psets["Methods"] == "surface depth default"


def _route_model(attributes: dict[str, Any] | None = None) -> BuildingModel:
    equipment = ElectricalEquipment(
        id="equip:pset-panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
        level_id="level:pset-route",
    )
    return BuildingModel(
        model_id="model:pset-route",
        levels=(Level(id="level:pset-route", elevation_m=0.0, height_m=2.7),),
        electrical_equipment=(equipment,),
        ports=(
            Port(
                id="port:pset-a",
                owner_id="equip:pset-panel",
                domain="electrical",
                role="source",
                pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
                direction=Vector3(x=1.0, y=0.0, z=0.0),
            ),
            Port(
                id="port:pset-b",
                owner_id="equip:pset-panel",
                domain="electrical",
                role="load",
                pose=Pose(position=Point3(x=3.0, y=1.5, z=1.0)),
                direction=Vector3(x=1.0, y=0.0, z=0.0),
            ),
        ),
        routes=(Route(
            id="route:pset-0",
            route_type="emt",
            start_port_id="port:pset-a",
            end_port_id="port:pset-b",
            centerline=Polyline3D(points=(
                Point3(x=0.0, y=0.0, z=1.0),
                Point3(x=3.0, y=1.5, z=1.0),
            )),
            nominal_diameter_m=0.021,
            attributes=attributes if attributes is not None else {},
        ),),
    )


def test_proposed_route_carries_design_status() -> None:
    ifc = to_ifc(_route_model({"design_status": "proposed"}))
    psets = _provenance_psets(_by_canonical_id(ifc, "route:pset-0"))
    assert psets["DesignStatus"] == "proposed"


def test_route_without_design_status_omits_the_property() -> None:
    ifc = to_ifc(_route_model())
    psets = _provenance_psets(_by_canonical_id(ifc, "route:pset-0"))
    assert "DesignStatus" not in psets


def test_fully_inferred_device_reads_inferred_without_claims() -> None:
    model = BuildingModel(
        model_id="model:pset-inferred",
        levels=(Level(id="level:pset-inferred", elevation_m=0.0, height_m=2.7),),
        electrical_devices=(ElectricalDevice(
            id="device:pset-inferred",
            device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=1.0, y=2.0, z=0.3)),
            level_id="level:pset-inferred",
            provenance=(Provenance(
                source_kind="plan",
                source_id="plan:power-sheet",
                derivation="inferred",
            ),),
        ),),
    )
    ifc = to_ifc(model)
    psets = _provenance_psets(_by_canonical_id(ifc, "device:pset-inferred"))
    assert psets["Derivation"] == "inferred"
    assert psets["InferredClaims"] == ""
    assert psets["UserClaims"] == ""


def test_entity_without_provenance_reads_unstated() -> None:
    model = BuildingModel(
        model_id="model:pset-unstated",
        levels=(Level(id="level:pset-unstated", elevation_m=0.0, height_m=2.7),),
        electrical_devices=(ElectricalDevice(
            id="device:pset-unstated",
            device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=1.0, y=2.0, z=0.3)),
            level_id="level:pset-unstated",
        ),),
    )
    ifc = to_ifc(model)
    psets = _provenance_psets(_by_canonical_id(ifc, "device:pset-unstated"))
    assert psets["Derivation"] == "unstated"
    assert psets["InferredClaims"] == ""
    assert psets["UserClaims"] == ""
    assert psets["Sources"] == ""
    assert psets["Methods"] == ""
    assert psets["Confidence"] == pytest.approx(1.0)


def test_long_sources_truncate_deterministically() -> None:
    records = tuple(
        Provenance(
            source_kind="scan",
            source_id=f"suite-{index:03d}-" + "x" * 60,
            derivation="observed",
        )
        for index in range(40)
    )
    model = BuildingModel(
        model_id="model:pset-truncate",
        levels=(Level(id="level:pset-truncate", elevation_m=0.0, height_m=2.7),),
        electrical_devices=(ElectricalDevice(
            id="device:pset-truncate",
            device_type="receptacle_duplex",
            pose=Pose(position=Point3(x=1.0, y=2.0, z=0.3)),
            level_id="level:pset-truncate",
            provenance=records,
        ),),
    )
    untruncated = "; ".join(sorted(
        f"{record.source_kind}:{record.source_id}" for record in records
    ))
    assert len(untruncated) > 1000

    first = _provenance_psets(_by_canonical_id(to_ifc(model), "device:pset-truncate"))
    second = _provenance_psets(_by_canonical_id(to_ifc(model), "device:pset-truncate"))
    assert first == second
    sources = first["Sources"]
    assert len(sources) == 1000
    assert sources.endswith(" …")
    # The cut keeps the same sorted head of the list on every export.
    assert untruncated.startswith(sources[: -len(" …")])


@pytest.mark.parametrize(
    "case",
    [case for case in load_golden_cases(GOLDEN_ROOT) if case.valid],
    ids=lambda case: case.name,
)
def test_golden_case_validates_strictly_and_round_trips(case) -> None:
    import ifcopenshell.validate

    model = load_golden_model(case)
    ifc = to_ifc(model)

    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []

    assert round_trip(model).to_dict() == model.to_dict()


def test_every_canonical_product_carries_the_legible_pset() -> None:
    case = next(
        item for item in load_golden_cases(GOLDEN_ROOT) if item.name == "synthetic-garage"
    )
    model = load_golden_model(case)
    ifc = to_ifc(model)
    for entity in iter_entities(model):
        item = _by_canonical_id(ifc, entity.id)
        psets = ifcopenshell.util.element.get_psets(item)
        assert "OABM_Provenance" in psets, entity.id
        assert "OABM_Canonical" in psets, entity.id


def test_pset_guids_and_values_are_deterministic_across_exports() -> None:
    model = _assumed_thickness_wall_model()

    def snapshot(ifc: ifcopenshell.file) -> tuple[str, tuple[tuple[str, Any], ...]]:
        wall = _by_canonical_id(ifc, "wall:pset-0")
        pset_entity = _provenance_pset_entity(wall)
        assert pset_entity.GlobalId == canonical_id_to_ifc_guid(
            "wall:pset-0#OABM_Provenance"
        )
        values = _provenance_psets(wall)
        return (pset_entity.GlobalId, tuple(sorted(values.items())))

    assert snapshot(to_ifc(model)) == snapshot(to_ifc(model))
