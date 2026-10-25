"""Public-safe deterministic fixtures for the first suspended-ceiling proof."""

from __future__ import annotations

import math

from oabm.model import (
    BuildingModel,
    Ceiling,
    Level,
    Point3,
    Polygon3D,
    Provenance,
    Slab,
)

from .suspended_ceiling import (
    SuspendedCeilingAssemblyRequest,
    build_suspended_ceiling_assembly,
)


SYNTHETIC_CEILING_MODEL_ID = "model:ceiling-assembly-rectangular-proof"
SYNTHETIC_CEILING_ID = "ceiling:proof"
SYNTHETIC_DECK_ID = "slab:proof-deck"
SYNTHETIC_CEILING_ASSEMBLY_ID = "assembly:ceiling-proof"
SYNTHETIC_CEILING_GRID_ORIGIN_X_M = 0.0
SYNTHETIC_CEILING_GRID_ORIGIN_Y_M = 0.0
SYNTHETIC_CEILING_GRID_ANGLE_RAD = 0.0
SYNTHETIC_CEILING_EDGE_POLICY = "cut_tiles_without_perimeter_angle_v1"


def synthetic_rectangular_ceiling_source(
    *,
    ceiling_thickness_m: float | None = 0.05,
    origin_x_m: float = 0.0,
    origin_y_m: float = 0.0,
    width_m: float = 4.0,
    height_m: float = 3.0,
    rotation_rad: float = 0.0,
    ceiling_top_z_m: float = 2.0,
    deck_top_z_m: float = 3.0,
    deck_thickness_m: float = 0.2,
    clockwise: bool = False,
) -> BuildingModel:
    """Return a synthetic source Ceiling plus exactly one existing Slab deck."""
    cosine, sine = math.cos(rotation_rad), math.sin(rotation_rad)

    def xy(local_x: float, local_y: float) -> tuple[float, float]:
        return (
            origin_x_m + local_x * cosine - local_y * sine,
            origin_y_m + local_x * sine + local_y * cosine,
        )

    local_corners = ((0.0, 0.0), (width_m, 0.0), (width_m, height_m), (0.0, height_m))
    if clockwise:
        local_corners = (local_corners[0], local_corners[3], local_corners[2], local_corners[1])
    source_id = "fixture:suspended-ceiling-v1"
    ceiling_provenance = (Provenance(
        source_kind="synthetic_suspended_ceiling_fixture",
        source_id=source_id,
        source_element_id=SYNTHETIC_CEILING_ID,
        page=1,
        method="synthetic_fixture_authoring",
        confidence=1.0,
        derivation="observed",
    ),)
    slab_provenance = (Provenance(
        source_kind="synthetic_suspended_ceiling_fixture",
        source_id=source_id,
        source_element_id=SYNTHETIC_DECK_ID,
        page=1,
        method="synthetic_fixture_authoring",
        confidence=1.0,
        derivation="observed",
    ),)
    ceiling_points = tuple(
        Point3(x=x, y=y, z=ceiling_top_z_m) for x, y in (xy(*corner) for corner in local_corners)
    )
    deck_points = tuple(
        Point3(x=x, y=y, z=deck_top_z_m) for x, y in (xy(*corner) for corner in local_corners)
    )
    return BuildingModel(
        model_id=SYNTHETIC_CEILING_MODEL_ID,
        name="Public-safe synthetic suspended ceiling with one explicit Slab deck",
        schema_version="1.5.0",
        levels=(Level(
            id="level:proof",
            name="Synthetic level",
            elevation_m=0.0,
            height_m=3.5,
            provenance=(Provenance(
                source_kind="synthetic_suspended_ceiling_fixture",
                source_id=source_id,
                source_element_id="level:proof",
                page=1,
                method="synthetic_fixture_authoring",
                confidence=1.0,
                derivation="observed",
            ),),
        ),),
        ceilings=(Ceiling(
            id=SYNTHETIC_CEILING_ID,
            name="Synthetic rectangular nominal 24 in / 609.6 mm grid host",
            level_id="level:proof",
            footprint=Polygon3D(points=ceiling_points),
            thickness_m=ceiling_thickness_m,
            provenance=ceiling_provenance,
        ),),
        slabs=(Slab(
            id=SYNTHETIC_DECK_ID,
            name="Synthetic existing structural deck slab",
            level_id="level:proof",
            footprint=Polygon3D(points=deck_points),
            thickness_m=deck_thickness_m,
            provenance=slab_provenance,
        ),),
    )


def synthetic_rectangular_suspended_ceiling_assembly(
    *,
    ceiling_thickness_m: float | None = 0.05,
    origin_x_m: float = 0.0,
    origin_y_m: float = 0.0,
    rotation_rad: float = 0.0,
    clockwise: bool = False,
) -> BuildingModel:
    """Build the repeatable synthetic model used for consumer proofs."""
    source = synthetic_rectangular_ceiling_source(
        ceiling_thickness_m=ceiling_thickness_m,
        origin_x_m=origin_x_m,
        origin_y_m=origin_y_m,
        rotation_rad=rotation_rad,
        clockwise=clockwise,
    )
    first = source.ceilings[0].footprint.points[0]
    return build_suspended_ceiling_assembly(
        source,
        SuspendedCeilingAssemblyRequest(
            assembly_id=SYNTHETIC_CEILING_ASSEMBLY_ID,
            ceiling_id=SYNTHETIC_CEILING_ID,
            deck_id=SYNTHETIC_DECK_ID,
            selected_by="agent:synthetic-ceiling-fixture-selector",
            grid_origin_x_m=first.x,
            grid_origin_y_m=first.y,
            grid_axis_angle_rad=rotation_rad,
            edge_policy=SYNTHETIC_CEILING_EDGE_POLICY,
        ),
    )


__all__ = [
    "SYNTHETIC_CEILING_MODEL_ID", "SYNTHETIC_CEILING_ID", "SYNTHETIC_DECK_ID",
    "SYNTHETIC_CEILING_ASSEMBLY_ID", "SYNTHETIC_CEILING_GRID_ORIGIN_X_M",
    "SYNTHETIC_CEILING_GRID_ORIGIN_Y_M", "SYNTHETIC_CEILING_GRID_ANGLE_RAD",
    "SYNTHETIC_CEILING_EDGE_POLICY", "synthetic_rectangular_ceiling_source",
    "synthetic_rectangular_suspended_ceiling_assembly",
]
