"""Small hand-built 1.6 fixtures for bounded assembly-aware routing (#356)."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from oabm.model import (
    AssemblyComponent,
    Box3D,
    BuildingAssembly,
    BuildingModel,
    Ceiling,
    ContractError,
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    ElectricalDevice,
    ElectricalEquipment,
    HangerAttachment,
    Level,
    PanelGeometry,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Port,
    Profile2D,
    ProfileExtrusion,
    Provenance,
    Quaternion,
    ResolvedParameter,
    Size3,
    Slab,
    Vector3,
    Wall,
    WallAssemblyFrame,
    WireSweep,
    compute_assembly_input_digest,
    compute_assembly_output_digest,
    stable_assembly_component_id,
    upgrade_to_v1_6,
)
from oabm.routing import (
    AllowedRouteDomain,
    BundleHints,
    CircuitDesignRules,
    NoRouteError,
    PlacementError,
    RoutingError,
    RoutingOptions,
    design_proposed_circuits,
    route_between_ports,
)


def _box(minimum, maximum, rotation: Quaternion = Quaternion()) -> Box3D:
    return Box3D(
        pose=Pose(position=Point3(
            x=(minimum[0] + maximum[0]) / 2,
            y=(minimum[1] + maximum[1]) / 2,
            z=(minimum[2] + maximum[2]) / 2,
        ), rotation=rotation),
        size=Size3(
            x=maximum[0] - minimum[0],
            y=maximum[1] - minimum[1],
            z=maximum[2] - minimum[2],
        ),
    )


def _domain(minimum=(0.2, -0.8, 2.5), maximum=(3.8, 0.8, 2.75)) -> AllowedRouteDomain:
    return AllowedRouteDomain(
        id="domain:synthetic-plenum",
        kind="caller-selected-plenum",
        volumes=(_box(minimum, maximum),),
        # Descriptive source references only; this does not authorize a hole.
        source_ids=("assembly:ceiling", "slab:deck"),
    )


def _observed(source_element_id: str) -> Provenance:
    return Provenance(
        source_kind="synthetic-fixture",
        source_id="fixture:public-safe",
        source_element_id=source_element_id,
        method="hand-authored-canonical-fixture",
        derivation=DERIVATION_OBSERVED,
    )


def _component(
    assembly_id: str,
    part_key: str,
    component_kind: str,
    geometry,
    *,
    hanger_attachment: HangerAttachment | None = None,
) -> AssemblyComponent:
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id,
        part_key=part_key,
        level_id="level:main",
        component_kind=component_kind,
        geometry=geometry,
        geometry_basis="specified_parameterized_geometry",
        material_id=None,
        material_basis="unknown",
        hanger_attachment=hanger_attachment,
        provenance=(Provenance(
            source_kind="selected-preset",
            source_id="preset:fixture",
            method="manual-fixture-component",
            derivation=DERIVATION_INFERRED,
            scope_paths=("geometry",),
        ),),
    )


def _ceiling_fixture(
    *,
    ports=(
        Point3(x=0.5, y=0.0, z=2.6),
        Point3(x=3.5, y=0.0, z=2.6),
    ),
    directions=(Vector3(x=1, y=0, z=0), Vector3(x=-1, y=0, z=0)),
    port_diameter_m: float | None = 0.02,
    include_hanger: bool = True,
    include_panel: bool = True,
    include_blocking_tile: bool = False,
    profile_runner: bool = False,
) -> BuildingModel:
    """Manually create a validated ceiling, runner, optional hanger/deck model."""
    level = Level(id="level:main", elevation_m=0.0, height_m=3.2)
    ceiling = Ceiling(
        id="ceiling:room",
        level_id=level.id,
        footprint=Polygon3D(points=(
            Point3(x=0, y=-1, z=2), Point3(x=4, y=-1, z=2),
            Point3(x=4, y=1, z=2), Point3(x=0, y=1, z=2),
        )),
        thickness_m=0.04,
        provenance=(_observed("ceiling:room"),),
    )
    slab = Slab(
        id="slab:deck",
        level_id=level.id,
        footprint=Polygon3D(points=(
            Point3(x=0, y=-1, z=3), Point3(x=4, y=-1, z=3),
            Point3(x=4, y=1, z=3), Point3(x=0, y=1, z=3),
        )),
        thickness_m=0.2,
        provenance=(_observed("slab:deck"),),
    )
    equipment = ElectricalEquipment(
        id="equipment:source", equipment_type="panelboard",
        pose=Pose(position=ports[0]), level_id=level.id,
    )
    device = ElectricalDevice(
        id="device:load", device_type="luminaire",
        pose=Pose(position=ports[1]), level_id=level.id,
    )
    port_records = (
        # Port locations are supplied facts for this routing fixture; the
        # router does not infer a box/wall/deck exit from them.
        Port(
            id="port:source", owner_id=equipment.id, domain="electrical", role="source",
            pose=Pose(position=ports[0]), direction=directions[0],
            nominal_diameter_m=port_diameter_m,
        ),
        Port(
            id="port:load", owner_id=device.id, domain="electrical", role="load",
            pose=Pose(position=ports[1]), direction=directions[1],
            nominal_diameter_m=port_diameter_m,
        ),
    )
    source = BuildingModel(
        model_id="model:assembly-route-fixture",
        schema_version="1.5.0",
        levels=(level,),
        ceilings=(ceiling,),
        slabs=(slab,),
        electrical_equipment=(equipment,),
        electrical_devices=(device,),
        ports=port_records,
    )

    assignment_id = "assembly:ceiling"
    runner_id = stable_assembly_component_id(assignment_id, "runner:main")
    if profile_runner:
        runner_geometry = ProfileExtrusion(
            profile=Profile2D(
                kind="c_profile", overall_width_m=0.1, overall_depth_m=0.1,
                web_thickness_m=0.02, flange_thickness_m=0.02, return_lip_m=0.01,
            ),
            pose=Pose(
                position=Point3(x=2, y=0.4, z=2.6),
                rotation=Quaternion(x=math.sin(math.pi / 4), w=math.cos(math.pi / 4)),
            ),
            length_m=0.8,
        )
    else:
        runner_geometry = Box3D(
            pose=Pose(position=Point3(x=2, y=0, z=2.4)),
            size=Size3(x=3.6, y=0.04, z=0.04),
        )
    components = [
        _component(assignment_id, "runner:main", "tbar_main_runner", runner_geometry),
    ]
    if include_hanger:
        components.append(_component(
            assignment_id,
            "hanger:wire-01",
            "hanger_wire",
            WireSweep(
                centerline=Polyline3D(points=(
                    Point3(x=2, y=0, z=2.8), Point3(x=2, y=0, z=2.42),
                )),
                diameter_m=0.01,
                end_treatment="cut",
            ),
            hanger_attachment=HangerAttachment(
                top_host_id=slab.id,
                top_anchor=Point3(x=2, y=0, z=2.8),
                bottom_runner_id=runner_id,
                bottom_anchor=Point3(x=2, y=0, z=2.42),
            ),
        ))
    if include_panel:
        components.append(_component(
            assignment_id,
            "tile:synthetic-panel",
            "tile",
            PanelGeometry(
                boundary=Polygon3D(points=(
                    Point3(x=1.2, y=-0.18, z=2.58),
                    Point3(x=1.4, y=-0.18, z=2.58),
                    Point3(x=1.4, y=0.18, z=2.58),
                    Point3(x=1.2, y=0.18, z=2.58),
                )),
                thickness_m=0.04,
                normal=Vector3(x=0, y=0, z=1),
            ),
        ))
    if include_blocking_tile:
        components.append(_component(
            assignment_id,
            "tile:blocking-baffle",
            "tile",
            Box3D(
                pose=Pose(position=Point3(x=2, y=0, z=2.6)),
                size=Size3(x=0.2, y=0.92, z=0.24),
            ),
        ))
    components = tuple(components)

    selection_ref = json.dumps(
        ("catalog:fixture", "r1", "preset:ceiling-fixture", "1.0.0"),
        separators=(",", ":"),
    )
    assignment_provenance = [Provenance(
        source_kind="selection", source_id="agent:fixture", source_element_id=selection_ref,
        method="preset-selection", derivation=DERIVATION_USER,
        scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by"),
    )]
    assignment_provenance.append(Provenance(
        source_kind="synthetic-fixture", source_id="fixture:public-safe",
        source_element_id="ceiling:room", method="hand-authored-canonical-fixture",
        derivation=DERIVATION_OBSERVED, scope_paths=("host_id", "input_digest"),
    ))
    assignment_provenance.append(Provenance(
        source_kind="synthetic-fixture", source_id="fixture:public-safe",
        source_element_id="slab:deck", method="hand-authored-canonical-fixture",
        derivation=DERIVATION_OBSERVED, scope_paths=("deck_id", "input_digest"),
    ))
    assignment = BuildingAssembly(
        id=assignment_id,
        assembly_kind="suspended_ceiling",
        level_id=level.id,
        host_id=ceiling.id,
        host_representation="aggregate_host",
        catalog_id="catalog:fixture",
        catalog_revision="r1",
        preset_id="preset:ceiling-fixture",
        preset_revision="1.0.0",
        selected_by="agent:fixture",
        generator_version="manual-fixture-v1",
        resolved_parameters=(),
        component_ids=tuple(row.id for row in components),
        input_digest="0" * 64,
        output_digest="0" * 64,
        deck_id=slab.id,
        provenance=tuple(assignment_provenance),
    )
    assignment = replace(
        assignment,
        input_digest=compute_assembly_input_digest(source, assignment),
        output_digest=compute_assembly_output_digest(components),
    )
    return replace(
        upgrade_to_v1_6(source),
        building_assemblies=(assignment,),
        assembly_components=components,
    )


def _wall_fixture(*, source_ports=(0.5, 3.5), directions=(
    Vector3(x=1, y=0, z=0), Vector3(x=-1, y=0, z=0),
)) -> BuildingModel:
    level = Level(id="level:main", elevation_m=0.0, height_m=3.0)
    wall = Wall(
        id="wall:cavity", level_id=level.id,
        centerline=Polyline3D(points=(
            Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0),
        )),
        thickness_m=0.2, height_m=3.0, construction="framed",
        provenance=(_observed("wall:cavity"),),
    )
    equipment = ElectricalEquipment(
        id="equipment:source", equipment_type="panelboard",
        pose=Pose(position=Point3(x=source_ports[0], y=0, z=1.5)), level_id=level.id,
    )
    device = ElectricalDevice(
        id="device:load", device_type="luminaire",
        pose=Pose(position=Point3(x=source_ports[1], y=0, z=1.5)), level_id=level.id,
    )
    ports = (
        Port(id="port:source", owner_id=equipment.id, domain="electrical", role="source",
             pose=Pose(position=equipment.pose.position), direction=directions[0], nominal_diameter_m=0.02),
        Port(id="port:load", owner_id=device.id, domain="electrical", role="load",
             pose=Pose(position=device.pose.position), direction=directions[1], nominal_diameter_m=0.02),
    )
    source = BuildingModel(
        model_id="model:wall-cavity-fixture", schema_version="1.5.0",
        levels=(level,), walls=(wall,), electrical_equipment=(equipment,),
        electrical_devices=(device,), ports=ports,
    )
    assignment_id = "assembly:wall"
    parts = [
        _component(
            assignment_id, "stud:station-2", "stud",
            Box3D(
                pose=Pose(position=Point3(x=2, y=0, z=1.5)),
                size=Size3(x=0.1, y=0.2, z=3),
            ),
        ),
    ]
    for part_key, y, normal_y in (("drywall:left", -0.0725, -1), ("drywall:right", 0.0725, 1)):
        parts.append(_component(
            assignment_id, part_key, "drywall_panel",
            PanelGeometry(
                boundary=Polygon3D(points=(
                    Point3(x=0, y=y, z=0), Point3(x=4, y=y, z=0),
                    Point3(x=4, y=y, z=3), Point3(x=0, y=y, z=3),
                )),
                thickness_m=0.055,
                normal=Vector3(x=0, y=normal_y, z=0),
            ),
        ))
    components = tuple(parts)
    selection_ref = json.dumps(
        ("catalog:fixture", "r1", "preset:wall-cavity", "1.0.0"),
        separators=(",", ":"),
    )
    rows = [Provenance(
        source_kind="selection", source_id="agent:fixture", source_element_id=selection_ref,
        method="preset-selection", derivation=DERIVATION_USER,
        scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by"),
    ), Provenance(
        source_kind="synthetic-fixture", source_id="fixture:public-safe",
        source_element_id="wall:cavity", method="hand-authored-canonical-fixture",
        derivation=DERIVATION_OBSERVED, scope_paths=("host_id", "input_digest"),
    )]
    preset_id = "preset:wall-cavity"
    parameters = tuple(
        ResolvedParameter(
            parameter_id=parameter_id,
            value_kind="length", value=value, unit="m", origin="preset_default",
            provenance=(Provenance(
                source_kind="preset_parameter", source_id="catalog:fixture",
                source_element_id=json.dumps(("r1", preset_id, "1.0.0", parameter_id), separators=(",", ":")),
                method="parameter-resolution", derivation=DERIVATION_INFERRED,
                scope_paths=("value", "unit", "origin"),
            ),),
            source_entity_id=None, source_field_path=None,
        )
        for parameter_id, value in (
            ("wall.stack.framing_depth_m", 0.09),
            ("wall.stack.face_left.gypsum", 0.055),
            ("wall.stack.face_right.gypsum", 0.055),
        )
    )
    assignment = BuildingAssembly(
        id=assignment_id, assembly_kind="wall", level_id=level.id,
        host_id=wall.id, host_representation="aggregate_host",
        catalog_id="catalog:fixture", catalog_revision="r1",
        preset_id=preset_id, preset_revision="1.0.0", selected_by="agent:fixture",
        generator_version="manual-fixture-v1", resolved_parameters=parameters,
        component_ids=tuple(row.id for row in components), input_digest="0" * 64,
        output_digest="0" * 64,
        wall_frame=WallAssemblyFrame(
            origin=Point3(x=0, y=0, z=0), axis=Vector3(x=1, y=0, z=0),
            left_normal=Vector3(x=0, y=1, z=0), base_z_m=0.0,
        ),
        provenance=tuple(rows),
    )
    assignment = replace(
        assignment,
        input_digest=compute_assembly_input_digest(source, assignment),
        output_digest=compute_assembly_output_digest(components),
    )
    return replace(
        upgrade_to_v1_6(source), building_assemblies=(assignment,),
        assembly_components=components,
    )


def _refresh_assembly_components(model: BuildingModel, components: tuple[AssemblyComponent, ...]):
    source = replace(model, building_assemblies=(), assembly_components=(), routes=(),
                     route_fittings=(), circuits=(), conductors=())
    assignment = replace(
        model.building_assemblies[0],
        component_ids=tuple(row.id for row in components),
        input_digest="0" * 64,
        output_digest="0" * 64,
    )
    assignment = replace(
        assignment,
        input_digest=compute_assembly_input_digest(source, assignment),
        output_digest=compute_assembly_output_digest(components),
    )
    return replace(
        model,
        routes=(), route_fittings=(), circuits=(), conductors=(),
        building_assemblies=(assignment,), assembly_components=components,
    )


def test_ceiling_route_uses_runner_hanger_panel_and_reused_slab_with_repeatable_receipts():
    model = _ceiling_fixture()
    allowed = _domain()
    options = RoutingOptions(port_stub_m=0.15, clearance_m=0.01)
    first = route_between_ports(
        model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, options=options, allowed_domain=allowed,
    )
    second = route_between_ports(
        model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, options=options, allowed_domain=allowed,
    )
    route, fittings = first
    assert first == second

    runner = next(row for row in model.assembly_components if row.component_kind == "tbar_main_runner")
    wire = next(row for row in model.assembly_components if row.component_kind == "hanger_wire")
    panel = next(row for row in model.assembly_components if isinstance(row.geometry, PanelGeometry))
    assert all(point.z > runner.geometry.pose.position.z + runner.geometry.size.z / 2 for point in route.centerline.points)
    assert all(point.z < 3.0 - 0.2 - 0.02 for point in route.centerline.points)
    assert any(abs(point.y) > 0.025 for point in route.centerline.points), "route should detour around hanger wire"
    context = route.provenance[0].attributes["bounded_routing_context"]
    assembly_context = context["assembly_routing_context"]
    assert assembly_context["version"] == "assembly-routing-context-v1"
    assert assembly_context["checked_component_ids"] == sorted(row.id for row in model.assembly_components)
    assert assembly_context["checked_deck_ids"] == ["slab:deck"]
    assert assembly_context["checked_deck_kinds"] == [{"deck_id": "slab:deck", "kind": "existing_slab"}]
    check = route.attributes["geometry_check"]
    assert set(check["checked_blocker_ids"]) == {
        row.id for row in model.assembly_components
    } | {"slab:deck"}
    assert check["checked_blocker_count"] == len(check["checked_blocker_ids"])
    assert check["checked_blocker_ids_truncated"] is False
    assert check["assembly_routing_context"] == assembly_context
    assert "SelectedSlabDeck" in check["checked_blocker_classes"]
    assert "AssemblyComponentWire" in check["checked_blocker_classes"]
    assert "AssemblyComponentPanel" in check["checked_blocker_classes"]
    # The ceiling host and its legacy preference are not emitted as duplicate blockers.
    assert "ceiling:ceiling:room" not in check["checked_blocker_ids"]
    assert check["envelope_scope"] == "nominal_radius_only; actual_outer_diameter_not_verified"
    assert check["code_review"] == "not_evaluated"
    assert not fittings or all(row.route_id == route.id for row in fittings)
    from oabm.routing.router import _collect_routing_geometry
    geometry = _collect_routing_geometry(model, "emt", 0.01, options, allowed_domain=allowed)
    assert not any(row.id == "ceiling:ceiling:room" for row in geometry.surfaces)


def test_assembly_routing_requires_bounded_positive_diameter_and_rejects_endpoint_or_stub_escape():
    model = _ceiling_fixture(port_diameter_m=None, include_panel=False)
    allowed = _domain()
    with pytest.raises(RoutingError, match="requires an explicit AllowedRouteDomain"):
        route_between_ports(model, "port:source", "port:load", "emt")
    with pytest.raises(RoutingError, match="known positive nominal diameter"):
        route_between_ports(model, "port:source", "port:load", "emt", allowed_domain=allowed)
    with pytest.raises(RoutingError, match="finite and > 0"):
        route_between_ports(
            model, "port:source", "port:load", "emt",
            nominal_diameter_m=0, allowed_domain=allowed,
        )

    moved = replace(
        model,
        ports=(replace(model.ports[0], pose=Pose(position=Point3(x=0.05, y=0, z=2.6))), model.ports[1]),
    )
    with pytest.raises(NoRouteError) as endpoint:
        route_between_ports(
            moved, "port:source", "port:load", "emt",
            nominal_diameter_m=0.02, allowed_domain=allowed,
        )
    assert endpoint.value.reason == "start_outside_allowed_domain"

    stub = replace(
        model,
        ports=(replace(
            model.ports[0],
            pose=Pose(position=Point3(x=0.3, y=0, z=2.6)),
            direction=Vector3(x=-1, y=0, z=0),
        ), model.ports[1]),
    )
    with pytest.raises(NoRouteError) as stub_error:
        route_between_ports(
            stub, "port:source", "port:load", "emt",
            nominal_diameter_m=0.02, allowed_domain=allowed,
        )
    assert stub_error.value.reason == "start_stub_outside_allowed_domain"


def test_impossible_blocked_plenum_reports_the_component_that_closes_the_domain():
    model = _ceiling_fixture(include_blocking_tile=True)
    with pytest.raises(NoRouteError) as caught:
        route_between_ports(
            model, "port:source", "port:load", "emt",
            nominal_diameter_m=0.02,
            options=RoutingOptions(clearance_m=0.01),
            allowed_domain=_domain(minimum=(0.2, -0.4, 2.5), maximum=(3.8, 0.4, 2.75)),
        )
    blocker = next(row for row in model.assembly_components if row.part_key == "tile:blocking-baffle")
    assert caught.value.reason == "no_path_within_allowed_domain"
    assert blocker.id in caught.value.blocker_ids


def test_panel_thickness_and_route_radius_clearance_force_a_vertical_detour():
    model = _ceiling_fixture(include_hanger=False, include_panel=False)
    panel = _component(
        "assembly:ceiling", "tile:full-plenum-panel", "tile",
        PanelGeometry(
            boundary=Polygon3D(points=(
                Point3(x=1.2, y=-0.2, z=2.57),
                Point3(x=2.8, y=-0.2, z=2.57),
                Point3(x=2.8, y=0.2, z=2.57),
                Point3(x=1.2, y=0.2, z=2.57),
            )),
            thickness_m=0.04,
            normal=Vector3(x=0, y=0, z=1),
        ),
    )
    components = (*model.assembly_components, panel)
    model = _refresh_assembly_components(model, components)
    allowed = _domain(minimum=(0.2, -0.1, 2.5), maximum=(3.8, 0.1, 2.75))
    route, _ = route_between_ports(
        model, "port:source", "port:load", "emt", nominal_diameter_m=0.02,
        options=RoutingOptions(clearance_m=0.01), allowed_domain=allowed,
    )
    assert any(point.z > 2.61 for point in route.centerline.points)
    assert panel.id in route.attributes["geometry_check"]["checked_blocker_ids"]


def test_selected_slab_blocks_from_its_top_face_downward_by_canonical_thickness():
    below_top = _ceiling_fixture(
        ports=(Point3(x=0.5, y=0, z=2.85), Point3(x=3.5, y=0, z=2.85)),
        include_hanger=False, include_panel=False,
    )
    allowed = _domain(minimum=(0.2, -0.8, 2.7), maximum=(3.8, 0.8, 3.2))
    with pytest.raises(NoRouteError) as inside_slab:
        route_between_ports(
            below_top, "port:source", "port:load", "emt",
            nominal_diameter_m=0.02, allowed_domain=allowed,
        )
    assert inside_slab.value.reason == "start_endpoint_inside_hard_blocker"
    assert inside_slab.value.blocker_ids == ("slab:deck",)

    above_top = _ceiling_fixture(
        ports=(Point3(x=0.5, y=0, z=3.1), Point3(x=3.5, y=0, z=3.1)),
        include_hanger=False, include_panel=False,
    )
    route, _ = route_between_ports(
        above_top, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, allowed_domain=allowed,
    )
    assert all(abs(point.z - 3.1) < 1e-9 for point in route.centerline.points)


def test_wall_cavity_does_not_route_through_stud_and_reports_stub_collision():
    model = _wall_fixture()
    domain = AllowedRouteDomain(
        id="domain:wall-cavity", kind="caller-selected-wall-cavity",
        volumes=(_box((0.2, -0.04, 0.1), (3.8, 0.04, 2.9)),),
        source_ids=("assembly:wall",),
    )
    stud = next(row for row in model.assembly_components if row.component_kind == "stud")
    with pytest.raises(NoRouteError) as across_stud:
        route_between_ports(
            model, "port:source", "port:load", "emt",
            options=RoutingOptions(clearance_m=0.005), allowed_domain=domain,
        )
    assert across_stud.value.reason == "no_path_within_allowed_domain"
    assert stud.id in across_stud.value.blocker_ids

    near_stud = _wall_fixture(source_ports=(1.9, 3.5))
    with pytest.raises(NoRouteError) as stub:
        route_between_ports(
            near_stud, "port:source", "port:load", "emt",
            options=RoutingOptions(clearance_m=0.005), allowed_domain=domain,
        )
    assert stub.value.reason == "start_stub_intersects_hard_blocker"
    near_stud_id = next(row.id for row in near_stud.assembly_components if row.component_kind == "stud")
    assert near_stud_id in stub.value.blocker_ids


def test_unassigned_aggregate_hosts_are_refused_instead_of_claimed_as_checked():
    model = _ceiling_fixture()
    unassigned = Wall(
        id="wall:unassigned", level_id="level:main",
        centerline=Polyline3D(points=(Point3(x=0, y=-0.8, z=0), Point3(x=4, y=-0.8, z=0))),
        thickness_m=0.1, height_m=3.0, construction="framed",
        provenance=(_observed("wall:unassigned"),),
    )
    model = replace(model, walls=(unassigned,))
    with pytest.raises(RoutingError, match="unassigned aggregate hosts.*wall:wall:unassigned"):
        route_between_ports(model, "port:source", "port:load", "emt", allowed_domain=_domain())


def test_rotated_box_uses_exact_obb_and_rotated_profile_void_is_conservatively_blocked():
    yaw = Quaternion(z=math.sin(math.pi / 8), w=math.cos(math.pi / 8))
    rotated_box = _component(
        "assembly:ceiling", "runner:rotated-box", "tbar_cross_runner",
        Box3D(
            pose=Pose(position=Point3(x=2, y=0, z=2.6), rotation=yaw),
            size=Size3(x=2, y=0.1, z=0.1),
        ),
    )
    obb_model = _ceiling_fixture(
        ports=(Point3(x=1.3, y=0.7, z=2.6), Point3(x=1.7, y=0.7, z=2.6)),
        directions=(Vector3(x=1, y=0, z=0), Vector3(x=-1, y=0, z=0)),
        include_hanger=False, include_panel=False,
    )
    obb_model = _refresh_assembly_components(obb_model, (rotated_box,))
    allowed = _domain(minimum=(1.2, 0.6, 2.5), maximum=(1.8, 0.8, 2.7))
    route, _ = route_between_ports(
        obb_model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, options=RoutingOptions(clearance_m=0.01), allowed_domain=allowed,
    )
    assert all(abs(point.y - 0.7) < 1e-9 for point in route.centerline.points)
    assert all(abs(point.z - 2.6) < 1e-9 for point in route.centerline.points)
    assert rotated_box.id in route.attributes["geometry_check"]["checked_blocker_ids"]

    profile_model = _ceiling_fixture(
        ports=(Point3(x=2.05, y=0.25, z=2.65), Point3(x=2.05, y=-0.25, z=2.65)),
        directions=(Vector3(x=0, y=-1, z=0), Vector3(x=0, y=1, z=0)),
        include_hanger=False, include_panel=False, profile_runner=True,
    )
    profile = profile_model.assembly_components[0]
    profile_domain = _domain(
        minimum=(1.995, -0.3, 2.6), maximum=(2.105, 0.3, 2.7),
    )
    with pytest.raises(NoRouteError) as profile_error:
        route_between_ports(
            profile_model, "port:source", "port:load", "emt",
            nominal_diameter_m=0.02, options=RoutingOptions(clearance_m=0.01),
            allowed_domain=profile_domain,
        )
    assert profile.id in profile_error.value.blocker_ids


def test_assembly_obb_normalizes_tolerated_quaternion_before_full_route_collision_check():
    # Canonical 1.6 geometry treats this near-unit quaternion as its normalized
    # yaw-180 frame. The route is 0.5 µm inside the required swept-clearance
    # envelope of the box's +X face and must not be certified clear.
    rotation = Quaternion(
        z=math.sin(math.pi / 2) * 1.000004,
        w=math.cos(math.pi / 2) * 1.000004,
    )
    box = _component(
        "assembly:ceiling", "runner:near-unit-quaternion", "tbar_cross_runner",
        Box3D(
            pose=Pose(position=Point3(x=2, y=0, z=2.6), rotation=rotation),
            size=Size3(x=0.2, y=2.0, z=0.2),
        ),
    )
    source_x = 2.1009995
    model = _ceiling_fixture(
        ports=(Point3(x=source_x, y=-1.2, z=2.6), Point3(x=source_x, y=1.2, z=2.6)),
        directions=(Vector3(x=0, y=1, z=0), Vector3(x=0, y=-1, z=0)),
        include_hanger=False, include_panel=False,
    )
    model = _refresh_assembly_components(model, (box,))
    domain = _domain(
        minimum=(source_x - 0.001, -1.3, 2.599),
        maximum=(source_x + 0.001, 1.3, 2.601),
    )

    with pytest.raises(NoRouteError) as collision:
        route_between_ports(
            model, "port:source", "port:load", "emt",
            nominal_diameter_m=0.0002,
            options=RoutingOptions(clearance_m=0.0009),
            allowed_domain=domain,
        )
    assert collision.value.reason == "no_path_within_allowed_domain"
    assert box.id in collision.value.blocker_ids
    # The collision adapter is query-only and does not normalize source data.
    assert model.assembly_components[0].geometry.pose.rotation == rotation
    assert math.hypot(rotation.x, rotation.y, rotation.z, rotation.w) == pytest.approx(1.000004)


def test_stale_assembly_route_receipt_fails_model_update_and_bounded_design_reuse():
    model = _ceiling_fixture(include_panel=False)
    allowed = _domain()
    route, fittings = route_between_ports(
        model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, allowed_domain=allowed,
    )
    attached = replace(model, routes=(route,), route_fittings=fittings)
    runner = next(row for row in model.assembly_components if row.component_kind == "tbar_main_runner")
    shifted = replace(
        runner,
        geometry=replace(
            runner.geometry,
            pose=Pose(position=Point3(x=2, y=0.02, z=2.4)),
        ),
    )
    updated = _refresh_assembly_components(
        model, tuple(shifted if row.id == runner.id else row for row in model.assembly_components),
    )
    with pytest.raises(ContractError, match="stale assembly routing context"):
        replace(updated, routes=attached.routes, route_fittings=attached.route_fittings)
    # Canonical JSON reload passes when the finite source-bound receipt is fresh.
    assert BuildingModel.from_json(attached.to_json()).routes == attached.routes

    design_kwargs = dict(
        equipment_id="equipment:source",
        device_ids=("device:load",),
        rules=CircuitDesignRules(route_type="emt", nominal_diameter_m=0.02),
        user_groups=(("device:load",),),
        user_input_id="decision:assembly-route",
        source_port_id="port:source",
        load_port_ids={"device:load": "port:load"},
        routing_options=RoutingOptions(),
        allowed_domain=allowed,
    )
    designed = design_proposed_circuits(model, **design_kwargs)
    assert design_proposed_circuits(designed, **design_kwargs) is designed
    circuit = designed.circuits[0]
    design = dict(circuit.attributes["design"])
    saved_contexts = list(design["bounded_routing_contexts"])
    saved_contexts[0] = {
        **saved_contexts[0],
        "routing_context": {
            **saved_contexts[0]["routing_context"],
            "assembly_routing_context": {"version": "assembly-routing-context-v1", "sha256": "0" * 64},
        },
    }
    design["bounded_routing_contexts"] = saved_contexts
    damaged = replace(
        designed,
        circuits=(replace(circuit, attributes={**circuit.attributes, "design": design}),),
    )
    with pytest.raises(PlacementError, match="bounded routing context"):
        design_proposed_circuits(damaged, **design_kwargs)


def test_model_reload_rejects_missing_bounded_receipt_when_geometry_still_claims_assembly_check():
    model = _ceiling_fixture(include_hanger=False, include_panel=False)
    route, fittings = route_between_ports(
        model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02, allowed_domain=_domain(),
    )
    attached = replace(model, routes=(route,), route_fittings=fittings)
    runner = next(row for row in model.assembly_components if row.component_kind == "tbar_main_runner")
    shifted = replace(
        runner,
        geometry=replace(
            runner.geometry,
            pose=Pose(position=Point3(x=2, y=0, z=2.6)),
        ),
    )
    updated = _refresh_assembly_components(
        model, tuple(shifted if row.id == runner.id else row for row in model.assembly_components),
    )

    # Rebuild a canonical JSON document after refreshing valid assembly input
    # and output digests. Only the bounded receipt is stripped; the geometry
    # receipt still claims the now-stale assembly collision check.
    for malformed_value in ("missing", None):
        document = updated.to_dict()
        route_document = attached.to_dict()["routes"][0]
        if malformed_value == "missing":
            route_document["provenance"][0]["attributes"].pop("bounded_routing_context")
        else:
            route_document["provenance"][0]["attributes"]["bounded_routing_context"] = malformed_value
        document["routes"] = [route_document]
        document["route_fittings"] = attached.to_dict()["route_fittings"]
        with pytest.raises(ContractError, match="claims assembly-aware routing without its paired bounded"):
            BuildingModel.from_json(json.dumps(document))

    # Conversely, retaining only the bounded half is not a valid paired claim.
    fresh_document = attached.to_dict()
    fresh_document["routes"][0]["attributes"]["geometry_check"].pop(
        "assembly_routing_context",
    )
    with pytest.raises(ContractError, match="stale assembly routing context"):
        BuildingModel.from_json(json.dumps(fresh_document))


def test_bundle_aligned_assembly_route_attaches_and_roundtrips_primary_receipt():
    model = _ceiling_fixture(include_hanger=False, include_panel=False)
    hint = BundleHints(
        paths=(Polyline3D(points=(
            Point3(x=0.65, y=0.001, z=2.6),
            Point3(x=3.35, y=0.001, z=2.6),
        )),),
        route_type="emt",
    )
    route, fittings = route_between_ports(
        model, "port:source", "port:load", "emt",
        nominal_diameter_m=0.02,
        bundle_hints=hint,
        allowed_domain=_domain(),
    )

    assert route.attributes["bundle_alignment"]["status"] == "aligned"
    assert len(route.provenance) == 2
    primary, supplemental = route.provenance
    assert primary.method == route.attributes["routing_engine"]
    assert "bounded_routing_context" in primary.attributes
    assert supplemental.method == "bundle-source-alignment"
    assert "bounded_routing_context" not in supplemental.attributes
    attached = replace(model, routes=(route,), route_fittings=fittings)
    restored = BuildingModel.from_json(attached.to_json())
    assert restored.routes == attached.routes
    assert restored.route_fittings == attached.route_fittings

    malformed_extra_primary = replace(
        primary,
        attributes={**primary.attributes, "bounded_routing_context": None},
    )
    duplicate_primary_route = replace(
        route,
        provenance=(*route.provenance, malformed_extra_primary),
    )
    with pytest.raises(ContractError, match="exactly one primary router provenance"):
        replace(model, routes=(duplicate_primary_route,), route_fittings=fittings)


def test_empty_assembly_legacy_route_keeps_unbounded_result_and_has_no_assembly_receipt():
    model = BuildingModel(
        model_id="model:legacy-route",
        electrical_equipment=(ElectricalEquipment(
            id="equipment:source", equipment_type="panelboard", pose=Pose(position=Point3(x=0, y=0, z=0)),
        ),),
        electrical_devices=(ElectricalDevice(
            id="device:load", device_type="luminaire", pose=Pose(position=Point3(x=2, y=0, z=0)),
        ),),
        ports=(
            Port(
                id="port:source", owner_id="equipment:source", domain="electrical", role="source",
                pose=Pose(position=Point3(x=0, y=0, z=0)), direction=Vector3(x=1, y=0, z=0),
            ),
            Port(
                id="port:load", owner_id="device:load", domain="electrical", role="load",
                pose=Pose(position=Point3(x=2, y=0, z=0)), direction=Vector3(x=-1, y=0, z=0),
            ),
        ),
    )
    implicit = route_between_ports(model, "port:source", "port:load", "emt")
    explicit_none = route_between_ports(model, "port:source", "port:load", "emt", allowed_domain=None)
    assert implicit == explicit_none
    route = implicit[0]
    assert "geometry_check" not in route.attributes
    assert "bounded_routing_context" not in route.provenance[0].attributes
    assert "assembly_routing_context" not in route.provenance[0].attributes
