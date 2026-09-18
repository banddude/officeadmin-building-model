from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.drawings import (
    Annotation,
    Bounds2,
    DrawingGenerator,
    DrawingPrimitive,
    Point2,
    ProjectionFrame,
    SourceReference,
    VisibilityFilter,
    clip_polygon,
    clip_polyline,
    clip_segment_to_depth,
    render_svg,
    schedule_to_csv,
)
from oabm.model import BuildingModel, Point3

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "drawings" / "v1" / "drawing-model.json"
EXPECTED_HASH = ROOT / "fixtures" / "drawings" / "v1" / "expected-drawing-set.sha256"


@pytest.fixture()
def model() -> BuildingModel:
    return BuildingModel.load(FIXTURE)


def _source_ids(view) -> set[str]:
    return {primitive.source.entity_id for primitive in view.primitives}


def test_projection_frames_are_explicit_and_repeatable() -> None:
    point = Point3(x=2.0, y=3.0, z=4.0)
    plan = ProjectionFrame.plan(1.0)
    projected, depth = plan.project(point)
    assert projected == Point2(2.0, 3.0)
    assert depth == 3.0

    north = ProjectionFrame.elevation("north")
    projected, depth = north.project(point)
    assert projected == Point2(2.0, 4.0)
    assert depth == -3.0

    section = ProjectionFrame.section(Point3(x=0, y=0, z=0), Point3(x=10, y=0, z=0))
    projected, depth = section.project(point)
    assert projected == Point2(2.0, 4.0)
    assert depth == -3.0


def test_2d_clipping_handles_crossing_and_polygon_edges() -> None:
    bounds = Bounds2(-1, -1, 1, 1)
    runs = clip_polyline((Point2(-2, 0), Point2(2, 0)), bounds)
    assert runs == ((Point2(-1, 0), Point2(1, 0)),)

    polygon = clip_polygon(
        (Point2(-2, -2), Point2(2, -2), Point2(2, 2), Point2(-2, 2)),
        bounds,
    )
    assert len(polygon) == 4
    assert {point.x for point in polygon} == {-1, 1}
    assert {point.y for point in polygon} == {-1, 1}


def test_depth_clipping_keeps_segment_that_crosses_slice() -> None:
    frame = ProjectionFrame.section(Point3(x=0, y=0, z=0), Point3(x=5, y=0, z=0))
    clipped = clip_segment_to_depth(
        Point3(x=2, y=-1, z=1),
        Point3(x=2, y=1, z=1),
        frame,
        -0.2,
        0.2,
    )
    assert clipped is not None
    a, b = clipped
    assert sorted((a.y, b.y)) == pytest.approx([-0.2, 0.2])


def test_plan_filters_by_level_and_preserves_source_identity(model: BuildingModel) -> None:
    generator = DrawingGenerator(model)
    ground = generator.plan("level:ground")
    mezz = generator.plan("level:mezz")

    ground_ids = _source_ids(ground)
    mezz_ids = _source_ids(mezz)
    assert "wall:south" in ground_ids
    assert "wall:mezz-south" not in ground_ids
    assert "device:evse" in ground_ids
    assert "device:light" not in ground_ids

    assert "wall:mezz-south" in mezz_ids
    assert "device:light" in mezz_ids
    assert "device:evse" not in mezz_ids
    assert "route:evse" not in mezz_ids

    panel_primitives = [p for p in ground.primitives if p.source.entity_id == "equip:panel"]
    assert panel_primitives
    assert panel_primitives[0].source.provenance == ("synthetic:fixture:drawing-model-v1:",)


def test_visibility_filter_is_deterministic_and_category_scoped(model: BuildingModel) -> None:
    view = DrawingGenerator(model).plan(
        "level:ground",
        visibility=VisibilityFilter(include_categories=("routes",)),
        clip_bounds=Bounds2(0, 0, 8, 6),
    )
    assert _source_ids(view) == {"route:evse"}
    assert not view.annotations
    for primitive in view.primitives:
        for point in primitive.points:
            assert 0 <= point.x <= 8
            assert 0 <= point.y <= 6


def test_section_includes_wall_crossing_cut_even_when_endpoints_are_outside(model: BuildingModel) -> None:
    view = DrawingGenerator(model).section(
        "C-C",
        Point3(x=0, y=3, z=0),
        Point3(x=8, y=3, z=0),
        depth_m=0.2,
        visibility=VisibilityFilter(include_categories=("walls",)),
    )
    ids = _source_ids(view)
    assert "wall:west" in ids
    assert "wall:east" in ids
    assert any(
        primitive.source.entity_id == "wall:west"
        and any(point.y > 2.9 for point in primitive.points)
        for primitive in view.primitives
    )


def test_stable_label_and_primitive_order(model: BuildingModel) -> None:
    view = DrawingGenerator(model).plan("level:ground")
    ordering = [(item.layer, item.source.entity_id, item.id) for item in view.primitives]
    assert ordering == sorted(ordering)
    assert [annotation.id for annotation in view.annotations] == sorted(
        annotation.id for annotation in view.annotations
    )
    labels = {annotation.source.entity_id: annotation.text for annotation in view.annotations if annotation.source}
    assert labels["equip:panel"] == "Panel LP"
    assert labels["space:garage"] == "Garage"


def test_symbol_and_annotation_hooks_are_extension_points(model: BuildingModel) -> None:
    def symbol(entity, context):
        return (
            DrawingPrimitive(
                id=f"{context.view_id}:{entity.id}:custom",
                kind="point",
                layer="custom-symbol",
                points=(context.anchor,),
                source=context.source,
                metadata=(("hook", "custom"),),
            ),
        )

    def annotation(entity, context):
        return (
            Annotation(
                id=f"{context.view_id}:{entity.id}:custom-label",
                text=f"CUSTOM {entity.id}",
                position=context.anchor,
                source=context.source,
            ),
        )

    view = DrawingGenerator(model, symbol_hook=symbol, annotation_hook=annotation).plan("level:ground")
    custom = [p for p in view.primitives if p.layer == "custom-symbol"]
    assert custom
    assert all(p.metadata == (("hook", "custom"),) for p in custom)
    assert any(a.text == "CUSTOM equip:panel" for a in view.annotations)


def test_dimensions_are_derived_from_visible_geometry(model: BuildingModel) -> None:
    view = DrawingGenerator(model).plan(
        "level:ground",
        visibility=VisibilityFilter(include_categories=("walls",)),
    )
    by_id = {dimension.id: dimension for dimension in view.dimensions}
    horizontal = by_id["plan:level:ground:dimension:overall-horizontal"]
    vertical = by_id["plan:level:ground:dimension:overall-vertical"]
    assert horizontal.value_m == pytest.approx(8.0)
    assert vertical.value_m == pytest.approx(6.0)
    assert horizontal.source_entity_ids == ("wall:east", "wall:north", "wall:south", "wall:west")


def test_schedules_have_fixed_columns_stable_rows_and_source_refs(model: BuildingModel) -> None:
    schedules = {schedule.id: schedule for schedule in DrawingGenerator(model).schedules()}
    equipment = schedules["schedule:electrical-equipment"]
    devices = schedules["schedule:electrical-devices"]
    openings = schedules["schedule:openings"]
    circuits = schedules["schedule:circuits"]

    assert equipment.rows[0].values == (
        "equip:panel", "Panel LP", "panelboard", "level:ground", "space:garage",
        "120/240V-1ph", "240", "1",
    )
    assert equipment.rows[0].source.entity_id == "equip:panel"
    assert [row.source.entity_id for row in devices.rows] == ["device:evse", "device:light"]
    assert openings.rows[0].values[:4] == ("opening:door", "Service Door", "door", "wall:south")
    assert circuits.rows[0].values == (
        "circuit:evse", "12", "port:panel", "port:evse", "240", "2", "1ph", "9600", "route:evse"
    )


def test_svg_and_csv_are_deterministic_and_traceable(model: BuildingModel) -> None:
    generator = DrawingGenerator(model)
    view = generator.plan("level:ground")
    svg_a = render_svg(view)
    svg_b = render_svg(view)
    assert svg_a == svg_b
    assert 'data-view-id="plan:level:ground"' in svg_a
    assert 'data-model-id="model:drawing-fixture-v1"' in svg_a
    assert 'data-source-id="equip:panel"' in svg_a

    equipment = generator.schedules()[0]
    csv_text = schedule_to_csv(equipment)
    assert csv_text.startswith("ID,Name,Type,Level,Space,System,Voltage,Confidence\n")
    assert "equip:panel,Panel LP,panelboard" in csv_text


def test_default_set_contains_plans_elevations_sections_and_schedules(model: BuildingModel) -> None:
    drawing_set = DrawingGenerator(model).default_set()
    assert [view.id for view in drawing_set.views] == [
        "plan:level:ground",
        "plan:level:mezz",
        "elevation:north",
        "elevation:east",
        "elevation:south",
        "elevation:west",
        "section:A-A",
        "section:B-B",
    ]
    assert [schedule.id for schedule in drawing_set.schedules] == [
        "schedule:electrical-equipment",
        "schedule:electrical-devices",
        "schedule:openings",
        "schedule:circuits",
    ]


def test_repeatability_is_independent_of_canonical_collection_order(model: BuildingModel) -> None:
    original = DrawingGenerator(model).default_set().to_json(indent=None)
    reordered = replace(
        model,
        levels=tuple(reversed(model.levels)),
        spaces=tuple(reversed(model.spaces)),
        walls=tuple(reversed(model.walls)),
        slabs=tuple(reversed(model.slabs)),
        ceilings=tuple(reversed(model.ceilings)),
        openings=tuple(reversed(model.openings)),
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        obstacles=tuple(reversed(model.obstacles)),
        routes=tuple(reversed(model.routes)),
        route_fittings=tuple(reversed(model.route_fittings)),
        circuits=tuple(reversed(model.circuits)),
    )
    assert DrawingGenerator(reordered).default_set().to_json(indent=None) == original

    digest = hashlib.sha256((original + "\n").encode()).hexdigest()
    assert EXPECTED_HASH.read_text(encoding="utf-8").strip() == digest


def test_empty_model_generates_safe_empty_views_and_schedules() -> None:
    model = BuildingModel(model_id="model:empty")
    drawing_set = DrawingGenerator(model).default_set()
    assert [view.id for view in drawing_set.views] == [
        "elevation:north", "elevation:east", "elevation:south", "elevation:west"
    ]
    assert all(not view.primitives for view in drawing_set.views)
    assert all(not schedule.rows for schedule in drawing_set.schedules)
