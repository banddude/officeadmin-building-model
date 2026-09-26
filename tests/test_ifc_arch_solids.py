"""Known-answer tests for native Body solids on architecture entities.

Issue #85 part A: walls, slabs, ceilings, spaces and openings must export a
native ``Body`` solid derived from their canonical dimensions, while the
``Axis`` curve stays the only geometry read back on import. Every dimension,
coordinate and identifier here is synthetic and invented for these tests.
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell.geom
import ifcopenshell.util.shape
import ifcopenshell.validate
import numpy as np
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, round_trip, to_ifc
from oabm.model import (
    BuildingModel,
    Ceiling,
    Level,
    Opening,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Size3,
    Slab,
    Space,
    Wall,
)
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"

TOLERANCE_M = 1e-6

WALL_LENGTH_M = 4.0
WALL_THICKNESS_M = 0.2
WALL_HEIGHT_M = 2.7
FOOTPRINT_AREA_M2 = 12.0
SLAB_THICKNESS_M = 0.1
CEILING_THICKNESS_M = 0.02
SPACE_HEIGHT_M = 2.7
OPENING_WIDTH_M = 0.9
OPENING_HEIGHT_M = 2.1
OPENING_X_CENTER_M = 1.0

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


def _synthetic_model(
    *,
    with_opening: bool = False,
    ceiling_thickness: float | None = CEILING_THICKNESS_M,
    space_height: float | None = SPACE_HEIGHT_M,
) -> BuildingModel:
    level = Level(id="level:synth", name="Storey", elevation_m=0.0)
    wall = Wall(
        id="wall:synth",
        name=None,
        level_id="level:synth",
        centerline=Polyline3D(
            points=(Point3(x=0, y=0, z=0), Point3(x=WALL_LENGTH_M, y=0, z=0))
        ),
        thickness_m=WALL_THICKNESS_M,
        height_m=WALL_HEIGHT_M,
    )
    footprint = Polygon3D(
        points=(
            Point3(x=0, y=0, z=0),
            Point3(x=4, y=0, z=0),
            Point3(x=4, y=3, z=0),
            Point3(x=0, y=3, z=0),
        )
    )
    ceiling_footprint = Polygon3D(
        points=tuple(
            Point3(x=point.x, y=point.y, z=WALL_HEIGHT_M) for point in footprint.points
        )
    )
    slab = Slab(
        id="slab:synth",
        name=None,
        level_id="level:synth",
        footprint=footprint,
        thickness_m=SLAB_THICKNESS_M,
    )
    ceiling = Ceiling(
        id="ceiling:synth",
        name=None,
        level_id="level:synth",
        footprint=ceiling_footprint,
        thickness_m=ceiling_thickness,
    )
    space = Space(
        id="space:synth",
        name="Room",
        level_id="level:synth",
        footprint=footprint,
        height_m=space_height,
    )
    openings: tuple[Opening, ...] = ()
    if with_opening:
        openings = (
            Opening(
                id="opening:synth",
                name=None,
                host_id="wall:synth",
                opening_type="door",
                pose=Pose(
                    position=Point3(
                        x=OPENING_X_CENTER_M, y=0.0, z=OPENING_HEIGHT_M / 2.0
                    )
                ),
                size=Size3(
                    x=OPENING_WIDTH_M, y=WALL_THICKNESS_M, z=OPENING_HEIGHT_M
                ),
            ),
        )
    return BuildingModel(
        model_id="model:arch-solids-synth",
        name="Synthetic architecture solids",
        levels=(level,),
        spaces=(space,),
        walls=(wall,),
        slabs=(slab,),
        ceilings=(ceiling,),
        openings=openings,
    )


def test_wall_body_bbox_and_volume_match_canonical_dimensions() -> None:
    ifc = to_ifc(_synthetic_model())
    product, volume, verts = _triangulated_product(ifc, "wall:synth")

    assert np.allclose(verts.min(axis=0), (0.0, -WALL_THICKNESS_M / 2.0, 0.0), atol=TOLERANCE_M)
    assert np.allclose(
        verts.max(axis=0),
        (WALL_LENGTH_M, WALL_THICKNESS_M / 2.0, WALL_HEIGHT_M),
        atol=TOLERANCE_M,
    )
    assert volume == pytest.approx(
        WALL_LENGTH_M * WALL_THICKNESS_M * WALL_HEIGHT_M, abs=TOLERANCE_M
    )
    # The Axis curve survives untouched and the placement rule from the #89
    # port holds: a product with a shape representation has an ObjectPlacement.
    identifiers = {
        representation.RepresentationIdentifier
        for representation in product.Representation.Representations
    }
    assert identifiers == {"Axis", "Body"}
    assert product.ObjectPlacement is not None


def test_elbow_wall_body_sits_on_each_centerline_segment() -> None:
    level = Level(id="level:elbow", name="Storey", elevation_m=0.0)
    wall = Wall(
        id="wall:elbow",
        name=None,
        level_id="level:elbow",
        centerline=Polyline3D(
            points=(
                Point3(x=0, y=0, z=0),
                Point3(x=3, y=0, z=0),
                Point3(x=3, y=4, z=0),
            )
        ),
        thickness_m=0.2,
        height_m=2.7,
    )
    model = BuildingModel(
        model_id="model:arch-elbow-synth",
        name="Synthetic elbow wall",
        levels=(level,),
        walls=(wall,),
    )
    ifc = to_ifc(model)

    product, volume, verts = _triangulated_product(ifc, "wall:elbow")
    assert volume == pytest.approx((3.0 + 4.0) * WALL_THICKNESS_M * WALL_HEIGHT_M, abs=TOLERANCE_M)
    assert np.allclose(verts.min(axis=0), (0.0, -WALL_THICKNESS_M / 2.0, 0.0), atol=TOLERANCE_M)
    # Each rectangle is centered across its own segment and stops at the
    # segment ends, so the outside corner is a small notch, not a miter.
    assert np.allclose(verts.max(axis=0), (3.0 + WALL_THICKNESS_M / 2.0, 4.0, WALL_HEIGHT_M), atol=TOLERANCE_M)
    body = [
        representation
        for representation in product.Representation.Representations
        if representation.RepresentationIdentifier == "Body"
    ]
    assert len(body) == 1
    assert len(body[0].Items) == 2


def test_slab_ceiling_and_space_bodies_match_footprint_times_depth() -> None:
    ifc = to_ifc(_synthetic_model())

    _, slab_volume, slab_verts = _triangulated_product(ifc, "slab:synth")
    assert slab_volume == pytest.approx(FOOTPRINT_AREA_M2 * SLAB_THICKNESS_M, abs=TOLERANCE_M)
    # The footprint plane is the plate's top face; the slab hangs below it.
    assert np.allclose(slab_verts.min(axis=0), (0.0, 0.0, -SLAB_THICKNESS_M), atol=TOLERANCE_M)
    assert np.allclose(slab_verts.max(axis=0), (4.0, 3.0, 0.0), atol=TOLERANCE_M)

    _, ceiling_volume, ceiling_verts = _triangulated_product(ifc, "ceiling:synth")
    assert ceiling_volume == pytest.approx(FOOTPRINT_AREA_M2 * CEILING_THICKNESS_M, abs=TOLERANCE_M)
    assert np.allclose(
        ceiling_verts.min(axis=0), (0.0, 0.0, WALL_HEIGHT_M - CEILING_THICKNESS_M), atol=TOLERANCE_M
    )
    assert np.allclose(ceiling_verts.max(axis=0), (4.0, 3.0, WALL_HEIGHT_M), atol=TOLERANCE_M)

    _, space_volume, space_verts = _triangulated_product(ifc, "space:synth")
    assert space_volume == pytest.approx(FOOTPRINT_AREA_M2 * SPACE_HEIGHT_M, abs=TOLERANCE_M)
    # A space volume rises from its floor plane.
    assert np.allclose(space_verts.min(axis=0), (0.0, 0.0, 0.0), atol=TOLERANCE_M)
    assert np.allclose(space_verts.max(axis=0), (4.0, 3.0, SPACE_HEIGHT_M), atol=TOLERANCE_M)


def test_opening_body_exists_is_pointed_at_by_host_and_voids_wall() -> None:
    ifc = to_ifc(_synthetic_model(with_opening=True))

    opening_product = ifc.by_guid(canonical_id_to_ifc_guid("opening:synth"))
    assert _has_body(opening_product)
    assert opening_product.ObjectPlacement is not None

    wall_product = ifc.by_guid(canonical_id_to_ifc_guid("wall:synth"))
    relationships = list(wall_product.HasOpenings)
    assert len(relationships) == 1
    assert relationships[0].RelatedOpeningElement == opening_product
    assert relationships[0].RelatingBuildingElement == wall_product

    _, void_volume, void_verts = _triangulated_product(ifc, "opening:synth")
    assert void_volume == pytest.approx(
        OPENING_WIDTH_M * OPENING_HEIGHT_M * WALL_THICKNESS_M, abs=TOLERANCE_M
    )
    assert np.allclose(
        void_verts.min(axis=0),
        (OPENING_X_CENTER_M - OPENING_WIDTH_M / 2.0, -WALL_THICKNESS_M / 2.0, 0.0),
        atol=TOLERANCE_M,
    )
    assert np.allclose(
        void_verts.max(axis=0),
        (OPENING_X_CENTER_M + OPENING_WIDTH_M / 2.0, WALL_THICKNESS_M / 2.0, OPENING_HEIGHT_M),
        atol=TOLERANCE_M,
    )

    _, wall_volume, _ = _triangulated_product(ifc, "wall:synth")
    assert wall_volume == pytest.approx(
        WALL_LENGTH_M * WALL_THICKNESS_M * WALL_HEIGHT_M
        - OPENING_WIDTH_M * OPENING_HEIGHT_M * WALL_THICKNESS_M,
        abs=TOLERANCE_M,
    )


def test_ceiling_without_thickness_gets_no_body_and_carries_reason() -> None:
    ifc = to_ifc(_synthetic_model(ceiling_thickness=None))
    product = ifc.by_guid(canonical_id_to_ifc_guid("ceiling:synth"))
    assert not _has_body(product)
    properties = _adapter_pset(product)
    assert properties["Body"] == "no"
    assert "thickness" in str(properties["BodyReason"])


def test_space_without_height_gets_no_body_and_carries_reason() -> None:
    ifc = to_ifc(_synthetic_model(space_height=None))
    product = ifc.by_guid(canonical_id_to_ifc_guid("space:synth"))
    assert not _has_body(product)
    properties = _adapter_pset(product)
    assert properties["Body"] == "no"
    assert "height" in str(properties["BodyReason"])


@pytest.mark.parametrize("case_name", [*_VALID_GOLDEN_CASES, "synthetic-suite"])
def test_exports_pass_strict_express_validation(case_name: str) -> None:
    if case_name == "synthetic-suite":
        model = _synthetic_model(with_opening=True)
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


@pytest.mark.parametrize("case_name", _VALID_GOLDEN_CASES)
def test_golden_cases_round_trip_exactly(case_name: str) -> None:
    case = next(c for c in load_golden_cases(GOLDEN_ROOT) if c.name == case_name)
    model = load_golden_model(case)
    assert round_trip(model).to_dict() == model.to_dict()


def test_body_geometry_is_identical_across_two_exports() -> None:
    model = _synthetic_model(with_opening=True)
    first = to_ifc(model)
    second = to_ifc(model)
    for canonical_id in (
        "wall:synth",
        "slab:synth",
        "ceiling:synth",
        "space:synth",
        "opening:synth",
    ):
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
