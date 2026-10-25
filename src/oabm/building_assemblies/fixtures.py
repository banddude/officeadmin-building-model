"""Public-safe deterministic fixtures for the first wall producer proof."""

from __future__ import annotations

from oabm.model import (
    BuildingModel, Level, Opening, Point3, Polyline3D, Pose, Provenance,
    Quaternion, Size3, Wall,
)

from .wall import OpeningFramingSpecification, WallAssemblyRequest, build_wall_assembly

SYNTHETIC_MODEL_ID = "model:wall-assembly-two-opening-fixture"
SYNTHETIC_WALL_ID = "wall:proof"
SYNTHETIC_ASSEMBLY_ID = "assembly:wall-proof"
SYNTHETIC_DOOR_ID = "opening:proof-door"
SYNTHETIC_WINDOW_ID = "opening:proof-window"

# Explicit synthetic modeling inputs only. The 2-ply build-up, spacer, section
# depth, bearing and support counts are not engineering recommendations/sizing.
SYNTHETIC_OPENING_FRAMING_SPECS = (
    OpeningFramingSpecification(
        opening_id=SYNTHETIC_DOOR_ID,
        header_depth_m=0.1397,
        header_ply_thickness_m=0.0381,
        header_ply_count=2,
        header_spacer_thickness_m=0.0127,
        bearing_m=0.0381,
        king_count_per_side=1,
        jack_count_per_side=1,
    ),
    OpeningFramingSpecification(
        opening_id=SYNTHETIC_WINDOW_ID,
        header_depth_m=0.1397,
        header_ply_thickness_m=0.0381,
        header_ply_count=2,
        header_spacer_thickness_m=0.0127,
        bearing_m=0.0381,
        king_count_per_side=1,
        jack_count_per_side=1,
    ),
)


def synthetic_two_opening_wall_source() -> BuildingModel:
    """Return one source model with a 4.8 m wall and independent observed void facts."""
    source_kind = "synthetic_wall_assembly_fixture"
    source_id = "fixture:wall-assembly-v1"
    wall = Wall(
        id=SYNTHETIC_WALL_ID,
        name="Synthetic 4.8 m straight wall",
        level_id="level:proof",
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0),
            Point3(x=4.8, y=0.0, z=0.0),
        )),
        thickness_m=0.1143,
        height_m=2.4384,
        construction="framed",
        provenance=(Provenance(
            source_kind=source_kind,
            source_id=source_id,
            source_element_id=SYNTHETIC_WALL_ID,
            page=1,
            method="synthetic_fixture_authoring",
            confidence=1.0,
            derivation="observed",
        ),),
    )
    door = Opening(
        id=SYNTHETIC_DOOR_ID,
        name="Synthetic door void",
        host_id=SYNTHETIC_WALL_ID,
        opening_type="door",
        pose=Pose(position=Point3(x=1.35, y=0.0, z=1.016), rotation=Quaternion()),
        size=Size3(x=0.9144, y=0.1143, z=2.032),
        provenance=(Provenance(
            source_kind=source_kind,
            source_id=source_id,
            source_element_id=SYNTHETIC_DOOR_ID,
            page=1,
            method="synthetic_fixture_authoring",
            confidence=1.0,
            derivation="observed",
        ),),
    )
    window = Opening(
        id=SYNTHETIC_WINDOW_ID,
        name="Synthetic elevated window void",
        host_id=SYNTHETIC_WALL_ID,
        opening_type="window",
        pose=Pose(position=Point3(x=3.55, y=0.0, z=1.524), rotation=Quaternion()),
        size=Size3(x=1.016, y=0.1143, z=1.016),
        provenance=(Provenance(
            source_kind=source_kind,
            source_id=source_id,
            source_element_id=SYNTHETIC_WINDOW_ID,
            page=1,
            method="synthetic_fixture_authoring",
            confidence=1.0,
            derivation="observed",
        ),),
    )
    return BuildingModel(
        model_id=SYNTHETIC_MODEL_ID,
        name="Public-safe synthetic wall assembly proof",
        schema_version="1.5.0",
        levels=(Level(
            id="level:proof",
            name="Synthetic level",
            elevation_m=0.0,
            height_m=3.0,
            provenance=(Provenance(
                source_kind=source_kind, source_id=source_id,
                source_element_id="level:proof", page=1,
                method="synthetic_fixture_authoring", derivation="observed",
            ),),
        ),),
        walls=(wall,),
        openings=(door, window),
    )


def synthetic_two_opening_wall_assembly() -> BuildingModel:
    """Return the complete validated 1.6 timber framing/drywall wall model."""
    return build_wall_assembly(
        synthetic_two_opening_wall_source(),
        WallAssemblyRequest(
            assembly_id=SYNTHETIC_ASSEMBLY_ID,
            wall_id=SYNTHETIC_WALL_ID,
            selected_by="agent:synthetic-fixture-selector",
            opening_framing_specs=SYNTHETIC_OPENING_FRAMING_SPECS,
        ),
    )


__all__ = [
    "SYNTHETIC_MODEL_ID", "SYNTHETIC_WALL_ID", "SYNTHETIC_ASSEMBLY_ID",
    "SYNTHETIC_DOOR_ID", "SYNTHETIC_WINDOW_ID",
    "SYNTHETIC_OPENING_FRAMING_SPECS",
    "synthetic_two_opening_wall_source", "synthetic_two_opening_wall_assembly",
]
