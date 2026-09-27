"""Optional skipping of overlapping dimension text (#204, follows #194).

``dimension_overlap="skip_shorter"`` places dimensions in ``value_m``
descending order (ties by id) and drops any whose estimated text box hits an
already kept dimension's text box, reporting the count as a
``skipped_dimensions`` view metadata entry. Labels are evaluated after the
surviving dimensions are known, so ``label_overlap="skip_with_dimensions"``
only collides with dimensions that were kept. Default output stays
byte-identical. All content is synthetic.
"""

from __future__ import annotations

import pytest

from oabm.drawings import (
    Bounds2,
    ElevationSpec,
    PlanSpec,
    SectionSpec,
    generate_drawing_set,
    generate_elevation,
    generate_plan,
    generate_section,
)
from oabm.model import (
    BuildingModel,
    ElectricalDevice,
    Level,
    Opening,
    Point3,
    Polyline3D,
    Pose,
    Size3,
    Vector3,
    Wall,
)

LONG_LENGTH = 3.188208


def _wall(wall_id: str, *, x0: float, x1: float, y: float = 0.0) -> Wall:
    return Wall(
        id=wall_id,
        level_id="level:ground",
        centerline=Polyline3D(points=(Point3(x=x0, y=y, z=0), Point3(x=x1, y=y, z=0))),
        thickness_m=0.1,
        height_m=2.7,
    )


def _opening(opening_id: str, *, x: float) -> Opening:
    return Opening(
        id=opening_id,
        host_id="wall:zz-long",
        opening_type="door",
        pose=Pose(position=Point3(x=x, y=0, z=1.05)),
        size=Size3(x=0.9, y=0.12, z=2.1),
    )


def _device(device_id: str, name: str, *, x: float, y: float) -> ElectricalDevice:
    return ElectricalDevice(
        id=device_id,
        name=name,
        device_type="receptacle",
        pose=Pose(position=Point3(x=x, y=y, z=1.2)),
        level_id="level:ground",
    )


def _model() -> BuildingModel:
    """Two colliding wall runs, a tie pair, a clear wall, doors, devices.

    ``wall:zz-long`` (3.188208 m) and ``wall:aa-short`` (0.5 m) dimension
    text boxes collide near x 1.8; ``wall:tie-a``/``wall:tie-b`` (1.0 m
    each) collide near x 5.5; ``wall:far`` (2.0 m) is clear of everything.
    The id order is deliberately opposite the value order, so keeping the
    longer dimension proves the value-descending placement. The probe label
    at (2.0, 0.25) hits only ``wall:aa-short``'s text box (the long wall's
    box ends at x 1.9691, the short one reaches 2.0375).
    """
    return BuildingModel(
        model_id="model:drawings-dimension-overlap",
        name="Synthetic Dimension Overlap Model",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=(
            _wall("wall:zz-long", x0=0.0, x1=LONG_LENGTH),
            _wall("wall:aa-short", x0=1.6, x1=2.1, y=0.002),
            _wall("wall:far", x0=10.0, x1=12.0),
            _wall("wall:tie-a", x0=5.0, x1=6.0, y=5.0),
            _wall("wall:tie-b", x0=5.05, x1=6.05, y=5.002),
        ),
        openings=(
            _opening("opening:a", x=1.0),
            _opening("opening:b", x=1.15),
            _opening("opening:far", x=2.5),
        ),
        electrical_devices=(
            _device("device:aaa", "Panel A", x=1.0, y=2.0),
            _device("device:bbb", "Panel B", x=1.0, y=2.0),
            _device("device:ccc", "Panel C", x=0.5, y=3.5),
            _device("device:probe", "Probe", x=2.0, y=0.25),
        ),
    )


def _plan_spec() -> PlanSpec:
    return PlanSpec(
        id="plan:overlap",
        level_id="level:ground",
        bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=13.0, max_y=7.0),
    )


def _dimension_sources(view) -> set[str]:
    return {item.source_ids[0] for item in view.dimensions}


def _label_sources(view) -> set[str]:
    return {item.source_ids[0] for item in view.primitives if item.layer == "annotations:labels"}


def test_two_colliding_dimensions_keep_the_longer_one() -> None:
    view = generate_plan(_model(), _plan_spec(), dimension_overlap="skip_shorter")

    assert _dimension_sources(view) == {"wall:zz-long", "wall:far", "wall:tie-a"}
    # Id order would have kept wall:aa-short; value order keeps the longer run.
    assert ("skipped_dimensions", "2") in view.metadata
    keep = generate_plan(_model(), _plan_spec())
    assert keep.dimensions != view.dimensions
    assert len(keep.dimensions) == 5


def test_separated_dimensions_are_all_kept() -> None:
    view = generate_plan(_model(), _plan_spec(), dimension_overlap="skip_shorter")
    # wall:far (2.0 m) is shorter than wall:zz-long but nowhere near it.
    assert "wall:far" in _dimension_sources(view)
    assert "wall:tie-a" in _dimension_sources(view)


def test_equal_values_tie_break_by_id() -> None:
    model = BuildingModel(
        model_id="model:dimension-tie",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=(
            Wall(
                id="wall:tie-b",
                level_id="level:ground",
                centerline=Polyline3D(points=(Point3(x=5.05, y=5.002, z=0), Point3(x=6.05, y=5.002, z=0))),
                thickness_m=0.1,
                height_m=2.7,
            ),
            Wall(
                id="wall:tie-a",
                level_id="level:ground",
                centerline=Polyline3D(points=(Point3(x=5.0, y=5.0, z=0), Point3(x=6.0, y=5.0, z=0))),
                thickness_m=0.1,
                height_m=2.7,
            ),
        ),
    )
    view = generate_plan(
        model,
        PlanSpec(
            id="plan:tie",
            level_id="level:ground",
            bounds=Bounds2(min_x=4.0, min_y=4.0, max_x=7.0, max_y=7.0),
        ),
        dimension_overlap="skip_shorter",
    )
    # Both walls measure 1.0 m; the lower id is placed (and kept) first.
    assert _dimension_sources(view) == {"wall:tie-a"}
    assert ("skipped_dimensions", "1") in view.metadata


def test_labels_are_judged_against_the_surviving_dimensions() -> None:
    model = _model()

    plain = generate_plan(model, _plan_spec(), label_overlap="skip_with_dimensions")
    assert "device:probe" not in _label_sources(plain)
    assert ("skipped_labels", "2") in plain.metadata
    assert ("skipped_labels_by_dimension", "1") in plain.metadata
    assert "skipped_dimensions" not in dict(plain.metadata)

    composed = generate_plan(
        model,
        _plan_spec(),
        label_overlap="skip_with_dimensions",
        dimension_overlap="skip_shorter",
    )
    # The probe label collided only with the skipped shorter dimension, so it
    # is kept once that dimension is gone; device:bbb still loses to aaa.
    assert "device:probe" in _label_sources(composed)
    assert "device:bbb" not in _label_sources(composed)
    assert ("skipped_labels", "1") in composed.metadata
    assert ("skipped_labels_by_dimension", "0") in composed.metadata
    assert ("skipped_dimensions", "2") in composed.metadata


def test_elevation_dimensions_skip_the_shorter_one_too() -> None:
    model = _model()
    # The south elevation projects x mirrored, so the bounds reach negative.
    spec = ElevationSpec(
        id="elevation:south",
        origin=Point3(x=0, y=0, z=0),
        direction=Vector3(x=0, y=1, z=0),
        bounds=Bounds2(min_x=-3.5, min_y=-0.5, max_x=1.0, max_y=3.0),
    )
    keep = generate_elevation(model, spec)
    assert _dimension_sources(keep) == {"opening:a", "opening:b", "opening:far"}
    assert "skipped_dimensions" not in dict(keep.metadata)

    skip = generate_elevation(model, spec, dimension_overlap="skip_shorter")
    # opening:b is 0.15 m from opening:a, so its width and height text both
    # collide with opening:a's (equal values, lower id kept); opening:far is
    # clear and keeps both of its dimensions.
    assert _dimension_sources(skip) == {"opening:a", "opening:far"}
    assert ("skipped_dimensions", "2") in skip.metadata


def test_section_accepts_the_option_and_changes_nothing() -> None:
    model = _model()
    spec = SectionSpec(
        id="section:a",
        origin=Point3(x=0, y=0, z=0),
        direction=Vector3(x=0, y=1, z=0),
        depth_m=4.0,
        back_depth_m=0.2,
        bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=4.0, max_y=3.0),
    )
    assert generate_section(model, spec) == generate_section(model, spec, dimension_overlap="keep")
    assert generate_section(model, spec) == generate_section(model, spec, dimension_overlap="skip_shorter")


def test_drawing_set_passes_the_option_to_every_view_and_defaults_stay_identical() -> None:
    model = _model()
    plans = (_plan_spec(),)
    elevations = (
        ElevationSpec(
            id="elevation:south",
            origin=Point3(x=0, y=0, z=0),
            direction=Vector3(x=0, y=1, z=0),
            bounds=Bounds2(min_x=-3.5, min_y=-0.5, max_x=1.0, max_y=3.0),
        ),
    )
    sections = (
        SectionSpec(
            id="section:a",
            origin=Point3(x=0, y=0, z=0),
            direction=Vector3(x=0, y=1, z=0),
            depth_m=4.0,
            back_depth_m=0.2,
            bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=4.0, max_y=3.0),
        ),
    )

    default = generate_drawing_set(model, plans=plans, elevations=elevations, sections=sections)
    explicit = generate_drawing_set(
        model, plans=plans, elevations=elevations, sections=sections, dimension_overlap="keep"
    )
    assert default.to_json() == explicit.to_json()
    for view in explicit.views:
        assert "skipped_dimensions" not in dict(view.metadata)

    skipped = generate_drawing_set(
        model, plans=plans, elevations=elevations, sections=sections, dimension_overlap="skip_shorter"
    )
    plan_view = next(view for view in skipped.views if view.view_type == "plan")
    elevation_view = next(view for view in skipped.views if view.view_type == "elevation")
    section_view = next(view for view in skipped.views if view.view_type == "section")
    assert ("skipped_dimensions", "2") in plan_view.metadata
    assert ("skipped_dimensions", "2") in elevation_view.metadata
    assert "skipped_dimensions" not in dict(section_view.metadata)
    assert skipped.to_json() != default.to_json()


def test_generation_is_deterministic() -> None:
    model = _model()
    first = generate_plan(model, _plan_spec(), dimension_overlap="skip_shorter")
    second = generate_plan(model, _plan_spec(), dimension_overlap="skip_shorter")
    assert first == second


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        generate_plan(_model(), _plan_spec(), dimension_overlap="skip")
    with pytest.raises(ValueError):
        generate_drawing_set(_model(), dimension_overlap="nonsense")
