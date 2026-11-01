"""Enclosed public synthetic room with butt-ended independent wall envelopes.

No joined corner framing, perimeter ceiling angle or wall-to-deck connection
is inferred. Face contact closes the room boundary; connection design is unknown.
"""
from dataclasses import replace
from oabm.model import Point3, Polyline3D, Polygon3D, Space
from .fixtures import synthetic_two_opening_wall_source, SYNTHETIC_OPENING_FRAMING_SPECS
from .ceiling_fixtures import synthetic_rectangular_ceiling_source
from . import (WallAssemblyRequest, OpeningInsertAssemblyRequest,
    SuspendedCeilingAssemblyRequest, CeilingParameterOverride, DOOR_SINGLE_LEAF_PRESET_ID,
    WINDOW_FIXED_GLAZED_PRESET_ID, build_wall_assembly,
    build_opening_insert_assembly, build_suspended_ceiling_assembly)


def synthetic_room_requests():
    selector = "agent:synthetic-room"
    return (
        WallAssemblyRequest(assembly_id="assembly:wall-proof", wall_id="wall:proof",
            selected_by=selector, opening_framing_specs=SYNTHETIC_OPENING_FRAMING_SPECS),
        *(WallAssemblyRequest(assembly_id=f"assembly:{key}", wall_id=f"wall:{key}",
            selected_by=selector) for key in ("left", "right", "back")),
        OpeningInsertAssemblyRequest(assembly_id="assembly:door-proof", opening_id="opening:proof-door",
            selected_by=selector, preset_id=DOOR_SINGLE_LEAF_PRESET_ID),
        OpeningInsertAssemblyRequest(assembly_id="assembly:window-proof", opening_id="opening:proof-window",
            selected_by=selector, preset_id=WINDOW_FIXED_GLAZED_PRESET_ID),
        SuspendedCeilingAssemblyRequest(assembly_id="assembly:ceiling-proof", ceiling_id="ceiling:proof",
            deck_id="slab:proof-deck", selected_by=selector,
            grid_origin_x_m=.1143, grid_origin_y_m=.05715, grid_axis_angle_rad=0.,
            edge_policy="cut_tiles_without_perimeter_angle_v1",
            parameter_overrides=(CeilingParameterOverride(parameter_id="ceiling.module_spacing_m",
                value_kind="length",unit="m",value=1.2192),)),
    )


def synthetic_enclosed_room_source():
    source = synthetic_two_opening_wall_source()
    wall = source.walls[0]
    extra = tuple(replace(wall, id=f"wall:{key}", name=f"Synthetic {key} wall",
        centerline=Polyline3D(points=tuple(Point3(x=x,y=y,z=0.) for x,y in points)),
        provenance=tuple(replace(row,source_element_id=f"wall:{key}") for row in wall.provenance))
        for key,points in (
            ("left", ((.05715,.05715),(.05715,3.05715))),
            ("right", ((4.74285,.05715),(4.74285,3.05715))),
            ("back", ((0.,3.1143),(4.8,3.1143))),
        ))
    ceiling = synthetic_rectangular_ceiling_source(width_m=4.5714,height_m=3.,
        origin_x_m=.1143,origin_y_m=.05715,ceiling_top_z_m=2.3,
        ceiling_thickness_m=.05,deck_top_z_m=3.,deck_thickness_m=.2)
    space = Space(id="space:room",level_id="level:proof",name="Synthetic enclosed room",
        footprint=Polygon3D(points=tuple(Point3(x=x,y=y,z=0.) for x,y in
            ((.1143,.05715),(4.6857,.05715),(4.6857,3.05715),(.1143,3.05715)))),
        height_m=2.3,provenance=tuple(replace(row,source_element_id="space:room") for row in wall.provenance))
    return replace(source,model_id="model:enclosed-room",name="Synthetic enclosed preset room",
        walls=source.walls+extra,spaces=(space,),ceilings=ceiling.ceilings,slabs=ceiling.slabs)


def synthetic_enclosed_room_assembly():
    model = synthetic_enclosed_room_source()
    for request in synthetic_room_requests():
        if isinstance(request,WallAssemblyRequest):
            model=build_wall_assembly(model,request)
        elif isinstance(request,OpeningInsertAssemblyRequest):
            model=build_opening_insert_assembly(model,request)
        else:
            model=build_suspended_ceiling_assembly(model,request)
    return model


def synthetic_room_edit_requests():
    """Explicit illustrative header/frame/tile edits; source facts stay fixed."""
    from . import CeilingParameterOverride, OpeningInsertParameterOverride
    requests = list(synthetic_room_requests())
    requests[0] = replace(requests[0], opening_framing_specs=tuple(
        replace(spec, header_depth_m=.18415) for spec in requests[0].opening_framing_specs))
    requests[4] = replace(requests[4], parameter_overrides=(OpeningInsertParameterOverride(
        parameter_id="door.frame.face_width_m", value_kind="length", unit="m", value=.0635),))
    requests[-1] = replace(requests[-1], parameter_overrides=(CeilingParameterOverride(
        parameter_id="ceiling.tile.thickness_m", value_kind="length", unit="m", value=.025),
        *requests[-1].parameter_overrides))
    return tuple(requests)
