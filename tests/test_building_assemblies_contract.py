"""Synthetic contract-only tests for the isolated building-assembly 1.6 draft."""

from __future__ import annotations

from dataclasses import replace
import copy
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from oabm.model import (
    ASSEMBLY_COMPONENT_KINDS, ASSEMBLY_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS,
    AssemblyComponent, Box3D, BuildingAssembly, BuildingModel, Ceiling,
    ContractError, HangerAttachment, Level, Opening, PanelGeometry, Point2D,
    Point3, Polygon3D, Polyline3D, Pose, Profile2D, ProfileExtrusion,
    ProfileOutline2D, Provenance,
    Quaternion, ResolvedParameter, Size3, Slab, StructuralMember,
    Vector3, Wall, WallAssemblyFrame, WireSweep,
    compute_assembly_digests, compute_assembly_input_digest,
    compute_assembly_output_digest, stable_assembly_component_id,
    point_on_component_surface, point_on_slab_surface,
    upgrade_to_v1_1, upgrade_to_v1_2, upgrade_to_v1_3,
    upgrade_to_v1_4, upgrade_to_v1_5, upgrade_to_v1_6, validate_model,
)

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "contracts/oabm-model-v1.6.schema.json"


def _parameter(parameter_id: str, value, kind: str, unit: str, *, origin="preset_default"):
    source_entity_id=source_field_path=None
    if origin=="plan_observed":
        derivation="observed"; source_kind="plan"; source_id="sheet:a"
        source_element_id=None; source_entity_id="wall:north"; source_field_path="thickness_m"
        scopes=("value","unit","origin","source_entity_id","source_field_path")
    elif origin=="user_override":
        derivation="user"; source_kind="user_override"; source_id="user:synthetic"
        source_element_id=parameter_id; scopes=("value","unit","origin")
    else:
        derivation="inferred"; source_kind="preset_parameter"; source_id="catalog:public"
        source_element_id=json.dumps(("r1","preset:steel-wall","1.0.0",parameter_id),separators=(",",":"))
        scopes=("value","unit","origin")
    return ResolvedParameter(
        parameter_id=parameter_id, value_kind=kind, value=value, unit=unit, origin=origin,
        provenance=(Provenance(
            source_kind=source_kind,source_id=source_id,source_element_id=source_element_id,
            method="parameter-resolution", derivation=derivation,scope_paths=scopes,
        ),),
        source_entity_id=source_entity_id,source_field_path=source_field_path,
    )


def _assignment_provenance(source=None, host=None, *, selected_by="agenttakeoff",
                           preset_id="preset:steel-wall",deck=None,related=()):
    selection_ref=json.dumps(("catalog:public","r1",preset_id,"1.0.0"),separators=(",",":"))
    rows=[Provenance(source_kind="selection",source_id=selected_by,source_element_id=selection_ref,
        method="preset-selection",derivation="user",
        scope_paths=("catalog_id","catalog_revision","preset_id","preset_revision","selected_by"))]
    if source is not None and host is not None:
        source_rows=(row for row in host.provenance if row.derivation=="observed")
        for row in source_rows:
            rows.append(Provenance(source_kind=row.source_kind,source_id=row.source_id,
                source_element_id=row.source_element_id,page=row.page,method=row.method,
                confidence=row.confidence,derivation="observed",scope_paths=("host_id","input_digest"),
                attributes=row.attributes))
    else:
        rows.append(Provenance(source_kind="plan",source_id="sheet:synthetic",method="host-observation",
            derivation="observed",scope_paths=("host_id","input_digest")))
    for related_host in related:
        for row in related_host.provenance:
            if row.derivation=="observed":
                rows.append(Provenance(source_kind=row.source_kind,source_id=row.source_id,
                    source_element_id=row.source_element_id,page=row.page,method=row.method,
                    confidence=row.confidence,derivation="observed",scope_paths=("input_digest",),
                    attributes=row.attributes))
    if deck is not None:
        for row in deck.provenance:
            if row.derivation=="observed":
                rows.append(Provenance(source_kind=row.source_kind,source_id=row.source_id,
                    source_element_id=row.source_element_id,page=row.page,method=row.method,
                    confidence=row.confidence,derivation="observed",scope_paths=("deck_id","input_digest"),
                    attributes=row.attributes))
    return tuple(rows)


def _component_provenance(*scopes: str, observed=False):
    return (Provenance(
        source_kind="manufacturer" if observed else "selected-preset",
        source_id="manufacturer:synthetic" if observed else "preset:wall-v1",
        method="component-geometry", derivation="observed" if observed else "inferred",
        scope_paths=tuple(scopes),
    ),)


def _level() -> Level:
    return Level(id="level:main", elevation_m=0.0, height_m=3.0)


def _wall(opening: Opening | None = None) -> Wall:
    return Wall(
        id="wall:north", level_id="level:main",
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0), Point3(x=4.0, y=0.0, z=0.0))),
        thickness_m=0.20, height_m=3.0, construction="framed",
        provenance=(Provenance(source_kind="plan", source_id="sheet:a", derivation="observed"),),
    )


def _door(*, position=(2.0, 0.0, 1.05), size=(0.9, 0.2, 2.1), rotation=None,
          opening_id="opening:door") -> Opening:
    return Opening(
        id=opening_id, host_id="wall:north", opening_type="door",
        pose=Pose(position=Point3(x=position[0], y=position[1], z=position[2]),
                  rotation=rotation or Quaternion()),
        size=Size3(x=size[0], y=size[1], z=size[2]),
        provenance=(Provenance(source_kind="plan", source_id="sheet:a", derivation="observed"),),
    )


def _source_model(*, wall=None, opening: Opening | None = None, ceiling=None,
                  slabs: tuple[Slab,...] = ()) -> BuildingModel:
    return BuildingModel(
        model_id="model:assembly-test", schema_version="1.5.0",
        levels=(_level(),), walls=(wall or _wall(),),
        openings=() if opening is None else (opening,),
        ceilings=() if ceiling is None else (ceiling,), slabs=slabs,
    )


def _wall_frame(wall: Wall) -> WallAssemblyFrame:
    a,b=wall.centerline.points[0],wall.centerline.points[1]
    dx,dy=b.x-a.x,b.y-a.y; norm=math.hypot(dx,dy)
    return WallAssemblyFrame(
        origin=a, axis=Vector3(x=dx/norm,y=dy/norm,z=0),
        left_normal=Vector3(x=-dy/norm,y=dx/norm,z=0), base_z_m=a.z,
    )


def _wall_parameters(*, total=.20):
    return (
        _parameter("wall.stack.framing_depth_m", .09, "length", "m"),
        _parameter("wall.stack.face_left.gypsum", .055, "length", "m"),
        _parameter("wall.stack.face_right.gypsum", .055, "length", "m"),
    )


def _component(assembly_id="assembly:wall", *, part_key="stud:station:0",
               kind="stud", geometry=None, level_id="level:main", opening_id=None,
               material_id=None, material_basis="unknown", geometry_basis="specified_parameterized_geometry",
               hanger_attachment=None, provenance=None):
    if geometry is None:
        geometry=Box3D(
            pose=Pose(position=Point3(x=.05,y=0,z=1.5)),
            size=Size3(x=.1,y=.2,z=3.0),
        )
    scopes=("geometry",)+( ("material_id",) if material_basis=="manufacturer_actual" else () )
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id,part_key), assembly_id=assembly_id,
        part_key=part_key, level_id=level_id, component_kind=kind, geometry=geometry,
        geometry_basis=geometry_basis, material_id=material_id, material_basis=material_basis,
        opening_id=opening_id, hanger_attachment=hanger_attachment,
        provenance=provenance or _component_provenance(*scopes),
    )


def _assembly(source: BuildingModel, components: tuple[AssemblyComponent,...], *,
              kind="wall", host_id="wall:north", level_id="level:main", assembly_id="assembly:wall",
              deck_id=None, params=None, wall_frame=None, representation=None):
    representation=representation or ("source_opening_void" if kind=="opening_insert" else "aggregate_host")
    if kind=="wall" and wall_frame is None:
        wall_frame=_wall_frame(source.walls[0])
    host_rows={"wall":source.walls,"opening_insert":source.openings,
               "suspended_ceiling":source.ceilings}[kind]
    host=next(row for row in host_rows if row.id==host_id)
    preset_id={"wall":"preset:steel-wall","opening_insert":"preset:door-insert",
               "suspended_ceiling":"preset:tbar-ceiling"}[kind]
    deck=(next((row for row in source.slabs if row.id==deck_id),None)
          if isinstance(deck_id,str) else None)
    related=()
    if kind=="opening_insert":
        parent=next((row for row in (*source.walls,*source.ceilings) if row.id==host.host_id),None)
        if parent is not None: related=(parent,)
    elif kind=="wall":
        related=tuple(row for row in source.openings if row.host_id==host.id)
    draft=BuildingAssembly(
        id=assembly_id, assembly_kind=kind, level_id=level_id, host_id=host_id,
        host_representation=representation, catalog_id="catalog:public", catalog_revision="r1",
        preset_id=preset_id, preset_revision="1.0.0", selected_by="agenttakeoff",
        generator_version="oabm-assembly-contract-test/1",
        resolved_parameters=tuple(_wall_parameters() if params is None and kind=="wall" else (params or ())),
        component_ids=tuple(component.id for component in components), input_digest="0"*64,
        output_digest="0"*64, wall_frame=wall_frame, deck_id=deck_id,
        provenance=_assignment_provenance(source,host,selected_by="agenttakeoff",
            preset_id=preset_id,deck=deck,related=related),
    )
    incoming=compute_assembly_input_digest(source,draft)
    outgoing=compute_assembly_output_digest(components)
    return replace(draft,input_digest=incoming,output_digest=outgoing)


def _install(source: BuildingModel, components: tuple[AssemblyComponent,...], assembly: BuildingAssembly):
    upgraded=upgrade_to_v1_6(source)
    return replace(upgraded,building_assemblies=(assembly,),assembly_components=components)


def _valid_wall_model(*, opening: Opening | None=None, components=None, params=None):
    source=_source_model(opening=opening)
    parts=tuple(components or (_component(),))
    assembly=_assembly(source,parts,params=params)
    return _install(source,parts,assembly)


def _valid_ceiling_model(*, deck_component=False, components=None):
    from oabm.model import Polyline3D
    ceiling=Ceiling(id="ceiling:grid",level_id="level:main",footprint=Polygon3D(points=(
        Point3(x=0,y=0,z=2),Point3(x=4,y=0,z=2),Point3(x=4,y=3,z=2),Point3(x=0,y=3,z=2))),
        thickness_m=.2,provenance=_wall().provenance)
    slab=Slab(id="slab:deck",level_id="level:main",footprint=Polygon3D(points=(
        Point3(x=0,y=0,z=3),Point3(x=4,y=0,z=3),Point3(x=4,y=3,z=3),Point3(x=0,y=3,z=3))),
        thickness_m=.2,provenance=_wall().provenance)
    source=_source_model(ceiling=ceiling,slabs=() if deck_component else (slab,))
    if components is None:
        if not deck_component:
            components=(_component("assembly:ceiling",part_key="tile:0",kind="tile",
                geometry=Box3D(pose=Pose(position=Point3(x=1,y=1,z=2)),size=Size3(x=1,y=1,z=.02))),)
        else:
            components=(_component("assembly:ceiling",part_key="deck:only",kind="deck_panel",
                geometry=Box3D(pose=Pose(position=Point3(x=2,y=1.5,z=2.9)),size=Size3(x=4,y=3,z=.2))),)
    assignment=_assembly(source,components,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling",deck_id=(components[0].id if deck_component else "slab:deck"),params=())
    return _install(source,tuple(components),assignment)


def test_v16_is_opt_in_default_remains_v10_and_upgrade_preserves_prior_fields():
    source=BuildingModel(model_id="model:migration")
    assert source.schema_version=="1.0.0"
    assert ASSEMBLY_SCHEMA_VERSION=="1.6.0"
    assert "1.6.0" in SUPPORTED_SCHEMA_VERSIONS
    versions=[source]
    for upgrade in (upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3,upgrade_to_v1_4,upgrade_to_v1_5):
        versions.append(upgrade(versions[-1]))
    assert [row.schema_version for row in versions]==["1.0.0","1.1.0","1.2.0","1.3.0","1.4.0","1.5.0"]
    for old in versions:
        before=old.to_dict()
        upgraded=upgrade_to_v1_6(old)
        after=upgraded.to_dict()
        assert upgraded.schema_version=="1.6.0"
        assert not upgraded.building_assemblies and not upgraded.assembly_components
        for name,value in before.items():
            if name!="schema_version":
                assert after[name]==value
        assert after["building_assemblies"]==[] and after["assembly_components"]==[]
        assert old.to_dict()==before  # immutable and no legacy serialization mutation
    candidate=upgrade_to_v1_6(source)
    for downgrade in (upgrade_to_v1_1,upgrade_to_v1_2,upgrade_to_v1_3,upgrade_to_v1_4,upgrade_to_v1_5):
        with pytest.raises(ContractError,match="cannot downgrade version 1.6"):
            downgrade(candidate)


def test_pre_16_reader_rejects_candidate_fields_and_16_reader_requires_both_arrays():
    source=BuildingModel(model_id="model:strict")
    old=source.to_dict(); old["building_assemblies"]=[]
    with pytest.raises(ContractError,match="unknown building-assembly fields"):
        BuildingModel.from_dict(old)
    upgraded=upgrade_to_v1_6(source).to_dict()
    del upgraded["assembly_components"]
    with pytest.raises(ContractError,match="requires both building_assemblies"):
        BuildingModel.from_dict(upgraded)
    upgraded=upgrade_to_v1_6(source).to_dict()
    assert BuildingModel.from_json(json.dumps(upgraded)).schema_version=="1.6.0"


def test_parameter_values_are_strictly_discriminated_by_kind_unit_origin_and_scoped_provenance():
    good=_parameter("stud.spacing",.4064,"length","m",origin="preset_default")
    assert good.value==pytest.approx(.4064) and good.origin=="preset_default"
    assert good.provenance[0].scope_paths==("value","unit","origin")
    number=_parameter("grid.count",3,"count","each",origin="plan_observed")
    assert number.value==3 and isinstance(number.value,int)
    token=_parameter("preset.finish","type-x","token","1")
    assert token.value=="type-x"
    boolean=_parameter("hardware.enabled",True,"boolean","1")
    assert boolean.value is True
    with pytest.raises(ContractError,match="unit must be 'm'"):
        ResolvedParameter(parameter_id="bad:length",value_kind="length",value=.1,unit="mm",origin="preset_default",
            provenance=good.provenance,source_entity_id=None,source_field_path=None)
    with pytest.raises(ContractError,match="nonnegative integer"):
        ResolvedParameter(parameter_id="bad:count",value_kind="count",value=3.0,unit="each",origin="preset_default",
            provenance=good.provenance,source_entity_id=None,source_field_path=None)
    with pytest.raises(ContractError,match="field-qualified"):
        ResolvedParameter(parameter_id="bad:scope",value_kind="length",value=.1,unit="m",origin="preset_default",
            provenance=(Provenance(source_kind="preset",source_id="preset:x"),),
            source_entity_id=None,source_field_path=None)
    with pytest.raises(ContractError,match="observed source provenance"):
        ResolvedParameter(parameter_id="bad:plan",value_kind="length",value=.1,unit="m",origin="plan_observed",
            provenance=(Provenance(source_kind="plan",source_id="sheet:a",derivation="inferred",
                scope_paths=("value","unit","origin","source_entity_id","source_field_path")),),
            source_entity_id="wall:north",source_field_path="thickness_m")
    with pytest.raises(ContractError,match="preset parameter"):
        ResolvedParameter(parameter_id="bad:default",value_kind="length",value=.1,unit="m",origin="preset_default",
            provenance=(Provenance(source_kind="plan",source_id="sheet:a",derivation="observed",
                scope_paths=("value","unit","origin")),),source_entity_id=None,source_field_path=None)


def test_parameter_evidence_binds_selected_preset_and_observed_host_fields():
    observed=_parameter("wall.observed_thickness_m",.2,"length","m",origin="plan_observed")
    valid=_valid_wall_model(params=_wall_parameters()+(observed,))
    assert valid.building_assemblies[0].resolved_parameters[-1].source_entity_id=="wall:north"

    default=_parameter("wall.layer.test_m",.012,"length","m")
    wrong_ref=replace(default.provenance[0],source_element_id=json.dumps(
        ("r1","preset:other-wall","1.0.0",default.parameter_id),separators=(",",":")))
    wrong_default=replace(default,provenance=(wrong_ref,))
    with pytest.raises(ContractError,match="preset-default evidence does not bind"):
        _valid_wall_model(params=_wall_parameters()+(wrong_default,))

    wrong_observed=_parameter("wall.observed_thickness_m",.25,"length","m",origin="plan_observed")
    with pytest.raises(ContractError,match="observed value does not equal"):
        _valid_wall_model(params=_wall_parameters()+(wrong_observed,))

    wrong_source=replace(observed,provenance=(replace(observed.provenance[0],source_id="sheet:arbitrary"),))
    with pytest.raises(ContractError,match="provenance does not match its observed source field"):
        _valid_wall_model(params=_wall_parameters()+(wrong_source,))

    override=_parameter("wall.spacing_m",.4,"length","m",origin="user_override")
    with pytest.raises(ContractError,match="explicit parameter identity"):
        replace(override,provenance=(replace(override.provenance[0],source_element_id="wall.other"),))


def test_assignment_host_provenance_must_match_current_source_geometry():
    source=_source_model(); parts=(_component(),); assembly=_assembly(source,parts)
    host_record=next(row for row in assembly.provenance if row.derivation=="observed")
    bad_record=replace(host_record,source_id="sheet:arbitrary")
    bad=replace(assembly,provenance=tuple(bad_record if row is host_record else row
        for row in assembly.provenance))
    bad=replace(bad,input_digest=compute_assembly_input_digest(source,bad))
    with pytest.raises(ContractError,match="observed host-geometry provenance"):
        _install(source,parts,bad)


def test_assignment_host_geometry_evidence_requires_matching_field_coverage():
    wall=_wall()
    source_record=wall.provenance[0]
    name_only=replace(wall,provenance=(replace(source_record,scope_paths=("name",)),))
    source=_source_model(wall=name_only)
    parts=(_component(),)
    assignment=_assembly(source,parts)
    with pytest.raises(ContractError,match="does not cover geometry field.*centerline"):
        _install(source,parts,assignment)

    # Geometry scopes may be established by multiple matching observed
    # records; entity-wide scope=None remains the compact compatibility form.
    split_scopes=replace(wall,provenance=(
        replace(source_record,scope_paths=("level_id","centerline","thickness_m")),
        replace(source_record,scope_paths=("height_m",)),
    ))
    split_source=_source_model(wall=split_scopes)
    split_assignment=_assembly(split_source,parts)
    assert _install(split_source,parts,split_assignment).building_assemblies


def test_opening_parent_ceiling_and_selected_slab_need_geometry_scopes():
    # An opening insert cannot borrow a name-only observation of its parent wall.
    door=_door()
    parent=replace(_wall(),provenance=(replace(_wall().provenance[0],scope_paths=("name",)),))
    opening_source=_source_model(wall=parent,opening=door)
    insert=_component("assembly:door",part_key="door:leaf",kind="door_leaf",
        opening_id=door.id,geometry=Box3D(pose=Pose(position=Point3(x=2,y=0,z=1.05)),
            size=Size3(x=.8,y=.04,z=2.0)))
    assignment=_assembly(opening_source,(insert,),kind="opening_insert",host_id=door.id,
        assembly_id="assembly:door")
    with pytest.raises(ContractError,match="parent-host geometry.*centerline"):
        _install(opening_source,(insert,),assignment)

    # Check ceiling and slab observations independently; neither record may
    # borrow an unrelated field as proof of its physical dimensions.
    valid=_valid_ceiling_model()
    ceiling=valid.ceilings[0]
    bad_ceiling=replace(ceiling,provenance=(replace(ceiling.provenance[0],scope_paths=("name",)),))
    ceiling_source=_source_model(ceiling=bad_ceiling,slabs=valid.slabs)
    parts=valid.assembly_components
    assignment=_assembly(ceiling_source,parts,kind="suspended_ceiling",host_id=bad_ceiling.id,
        assembly_id="assembly:ceiling",deck_id="slab:deck",params=())
    with pytest.raises(ContractError,match="host-geometry.*footprint"):
        _install(ceiling_source,parts,assignment)

    slab=valid.slabs[0]
    bad_slab=replace(slab,provenance=(replace(slab.provenance[0],scope_paths=("name",)),))
    slab_source=_source_model(ceiling=ceiling,slabs=(bad_slab,))
    assignment=_assembly(slab_source,parts,kind="suspended_ceiling",host_id=ceiling.id,
        assembly_id="assembly:ceiling",deck_id=bad_slab.id,params=())
    with pytest.raises(ContractError,match="deck-geometry.*footprint"):
        _install(slab_source,parts,assignment)


def test_wall_opening_geometry_requires_source_scopes_too():
    door=_door()
    bad_door=replace(door,provenance=(replace(door.provenance[0],scope_paths=("name",)),))
    source=_source_model(opening=bad_door)
    parts=(_component(),)
    assignment=_assembly(source,parts)
    with pytest.raises(ContractError,match="opening geometry.*pose"):
        _install(source,parts,assignment)


def test_fixed_geometry_union_validates_profiles_panels_wires_and_semantic_ids():
    profile=Profile2D(kind="c_profile",overall_width_m=.1,overall_depth_m=.05,
        web_thickness_m=.005,flange_thickness_m=.005,return_lip_m=.005)
    extrusion=ProfileExtrusion(profile=profile,pose=Pose(position=Point3(x=0,y=0,z=0)),length_m=2)
    assert extrusion.kind=="profile_extrusion" and len(profile.outline_xy_m())<=12
    tbar=Profile2D(kind="t_profile",overall_width_m=.05,overall_depth_m=.04,
        web_thickness_m=.005,flange_thickness_m=.005)
    assert tbar.kind=="t_profile"
    panel=PanelGeometry(boundary=Polygon3D(points=(
        Point3(x=0,y=0,z=0),Point3(x=1,y=0,z=0),Point3(x=1,y=1,z=0),Point3(x=0,y=1,z=0))),
        thickness_m=.0127,normal=Vector3(x=0,y=0,z=1))
    assert panel.thickness_m==pytest.approx(.0127)
    wire=WireSweep(centerline=Polyline3D(points=(
        Point3(x=0,y=0,z=1),Point3(x=0,y=0,z=0))),diameter_m=.003,end_treatment="cut")
    assert wire.kind=="wire_sweep"
    source_outline=ProfileOutline2D(source_id="manufacturer:profile-x",
        points_m=(Point2D(x=0,y=0),Point2D(x=.1,y=0),Point2D(x=.1,y=.1),Point2D(x=0,y=.1)),
        provenance=(Provenance(source_kind="manufacturer",source_id="manufacturer:profile-x",
            derivation="observed",scope_paths=("points_m",)),))
    assert source_outline.source_id=="manufacturer:profile-x"
    with pytest.raises(ContractError,match="source-bound observed points_m provenance"):
        ProfileOutline2D(source_id="manufacturer:profile-x",
            points_m=(Point2D(x=0,y=0),Point2D(x=.1,y=0),Point2D(x=.1,y=.1),Point2D(x=0,y=.1)),
            provenance=(Provenance(source_kind="manufacturer",source_id="manufacturer:profile-x",
                derivation="observed",scope_paths=None),))
    assert stable_assembly_component_id("assembly:one","stud:bay:0") == stable_assembly_component_id("assembly:one","stud:bay:0")
    assert stable_assembly_component_id("assembly:one","stud:bay:0") != stable_assembly_component_id("assembly:one","stud:bay:1")
    with pytest.raises(ContractError,match="list index"):
        stable_assembly_component_id("assembly:one","index:4")
    with pytest.raises(ContractError,match="no return lips"):
        Profile2D(kind="t_profile",overall_width_m=.1,overall_depth_m=.05,
            web_thickness_m=.005,flange_thickness_m=.005,return_lip_m=.002)
    with pytest.raises(ContractError,match="simple section"):
        ProfileOutline2D(source_id="manufacturer:profile-x",points_m=(
            Point2D(x=0,y=0),Point2D(x=1,y=1),Point2D(x=0,y=1),Point2D(x=1,y=0)),
            provenance=(Provenance(source_kind="manufacturer",source_id="manufacturer:profile-x",
                derivation="observed",scope_paths=("points_m",)),))
    # Reviewer repro dimensions used to produce a profile whose generated lip
    # walk crossed the web. The corrected return-lip outline is strict-meshable.
    c_repro=Profile2D(kind="c_profile",overall_width_m=.05,overall_depth_m=.1,
        web_thickness_m=.003,flange_thickness_m=.003,return_lip_m=.01)
    from oabm.assembly_geometry import extrude_simple_section_m
    mesh=extrude_simple_section_m(c_repro.outline_xy_m(),1.0,((0,0,0),(0,0,0,1)))
    assert mesh.cross_section_area_m2>0
    with pytest.raises(ContractError,match="return lips"):
        Profile2D(kind="c_profile",overall_width_m=.05,overall_depth_m=.1,
            web_thickness_m=.003,flange_thickness_m=.003,return_lip_m=.047)
    with pytest.raises(ContractError,match="unit vector"):
        PanelGeometry(boundary=Polygon3D(points=(Point3(x=0,y=0,z=0),Point3(x=1,y=0,z=0),Point3(x=0,y=1,z=0))),
            thickness_m=.01,normal=Vector3(x=0,y=0,z=2))
    with pytest.raises(ContractError,match="straight segments"):
        WireSweep(centerline=Polyline3D(
            points=tuple(Point3(x=i,y=0,z=0) for i in range(34))),diameter_m=.01,end_treatment="cut")
    with pytest.raises(ContractError,match="hooks and loops need geometry"):
        WireSweep(centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=0,y=0,z=1))),
            diameter_m=.003,end_treatment="hook")


def test_translated_panel_area_and_tolerated_quaternions_use_shared_strict_math():
    origin=1e12
    translated=PanelGeometry(boundary=Polygon3D(points=(
        Point3(x=origin,y=origin,z=0),Point3(x=origin+1,y=origin,z=0),
        Point3(x=origin+1,y=origin+1,z=0),Point3(x=origin,y=origin+1,z=0))),
        thickness_m=.01,normal=Vector3(x=0,y=0,z=1))
    assert translated.thickness_m==pytest.approx(.01)
    scale=.999995
    q=Quaternion(z=math.sin(math.pi/4)*scale,w=math.cos(math.pi/4)*scale)
    component=_component(geometry=Box3D(pose=Pose(position=Point3(x=0,y=0,z=0),rotation=q),
        size=Size3(x=2,y=1,z=1)))
    assert point_on_component_surface(component,Point3(x=0,y=1,z=0))


def test_component_unknown_material_is_distinct_from_explicit_actual_basis():
    unknown=_component()
    assert unknown.material_id is None and unknown.material_basis=="unknown"
    with pytest.raises(ContractError,match="explicit material_id"):
        _component(material_id=None,material_basis="plan_specified")
    with pytest.raises(ContractError,match="cannot carry"):
        _component(material_id="material:wood",material_basis="unknown")
    actual=_component(material_id="material:steel",material_basis="manufacturer_actual",
        geometry_basis="manufacturer_actual_geometry",provenance=_component_provenance(
            "geometry","material_id",observed=True))
    assert actual.material_id=="material:steel" and actual.geometry_basis=="manufacturer_actual_geometry"
    plan_material=(_component_provenance("geometry")[0],Provenance(
        source_kind="plan",source_id="sheet:materials",derivation="observed",
        scope_paths=("material_id",)))
    plan_specified=_component(material_id="material:wood",material_basis="plan_specified",
        provenance=plan_material)
    assert plan_specified.material_basis=="plan_specified"
    caller_material=(_component_provenance("geometry")[0],Provenance(
        source_kind="user_override",source_id="user:material-choice",derivation="user",
        scope_paths=("material_id",)))
    caller_selected=_component(material_id="material:wood",material_basis="caller_selected",
        provenance=caller_material)
    assert caller_selected.material_basis=="caller_selected"
    for basis,scope,derivation in (
        ("plan_specified",("geometry",),"observed"),
        ("plan_specified",("material_id",),"inferred"),
        ("caller_selected",("geometry","material_id"),"inferred"),
        ("caller_selected",("material_id",),"observed"),
    ):
        bad_provenance=(_component_provenance("geometry")[0],Provenance(
            source_kind="plan",source_id="source:material",derivation=derivation,
            scope_paths=scope))
        with pytest.raises(ContractError,match="material_id provenance"):
            _component(material_id="material:wood",material_basis=basis,
                provenance=bad_provenance)
    with pytest.raises(ContractError,match="observed geometry provenance"):
        _component(material_id="material:steel",material_basis="caller_selected",
            geometry_basis="manufacturer_actual_geometry",provenance=caller_material)


def test_wall_assignment_checks_axis_opening_stack_depth_and_aggregate_ownership():
    model=_valid_wall_model(opening=_door())
    assert model.schema_version=="1.6.0"
    assert model.building_assemblies[0].host_representation=="aggregate_host"
    assert model.walls[0].thickness_m==.2  # observed envelope is never rewritten
    assert model.building_assemblies[0].wall_frame.left_normal==Vector3(x=0,y=1,z=0)
    validate_model(model)
    bad_params=(
        _parameter("wall.stack.framing_depth_m",.09,"length","m"),
        _parameter("wall.stack.face_left.gypsum",.05,"length","m"),
        _parameter("wall.stack.face_right.gypsum",.055,"length","m"),
    )
    source=_source_model()
    parts=(_component(),)
    candidate=_assembly(source,parts,params=bad_params)
    with pytest.raises(ContractError,match="stack must equal observed thickness"):
        _install(source,parts,candidate)
    assert source.walls[0].thickness_m==.2


@pytest.mark.parametrize("bad_opening,reason",[
    (_door(size=(.9,.18,2.1)),"match observed wall thickness"),
    (_door(position=(2,.01,1.05)),"not on the wall axis plane"),
    (_door(position=(2,0,1.1)),"door sill must coincide"),
    (_door(position=(.3,0,1.05)),"does not fit its wall host"),
    (_door(rotation=Quaternion(z=math.sqrt(.5),w=math.sqrt(.5))),"orientation is not aligned"),
])
def test_invalid_wall_opening_geometry_is_rejected(bad_opening,reason):
    source=_source_model(opening=bad_opening)
    parts=(_component(),)
    assembly=_assembly(source,parts)
    with pytest.raises(ContractError,match=reason):
        _install(source,parts,assembly)


def test_overlapping_openings_and_component_overlap_with_void_fail_closed():
    first=_door(opening_id="opening:door-a")
    second=_door(position=(2.4,0,1.05),opening_id="opening:door-b")
    source=_source_model()
    source=replace(source,openings=(first,second))
    parts=(_component(),)
    assembly=_assembly(source,parts)
    with pytest.raises(ContractError,match="opening/frame envelopes overlap"):
        _install(source,parts,assembly)
    opening=_door()
    overlapping=_component(geometry=Box3D(
        pose=Pose(position=Point3(x=2,y=0,z=1.5)),size=Size3(x=.5,y=.1,z=.5)))
    source=_source_model(opening=opening)
    assignment=_assembly(source,(overlapping,))
    with pytest.raises(ContractError,match="overlaps opening void"):
        _install(source,(overlapping,),assignment)


def test_curve_and_nonhorizontal_wall_axes_are_not_supported_in_first_proof():
    from oabm.model import Polyline3D
    for points in (
        (Point3(x=0,y=0,z=0),Point3(x=2,y=0,z=0),Point3(x=4,y=0,z=0)),
        (Point3(x=0,y=0,z=0),Point3(x=4,y=0,z=.1)),
    ):
        wall=replace(_wall(),centerline=Polyline3D(points=points))
        source=_source_model(wall=wall)
        parts=(_component(),)
        assignment=_assembly(source,parts,wall_frame=_wall_frame(wall))
        with pytest.raises(ContractError,match="two centerline points|horizontal"):
            _install(source,parts,assignment)


def test_assignment_input_and_output_digests_detect_stale_source_and_child_on_load():
    model=_valid_wall_model(opening=_door())
    assignment=model.building_assemblies[0]
    parts=model.assembly_components
    assert compute_assembly_digests(model,assignment,parts)==(assignment.input_digest,assignment.output_digest)
    assert assignment.input_digest==compute_assembly_input_digest(model,assignment)
    assert assignment.output_digest==compute_assembly_output_digest(parts)
    original=model.to_json()
    stale_source=copy.deepcopy(model.to_dict())
    stale_source["walls"][0]["provenance"][0]["derivation"]="inferred"
    with pytest.raises(ContractError,match="host-geometry provenance|input_digest is stale"):
        BuildingModel.from_json(json.dumps(stale_source))
    stale_child=copy.deepcopy(model.to_dict())
    stale_child["assembly_components"][0]["geometry"]["size"]["z"]=2.9
    with pytest.raises(ContractError,match="output_digest is stale"):
        BuildingModel.from_json(json.dumps(stale_child))
    assert model.to_json()==original  # invalid candidates never mutate the source model


def test_membership_is_exact_and_one_active_aggregate_per_host():
    model=_valid_wall_model()
    assignment=model.building_assemblies[0]
    component=model.assembly_components[0]
    duplicate=replace(assignment,id="assembly:duplicate")
    with pytest.raises(ContractError,match="one active aggregate/insert assignment"):
        replace(model,building_assemblies=(assignment,duplicate))
    with pytest.raises(ContractError,match="exactly one owning assignment"):
        replace(model,assembly_components=(component,_component(part_key="stud:unowned")))
    with pytest.raises(ContractError,match="missing component"):
        replace(model,building_assemblies=(replace(assignment,component_ids=("component:missing",)),))


def test_component_ids_and_roles_are_stable_and_assembly_compatible():
    component=_component()
    with pytest.raises(ContractError,match="stable for its assembly_id"):
        replace(component,id="component:arbitrary-id")
    with pytest.raises(ContractError,match="unique canonical IDs"):
        _valid_wall_model(components=(component,_component(part_key=component.part_key)))
    wrong_role=_component(kind="tile")
    with pytest.raises(ContractError,match="incompatible with wall assembly"):
        _valid_wall_model(components=(wrong_role,))
    wrong_geometry=_component(kind="stud",geometry=WireSweep(centerline=Polyline3D(
        points=(Point3(x=.05,y=0,z=1),Point3(x=.05,y=0,z=2))),diameter_m=.003,end_treatment="cut"))
    with pytest.raises(ContractError,match="geometry is incompatible with its stud role"):
        _valid_wall_model(components=(wrong_geometry,))


def test_ceiling_children_are_bounded_to_host_and_selected_deck():
    model=_valid_ceiling_model()
    tile=model.assembly_components[0]
    outside=replace(tile,geometry=Box3D(
        pose=Pose(position=Point3(x=100,y=100,z=2)),size=Size3(x=1,y=1,z=.02)))
    outside_parts=(outside,)
    bad_assignment=_assembly(model,outside_parts,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling",deck_id="slab:deck",params=())
    with pytest.raises(ContractError,match="exceeds its Ceiling host footprint"):
        _install(model,outside_parts,bad_assignment)

    above_deck=replace(tile,geometry=Box3D(
        pose=Pose(position=Point3(x=1,y=1,z=2.85)),size=Size3(x=1,y=1,z=.02)))
    high_parts=(above_deck,)
    bad_assignment=_assembly(model,high_parts,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling",deck_id="slab:deck",params=())
    with pytest.raises(ContractError,match="vertical bounds"):
        _install(model,high_parts,bad_assignment)


def test_unowned_structural_overlap_is_reported_without_deleting_either_identity():
    model=_valid_wall_model()
    before=model.to_json()
    member=StructuralMember(id="member:confirmed",kind="stud",level_id="level:main",
        pose=Pose(position=Point3(x=.05,y=0,z=1.5)),size=Size3(x=.1,y=.2,z=3.0),
        provenance=(Provenance(source_kind="caller",source_id="user:review",derivation="user"),))
    with pytest.raises(ContractError,match="member_overlap_unresolved"):
        replace(model,structural_members=(member,))
    assert model.to_json()==before


def test_opening_insert_is_one_explicit_assignment_and_geometry_fits_the_void():
    opening=_door()
    source=_source_model(opening=opening)
    insert=_component("assembly:door",part_key="door:leaf",kind="door_leaf",opening_id=opening.id,
        geometry=Box3D(pose=Pose(position=Point3(x=2,y=0,z=1.05)),size=Size3(x=.85,y=.04,z=2.0)))
    assignment=_assembly(source,(insert,),kind="opening_insert",host_id=opening.id,assembly_id="assembly:door")
    model=_install(source,(insert,),assignment)
    assert model.building_assemblies[0].host_representation=="source_opening_void"
    assert model.assembly_components[0].opening_id==opening.id
    duplicate=replace(assignment,id="assembly:door-copy")
    with pytest.raises(ContractError,match="one active aggregate/insert assignment"):
        replace(model,building_assemblies=(assignment,duplicate))
    too_wide=_component("assembly:door-wide",part_key="door:leaf",kind="door_leaf",opening_id=opening.id,
        geometry=Box3D(pose=Pose(position=Point3(x=2,y=0,z=1.05)),size=Size3(x=1.2,y=.04,z=2.0)))
    bad_assignment=_assembly(source,(too_wide,),kind="opening_insert",host_id=opening.id,assembly_id="assembly:door-wide")
    with pytest.raises(ContractError,match="exceeds its exact opening void"):
        _install(source,(too_wide,),bad_assignment)


def test_suspended_ceiling_reuses_one_deck_identity_and_requires_a_deck():
    model=_valid_ceiling_model()
    assert model.building_assemblies[0].deck_id=="slab:deck"
    assert not any(row.component_kind=="deck_panel" for row in model.assembly_components)
    with pytest.raises(ContractError,match="requires exactly one existing or generated deck"):
        BuildingAssembly(id="assembly:no-deck",assembly_kind="suspended_ceiling",level_id="level:main",
            host_id="ceiling:grid",host_representation="aggregate_host",catalog_id="catalog:public",
            catalog_revision="r1",preset_id="preset:ceiling",preset_revision="1",selected_by="agenttakeoff",
            generator_version="g1",resolved_parameters=(),component_ids=("component:x",),input_digest="0"*64,
            output_digest="0"*64,provenance=_assignment_provenance())
    generated=_valid_ceiling_model(deck_component=True)
    assert generated.building_assemblies[0].deck_id==generated.assembly_components[0].id
    ceiling=generated.ceilings[0]
    slab=Slab(id="slab:deck",level_id="level:main",footprint=Polygon3D(points=(
        Point3(x=0,y=0,z=3),Point3(x=4,y=0,z=3),Point3(x=4,y=3,z=3),Point3(x=0,y=3,z=3))),
        thickness_m=.2,provenance=_wall().provenance)
    source=_source_model(ceiling=ceiling,slabs=(slab,))
    assembly=generated.building_assemblies[0]
    assigned=replace(assembly,deck_id="slab:deck",
        provenance=_assignment_provenance(source,source.ceilings[0],preset_id="preset:tbar-ceiling",deck=slab))
    assigned=replace(assigned,input_digest=compute_assembly_input_digest(source,assigned))
    with pytest.raises(ContractError,match="existing Slab deck must be reused exactly once"):
        _install(source,generated.assembly_components,assigned)


def test_generated_deck_uses_actual_horizontal_yawed_footprint_not_its_aabb():
    yaw=Quaternion(z=math.sin(math.pi/8),w=math.cos(math.pi/8))
    deck=_component("assembly:ceiling",part_key="deck:only",kind="deck_panel",
        geometry=Box3D(pose=Pose(position=Point3(x=2,y=1.5,z=3),rotation=yaw),
            size=Size3(x=2,y=2,z=.2)))
    fitting_tile=_component("assembly:ceiling",part_key="tile:inside",kind="tile",
        geometry=Box3D(pose=Pose(position=Point3(x=2,y=1.5,z=2.5),rotation=yaw),
            size=Size3(x=.5,y=.5,z=.1)))
    accepted=_valid_ceiling_model(deck_component=True,components=(deck,fitting_tile))
    assert accepted.assembly_components[0].id==accepted.building_assemblies[0].deck_id

    # This tile lies in both the deck AABB and ceiling host, but outside the
    # true 45-degree rotated deck footprint.
    outside_tile=_component("assembly:ceiling",part_key="tile:outside",kind="tile",
        geometry=Box3D(pose=Pose(position=Point3(x=3.3,y=2.8,z=2.5)),
            size=Size3(x=.1,y=.1,z=.1)))
    with pytest.raises(ContractError,match="exceeds its generated deck footprint"):
        _valid_ceiling_model(deck_component=True,components=(deck,outside_tile))

    tilt=Quaternion(y=math.sin(math.radians(5)),w=math.cos(math.radians(5)))
    sloped_deck=replace(deck,geometry=Box3D(
        pose=Pose(position=Point3(x=2,y=1.5,z=3),rotation=tilt),size=Size3(x=2,y=2,z=.2)))
    with pytest.raises(ContractError,match="tilted/sloped decks are unsupported"):
        _valid_ceiling_model(deck_component=True,components=(sloped_deck,))

    root2=math.sqrt(2.0)
    horizontal_panel=_component("assembly:ceiling",part_key="deck:panel",kind="deck_panel",
        geometry=PanelGeometry(boundary=Polygon3D(points=(
            Point3(x=2,y=1.5-root2,z=3),Point3(x=2+root2,y=1.5,z=3),
            Point3(x=2,y=1.5+root2,z=3),Point3(x=2-root2,y=1.5,z=3))),
            thickness_m=.2,normal=Vector3(x=0,y=0,z=1)))
    _valid_ceiling_model(deck_component=True,components=(horizontal_panel,fitting_tile))
    slope_normal=Vector3(x=-.1/math.sqrt(1.01),y=0,z=1/math.sqrt(1.01))
    sloped_panel=replace(horizontal_panel,geometry=PanelGeometry(
        boundary=Polygon3D(points=(
            Point3(x=1,y=.5,z=2.9),Point3(x=3,y=.5,z=3.1),
            Point3(x=3,y=2.5,z=3.1),Point3(x=1,y=2.5,z=2.9))),
        thickness_m=.2,normal=slope_normal))
    with pytest.raises(ContractError,match="sloped decks are unsupported"):
        _valid_ceiling_model(deck_component=True,components=(sloped_panel,))


def test_hanger_wire_requires_real_top_and_bottom_surface_anchors():
    ceiling=Ceiling(id="ceiling:grid",level_id="level:main",footprint=Polygon3D(points=(
        Point3(x=0,y=0,z=2),Point3(x=4,y=0,z=2),Point3(x=4,y=3,z=2),Point3(x=0,y=3,z=2))),
        thickness_m=.2,provenance=_wall().provenance)
    slab=Slab(id="slab:deck",level_id="level:main",footprint=Polygon3D(points=(
        Point3(x=0,y=0,z=3),Point3(x=4,y=0,z=3),Point3(x=4,y=3,z=3),Point3(x=0,y=3,z=3))),
        thickness_m=.2,provenance=_wall().provenance)
    source=_source_model(ceiling=ceiling,slabs=(slab,))
    q=Quaternion(x=0,y=math.sqrt(.5),z=0,w=math.sqrt(.5))
    profile=Profile2D(kind="t_profile",overall_width_m=.1,overall_depth_m=.05,
        web_thickness_m=.01,flange_thickness_m=.01)
    runner=_component("assembly:ceiling-wire",part_key="runner:main:0",kind="tbar_main_runner",
        geometry=ProfileExtrusion(profile=profile,pose=Pose(position=Point3(x=0,y=.5,z=2),rotation=q),length_m=3))
    top=Point3(x=1.5,y=.5,z=2.8); bottom=Point3(x=1.5,y=.5,z=1.95)
    wire=_component("assembly:ceiling-wire",part_key="hanger:station:0",kind="hanger_wire",
        geometry=WireSweep(centerline=Polyline3D(points=(top,bottom)),diameter_m=.003,end_treatment="cut"),
        hanger_attachment=HangerAttachment(
            top_host_id="slab:deck",top_anchor=top,bottom_runner_id=runner.id,bottom_anchor=bottom))
    parts=(runner,wire)
    assignment=_assembly(source,parts,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling-wire",deck_id="slab:deck",params=())
    model=_install(source,parts,assignment)
    assert model.assembly_components[1].hanger_attachment.bottom_runner_id==runner.id
    bad=replace(wire,hanger_attachment=replace(wire.hanger_attachment,bottom_anchor=Point3(x=1.5,y=.58,z=1.95)))
    bad_parts=(runner,bad)
    bad_assignment=_assembly(source,bad_parts,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling-wire",deck_id="slab:deck",params=())
    with pytest.raises(ContractError,match="wire endpoints must match"):
        _install(source,bad_parts,bad_assignment)
    wrong_anchor=Point3(x=1.5,y=.52,z=1.98)  # in the AABB, outside the T section material
    wrong_top=Point3(x=1.5,y=.52,z=2.8)
    wrong_surface=replace(wire,
        geometry=WireSweep(centerline=Polyline3D(points=(wrong_top,wrong_anchor)),diameter_m=.003,end_treatment="cut"),
        hanger_attachment=replace(wire.hanger_attachment,top_anchor=wrong_top,bottom_anchor=wrong_anchor))
    # Keep the endpoint exact but move the runner's cross-section surface: the
    # anchor is inside the extrusion envelope, not on its actual profile.
    altered_runner=replace(runner,geometry=replace(runner.geometry,profile=Profile2D(
        kind="t_profile",overall_width_m=.1,overall_depth_m=.05,
        web_thickness_m=.005,flange_thickness_m=.005)))
    wrong_parts=(altered_runner,wrong_surface)
    wrong_assignment=_assembly(source,wrong_parts,kind="suspended_ceiling",host_id="ceiling:grid",
        assembly_id="assembly:ceiling-wire",deck_id="slab:deck",params=())
    with pytest.raises(ContractError,match="bottom anchor is not on its named physical runner surface"):
        _install(source,wrong_parts,wrong_assignment)


def test_slab_hanger_surface_uses_footprint_top_and_downward_underside():
    slab=_valid_ceiling_model().slabs[0]
    assert point_on_slab_surface(slab,Point3(x=1,y=1,z=3.0))
    assert point_on_slab_surface(slab,Point3(x=1,y=1,z=2.8))
    assert not point_on_slab_surface(slab,Point3(x=1,y=1,z=2.9))
    assert not point_on_slab_surface(slab,Point3(x=5,y=1,z=3.0))


def test_schema_draft_is_strict_for_union_parameter_units_and_v16_root():
    schema=json.loads(SCHEMA_PATH.read_text())
    assert schema["$id"].endswith("oabm-model-v1.6.schema.json")
    Draft202012Validator.check_schema(schema)
    model=_valid_wall_model()
    document=model.to_dict()
    Draft202012Validator(schema).validate(document)
    decoded=BuildingModel.from_json(model.to_json())
    assert decoded==model
    assert decoded.building_assemblies[0].input_digest==model.building_assemblies[0].input_digest
    assert BuildingModel.from_json(decoded.to_json()).to_json()==decoded.to_json()
    bad=copy.deepcopy(document)
    bad["building_assemblies"][0]["resolved_parameters"][0]["unit"]="mm"
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    with pytest.raises(ContractError,match="unit must be 'm'"):
        BuildingModel.from_dict(bad)
    bad=copy.deepcopy(document)
    bad["assembly_components"][0]["geometry"]["kind"]="freeform_brep"
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    bad=copy.deepcopy(document)
    bad["assembly_components"][0]["unrecognized_geometry_hint"]="guess"
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    bad=copy.deepcopy(document)
    bad["building_assemblies"][0]["component_ids"].append(bad["building_assemblies"][0]["component_ids"][0])
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    bad=copy.deepcopy(document)
    bad["building_assemblies"][0]["catalog_id"]="x"*2049
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    with pytest.raises(ContractError,match="must be non-empty text"):
        BuildingModel.from_dict(bad)
    bad=copy.deepcopy(document)
    bad["assembly_components"][0]["provenance"][0]["scope_paths"]=["name"]
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    with pytest.raises(ContractError,match="geometry"):
        BuildingModel.from_dict(bad)

    # Manufacturer outlines need source-bound points_m evidence in both
    # Python and the published Draft 2020-12 structure.
    outline=ProfileOutline2D(source_id="manufacturer:profile-x",
        points_m=(Point2D(x=0,y=0),Point2D(x=.1,y=0),
            Point2D(x=.1,y=.2),Point2D(x=0,y=.2)),
        provenance=(Provenance(source_kind="manufacturer",source_id="manufacturer:profile-x",
            derivation="observed",scope_paths=("points_m",)),))
    profile_component=_component(geometry=ProfileExtrusion(profile=outline,
        pose=Pose(position=Point3(x=0,y=-.1,z=0)),length_m=3.0),
        geometry_basis="manufacturer_actual_geometry",
        provenance=_component_provenance("geometry",observed=True))
    outline_model=_valid_wall_model(components=(profile_component,))
    Draft202012Validator(schema).validate(outline_model.to_dict())
    bad_outline=copy.deepcopy(outline_model.to_dict())
    bad_outline["assembly_components"][0]["geometry"]["profile"]["provenance"][0]["scope_paths"]=["name"]
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad_outline)
    with pytest.raises(ContractError,match="points_m provenance"):
        ProfileOutline2D(source_id="manufacturer:profile-x",
            points_m=outline.points_m,
            provenance=(Provenance(source_kind="manufacturer",source_id="manufacturer:profile-x",
                derivation="observed",scope_paths=("name",)),))
    with pytest.raises(ContractError,match="points_m provenance"):
        BuildingModel.from_dict(bad_outline)

    for basis,derivation in (("plan_specified","observed"),("caller_selected","user")):
        material_model=_valid_wall_model(components=(_component(
            material_id="material:wood",material_basis=basis,
                provenance=(_component_provenance("geometry")[0],Provenance(
                source_kind="plan" if basis=="plan_specified" else "user_override",
                source_id="source:material",derivation=derivation,
                scope_paths=("material_id",)))),))
        Draft202012Validator(schema).validate(material_model.to_dict())
        assert BuildingModel.from_json(material_model.to_json())==material_model

    for basis in ("plan_specified","caller_selected"):
        bad=copy.deepcopy(document)
        bad["assembly_components"][0]["material_id"]="material:wood"
        bad["assembly_components"][0]["material_basis"]=basis
        with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
        with pytest.raises(ContractError,match="material_id provenance"):
            BuildingModel.from_dict(bad)

    # A mixed material record is a cross-record semantic check: ordinary
    # structural schema permits the evidence rows but Python refuses the
    # inconsistent manufacturer-actual claim.
    actual_material=_component(material_id="material:steel",material_basis="manufacturer_actual",
        provenance=(_component_provenance("geometry")[0],Provenance(
            source_kind="manufacturer",source_id="manufacturer:steel",derivation="observed",
            scope_paths=("material_id",))))
    mixed=copy.deepcopy(_valid_wall_model(components=(actual_material,)).to_dict())
    mixed["assembly_components"][0]["provenance"].append({
        "source_kind":"preset","source_id":"preset:steel","source_element_id":None,
        "page":None,"method":None,"confidence":1.0,"derivation":"inferred",
        "scope_paths":["material_id"],"attributes":{},
    })
    Draft202012Validator(schema).validate(mixed)
    with pytest.raises(ContractError,match="manufacturer_actual material requires observed"):
        BuildingModel.from_dict(mixed)
    bad=copy.deepcopy(document)
    selection=next(row for row in bad["building_assemblies"][0]["provenance"]
                   if row.get("derivation")=="user")
    selection["scope_paths"]=["preset_id"]
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    with pytest.raises(ContractError,match="field-qualify selection"):
        BuildingModel.from_dict(bad)
    # Draft 2020-12 cannot express uniqueness by an array item's property;
    # this structural document remains schema-valid but Python rejects the
    # duplicate semantic parameter key before accepting the canonical model.
    duplicate=copy.deepcopy(document)
    row=copy.deepcopy(duplicate["building_assemblies"][0]["resolved_parameters"][0])
    row["value"]+=.001
    duplicate["building_assemblies"][0]["resolved_parameters"].append(row)
    Draft202012Validator(schema).validate(duplicate)
    with pytest.raises(ContractError,match="duplicate resolved parameter IDs"):
        BuildingModel.from_dict(duplicate)

    evidence_model=_valid_wall_model(params=_wall_parameters()+(
        _parameter("wall.observed_thickness_m",.2,"length","m",origin="plan_observed"),
        _parameter("wall.user_spacing_m",.4,"length","m",origin="user_override"),
    ))
    evidence_document=evidence_model.to_dict()
    Draft202012Validator(schema).validate(evidence_document)
    assert BuildingModel.from_json(evidence_model.to_json())==evidence_model
    bad=copy.deepcopy(evidence_document)
    observed=next(row for row in bad["building_assemblies"][0]["resolved_parameters"]
                  if row["origin"]=="plan_observed")
    observed["source_field_path"]=None
    with pytest.raises(ValidationError): Draft202012Validator(schema).validate(bad)
    with pytest.raises(ContractError): BuildingModel.from_dict(bad)

