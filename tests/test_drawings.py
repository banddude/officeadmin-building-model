from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.drawings import (
    Bounds2,
    ElevationSpec,
    PlanSpec,
    Point2,
    SectionSpec,
    Symbol,
    VisibilityPolicy,
    clip_polygon,
    clip_polyline,
    clip_segment,
    generate_drawing_set,
    generate_plan,
    generate_schedules,
    generate_section,
    view_to_svg,
)
from oabm.model import BuildingModel, ElectricalDevice, Point3, Polyline3D, Vector3

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "drawings" / "v1" / "drawing-model.json"
GOLDEN = ROOT / "fixtures" / "drawings" / "v1" / "expected-drawing-set.sha256"


def _model() -> BuildingModel:
    return BuildingModel.load(FIXTURE)


def _default_set(model: BuildingModel | None = None):
    model = model or _model()
    return generate_drawing_set(
        model,
        plans=(
            PlanSpec(
                id="plan:ground",
                level_id="level:ground",
                bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=6.5, max_y=5.5),
            ),
        ),
        elevations=(
            ElevationSpec(
                id="elevation:south",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                near_m=-0.5,
                far_m=6.0,
                bounds=Bounds2(min_x=-6.5, min_y=-0.5, max_x=0.5, max_y=6.5),
            ),
        ),
        sections=(
            SectionSpec(
                id="section:a",
                origin=Point3(x=3, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                depth_m=5.5,
                back_depth_m=0.2,
                bounds=Bounds2(min_x=-3.5, min_y=-0.5, max_x=3.5, max_y=6.5),
            ),
        ),
    )


def test_plan_projects_canonical_wall_volume_at_cut_plane() -> None:
    view = generate_plan(
        _model(),
        PlanSpec(
            id="plan:ground",
            level_id="level:ground",
            bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=6.5, max_y=5.5),
        ),
    )
    south = [p for p in view.primitives if p.source_ids == ("wall:south",) and p.layer == "architecture:walls"]
    assert len(south) == 1
    primitive = south[0]
    assert primitive.kind == "polygon"
    assert primitive.style.stroke == "cut"
    assert primitive.style.weight == "heavy"
    assert {round(point.y, 6) for point in primitive.points} == {-0.1, 0.1}
    assert min(point.x for point in primitive.points) == 0
    assert max(point.x for point in primitive.points) == 6


def test_plan_visibility_is_level_and_depth_aware() -> None:
    model = _model()
    base = generate_plan(
        model,
        PlanSpec(
            id="plan:ground",
            level_id="level:ground",
            bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=6.5, max_y=5.5),
        ),
    )
    sources = {source_id for primitive in base.primitives for source_id in primitive.source_ids}
    assert "device:evse" in sources
    assert "device:upper-light" not in sources
    assert "ceiling:garage" not in sources
    assert "obstacle:beam" not in sources
    assert "constraint:ceiling" not in sources

    coordination = generate_plan(
        model,
        PlanSpec(
            id="plan:coordination",
            level_id="level:ground",
            cut_height_m=2.5,
            view_depth_above_m=0.5,
            bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=6.5, max_y=5.5),
            visibility=VisibilityPolicy(obstacles=True, constraints=True, labels=False),
        ),
    )
    coordination_sources = {source_id for primitive in coordination.primitives for source_id in primitive.source_ids}
    assert "obstacle:beam" in coordination_sources
    assert "constraint:ceiling" in coordination_sources
    assert not [p for p in coordination.primitives if p.layer == "annotations:labels"]


def test_plan_clips_route_segments_to_finite_view_depth_before_projection() -> None:
    model = _model()
    route = model.routes[0]
    clipped_route = replace(
        route,
        centerline=Polyline3D(
            points=(
                Point3(x=0.4, y=0.25, z=1.5),
                Point3(x=1.0, y=0.25, z=1.5),
                Point3(x=2.0, y=0.25, z=3.0),
                Point3(x=4.0, y=0.25, z=3.0),
                Point3(x=5.0, y=0.25, z=1.2),
                Point3(x=5.2, y=0.25, z=1.2),
            )
        ),
    )
    model = replace(model, routes=(clipped_route,))

    view = generate_plan(
        model,
        PlanSpec(
            id="plan:route-depth",
            level_id="level:ground",
            cut_height_m=1.5,
            view_depth_above_m=0.25,
            bounds=Bounds2(min_x=0.0, min_y=0.0, max_x=6.0, max_y=1.0),
            visibility=VisibilityPolicy(labels=False, dimensions=False),
        ),
    )

    route_primitives = [
        primitive
        for primitive in view.primitives
        if primitive.source_ids == (route.id,) and primitive.layer == "electrical:routes"
    ]
    assert len(route_primitives) == 2
    x_ranges = sorted(
        (min(point.x for point in primitive.points), max(point.x for point in primitive.points))
        for primitive in route_primitives
    )
    assert x_ranges[0] == pytest.approx((0.4, 1.1666666667))
    assert x_ranges[1] == pytest.approx((4.6944444444, 5.2))
    assert all(
        not (min(point.x for point in primitive.points) < 3.0 < max(point.x for point in primitive.points))
        for primitive in route_primitives
    )


def test_section_clips_route_segments_to_finite_depth_before_projection() -> None:
    model = _model()
    route = model.routes[0]
    clipped_route = replace(
        route,
        centerline=Polyline3D(
            points=(
                Point3(x=0.4, y=0.25, z=1.5),
                Point3(x=1.0, y=0.25, z=1.5),
                Point3(x=2.0, y=3.0, z=1.5),
                Point3(x=4.0, y=3.0, z=1.2),
                Point3(x=5.0, y=0.25, z=1.2),
                Point3(x=5.2, y=0.25, z=1.2),
            )
        ),
    )
    model = replace(model, routes=(clipped_route,))

    view = generate_section(
        model,
        SectionSpec(
            id="section:route-depth",
            origin=Point3(x=0, y=0, z=0),
            direction=Vector3(x=0, y=1, z=0),
            depth_m=1.0,
            back_depth_m=0.2,
            bounds=Bounds2(min_x=-6.0, min_y=0.0, max_x=0.0, max_y=2.0),
            visibility=VisibilityPolicy(labels=False, dimensions=False),
        ),
    )

    route_primitives = [
        primitive
        for primitive in view.primitives
        if primitive.source_ids == (route.id,) and primitive.layer == "electrical:routes"
    ]
    assert len(route_primitives) == 2
    x_ranges = sorted(
        (min(point.x for point in primitive.points), max(point.x for point in primitive.points))
        for primitive in route_primitives
    )
    assert x_ranges[0] == pytest.approx((-5.2, -4.7272727273))
    assert x_ranges[1] == pytest.approx((-1.2727272727, -0.4))
    assert all(
        not (min(point.x for point in primitive.points) < -3.0 < max(point.x for point in primitive.points))
        for primitive in route_primitives
    )


def test_elevation_projects_height_and_section_marks_cut_geometry() -> None:
    drawing_set = _default_set()
    elevation = next(view for view in drawing_set.views if view.id == "elevation:south")
    section = next(view for view in drawing_set.views if view.id == "section:a")

    south_elevation = [p for p in elevation.primitives if p.source_ids == ("wall:south",) and p.layer == "architecture:walls"]
    assert south_elevation
    assert max(point.y for point in south_elevation[0].points) == 3

    cut_wall_primitives = [p for p in section.primitives if p.layer == "architecture:walls" and p.style.weight == "heavy"]
    assert cut_wall_primitives
    south_cut = next(p for p in cut_wall_primitives if "wall:south" in p.source_ids)
    # The section cut is the plane/solid intersection, not merely a heavy
    # version of every projected wall silhouette.
    assert min(point.y for point in south_cut.points) == 0
    assert max(point.y for point in south_cut.points) == 3
    assert min(point.x for point in south_cut.points) == -3
    assert max(point.x for point in south_cut.points) == 3


def test_schedules_are_semantic_not_quantity_remeasurement() -> None:
    schedules = {schedule.id: schedule for schedule in generate_schedules(_model())}
    assert set(schedules) == {
        "schedule:circuits",
        "schedule:devices",
        "schedule:equipment",
        "schedule:openings",
        "schedule:rooms",
    }
    device_rows = schedules["schedule:devices"].rows
    assert [row.source_id for row in device_rows] == ["device:evse", "device:upper-light"]
    assert device_rows[0].cells[2] == "evse"
    circuit = schedules["schedule:circuits"].rows[0]
    assert circuit.source_id == "circuit:evse"
    assert "equip:panel" in circuit.cells
    assert "device:evse" in circuit.cells
    # The drawing lane intentionally does not synthesize conduit/conductor lengths.
    assert all("length" not in column.key for schedule in schedules.values() for column in schedule.columns)


def test_stable_order_and_repeatability_ignore_canonical_collection_order() -> None:
    model = _model()
    first = _default_set(model)
    reversed_model = replace(
        model,
        levels=tuple(reversed(model.levels)),
        spaces=tuple(reversed(model.spaces)),
        walls=tuple(reversed(model.walls)),
        slabs=tuple(reversed(model.slabs)),
        ceilings=tuple(reversed(model.ceilings)),
        openings=tuple(reversed(model.openings)),
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        ports=tuple(reversed(model.ports)),
        obstacles=tuple(reversed(model.obstacles)),
        route_constraints=tuple(reversed(model.route_constraints)),
        routes=tuple(reversed(model.routes)),
        route_fittings=tuple(reversed(model.route_fittings)),
        circuits=tuple(reversed(model.circuits)),
        conductors=tuple(reversed(model.conductors)),
    )
    second = _default_set(reversed_model)
    assert first.to_json() == second.to_json()
    assert first.to_json() == _default_set(model).to_json()


def test_source_index_preserves_identity_confidence_and_provenance() -> None:
    drawing_set = _default_set()
    refs = {item.canonical_id: item for item in drawing_set.source_index}
    assert set(refs) >= {"wall:south", "port:panel-load", "conductor:lines", "level:ground"}
    wall = refs["wall:south"]
    assert wall.confidence == 1.0
    assert wall.provenance[0].source_kind == "synthetic"
    assert wall.provenance[0].source_id == "fixture:drawing-model-v1"
    assert wall.provenance[0].method == "hand-authored drawing fixture"


def test_symbol_and_annotation_hooks_are_used_without_mutating_model() -> None:
    model = _model()

    def symbol_provider(entity, view_type):
        if isinstance(entity, ElectricalDevice):
            return Symbol(token=f"custom:{entity.device_type}", label="D")
        return None

    def label_provider(entity, view_type):
        if isinstance(entity, ElectricalDevice):
            return f"LABEL {entity.id}"
        return None

    view = generate_plan(
        model,
        PlanSpec(id="plan:hooks", level_id="level:ground", bounds=Bounds2(min_x=-1, min_y=-1, max_x=7, max_y=6)),
        symbol_provider=symbol_provider,
        label_provider=label_provider,
    )
    evse_symbol = next(p for p in view.primitives if p.kind == "symbol" and p.source_ids == ("device:evse",))
    evse_label = next(p for p in view.primitives if p.kind == "text" and p.source_ids == ("device:evse",))
    assert evse_symbol.symbol == "custom:evse"
    assert evse_symbol.text == "D"
    assert evse_label.text == "LABEL device:evse"
    assert model == _model()


def test_generated_drawing_set_matches_checked_in_golden() -> None:
    expected = GOLDEN.read_text(encoding="utf-8").strip()
    actual = hashlib.sha256(_default_set().to_json().encode("utf-8")).hexdigest()
    assert actual == expected


def test_clipping_handles_boundaries_crossings_and_disconnected_fragments() -> None:
    bounds = Bounds2(min_x=0, min_y=0, max_x=10, max_y=10)
    assert clip_segment(Point2(x=-2, y=5), Point2(x=12, y=5), bounds) == (
        Point2(x=0, y=5), Point2(x=10, y=5)
    )
    assert clip_segment(Point2(x=-1, y=-1), Point2(x=-1, y=11), bounds) is None

    fragments = clip_polyline(
        (
            Point2(x=-1, y=1),
            Point2(x=5, y=1),
            Point2(x=11, y=1),
            Point2(x=11, y=9),
            Point2(x=5, y=9),
        ),
        bounds,
    )
    assert fragments == (
        (Point2(x=0, y=1), Point2(x=5, y=1), Point2(x=10, y=1)),
        (Point2(x=10, y=9), Point2(x=5, y=9)),
    )

    polygon = clip_polygon(
        (Point2(x=-2, y=2), Point2(x=5, y=2), Point2(x=5, y=12), Point2(x=-2, y=12)),
        bounds,
    )
    assert set(polygon) == {Point2(x=0, y=2), Point2(x=5, y=2), Point2(x=5, y=10), Point2(x=0, y=10)}


def test_svg_is_deterministic_and_embeds_canonical_source_references() -> None:
    view = next(view for view in _default_set().views if view.id == "plan:ground")
    first = view_to_svg(view)
    second = view_to_svg(view)
    assert first == second
    assert 'data-view-id="plan:ground"' in first
    assert 'data-source-ids="wall:south"' in first
    assert '<g id="architecture:walls">' in first
    assert '<g id="dimensions">' in first


def test_clipped_view_omits_outside_entities_but_keeps_crossing_geometry() -> None:
    view = generate_plan(
        _model(),
        PlanSpec(
            id="plan:clip",
            level_id="level:ground",
            bounds=Bounds2(min_x=2.5, min_y=-0.2, max_x=3.5, max_y=0.2),
            visibility=VisibilityPolicy(electrical=False, routes=False, spaces=False, labels=False, dimensions=False),
        ),
    )
    south = [p for p in view.primitives if p.source_ids == ("wall:south",)]
    assert south
    assert all(2.5 <= point.x <= 3.5 for primitive in south for point in primitive.points)
    assert not [p for p in view.primitives if p.source_ids == ("wall:north",)]
