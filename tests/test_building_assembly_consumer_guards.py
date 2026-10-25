"""Fail-closed consumer boundaries for populated canonical 1.6 assemblies."""

from __future__ import annotations

import json
from dataclasses import replace

import ifcopenshell.api.root
import pytest

from oabm.assembly_compat import require_no_populated_building_assemblies
from oabm.drawings import (
    ElevationSpec, PlanSpec, SectionSpec, generate_drawing_set,
    generate_elevation, generate_plan, generate_schedules, generate_section,
)
from oabm.exports.review_bundle import build_review_bundle
from oabm.ifc import IfcAdapterError, canonical_id_to_ifc_guid, from_ifc, round_trip, to_ifc
from oabm.ifc.adapter import _add_canonical_pset
from oabm.model import (
    AssemblyComponent, Box3D, BuildingAssembly, BuildingModel,
    Level, Point3, Polyline3D, Pose, Provenance,
    ResolvedParameter, Size3, Vector3, Wall, WallAssemblyFrame,
    compute_assembly_digests, compute_assembly_input_digest, compute_assembly_output_digest,
    stable_assembly_component_id, upgrade_to_v1_6, validate_model,
)
from oabm.qa.confidence_review import ReviewConfig, build_confidence_review
from oabm.qa.revision_diff import build_revision_diff
from oabm.reports.panel_schedules import generate_panel_schedule_report
from oabm.reports.scope_ledger import ScopePolicy, generate_scope_ledger
from oabm.model import RuleReference
from oabm.routing import (
    RoutingError, consolidate_bundled_routes, find_overlapping_route_runs,
    route_between_ports,
)


def _populated_model() -> BuildingModel:
    """One valid synthetic wall assembly, independent of contract-test helpers."""
    level = Level(id="level:main", elevation_m=0.0, height_m=3.0)
    wall = Wall(
        id="wall:north", level_id=level.id,
        centerline=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0), Point3(x=4.0, y=0.0, z=0.0),
        )),
        thickness_m=0.20, height_m=3.0, construction="framed",
        provenance=(Provenance(
            source_kind="plan", source_id="sheet:synthetic", derivation="observed",
        ),),
    )
    source = BuildingModel(
        model_id="model:assembly-consumer-guard", name="Synthetic wall",
        schema_version="1.5.0", levels=(level,), walls=(wall,),
    )

    parameters = tuple(
        ResolvedParameter(
            parameter_id=key, value_kind="length", value=value, unit="m",
            origin="preset_default",
            provenance=(Provenance(
                source_kind="preset_parameter", source_id="catalog:public",
                source_element_id=json.dumps(
                    ("r1", "preset:steel-wall", "1.0.0", key), separators=(",", ":"),
                ), method="parameter-resolution", derivation="inferred",
                scope_paths=("value", "unit", "origin"),
            ),),
            source_entity_id=None, source_field_path=None,
        )
        for key, value in (
            ("wall.stack.framing_depth_m", 0.09),
            ("wall.stack.face_left.gypsum", 0.055),
            ("wall.stack.face_right.gypsum", 0.055),
        )
    )
    part_key = "stud:station:0"
    assembly_id = "assembly:wall"
    component = AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id, part_key=part_key, level_id=level.id,
        component_kind="stud",
        geometry=Box3D(
            pose=Pose(position=Point3(x=0.05, y=0.0, z=1.5)),
            size=Size3(x=0.1, y=0.2, z=3.0),
        ),
        geometry_basis="specified_parameterized_geometry", material_id=None,
        material_basis="unknown", opening_id=None,
        provenance=(Provenance(
            source_kind="selected-preset", source_id="preset:wall-v1",
            method="component-geometry", derivation="inferred",
            scope_paths=("geometry",),
        ),),
    )
    selection_ref = json.dumps(
        ("catalog:public", "r1", "preset:steel-wall", "1.0.0"), separators=(",", ":"),
    )
    assignment_provenance = (
        Provenance(
            source_kind="selection", source_id="agent:synthetic",
            source_element_id=selection_ref, method="preset-selection", derivation="user",
            scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by"),
        ),
        Provenance(
            source_kind="plan", source_id="sheet:synthetic",
            derivation="observed", scope_paths=("host_id", "input_digest"),
        ),
    )
    frame = WallAssemblyFrame(
        origin=wall.centerline.points[0], axis=Vector3(x=1.0, y=0.0, z=0.0),
        left_normal=Vector3(x=0.0, y=1.0, z=0.0), base_z_m=0.0,
    )
    provisional = BuildingAssembly(
        id=assembly_id, assembly_kind="wall", level_id=level.id, host_id=wall.id,
        host_representation="aggregate_host", catalog_id="catalog:public",
        catalog_revision="r1", preset_id="preset:steel-wall", preset_revision="1.0.0",
        selected_by="agent:synthetic", generator_version="guard-test/1",
        resolved_parameters=parameters, component_ids=(component.id,),
        input_digest="0" * 64, output_digest="0" * 64, wall_frame=frame,
        deck_id=None, provenance=assignment_provenance,
    )
    assembly = replace(
        provisional,
        input_digest=compute_assembly_input_digest(source, provisional),
        output_digest=compute_assembly_output_digest((component,)),
    )
    model = replace(
        upgrade_to_v1_6(source), building_assemblies=(assembly,),
        assembly_components=(component,),
    )
    validate_model(model)
    return model


def _empty_assembly_model(model: BuildingModel) -> BuildingModel:
    return replace(model, building_assemblies=(), assembly_components=())


@pytest.fixture(scope="module")
def populated_model() -> BuildingModel:
    return _populated_model()


def test_fixture_is_valid_populated_16_and_keeps_model_serialization_stable(populated_model):
    assert populated_model.schema_version == "1.6.0"
    assert len(populated_model.building_assemblies) == 1
    assert len(populated_model.assembly_components) == 1
    before = populated_model.to_dict()
    validate_model(populated_model)
    assert populated_model.to_dict() == before
    assembly = populated_model.building_assemblies[0]
    assert compute_assembly_digests(
        populated_model, assembly, populated_model.assembly_components,
    ) == (assembly.input_digest, assembly.output_digest)


def test_ifc_export_and_round_trip_refuse_before_writing_or_returning(populated_model, tmp_path):
    before = populated_model.to_dict()
    destination = tmp_path / "partial.ifc"
    with pytest.raises(IfcAdapterError, match="IFC export does not support populated canonical building assemblies"):
        to_ifc(populated_model, destination)
    assert not destination.exists()
    with pytest.raises(IfcAdapterError, match="IFC round trip does not support populated canonical building assemblies"):
        round_trip(populated_model)
    assert populated_model.to_dict() == before


@pytest.mark.parametrize(
    ("consumer", "invoke"),
    [
        ("generate_drawing_set", lambda m: generate_drawing_set(m)),
        ("generate_plan", lambda m: generate_plan(m, PlanSpec(id="plan:test", level_id="level:main"))),
        ("generate_elevation", lambda m: generate_elevation(m, ElevationSpec(
            id="elevation:test", origin=Point3(x=0, y=0, z=0), direction=Vector3(x=1, y=0, z=0),
        ))),
        ("generate_section", lambda m: generate_section(m, SectionSpec(
            id="section:test", origin=Point3(x=0, y=0, z=0), direction=Vector3(x=1, y=0, z=0),
        ))),
        ("generate_schedules", lambda m: generate_schedules(m)),
    ],
)
def test_each_public_drawing_entry_refuses_populated_assemblies(populated_model, consumer, invoke):
    with pytest.raises(ValueError, match=f"{consumer} does not support populated canonical building assemblies"):
        invoke(populated_model)


def test_routing_entries_refuse_before_output_and_leave_model_unchanged(populated_model):
    before = populated_model.to_dict()
    with pytest.raises(RoutingError, match="route_between_ports does not support populated canonical building assemblies"):
        route_between_ports(populated_model, "missing:start", "missing:end", "emt")
    with pytest.raises(RoutingError, match="consolidate_bundled_routes does not support populated canonical building assemblies"):
        consolidate_bundled_routes(populated_model)
    assert populated_model.to_dict() == before


@pytest.mark.parametrize("populated_side", ["base", "target"])
def test_revision_diff_refuses_either_populated_snapshot(populated_model, populated_side):
    empty = _empty_assembly_model(populated_model)
    base, target = ((populated_model, empty) if populated_side == "base" else (empty, populated_model))
    with pytest.raises(ValueError, match=f"build_revision_diff {populated_side} snapshot does not support populated canonical building assemblies"):
        build_revision_diff(base, target)


def test_confidence_review_and_review_bundle_fail_closed_before_partial_manifest(populated_model):
    with pytest.raises(ValueError, match="build_confidence_review does not support populated canonical building assemblies"):
        build_confidence_review(populated_model, ReviewConfig(confidence_threshold=0.5))
    with pytest.raises(ValueError, match="build_review_bundle does not support populated canonical building assemblies"):
        build_review_bundle(
            populated_model, glb_bytes=b"", model_reference="", glb_reference="",
            confidence_manifest={}, confidence_reference="",
            source_reference_mapping_reference="", source_references={},
        )


def test_electrical_reports_and_route_overlap_analysis_are_not_gated(populated_model):
    panel_report = generate_panel_schedule_report(populated_model)
    assert panel_report.source_model_id == populated_model.model_id
    assert panel_report.panels == ()
    scope_policy = ScopePolicy(
        version="1.0.0", mode="preserve_unresolved",
        basis=RuleReference(authority="synthetic caller", revision="r1", rule_id="scope.test"),
        actor="synthetic reviewer",
        provenance=(Provenance(
            source_kind="user", source_id="fixture:policy", derivation="user",
            scope_paths=("mode", "basis", "actor"),
        ),),
    )
    assert generate_scope_ledger(populated_model, policy=scope_policy).rows == ()
    assert find_overlapping_route_runs(populated_model) == ()


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"])
def test_shared_gate_allows_empty_legacy_and_empty_upgraded_models(version):
    model = BuildingModel(model_id=f"model:empty-{version}", schema_version=version)
    if version == "1.6.0":
        model = upgrade_to_v1_6(BuildingModel(model_id="model:empty-upgraded"))
    assert not model.building_assemblies and not model.assembly_components
    assert require_no_populated_building_assemblies(model, "test consumer") is None


def test_empty_16_can_still_serialize_validate_draw_and_export(populated_model):
    empty = _empty_assembly_model(populated_model)
    validate_model(empty)
    payload = empty.to_dict()
    assert payload["building_assemblies"] == []
    assert payload["assembly_components"] == []
    assert generate_schedules(empty) == ()
    assert generate_drawing_set(empty).source_index
    assert to_ifc(empty).schema == "IFC4"
    assert round_trip(empty).to_dict() == empty.to_dict()


def test_from_ifc_still_rejects_unknown_canonical_entity_kinds():
    empty = upgrade_to_v1_6(BuildingModel(
        model_id="model:unknown-ifc-kind", name="Unknown IFC kind",
    ))
    ifc = to_ifc(empty)
    item = ifcopenshell.api.root.create_entity(
        ifc, ifc_class="IfcBuildingElementProxy", name="Unsupported assembly record",
    )
    item.GlobalId = canonical_id_to_ifc_guid("unknown:assembly-record")
    _add_canonical_pset(ifc, item, "building_assembly", 0, {
        "id": "assembly:unsupported", "schema_version": "1.6.0", "provenance": [],
    })
    with pytest.raises(IfcAdapterError, match="unknown OABM canonical kind 'building_assembly'"):
        from_ifc(ifc)
