from __future__ import annotations

from dataclasses import replace

import pytest

from oabm.drawings import (
    DrawingError,
    ElevationViewSpec,
    PlanViewSpec,
    Rect2,
    SectionViewSpec,
    Visibility,
    generate_package,
    generate_schedule,
    generate_standard_package,
    generate_view,
)
from oabm.model import (
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
    Provenance,
    Quaternion,
    Route,
    RouteConstraint,
    RouteFitting,
    Size3,
    Slab,
    Space,
    Vector3,
    Wall,
    Box3D,
)


def _poly(points):
    return Polygon3D(points=tuple(Point3(x=x, y=y, z=z) for x, y, z in points))


def _line(points):
    return Polyline3D(points=tuple(Point3(x=x, y=y, z=z) for x, y, z in points))


def _fixture_model() -> BuildingModel:
    provenance = (Provenance(source_kind="synthetic", source_id="fixture:drawings-v1"),)
    ground = Level(id="level:ground", name="Ground", elevation_m=0, height_m=3, provenance=provenance)
    upper = Level(id="level:upper", name="Upper", elevation_m=3, height_m=3, provenance=provenance)
    ground_fp = _poly(((0, 0, 0), (10, 0, 0), (10, 6, 0), (0, 6, 0)))
    upper_fp = _poly(((1, 1, 3), (9, 1, 3), (9, 5, 3), (1, 5, 3)))
    spaces = (
        Space(id="space:ground", name="Garage", level_id=ground.id, footprint=ground_fp, height_m=3, usage="garage", provenance=provenance),
        Space(id="space:upper", name="Loft", level_id=upper.id, footprint=upper_fp, height_m=None, provenance=provenance),
    )
    walls = (
        Wall(id="wall:south", level_id=ground.id, centerline=_line(((0, 0, 0), (10, 0, 0))), thickness_m=0.2, height_m=3, provenance=provenance),
        Wall(id="wall:middle", name="Middle", level_id=ground.id, centerline=_line(((0, 2, 0), (10, 2, 0))), thickness_m=0.15, height_m=3, provenance=provenance),
        Wall(id="wall:upper", level_id=upper.id, centerline=_line(((1, 1, 3), (9, 1, 3))), thickness_m=0.12, height_m=3, provenance=provenance),
    )
    slab = Slab(id="slab:ground", level_id=ground.id, footprint=ground_fp, thickness_m=0.15, provenance=provenance)
    ceiling = Ceiling(
        id="ceiling:ground",
        level_id=ground.id,
        footprint=_poly(((0, 0, 3), (10, 0, 3), (10, 6, 3), (0, 6, 3))),
        thickness_m=0.05,
        provenance=provenance,
    )
    opening = Opening(
        id="opening:door",
        name="Door",
        host_id="wall:south",
        opening_type="door",
        pose=Pose(position=Point3(x=5, y=0, z=1)),
        size=Size3(x=2, y=0.2, z=2),
        provenance=provenance,
    )
    panel = ElectricalEquipment(
        id="equip:panel",
        name="Panel LP",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=1, y=1, z=1.5)),
        level_id=ground.id,
        space_id="space:ground",
        size=Size3(x=0.4, y=0.2, z=0.8),
        rated_voltage_v=240,
        system="120/240V-1ph",
        provenance=provenance,
    )
    evse = ElectricalDevice(
        id="device:evse",
        name="EVSE",
        device_type="evse",
        pose=Pose(position=Point3(x=9, y=1, z=1.2)),
        level_id=ground.id,
        space_id="space:ground",
        size=Size3(x=0.3, y=0.2, z=0.5),
        rated_voltage_v=240,
        provenance=provenance,
    )
    unhosted = ElectricalDevice(
        id="device:unhosted",
        device_type="sensor",
        pose=Pose(position=Point3(x=2, y=4, z=0.8)),
        provenance=provenance,
    )
    ports = (
        Port(
            id="port:panel",
            owner_id=panel.id,
            domain="power",
            role="source",
            pose=Pose(position=Point3(x=1, y=1, z=1.5)),
            direction=Vector3(x=0, y=0, z=1),
            nominal_diameter_m=0.021,
            provenance=provenance,
        ),
        Port(
            id="port:evse",
            owner_id=evse.id,
            domain="power",
            role="sink",
            pose=Pose(position=Point3(x=9, y=1, z=1.2)),
            direction=Vector3(x=0, y=0, z=1),
            nominal_diameter_m=0.021,
            provenance=provenance,
        ),
    )
    route = Route(
        id="route:panel-evse",
        name="EVSE Raceway",
        route_type="emt",
        start_port_id="port:panel",
        end_port_id="port:evse",
        centerline=_line(((1, 1, 1.5), (1, 1, 2.5), (9, 1, 2.5), (9, 1, 1.2))),
        nominal_diameter_m=0.021,
        fitting_ids=("fitting:a", "fitting:b"),
        provenance=provenance,
    )
    fittings = (
        RouteFitting(
            id="fitting:b",
            route_id=route.id,
            fitting_type="elbow-90",
            pose=Pose(position=Point3(x=9, y=1, z=2.5)),
            angle_radians=1.5707963267948966,
            provenance=provenance,
        ),
        RouteFitting(
            id="fitting:a",
            route_id=route.id,
            fitting_type="elbow-90",
            pose=Pose(position=Point3(x=1, y=1, z=2.5)),
            angle_radians=1.5707963267948966,
            provenance=provenance,
        ),
    )
    obstacle = Obstacle(
        id="obstacle:beam",
        level_id=ground.id,
        geometry=Box3D(pose=Pose(position=Point3(x=5, y=3, z=2.6)), size=Size3(x=0.5, y=0.5, z=0.5)),
        provenance=provenance,
    )
    constraint = RouteConstraint(
        id="constraint:corridor",
        constraint_type="preferred-corridor",
        level_id=ground.id,
        hard=False,
        geometry=_line(((1, 1, 2.5), (9, 1, 2.5))),
        provenance=provenance,
    )
    circuit = Circuit(
        id="circuit:evse",
        source_port_id="port:panel",
        load_port_ids=("port:evse",),
        circuit_number="12",
        voltage_v=240,
        poles=2,
        route_ids=(route.id,),
        provenance=provenance,
    )
    conductor = Conductor(
        id="conductor:l1",
        circuit_id=circuit.id,
        role="line",
        material="copper",
        size="#6 AWG",
        route_ids=(route.id,),
        provenance=provenance,
    )
    return BuildingModel(
        model_id="model:drawings-fixture",
        name="Drawings fixture",
        levels=(upper, ground),
        spaces=spaces,
        walls=walls,
        slabs=(slab,),
        ceilings=(ceiling,),
        openings=(opening,),
        electrical_equipment=(panel,),
        electrical_devices=(evse, unhosted),
        ports=ports,
        obstacles=(obstacle,),
        route_constraints=(constraint,),
        routes=(route,),
        route_fittings=fittings,
        circuits=(circuit,),
        conductors=(conductor,),
        provenance=provenance,
    )


def test_plan_projects_architecture_electrical_routes_and_traceability() -> None:
    model = _fixture_model()
    view = generate_view(model, PlanViewSpec(view_id="view:ground", level_id="level:ground"))
    assert view.kind == "plan"
    assert view.metadata["canonical_level_id"] == "level:ground"
    assert view.source_model_id == model.model_id
    assert view.source_schema_version == model.schema_version

    source_ids = {source_id for element in view.elements for source_id in element.source_ids}
    assert {"space:ground", "wall:south", "opening:door", "equip:panel", "device:evse", "route:panel-evse"} <= source_ids
    assert "wall:upper" not in source_ids
    # Level-less electrical objects fall back to explicit canonical Z geometry.
    assert "device:unhosted" in source_ids

    wall = next(item for item in view.elements if item.role == "wall-centerline" and item.source_ids == ("wall:south",))
    assert [(p.x, p.y) for p in wall.points] == [(0.0, 0.0), (10.0, 0.0)]
    assert wall.line_width_m == 0.2
    assert wall.provenance[0].source_id == "fixture:drawings-v1"

    # Vertical route legs collapse to a plan point and are not emitted as bogus zero-length lines.
    route_lines = [item for item in view.elements if item.role == "route-centerline"]
    assert len(route_lines) == 1
    assert [(p.x, p.y) for p in route_lines[0].points] == [(1.0, 1.0), (9.0, 1.0)]


def test_plan_crop_clips_geometry_and_visibility_filters_categories() -> None:
    model = _fixture_model()
    visibility = Visibility(
        electrical_equipment=False,
        electrical_devices=False,
        routes=False,
        route_fittings=False,
        annotations=False,
        dimensions=False,
    )
    view = generate_view(
        model,
        PlanViewSpec(
            view_id="view:crop",
            level_id="level:ground",
            crop=Rect2(min_x=0, min_y=-1, max_x=4, max_y=4),
            visibility=visibility,
        ),
    )
    south = next(item for item in view.elements if item.role == "wall-centerline" and item.source_ids == ("wall:south",))
    assert [(p.x, p.y) for p in south.points] == [(0.0, 0.0), (4.0, 0.0)]
    assert not any(item.layer.startswith("electrical") for item in view.elements)
    assert not any(item.kind in ("label", "dimension") for item in view.elements)


def test_plan_dimensions_are_derived_annotations_not_takeoff_totals() -> None:
    view = generate_view(_fixture_model(), PlanViewSpec(view_id="view:dimensions", level_id="level:ground"))
    dimension = next(item for item in view.elements if item.role == "wall-dimension" and item.source_ids == ("wall:south",))
    assert dimension.text == "10.000 m"
    assert (dimension.start.x, dimension.end.x) == (0.0, 10.0)


def test_elevation_projects_z_and_preserves_opening_dimension() -> None:
    view = generate_view(
        _fixture_model(),
        ElevationViewSpec(view_id="view:south", direction="south", level_ids=("level:ground",)),
    )
    assert view.kind == "elevation"
    panel = next(item for item in view.elements if item.kind == "symbol" and item.source_ids == ("equip:panel",))
    assert (panel.point.x, panel.point.y) == (1.0, 1.5)
    opening_dim = next(item for item in view.elements if item.role == "opening-height-dimension")
    assert opening_dim.text == "2.000 m"
    assert abs(opening_dim.end.y - opening_dim.start.y - 2.0) < 1e-9
    # The route's rise/drop are visible in elevation even though they collapse in plan.
    assert len([item for item in view.elements if item.role == "route-centerline"]) >= 3


def test_north_elevation_flips_horizontal_axis_deterministically() -> None:
    south = generate_view(_fixture_model(), ElevationViewSpec(view_id="south", direction="south"))
    north = generate_view(_fixture_model(), ElevationViewSpec(view_id="north", direction="north"))
    south_panel = next(item for item in south.elements if item.kind == "symbol" and item.source_ids == ("equip:panel",))
    north_panel = next(item for item in north.elements if item.kind == "symbol" and item.source_ids == ("equip:panel",))
    assert south_panel.point.x == 1.0
    assert north_panel.point.x == -1.0
    assert south_panel.point.y == north_panel.point.y == 1.5


def test_section_cuts_spaces_walls_and_routes_at_explicit_plane() -> None:
    view = generate_view(
        _fixture_model(),
        SectionViewSpec(view_id="section:x5", axis="x", offset_m=5, direction="positive", depth_m=0.2),
    )
    assert view.kind == "section"
    assert view.metadata["section_plane_source"] == "explicit view specification"
    assert any(item.role.startswith("space-cut") and item.source_ids == ("space:ground",) for item in view.elements)
    wall_cuts = [item for item in view.elements if item.role == "wall-cut"]
    assert {item.source_ids[0] for item in wall_cuts} >= {"wall:south", "wall:middle"}
    assert any(item.role == "route-cut" for item in view.elements)


def test_section_with_incomplete_space_height_does_not_invent_height() -> None:
    view = generate_view(
        _fixture_model(),
        SectionViewSpec(
            view_id="section:upper",
            axis="x",
            offset_m=5,
            level_ids=("level:upper",),
        ),
    )
    loft = [item for item in view.elements if item.source_ids == ("space:upper",)]
    assert loft
    # Height is unknown, so the derived section contains the footprint cut only, not an invented extrusion.
    assert all(len(item.points) == 2 for item in loft if item.kind == "polyline")
    assert max(point.y for item in loft if item.kind == "polyline" for point in item.points) == 3.0


def test_schedules_are_sorted_traceable_and_do_not_implement_route_takeoff() -> None:
    model = _fixture_model()
    routes = generate_schedule(model, "routes")
    assert routes.columns == (
        "name",
        "route_type",
        "start_port_id",
        "end_port_id",
        "nominal_diameter_m",
        "fitting_ids",
    )
    assert "length_m" not in routes.columns
    assert routes.rows[0].source_id == "route:panel-evse"
    assert routes.rows[0].values[-1] == ["fitting:a", "fitting:b"]

    devices = generate_schedule(model, "electrical_devices", level_id="level:ground")
    assert [row.source_id for row in devices.rows] == ["device:evse"]


def test_all_standard_schedule_types_are_repeatable() -> None:
    model = _fixture_model()
    first = generate_standard_package(model).to_json(indent=None)
    second = generate_standard_package(model).to_json(indent=None)
    assert first == second
    package = generate_standard_package(
        model,
        section_specs=(SectionViewSpec(view_id="section:y1", axis="y", offset_m=1),),
    )
    serialized_view_ids = [view["view_id"] for view in package.to_dict()["views"]]
    assert serialized_view_ids == sorted(serialized_view_ids)
    assert {view.kind for view in package.views} == {"plan", "elevation", "section"}
    assert {schedule.schedule_type for schedule in package.schedules} >= {
        "levels", "spaces", "openings", "electrical_equipment", "electrical_devices", "routes", "circuits", "conductors"
    }


def test_output_is_independent_of_canonical_collection_order() -> None:
    model = _fixture_model()
    specs = (
        PlanViewSpec(view_id="view:ground", level_id="level:ground"),
        ElevationViewSpec(view_id="view:south", direction="south"),
        SectionViewSpec(view_id="view:section", axis="x", offset_m=5),
    )
    original = generate_package(model, view_specs=specs, include_standard_schedules=True).to_json(indent=None)
    reordered = replace(
        model,
        levels=tuple(reversed(model.levels)),
        spaces=tuple(reversed(model.spaces)),
        walls=tuple(reversed(model.walls)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        route_fittings=tuple(reversed(model.route_fittings)),
    )
    again = generate_package(reordered, view_specs=specs, include_standard_schedules=True).to_json(indent=None)
    assert again == original


def test_custom_symbol_and_annotation_hooks_do_not_touch_canonical_model() -> None:
    class Symbols:
        def symbol_for(self, entity):
            return f"custom::{entity.id}"

    class Labels:
        def label_for(self, entity):
            return f"LABEL::{entity.id}"

    model = _fixture_model()
    before = model.to_json(indent=None)
    view = generate_view(
        model,
        PlanViewSpec(view_id="view:hooks", level_id="level:ground"),
        symbol_provider=Symbols(),
        annotation_provider=Labels(),
    )
    panel = next(item for item in view.elements if item.kind == "symbol" and item.source_ids == ("equip:panel",))
    label = next(item for item in view.elements if item.kind == "label" and item.source_ids == ("equip:panel",))
    assert panel.symbol == "custom::equip:panel"
    assert label.text == "LABEL::equip:panel"
    assert model.to_json(indent=None) == before


def test_obstacles_and_constraints_are_opt_in_reference_layers() -> None:
    model = _fixture_model()
    default = generate_view(model, PlanViewSpec(view_id="default", level_id="level:ground"))
    assert "obstacle:beam" not in {sid for item in default.elements for sid in item.source_ids}
    visible = generate_view(
        model,
        PlanViewSpec(
            view_id="refs",
            level_id="level:ground",
            visibility=Visibility(obstacles=True, route_constraints=True),
        ),
    )
    refs = {sid for item in visible.elements for sid in item.source_ids}
    assert {"obstacle:beam", "constraint:corridor"} <= refs


def test_invalid_view_and_schedule_requests_fail_explicitly() -> None:
    model = _fixture_model()
    with pytest.raises(DrawingError, match="unknown level"):
        generate_view(model, PlanViewSpec(view_id="bad", level_id="level:missing"))
    with pytest.raises(DrawingError, match="unknown level"):
        generate_view(model, ElevationViewSpec(view_id="bad", direction="south", level_ids=("level:missing",)))
    with pytest.raises(DrawingError, match="unknown level"):
        generate_schedule(model, "spaces", level_id="level:missing")
    with pytest.raises(DrawingError, match="unsupported schedule"):
        generate_schedule(model, "quantities")
    with pytest.raises(DrawingError, match="positive width"):
        Rect2(min_x=1, min_y=0, max_x=1, max_y=2)
    with pytest.raises(DrawingError, match="depth_m"):
        SectionViewSpec(view_id="bad", axis="x", offset_m=0, depth_m=0)


def test_element_ids_and_serialization_are_stable_across_repeated_runs() -> None:
    model = _fixture_model()
    spec = PlanViewSpec(view_id="view:stable", level_id="level:ground")
    one = generate_view(model, spec)
    two = generate_view(model, spec)
    assert [item.element_id for item in one.elements] == [item.element_id for item in two.elements]
    assert one.to_json(indent=None) == two.to_json(indent=None)
    assert len({item.element_id for item in one.elements}) == len(one.elements)


def test_rotated_box_projection_is_deterministic() -> None:
    model = _fixture_model()
    half = 2 ** -0.5
    rotated_panel = replace(
        model.electrical_equipment[0],
        pose=Pose(
            position=model.electrical_equipment[0].pose.position,
            rotation=Quaternion(z=half, w=half),
        ),
    )
    rotated = replace(model, electrical_equipment=(rotated_panel,))
    one = generate_view(rotated, ElevationViewSpec(view_id="rotated", direction="south")).to_json(indent=None)
    two = generate_view(rotated, ElevationViewSpec(view_id="rotated", direction="south")).to_json(indent=None)
    assert one == two


def test_checked_in_public_synthetic_fixture_drives_standard_outputs() -> None:
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "model" / "v1" / "drawings-two-level.json"
    model = BuildingModel.load(fixture)
    package = generate_standard_package(
        model,
        section_specs=(SectionViewSpec(view_id="section:fixture", axis="x", offset_m=5),),
    )
    assert model.model_id == "model:drawings-fixture"
    assert {view.kind for view in package.views} == {"plan", "elevation", "section"}
    assert any(row.source_id == "route:panel-evse" for schedule in package.schedules for row in schedule.rows)
    assert package.to_json(indent=None) == generate_standard_package(
        BuildingModel.load(fixture),
        section_specs=(SectionViewSpec(view_id="section:fixture", axis="x", offset_m=5),),
    ).to_json(indent=None)
