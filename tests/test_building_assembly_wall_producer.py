"""Public-safe known-answer tests for the first preset-driven wall producer."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import math

import pytest

from oabm.building_assemblies import (
    BuildingAssemblyPresetCatalog,
    DEFAULT_PRESET_CATALOG,
    LEGACY_PRESET_CATALOG,
    LEGACY_WALL_GENERATOR_VERSION,
    OpeningFramingSpecification,
    ParameterOverride,
    WallAssemblyRequest,
    WallPresetError,
    build_wall_assembly,
    resolve_wall_preset,
)
from oabm.building_assemblies.fixtures import (
    SYNTHETIC_ASSEMBLY_ID,
    SYNTHETIC_DOOR_ID,
    SYNTHETIC_WALL_ID,
    SYNTHETIC_WINDOW_ID,
    SYNTHETIC_OPENING_FRAMING_SPECS,
    synthetic_two_opening_wall_assembly,
    synthetic_two_opening_wall_source,
)
from oabm.model import (
    AssemblyComponent,
    Box3D,
    BuildingModel,
    ContractError,
    Opening,
    PanelGeometry,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Quaternion,
    Route,
    Size3,
    Vector3,
    Wall,
    compute_assembly_digests,
    component_world_points,
    stable_assembly_component_id,
)
from oabm.model.assembly_validation import _opening_clearance


def _request(*, assembly_id=SYNTHETIC_ASSEMBLY_ID, overrides=(), specs=SYNTHETIC_OPENING_FRAMING_SPECS):
    return WallAssemblyRequest(
        assembly_id=assembly_id,
        wall_id=SYNTHETIC_WALL_ID,
        selected_by="agent:test-selector",
        parameter_overrides=tuple(overrides),
        opening_framing_specs=tuple(specs),
    )


def _wall_frame_projection(wall: Wall, point: Point3):
    first, last = wall.centerline.points
    dx, dy = last.x - first.x, last.y - first.y
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    x, y, z = ((point.x, point.y, point.z) if isinstance(point, Point3) else point)
    return (
        (x - first.x) * ux + (y - first.y) * uy,
        (x - first.x) * nx + (y - first.y) * ny,
        z,
    )


def _local_component_bounds(wall: Wall, component: AssemblyComponent):
    points = tuple(_wall_frame_projection(wall, point)
                   for point in component_world_points(component))
    return (
        tuple(min(row[index] for row in points) for index in range(3)),
        tuple(max(row[index] for row in points) for index in range(3)),
    )


def test_versioned_catalog_is_finite_immutable_and_strictly_decoded():
    catalog = DEFAULT_PRESET_CATALOG
    assert catalog.catalog_version == "1"
    assert catalog.catalog_revision == "2.0.0"
    assert tuple(row.preset_id for row in catalog.wall_presets) == ("wall.wood_rect_stock",)
    assert BuildingAssemblyPresetCatalog.from_dict(catalog.to_dict()) == catalog
    assert BuildingAssemblyPresetCatalog.from_json(catalog.to_json(indent=None)) == catalog
    assert LEGACY_PRESET_CATALOG.catalog_revision == "1.0.0"
    assert LEGACY_PRESET_CATALOG.wall_presets[0].preset_revision == "1.0.0"
    assert LEGACY_PRESET_CATALOG.wall_presets[0].generator_kind == "wood_rect_stock_v1"
    assert LEGACY_WALL_GENERATOR_VERSION == "oabm-wall-producer/1.0.0"
    assert BuildingAssemblyPresetCatalog.from_json(
        LEGACY_PRESET_CATALOG.to_json(indent=None)
    ) == LEGACY_PRESET_CATALOG

    bad = catalog.to_dict()
    bad["not_a_catalog_field"] = "ignored fields are forbidden"
    with pytest.raises(ContractError, match="extra=.*not_a_catalog_field"):
        BuildingAssemblyPresetCatalog.from_dict(bad)
    with pytest.raises(ContractError, match="duplicate catalog JSON key"):
        BuildingAssemblyPresetCatalog.from_json('{"catalog_version":"1","catalog_version":"1"}')

    bad = catalog.to_dict()
    bad["wall_presets"][0]["parameters"][0]["unit"] = "mm"
    with pytest.raises(ContractError, match="unit must be 'm'"):
        BuildingAssemblyPresetCatalog.from_dict(bad)

    bad = catalog.to_dict()
    bad["wall_presets"].append(dict(bad["wall_presets"][0]))
    with pytest.raises(ContractError, match="preset IDs must be unique"):
        BuildingAssemblyPresetCatalog.from_dict(bad)


def test_resolver_snapshots_explicit_defaults_and_distinct_typed_overrides():
    default_request = _request()
    default = resolve_wall_preset(default_request)
    assert len(default.parameters) == 13
    assert {row.origin for row in default.parameters} == {"preset_default"}
    spacing = default.parameter_map()["wall.stud.spacing_m"]
    assert spacing.value == 0.4064
    assert spacing.value_kind == "length" and spacing.unit == "m"
    assert spacing.provenance[0].derivation == "inferred"
    assert spacing.provenance[0].source_kind == "preset_parameter"
    assert spacing.provenance[0].attributes["default_basis"] == "illustrative_modeling_assumption"

    override = ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.6096, unit="m",
    )
    resolved = resolve_wall_preset(_request(overrides=(override,)))
    selected = resolved.parameter_map()["wall.stud.spacing_m"]
    assert selected.value == 0.6096
    assert selected.origin == "user_override"
    assert selected.provenance[0].derivation == "user"
    assert selected.provenance[0].source_kind == "user_override"
    assert selected.provenance[0].source_element_id == selected.parameter_id
    assert all(row.origin == "preset_default" for row in resolved.parameters if row.parameter_id != selected.parameter_id)

    with pytest.raises(WallPresetError, match="must be length in m"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.stud.spacing_m", value_kind="length",
            value=16.0, unit="in",
        ),)))
    with pytest.raises(WallPresetError, match="invalid typed value"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.plate.top_count", value_kind="count",
            value=2.0, unit="each",
        ),)))
    with pytest.raises(WallPresetError, match="unsupported wall parameter overrides"):
        resolve_wall_preset(_request(overrides=(ParameterOverride(
            parameter_id="wall.structural_capacity", value_kind="token",
            value="unknown", unit="1",
        ),)))
    with pytest.raises(WallPresetError, match="unknown or unsupported wall preset"):
        resolve_wall_preset(replace(default_request, preset_id="wall.cold_formed_c_u"))


def test_builtin_wall_preset_identity_is_reserved_but_custom_revision_is_caller_owned():
    published = DEFAULT_PRESET_CATALOG.wall_presets[0]
    changed_parameters = tuple(
        replace(row, default_value=0.5) if row.parameter_id == "wall.stud.spacing_m" else row
        for row in published.parameters
    )
    changed_builtin = replace(published, parameters=changed_parameters)
    tampered_catalog = replace(DEFAULT_PRESET_CATALOG, wall_presets=(changed_builtin,))
    with pytest.raises(WallPresetError, match="published wall preset identity .* is reserved"):
        resolve_wall_preset(_request(), catalog=tampered_catalog)

    # A typed override remains valid with the exact published recipe.
    override = ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length", value=0.5, unit="m",
    )
    assert resolve_wall_preset(_request(overrides=(override,))).parameter_map()[
        "wall.stud.spacing_m"
    ].origin == "user_override"

    # Revisions owned by a caller can change the same finite recipe defaults.
    custom_preset = replace(changed_builtin, preset_revision="2.0.0")
    custom_catalog = replace(
        DEFAULT_PRESET_CATALOG,
        catalog_id="catalog:custom-walls",
        catalog_revision="2.0.0",
        wall_presets=(custom_preset,),
    )
    resolved = resolve_wall_preset(_request(), catalog=custom_catalog)
    assert resolved.preset.preset_revision == "2.0.0"
    assert resolved.parameter_map()["wall.stud.spacing_m"].value == pytest.approx(0.5)
    assert resolved.parameter_map()["wall.stud.spacing_m"].origin == "preset_default"


def test_legacy_simplified_recipe_is_reserved_decodable_but_not_a_new_build_default():
    legacy = LEGACY_PRESET_CATALOG.wall_presets[0]
    decoded = resolve_wall_preset(_request(), catalog=LEGACY_PRESET_CATALOG)
    assert decoded.preset == legacy
    assert len(decoded.parameters) == 14
    with pytest.raises(WallPresetError, match="historical single-jamb v1 recipe is decode-only"):
        build_wall_assembly(
            synthetic_two_opening_wall_source(), _request(), catalog=LEGACY_PRESET_CATALOG,
        )
    changed = replace(legacy, parameters=tuple(
        replace(row, default_value=0.0508)
        if row.parameter_id == "wall.opening.header_height_m" else row
        for row in legacy.parameters
    ))
    reserved = replace(LEGACY_PRESET_CATALOG, wall_presets=(changed,))
    with pytest.raises(WallPresetError, match="published wall preset identity .* is reserved"):
        resolve_wall_preset(_request(), catalog=reserved)


def test_wall_catalog_cannot_redefine_referenced_builtin_sources_but_new_source_ids_work():
    published_preset = DEFAULT_PRESET_CATALOG.wall_presets[0]
    builtin_source = next(row for row in DEFAULT_PRESET_CATALOG.sources
                          if row.source_id == "nist_ps20_25")
    changed_source = replace(builtin_source, locator=builtin_source.locator + "; altered source claim")
    custom_revision = replace(published_preset, preset_revision="2.0.0")
    tampered_source_catalog = replace(
        DEFAULT_PRESET_CATALOG,
        catalog_id="catalog:custom-walls",
        catalog_revision="2.0.0",
        sources=tuple(changed_source if row.source_id == builtin_source.source_id else row
                      for row in DEFAULT_PRESET_CATALOG.sources),
        wall_presets=(custom_revision,),
    )
    with pytest.raises(WallPresetError, match="published wall catalog source ID 'nist_ps20_25' is reserved"):
        resolve_wall_preset(_request(), catalog=tampered_source_catalog)

    caller_source_id = "source:custom-wood-dimensions"
    caller_source = replace(builtin_source, source_id=caller_source_id)
    caller_preset = replace(
        custom_revision,
        parameters=tuple(replace(row, source_id=caller_source_id)
                         if row.source_id == builtin_source.source_id else row
                         for row in custom_revision.parameters),
    )
    caller_catalog = replace(
        tampered_source_catalog,
        sources=tuple(caller_source if row.source_id == builtin_source.source_id else row
                      for row in DEFAULT_PRESET_CATALOG.sources),
        wall_presets=(caller_preset,),
    )
    resolved = resolve_wall_preset(_request(), catalog=caller_catalog)
    assert resolved.parameter_map()["wall.stock.wall_axis_width_m"].provenance[0].source_id == \
        "catalog:custom-walls"
    assert resolved.parameter_map()["wall.stock.wall_axis_width_m"].value == pytest.approx(0.0381)


def test_canonical_validator_consumes_nonzero_producer_opening_clearance():
    clearance = 0.05
    model = build_wall_assembly(
        synthetic_two_opening_wall_source(),
        _request(overrides=(ParameterOverride(
            parameter_id="wall.opening_clearance_m", value_kind="length",
            value=clearance, unit="m",
        ),)),
    )
    assembly = model.building_assemblies[0]
    parameter = next(row for row in assembly.resolved_parameters
                     if row.parameter_id == "wall.opening_clearance_m")
    assert parameter.value == clearance
    assert parameter.origin == "user_override"
    # The returned BuildingModel was validated by this contract function. It
    # must see the producer's exact nonzero envelope rather than defaulting to 0.
    assert _opening_clearance(assembly) == clearance


def test_override_count_limit_rejects_before_scanning_override_rows():
    # Invalid rows would otherwise fail the per-item type check. The count
    # guard must fire first, before scanning or uniqueness tuple/set allocation.
    too_many_invalid_rows = (object(),) * 15
    with pytest.raises(WallPresetError, match="override count exceeds the finite v1 limit 14"):
        WallAssemblyRequest(
            assembly_id=SYNTHETIC_ASSEMBLY_ID,
            wall_id=SYNTHETIC_WALL_ID,
            selected_by="agent:test-selector",
            parameter_overrides=too_many_invalid_rows,
        )


def test_two_opening_wall_known_answer_fit_voids_ids_counts_materials_and_digests():
    source = synthetic_two_opening_wall_source()
    source_json = source.to_json(indent=None)
    model = build_wall_assembly(source, _request(assembly_id=SYNTHETIC_ASSEMBLY_ID))
    wall = model.walls[0]
    assembly = model.building_assemblies[0]
    parts = model.assembly_components

    assert source.to_json(indent=None) == source_json
    assert model.openings == source.openings
    assert model.schema_version == "1.6.0"
    assert wall.thickness_m == 0.1143 and wall.height_m == 2.4384
    assert math.dist(
        (wall.centerline.points[0].x, wall.centerline.points[0].y),
        (wall.centerline.points[1].x, wall.centerline.points[1].y),
    ) == 4.8
    assert assembly.assembly_kind == "wall"
    assert assembly.host_id == SYNTHETIC_WALL_ID
    assert assembly.host_representation == "aggregate_host"
    assert assembly.preset_id == "wall.wood_rect_stock"
    assert assembly.catalog_revision == "2.0.0"
    assert assembly.generator_version == "oabm-wall-producer/2.0.0"
    assert set(assembly.component_ids) == {part.id for part in parts}
    assert assembly.component_ids == tuple(sorted(assembly.component_ids))
    assert all(part.id == stable_assembly_component_id(assembly.id, part.part_key) for part in parts)
    assert all(part.geometry_basis == "specified_parameterized_geometry" for part in parts)
    assert all(part.material_id is None and part.material_basis == "unknown" for part in parts)
    assert all(part.provenance[0].derivation == "inferred" for part in parts)
    assert all("geometry" in part.provenance[0].scope_paths for part in parts)
    assert any(isinstance(part.geometry, Box3D) for part in parts)
    assert any(isinstance(part.geometry, PanelGeometry) for part in parts)
    assert not any(part.component_kind in {"door_leaf", "door_frame", "window_frame", "window_glazing"} for part in parts)

    counts = Counter(part.component_kind for part in parts)
    assert counts == Counter({
        "bottom_plate": 2,
        "top_plate": 2,
        "stud": 15,
        "cripple": 6,
        "header": 6,
        "sill": 1,
        "drywall_panel": 24,
    })
    parameters = {row.parameter_id: row.value for row in assembly.resolved_parameters}
    assert abs(math.fsum((
        parameters["wall.stack.framing_depth_m"],
        parameters["wall.stack.face_left.layer_1_m"],
        parameters["wall.stack.face_right.layer_1_m"],
    )) - wall.thickness_m) <= 1e-9
    assert parameters["wall.opening_clearance_m"] == 0.0

    # Exact parameterized section values and the explicitly labeled illustrative
    # construction assumptions are visible in the resolved evidence snapshot.
    assert parameters["wall.stock.wall_axis_width_m"] == 0.0381
    assert parameters["wall.stack.framing_depth_m"] == 0.0889
    assert parameters["wall.drywall.thickness_m"] == 0.0127
    for parameter_id in (
        "wall.stud.spacing_m", "wall.stud.start_offset_m", "wall.stud.end_offset_m",
        "wall.plate.bottom_count", "wall.plate.top_count", "wall.opening.sill_height_m",
    ):
        record = next(row for row in assembly.resolved_parameters if row.parameter_id == parameter_id)
        assert record.origin == "preset_default"
        assert record.provenance[0].attributes["default_basis"] == "illustrative_modeling_assumption"

    bottoms = [part for part in parts if part.component_kind == "bottom_plate"]
    tops = [part for part in parts if part.component_kind == "top_plate"]
    headers = [part for part in parts if part.component_kind == "header"]
    header_plies = [part for part in headers if part.provenance[0].attributes.get("framing_role") == "header_ply"]
    header_spacers = [part for part in headers if part.provenance[0].attributes.get("framing_role") == "header_spacer"]
    sills = [part for part in parts if part.component_kind == "sill"]
    drywall = [part for part in parts if part.component_kind == "drywall_panel"]
    king_studs = [part for part in parts if part.provenance[0].attributes.get("framing_role") == "king"]
    jack_studs = [part for part in parts if part.provenance[0].attributes.get("framing_role") == "jack"]
    cripples = [part for part in parts if part.component_kind == "cripple"]
    ordinary_studs = [part for part in parts if part.provenance[0].attributes.get("framing_role") == "ordinary"]
    assert len(bottoms) == 2  # the bottom plate is cut at the door opening
    assert len(tops) == 2
    assert all(isinstance(part.geometry, Box3D) for part in (*bottoms, *tops, *headers, *sills))
    assert all(abs(part.geometry.size.y - 0.0889) <= 1e-12 for part in (*bottoms, *tops, *sills))
    assert all(abs(part.geometry.size.z - 0.0381) <= 1e-12 for part in (*tops, *sills))
    assert all(abs(part.geometry.size.x - 4.8) <= 1e-12 for part in tops)
    assert len(header_plies) == 4 and len(header_spacers) == 2
    assert {part.geometry.size.x for part in headers} == {0.9906, 1.0922}
    assert {part.geometry.size.y for part in header_plies} == {0.0381}
    assert {part.geometry.size.y for part in header_spacers} == {0.0127}
    assert {part.geometry.size.z for part in headers} == {0.1397}
    assert all(part.geometry.size.y > 0 for part in (*header_plies, *header_spacers))
    for opening_id in (SYNTHETIC_DOOR_ID, SYNTHETIC_WINDOW_ID):
        this_plies = [part for part in header_plies if part.opening_id == opening_id]
        this_spacers = [part for part in header_spacers if part.opening_id == opening_id]
        assert math.fsum(part.geometry.size.y for part in (*this_plies, *this_spacers)) == pytest.approx(0.0889)
        ply_bounds = [_local_component_bounds(wall, part) for part in this_plies]
        spacer_bounds = [_local_component_bounds(wall, part) for part in this_spacers]
        assert all(box[0][0] == pytest.approx(ply_bounds[0][0][0])
                   and box[1][0] == pytest.approx(ply_bounds[0][1][0])
                   and box[0][2] == pytest.approx(ply_bounds[0][0][2])
                   and box[1][2] == pytest.approx(ply_bounds[0][1][2])
                   for box in spacer_bounds)
    assert {part.geometry.size.x for part in sills} == {1.016}
    assert {part.geometry.size.y for part in sills} == {0.0889}
    assert math.fsum(part.geometry.size.x * part.geometry.size.y * part.geometry.size.z
                     for part in headers) == pytest.approx(
        (0.9906 + 1.0922) * 0.0889 * 0.1397,
    )
    assert len(king_studs) == len(jack_studs) == 4
    assert len(ordinary_studs) == 7
    assert all(part.geometry.size.z == pytest.approx(2.3241) for part in king_studs)
    assert all(part.geometry.size.z == pytest.approx(1.9939) for part in jack_studs)
    assert math.fsum(part.geometry.size.x * part.geometry.size.y * part.geometry.size.z
                     for part in king_studs) == pytest.approx(4 * 0.0381 * 0.0889 * 2.3241)
    assert math.fsum(part.geometry.size.x * part.geometry.size.y * part.geometry.size.z
                     for part in jack_studs) == pytest.approx(4 * 0.0381 * 0.0889 * 1.9939)
    assert math.fsum(part.geometry.size.x * part.geometry.size.y * part.geometry.size.z
                     for part in cripples) == pytest.approx(
        4 * 0.0381 * 0.0889 * 0.1905 + 2 * 0.0381 * 0.0889 * 0.9398
    )
    assert math.fsum(part.geometry.size.x * part.geometry.size.y * part.geometry.size.z
                     for part in sills) == pytest.approx(1.016 * 0.0889 * 0.0381)
    full_station = next(part for part in parts if part.part_key == "stud:station:wall-start")
    assert isinstance(full_station.geometry, Box3D)
    assert full_station.geometry.size.x == 0.0381
    assert full_station.geometry.size.y == 0.0889
    assert abs(full_station.geometry.size.z - (2.4384 - 3 * 0.0381)) <= 1e-12
    assert all(isinstance(part.geometry, PanelGeometry) and part.geometry.thickness_m == 0.0127
               for part in drywall)

    assert compute_assembly_digests(model, assembly, parts) == (
        assembly.input_digest, assembly.output_digest,
    )
    openings = {opening.id: opening for opening in model.openings}
    projected_openings = {}
    for opening in openings.values():
        center = _wall_frame_projection(wall, opening.pose.position)[0]
        projected_openings[opening.id] = (
            center - opening.size.x / 2.0, center + opening.size.x / 2.0,
            opening.pose.position.z - opening.size.z / 2.0,
            opening.pose.position.z + opening.size.z / 2.0,
        )

    # Every physical extremum is inside the host envelope and no component
    # crosses the exact observed opening void. This includes panel thickness.
    bounds = []
    for part in parts:
        lower, upper = _local_component_bounds(wall, part)
        axis_min, axis_max = lower[0], upper[0]
        depth_min, depth_max = lower[1], upper[1]
        z_min, z_max = lower[2], upper[2]
        assert axis_min >= -1e-9 and axis_max <= 4.8 + 1e-9
        assert depth_min >= -wall.thickness_m / 2 - 1e-9
        assert depth_max <= wall.thickness_m / 2 + 1e-9
        assert z_min >= -1e-9 and z_max <= 2.4384 + 1e-9
        for left, right, bottom, top in projected_openings.values():
            assert not (
                min(axis_max, right) - max(axis_min, left) > 1e-9
                and min(z_max, top) - max(z_min, bottom) > 1e-9
                and depth_max - depth_min > 1e-9
            ), f"{part.id} bridges a source opening void"
        bounds.append((part, lower, upper))

    # This illustrative recipe uses touching interfaces, never positive-volume
    # component overlap: kings/jacks/cripples meet plates/headers/sills,
    # and finish panels meet framing without creating duplicate solids.
    for index, (first, first_lo, first_hi) in enumerate(bounds):
        for second, second_lo, second_hi in bounds[index + 1:]:
            overlap = tuple(
                min(first_hi[axis], second_hi[axis]) - max(first_lo[axis], second_lo[axis])
                for axis in range(3)
            )
            assert not all(value > 1e-9 for value in overlap), (
                f"{first.part_key} and {second.part_key} overlap in positive volume"
            )

    # The explicit framing specification is preserved as typed user evidence
    # in the assignment digest, not smuggled into a header default.
    specs = {row.opening_id: row for row in SYNTHETIC_OPENING_FRAMING_SPECS}
    for opening in openings.values():
        spec = specs[opening.id]
        params = [row for row in assembly.resolved_parameters
                  if row.provenance[0].attributes.get("opening_id") == opening.id]
        assert len(params) == 7
        assert all(row.origin == "user_override" for row in params)
        assert all(row.provenance[0].method == "explicit-selected-opening-framing-specification"
                   for row in params)
        king_studs = [part for part in parts if part.provenance[0].attributes.get("opening_id") == opening.id
                      and part.provenance[0].attributes.get("framing_role") == "king"]
        jack_studs = [part for part in parts if part.provenance[0].attributes.get("opening_id") == opening.id
                      and part.provenance[0].attributes.get("framing_role") == "jack"]
        headers_for_opening = [part for part in header_plies if part.opening_id == opening.id]
        spacers_for_opening = [part for part in header_spacers if part.opening_id == opening.id]
        assert len(king_studs) == 2 * spec.king_count_per_side
        assert len(jack_studs) == 2 * spec.jack_count_per_side
        assert len(headers_for_opening) == spec.header_ply_count
        assert len(spacers_for_opening) == spec.header_ply_count - 1
        left_king = next(part for part in king_studs if part.provenance[0].attributes["side"] == "left")
        right_king = next(part for part in king_studs if part.provenance[0].attributes["side"] == "right")
        left_king_bounds = _local_component_bounds(wall, left_king)
        right_king_bounds = _local_component_bounds(wall, right_king)
        assert left_king_bounds[0][2] == pytest.approx(0.0381)
        assert left_king_bounds[1][2] == pytest.approx(2.3622)
        assert right_king_bounds[0][2] == pytest.approx(0.0381)
        assert right_king_bounds[1][2] == pytest.approx(2.3622)
        opening_bounds = projected_openings[opening.id]
        left_jack = next(part for part in jack_studs if part.provenance[0].attributes["side"] == "left")
        right_jack = next(part for part in jack_studs if part.provenance[0].attributes["side"] == "right")
        assert _local_component_bounds(wall, left_jack)[1][0] == pytest.approx(opening_bounds[0])
        assert _local_component_bounds(wall, right_jack)[0][0] == pytest.approx(opening_bounds[1])
        for header in headers_for_opening:
            hlow, hhigh = _local_component_bounds(wall, header)
            assert hlow[0] == pytest.approx(opening_bounds[0] - spec.bearing_m)
            assert hhigh[0] == pytest.approx(opening_bounds[1] + spec.bearing_m)
            assert hlow[0] == pytest.approx(left_king_bounds[1][0])
            assert hhigh[0] == pytest.approx(right_king_bounds[0][0])
            assert hlow[2] == pytest.approx(opening_bounds[3])
            assert header.geometry.size.y == pytest.approx(spec.header_ply_thickness_m)
            assert header.geometry.size.z == pytest.approx(spec.header_depth_m)
            assert header.geometry.pose.position.y == pytest.approx(
                (-0.0254 if header.provenance[0].attributes["ply"] == 1 else 0.0254)
            )
            for side in ("left", "right"):
                jack = next(part for part in jack_studs if part.provenance[0].attributes["side"] == side)
                jlow, jhigh = _local_component_bounds(wall, jack)
                assert jlow[2] == pytest.approx(0.0381)
                assert jhigh[2] == pytest.approx(hlow[2])
                if side == "left":
                    overlap_axis = min(hhigh[0], jhigh[0]) - max(hlow[0], jlow[0])
                else:
                    overlap_axis = min(hhigh[0], jhigh[0]) - max(hlow[0], jlow[0])
                assert overlap_axis == pytest.approx(spec.bearing_m)
                assert overlap_axis * header.geometry.size.y > 0.0
        if opening.opening_type == "window":
            sill = next(part for part in sills if part.opening_id == opening.id)
            sill_bounds = _local_component_bounds(wall, sill)
            assert sill_bounds[1][2] == pytest.approx(opening_bounds[2])
            below = [part for part in parts if part.component_kind == "cripple"
                     and part.opening_id == opening.id
                     and _local_component_bounds(wall, part)[1][2] <= sill_bounds[0][2] + 1e-9]
            above = [part for part in parts if part.component_kind == "cripple"
                     and part.opening_id == opening.id
                     and _local_component_bounds(wall, part)[0][2] >= opening_bounds[3] + spec.header_depth_m - 1e-9]
            assert len(below) == 2 and all(
                _local_component_bounds(wall, part)[1][2] == pytest.approx(sill_bounds[0][2])
                for part in below
            )
            assert len(above) == 2 and all(
                _local_component_bounds(wall, part)[0][2] == pytest.approx(opening_bounds[3] + spec.header_depth_m)
                for part in above
            )
    door_bounds = projected_openings[SYNTHETIC_DOOR_ID]
    door_plate_bounds = sorted(
        (_local_component_bounds(wall, part)[0][0], _local_component_bounds(wall, part)[1][0])
        for part in bottoms
    )
    assert len(door_plate_bounds) == 2
    assert door_plate_bounds[0][1] == pytest.approx(door_bounds[0])
    assert door_plate_bounds[1][0] == pytest.approx(door_bounds[1])
    assert not any(min(plate_right, door_bounds[1]) - max(plate_left, door_bounds[0]) > 1e-9
                   for plate_left, plate_right in door_plate_bounds)
    again = build_wall_assembly(source, _request(assembly_id=SYNTHETIC_ASSEMBLY_ID))
    assert again.to_json(indent=None) == model.to_json(indent=None)
    restored = BuildingModel.from_json(model.to_json(indent=None))
    assert restored.to_json(indent=None) == model.to_json(indent=None)


def test_user_override_changes_layout_without_mutating_input_or_relabeling_default():
    source = synthetic_two_opening_wall_source()
    original = source.to_json(indent=None)
    request = _request(overrides=(ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.6096, unit="m",
    ),))
    model = build_wall_assembly(source, request)
    assembly = model.building_assemblies[0]
    values = {row.parameter_id: row for row in assembly.resolved_parameters}
    assert values["wall.stud.spacing_m"].origin == "user_override"
    assert values["wall.stud.spacing_m"].value == 0.6096
    assert values["wall.stack.framing_depth_m"].origin == "preset_default"
    assert source.to_json(indent=None) == original
    assert len(model.assembly_components) != len(synthetic_two_opening_wall_assembly().assembly_components)
    assert model.building_assemblies[0].input_digest != synthetic_two_opening_wall_assembly().building_assemblies[0].input_digest


def test_opening_framing_specs_are_required_complete_and_physically_reconciled_atomically():
    source = synthetic_two_opening_wall_source()
    before = source.to_json(indent=None)
    with pytest.raises(WallPresetError, match="explicit opening framing specifications must exactly match"):
        build_wall_assembly(source, _request(specs=()))
    with pytest.raises(WallPresetError, match="explicit opening framing specifications must exactly match"):
        build_wall_assembly(source, _request(specs=SYNTHETIC_OPENING_FRAMING_SPECS[:1]))
    assert source.to_json(indent=None) == before


def test_opening_spec_validation_digest_and_parameter_cap_are_bounded():
    source = synthetic_two_opening_wall_source()
    original = source.to_json(indent=None)
    door = next(row for row in source.openings if row.id == SYNTHETIC_DOOR_ID)
    rows = []
    specs = []
    for index in range(17):
        opening_id = f"opening:cap-{index:02d}"
        station = 0.25 + index * 0.27
        rows.append(replace(
            door,
            id=opening_id,
            name=f"Synthetic cap door {index}",
            pose=replace(door.pose, position=Point3(x=station, y=0.0, z=0.5)),
            size=Size3(x=0.1, y=0.1143, z=1.0),
            provenance=(Provenance(
                source_kind="synthetic_wall_assembly_fixture", source_id="fixture:wall-assembly-v1",
                source_element_id=opening_id, page=1, method="synthetic_fixture_authoring",
                derivation="observed",
            ),),
        ))
        specs.append(replace(SYNTHETIC_OPENING_FRAMING_SPECS[0], opening_id=opening_id))
    expanded = replace(source, openings=tuple(rows))
    with pytest.raises(WallPresetError, match="snapshot exceeds the canonical 128 resolved-parameter limit"):
        build_wall_assembly(expanded, WallAssemblyRequest(
            assembly_id="assembly:parameter-cap", wall_id=SYNTHETIC_WALL_ID,
            selected_by="agent:test-selector", opening_framing_specs=tuple(specs),
        ))
    assert source.to_json(indent=None) == original

    door, window = SYNTHETIC_OPENING_FRAMING_SPECS
    too_thick = replace(window, header_spacer_thickness_m=0.02)
    with pytest.raises(WallPresetError, match="header ply build-up exceeds"):
        build_wall_assembly(source, _request(specs=(door, too_thick)))
    with pytest.raises(WallPresetError, match="cannot select a header spacer without multiple plies"):
        replace(window, header_ply_count=1)
    undersupported = replace(door, bearing_m=0.01905)
    with pytest.raises(WallPresetError, match="header end bearing must exactly span the jack support pack"):
        build_wall_assembly(source, _request(specs=(undersupported, window)))
    too_deep = replace(window, header_depth_m=0.4)
    with pytest.raises(WallPresetError, match="selected header depth overlaps"):
        build_wall_assembly(source, _request(specs=(door, too_deep)))
    changed_window = replace(window, header_depth_m=0.1524)
    changed_spec_model = build_wall_assembly(source, _request(specs=(door, changed_window)))
    assert changed_spec_model.building_assemblies[0].input_digest != \
        synthetic_two_opening_wall_assembly().building_assemblies[0].input_digest
    changed_header = next(part for part in changed_spec_model.assembly_components
                          if part.opening_id == SYNTHETIC_WINDOW_ID and part.component_kind == "header")
    assert changed_header.geometry.size.z == pytest.approx(0.1524)
    assert source.to_json(indent=None) == original


def test_adjacent_frame_envelopes_and_wall_end_support_space_fail_closed():
    source = synthetic_two_opening_wall_source()
    metal_wall = replace(source.walls[0], attributes={"framing_system": "cold-formed steel studs"})
    with pytest.raises(WallPresetError, match="relevant source metadata declares metal/steel stud framing"):
        build_wall_assembly(replace(source, walls=(metal_wall,)), _request())

    door = next(row for row in source.openings if row.id == SYNTHETIC_DOOR_ID)
    window = next(row for row in source.openings if row.id == SYNTHETIC_WINDOW_ID)
    close_window = replace(window, pose=replace(
        window.pose, position=Point3(x=2.438, y=0.0, z=window.pose.position.z),
    ))
    with pytest.raises(WallPresetError, match="rough-frame envelopes overlap"):
        build_wall_assembly(replace(source, openings=(door, close_window)), _request())

    near_end = replace(door, pose=replace(
        door.pose, position=Point3(x=0.5, y=0.0, z=door.pose.position.z),
    ))
    one_opening = replace(source, openings=(near_end,))
    with pytest.raises(WallPresetError, match="king/jack/header frame does not fit inside"):
        build_wall_assembly(one_opening, _request(specs=(SYNTHETIC_OPENING_FRAMING_SPECS[0],)))


@pytest.mark.parametrize("construction", ["masonry", "concrete", "glazed"])
def test_typed_nonframed_hosts_are_not_misclassified_as_wood(construction):
    source = synthetic_two_opening_wall_source()
    wall = replace(source.walls[0], construction=construction)
    with pytest.raises(WallPresetError, match="typed construction .* is not a framed wall"):
        build_wall_assembly(replace(source, walls=(wall,)), _request())


def test_host_classification_uses_stud_facts_not_fasteners_or_unrelated_prose():
    source = synthetic_two_opening_wall_source()
    wood_wall = replace(source.walls[0], construction=None, attributes={
        "framing": {"stud_material": "wood", "fastener_material": "steel nails"},
        "notes": "steel channel noted nearby; unrelated annotation",
    })
    built = build_wall_assembly(replace(source, walls=(wood_wall,)), _request())
    selected = built.building_assemblies[0].provenance[0]
    assert selected.attributes["host_framing_basis"] == "source_metadata_declares_wood_stud_material"

    unknown_wall = replace(source.walls[0], construction=None, attributes={})
    unknown_built = build_wall_assembly(replace(source, walls=(unknown_wall,)), _request())
    unknown_selection = unknown_built.building_assemblies[0].provenance[0]
    assert unknown_selection.attributes["host_framing_basis"] == \
        "wood_framing_is_selected_preset_assumption;host_material_unknown"


@pytest.mark.parametrize(("field_name", "value"), [
    ("framing_system", "wood studs with steel ties"),
    ("framing_system", "wood framing with steel tie connectors"),
    ("stud_system", "wood studs with steel ties"),
    ("stud_material", "wood with steel nail plates"),
])
def test_wood_framing_fields_allow_explicit_steel_accessory_phrases(field_name, value):
    source = synthetic_two_opening_wall_source()
    wall = replace(source.walls[0], construction=None, attributes={field_name: value})
    built = build_wall_assembly(replace(source, walls=(wall,)), _request())
    selection = built.building_assemblies[0].provenance[0]
    expected_basis = ("source_metadata_declares_wood_stud_material"
                      if field_name == "stud_material"
                      else "source_metadata_declares_wood_framing_system")
    assert selection.attributes["host_framing_basis"] == expected_basis


@pytest.mark.parametrize(("field_name", "value"), [
    ("framing_system", "wood studs with steel studs"),
    ("framing_system", "wood framing with cold-formed steel tracks"),
    ("stud_system", "wood studs and metal studs"),
    ("stud_material", "wood with steel studs"),
])
def test_ambiguous_mixed_wood_and_metal_stud_systems_refuse(field_name, value):
    source = synthetic_two_opening_wall_source()
    wall = replace(source.walls[0], construction=None, attributes={field_name: value})
    with pytest.raises(WallPresetError, match="relevant source metadata declares metal/steel stud framing"):
        build_wall_assembly(replace(source, walls=(wall,)), _request())


def test_sub_tolerance_positive_member_inputs_refuse_instead_of_disappearing():
    base = SYNTHETIC_OPENING_FRAMING_SPECS[0]
    for field_name in ("header_depth_m", "header_ply_thickness_m", "bearing_m"):
        with pytest.raises(WallPresetError, match="greater than the 1e-09 m geometric tolerance"):
            replace(base, **{field_name: 1e-10})
    with pytest.raises(WallPresetError, match="must be zero or greater than the 1e-09 m geometric tolerance"):
        replace(base, header_spacer_thickness_m=1e-10)


def test_regular_grid_cripple_above_jack_survives_and_no_center_member_is_invented():
    source = synthetic_two_opening_wall_source()
    door = next(row for row in source.openings if row.id == SYNTHETIC_DOOR_ID)
    narrow_door = replace(
        door,
        pose=replace(door.pose, position=Point3(x=1.4073, y=0.0, z=1.016)),
        size=replace(door.size, x=0.3),
    )
    source = replace(source, openings=(narrow_door,))
    spec = SYNTHETIC_OPENING_FRAMING_SPECS[0]
    built = build_wall_assembly(source, _request(specs=(spec,)))
    wall = source.walls[0]
    parts = built.assembly_components
    station = 1.23825
    left_jack = next(part for part in parts
                     if part.provenance[0].attributes.get("framing_role") == "jack"
                     and part.provenance[0].attributes.get("side") == "left")
    upper_cripple = next(part for part in parts
                         if part.component_kind == "cripple"
                         and part.provenance[0].attributes.get("framing_role") == "cripple"
                         and _local_component_bounds(wall, part)[0][0] == pytest.approx(station - 0.01905))
    jack_bounds = _local_component_bounds(wall, left_jack)
    cripple_bounds = _local_component_bounds(wall, upper_cripple)
    assert jack_bounds[0][0] == pytest.approx(station - 0.01905)
    assert jack_bounds[0][2] == pytest.approx(0.0381)
    assert jack_bounds[1][2] == pytest.approx(2.032)
    assert cripple_bounds[0][2] == pytest.approx(2.032 + spec.header_depth_m)
    assert cripple_bounds[1][2] == pytest.approx(2.3622)
    assert not any(part.part_key == "stud:station:station-0003" for part in parts)

    # If the door/header consumes the whole opening-to-top band and the nearby
    # regular grid columns are occupied by full-height kings, the recipe emits
    # no off-grid center cripple. This is a bounded layout policy, not a
    # capacity claim.
    tall = replace(narrow_door,
                   pose=replace(narrow_door.pose, position=Point3(x=1.44539, y=0.0, z=1.11125)),
                   size=replace(narrow_door.size, z=2.2225))
    tall_source = replace(source, openings=(tall,))
    tall_built = build_wall_assembly(tall_source, _request(
        assembly_id="assembly:narrow-tall-door", specs=(spec,),
    ))
    tall_kings = [part for part in tall_built.assembly_components
                  if part.provenance[0].attributes.get("framing_role") == "king"]
    assert any(_local_component_bounds(wall, part)[0][0] == pytest.approx(1.21919)
               for part in tall_kings)
    assert any(_local_component_bounds(wall, part)[0][0] == pytest.approx(1.63349)
               for part in tall_kings)
    assert not any(part.component_kind == "cripple" for part in tall_built.assembly_components)
    assert not any("station-0003" in part.part_key and part.component_kind in {"stud", "cripple"}
                   for part in tall_built.assembly_components)


def test_invalid_stack_orientation_overlap_and_dependency_fail_atomically():
    source = synthetic_two_opening_wall_source()
    before = source.to_json(indent=None)
    wrong_stack = replace(source.walls[0], thickness_m=0.1)
    with pytest.raises(WallPresetError, match="unresolved wall thickness stack"):
        build_wall_assembly(replace(source, walls=(wrong_stack,), openings=()), _request(specs=()))
    assert source.to_json(indent=None) == before

    window = next(row for row in source.openings if row.id == SYNTHETIC_WINDOW_ID)
    bad_window = replace(window, pose=replace(window.pose, rotation=Quaternion(z=0.1, w=math.sqrt(0.99))))
    with pytest.raises(WallPresetError, match="orientation is not aligned"):
        build_wall_assembly(replace(source, openings=tuple(
            bad_window if row.id == window.id else row for row in source.openings
        )), _request())
    assert source.to_json(indent=None) == before

    door = next(row for row in source.openings if row.id == SYNTHETIC_DOOR_ID)
    overlap = replace(window, pose=replace(window.pose, position=Point3(x=1.6, y=0, z=1.524)))
    with pytest.raises(WallPresetError, match="opening/frame envelopes overlap"):
        build_wall_assembly(replace(source, openings=(door, overlap)), _request())
    assert source.to_json(indent=None) == before

    already_built = synthetic_two_opening_wall_assembly()
    with pytest.raises(WallPresetError, match="requires_derived_rebuild"):
        build_wall_assembly(already_built, _request(assembly_id="assembly:replacement-attempt"))

    wall = source.walls[0]
    port_a = Port(
        id="port:existing-route-a", owner_id=wall.id, domain="electrical", role="source",
        pose=Pose(position=Point3(x=0.1, y=0, z=1)), direction=Vector3(x=1, y=0, z=0),
    )
    port_b = Port(
        id="port:existing-route-b", owner_id=wall.id, domain="electrical", role="load",
        pose=Pose(position=Point3(x=4.7, y=0, z=1)), direction=Vector3(x=-1, y=0, z=0),
    )
    route = Route(
        id="route:existing", route_type="conduit", start_port_id=port_a.id,
        end_port_id=port_b.id,
        centerline=Polyline3D(points=(port_a.pose.position, port_b.pose.position)),
    )
    routed_source = replace(source, ports=(port_a, port_b), routes=(route,))
    routed_json = routed_source.to_json(indent=None)
    with pytest.raises(WallPresetError, match="refuses models with existing routes"):
        build_wall_assembly(routed_source, _request())
    assert routed_source.to_json(indent=None) == routed_json


def test_curved_or_sloped_wall_and_resource_overrun_are_refused_before_generation():
    source = synthetic_two_opening_wall_source()
    wall = source.walls[0]
    curved = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=2, y=0.1, z=0), Point3(x=4.8, y=0, z=0),
    )))
    with pytest.raises(WallPresetError, match="two-point straight centerline"):
        build_wall_assembly(replace(source, walls=(curved,)), _request())
    sloped = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=4.8, y=0, z=0.01),
    )))
    with pytest.raises(WallPresetError, match="share one horizontal base elevation"):
        build_wall_assembly(replace(source, walls=(sloped,)), _request())

    long_wall = replace(wall, centerline=Polyline3D(points=(
        Point3(x=0, y=0, z=0), Point3(x=1000, y=0, z=0),
    )))
    too_dense = ParameterOverride(
        parameter_id="wall.stud.spacing_m", value_kind="length",
        value=0.0508, unit="m",
    )
    with pytest.raises(WallPresetError, match="component_cap_exceeded"):
        build_wall_assembly(replace(source, walls=(long_wall,), openings=()),
                            _request(overrides=(too_dense,), specs=()))


def test_ordered_rotated_horizontal_wall_preserves_stable_semantic_identity():
    source = synthetic_two_opening_wall_source()
    original = build_wall_assembly(source, _request())
    wall = replace(source.walls[0], centerline=Polyline3D(points=(
        Point3(x=10.0, y=20.0, z=0.0), Point3(x=10.0, y=24.8, z=0.0),
    )))
    rotation = Quaternion(z=math.sqrt(0.5), w=math.sqrt(0.5))
    rotated_openings = []
    for opening in source.openings:
        station = opening.pose.position.x
        rotated_openings.append(replace(
            opening,
            pose=replace(
                opening.pose,
                position=Point3(x=10.0, y=20.0 + station, z=opening.pose.position.z),
                rotation=rotation,
            ),
        ))
    rotated_source = replace(source, walls=(wall,), openings=tuple(rotated_openings))
    rotated = build_wall_assembly(rotated_source, _request())
    assert tuple(row.id for row in original.assembly_components) == tuple(
        row.id for row in rotated.assembly_components
    )
    assert original.building_assemblies[0].input_digest != rotated.building_assemblies[0].input_digest
    assert original.building_assemblies[0].output_digest != rotated.building_assemblies[0].output_digest
    assert rotated.building_assemblies[0].wall_frame.axis == Vector3(x=0.0, y=1.0, z=0.0)
    assert rotated.building_assemblies[0].wall_frame.left_normal == Vector3(x=-1.0, y=0.0, z=0.0)


def test_drywall_stack_mismatch_and_missing_source_evidence_fail_closed():
    source = synthetic_two_opening_wall_source()
    mismatched = (
        ParameterOverride(parameter_id="wall.drywall.thickness_m", value_kind="length", value=0.0159, unit="m"),
        ParameterOverride(parameter_id="wall.stack.face_left.layer_1_m", value_kind="length", value=0.0159, unit="m"),
        ParameterOverride(parameter_id="wall.stack.face_right.layer_1_m", value_kind="length", value=0.0159, unit="m"),
    )
    with pytest.raises(WallPresetError, match="unresolved wall thickness stack"):
        build_wall_assembly(source, _request(overrides=mismatched))

    missing_evidence = replace(source.walls[0], provenance=())
    with pytest.raises(WallPresetError, match="no observed source provenance"):
        build_wall_assembly(replace(source, walls=(missing_evidence,)), _request())
