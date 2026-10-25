"""Synthetic wall, insert and ceiling proof built through public producer APIs.

This is one wall and a ceiling region, not an enclosed room or a structural
assembly design. The existing deck is reused; support/connection limitations
of the explicitly chosen illustrative presets remain visible.
"""
from dataclasses import replace

from oabm.model import BuildingModel
from . import (
    DOOR_SINGLE_LEAF_PRESET_ID, WINDOW_FIXED_GLAZED_PRESET_ID,
    OpeningInsertAssemblyRequest, SuspendedCeilingAssemblyRequest,
    WallAssemblyRequest, build_opening_insert_assembly,
    build_suspended_ceiling_assembly, build_wall_assembly,
)
from .ceiling_fixtures import synthetic_rectangular_ceiling_source
from .fixtures import SYNTHETIC_OPENING_FRAMING_SPECS, synthetic_two_opening_wall_source


def synthetic_wall_inserts_ceiling_assembly() -> BuildingModel:
    """Return one source-driven wall plus door, fixed window, ceiling and deck."""
    wall_source = synthetic_two_opening_wall_source()
    # These dimensions are explicit synthetic source facts. The source ceiling
    # thickness equals the chosen fixture's tile-plus-flange down-stack.
    ceiling_source = synthetic_rectangular_ceiling_source(
        width_m=4.8, height_m=3.0, origin_y_m=0.05715,
        ceiling_top_z_m=2.3, ceiling_thickness_m=0.022225,
        deck_top_z_m=3.0, deck_thickness_m=0.2,
    )
    source = replace(
        wall_source, model_id="model:combined-assembly-proof",
        name="Synthetic wall and ceiling region with explicit opening inserts",
        ceilings=ceiling_source.ceilings, slabs=ceiling_source.slabs,
    )
    model = build_wall_assembly(source, WallAssemblyRequest(
        assembly_id="assembly:wall-proof", wall_id="wall:proof",
        selected_by="agent:synthetic-combined-fixture",
        opening_framing_specs=SYNTHETIC_OPENING_FRAMING_SPECS,
    ))
    for opening_id, assembly_id, preset_id in (
        ("opening:proof-door", "assembly:door-proof", DOOR_SINGLE_LEAF_PRESET_ID),
        ("opening:proof-window", "assembly:window-proof", WINDOW_FIXED_GLAZED_PRESET_ID),
    ):
        model = build_opening_insert_assembly(model, OpeningInsertAssemblyRequest(
            assembly_id=assembly_id, opening_id=opening_id,
            selected_by="agent:synthetic-combined-fixture", preset_id=preset_id,
        ))
    return build_suspended_ceiling_assembly(model, SuspendedCeilingAssemblyRequest(
        assembly_id="assembly:ceiling-proof", ceiling_id="ceiling:proof",
        deck_id="slab:proof-deck", selected_by="agent:synthetic-combined-fixture",
        grid_origin_x_m=0.0, grid_origin_y_m=0.05715, grid_axis_angle_rad=0.0,
        edge_policy="cut_tiles_without_perimeter_angle_v1",
    ))
