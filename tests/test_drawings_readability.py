"""Opt-in dimension precision and label-overlap skipping on derived drawings.

``dimension_decimals`` rescales only dimension display text (``value_m`` stays
full precision) and ``label_overlap="skip"`` drops text labels whose estimated
box hits an already kept label's box in sorted source-id order. Both are
keyword-only, default off, and leave default output byte-identical. Symbols,
dimensions, and geometry are never dropped. All content is synthetic.
"""

from __future__ import annotations

import pytest

from oabm.drawings import (
    Bounds2,
    ElevationSpec,
    PlanSpec,
    SectionSpec,
    VisibilityPolicy,
    generate_drawing_set,
    generate_elevation,
    generate_plan,
    generate_section,
    paper_text_height_m,
    view_to_svg,
)
from oabm.drawings.generator import _LABEL_TEXT_HEIGHT_PAPER_M
from oabm.model import BuildingModel, ElectricalDevice, Level, Opening, Point3, Polyline3D, Pose, Size3, Vector3, Wall

WALL_LENGTH = 3.188208


def _device(device_id: str, name: str, *, x: float, y: float) -> ElectricalDevice:
    return ElectricalDevice(
        id=device_id,
        name=name,
        device_type="receptacle",
        pose=Pose(position=Point3(x=x, y=y, z=1.2)),
        level_id="level:ground",
    )


def _model() -> BuildingModel:
    return BuildingModel(
        model_id="model:drawings-readability",
        name="Synthetic Readability Drawings Model",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=(
            Wall(
                id="wall:main",
                level_id="level:ground",
                centerline=Polyline3D(points=(Point3(x=0, y=0, z=0), Point3(x=WALL_LENGTH, y=0, z=0))),
                thickness_m=0.1,
                height_m=2.7,
            ),
        ),
        openings=(
            Opening(
                id="opening:door",
                host_id="wall:main",
                opening_type="door",
                pose=Pose(position=Point3(x=1.5, y=0, z=1.05)),
                size=Size3(x=0.9, y=0.12, z=2.1),
            ),
        ),
        electrical_devices=(
            _device("device:aaa", "Panel A", x=1.0, y=2.0),
            _device("device:bbb", "Panel B", x=1.0, y=2.0),
            _device("device:ccc", "Panel C", x=0.5, y=3.5),
        ),
    )


def _plan_spec() -> PlanSpec:
    return PlanSpec(
        id="plan:readability",
        level_id="level:ground",
        bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=4.0, max_y=4.5),
    )


def _wall_dimension(view):
    return next(item for item in view.dimensions if item.source_ids == ("wall:main",))


def test_defaults_stay_byte_identical_on_the_fixture_suite() -> None:
    base = generate_drawing_set(
        _model(),
        plans=(_plan_spec(),),
        elevations=(
            ElevationSpec(
                id="elevation:south",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=1.0, max_y=3.0),
            ),
        ),
        sections=(
            SectionSpec(
                id="section:a",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                depth_m=4.0,
                back_depth_m=0.2,
                bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=4.0, max_y=3.0),
            ),
        ),
    )
    explicit = generate_drawing_set(
        _model(),
        plans=(_plan_spec(),),
        elevations=(
            ElevationSpec(
                id="elevation:south",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=1.0, max_y=3.0),
            ),
        ),
        sections=(
            SectionSpec(
                id="section:a",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
                depth_m=4.0,
                back_depth_m=0.2,
                bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=4.0, max_y=3.0),
            ),
        ),
        dimension_decimals=None,
        label_overlap="keep",
    )
    assert base.to_json() == explicit.to_json()
    assert all(name != "skipped_labels" for view in base.views for name, _ in view.metadata)


def test_dimension_decimals_two_rounds_text_but_not_value() -> None:
    view = generate_plan(_model(), _plan_spec(), dimension_decimals=2)
    dimension = _wall_dimension(view)
    assert dimension.text == "3.19 m"
    assert dimension.value_m == pytest.approx(WALL_LENGTH)


def test_dimension_decimals_zero_prints_no_decimal_point() -> None:
    view = generate_plan(_model(), _plan_spec(), dimension_decimals=0)
    assert _wall_dimension(view).text == "3 m"


def test_dimension_decimals_formats_elevation_opening_dimensions() -> None:
    view = generate_elevation(
        _model(),
        ElevationSpec(
            id="elevation:south",
            origin=Point3(x=0, y=0, z=0),
            direction=Vector3(x=0, y=1, z=0),
            bounds=Bounds2(min_x=-4.0, min_y=-1.0, max_x=1.0, max_y=3.0),
        ),
        dimension_decimals=2,
    )
    texts = sorted(item.text for item in view.dimensions)
    assert texts == ["0.90 m", "2.10 m"]
    values = sorted(item.value_m for item in view.dimensions)
    assert values == [pytest.approx(0.9), pytest.approx(2.1)]


def test_label_overlap_skip_keeps_lower_source_id_at_one_anchor() -> None:
    view = generate_plan(_model(), _plan_spec(), label_overlap="skip")
    labels = sorted(p.source_ids[0] for p in view.primitives if p.layer == "annotations:labels")
    assert labels == ["device:aaa", "device:ccc"]
    assert ("skipped_labels", "1") in view.metadata


def test_label_overlap_skip_keeps_all_well_separated_labels() -> None:
    model = _model()
    separated = ElectricalDevice(
        id="device:aaa",
        name="Panel A",
        device_type="receptacle",
        pose=Pose(position=Point3(x=0.5, y=2.0, z=1.2)),
        level_id="level:ground",
    )
    model = BuildingModel(
        model_id=model.model_id,
        name=model.name,
        levels=model.levels,
        walls=model.walls,
        openings=model.openings,
        electrical_devices=(separated,),
    )
    view = generate_plan(model, _plan_spec(), label_overlap="skip")
    labels = [p for p in view.primitives if p.layer == "annotations:labels"]
    assert [p.source_ids[0] for p in labels] == ["device:aaa"]
    assert ("skipped_labels", "0") in view.metadata


def test_label_overlap_skip_never_drops_symbols_dimensions_or_geometry() -> None:
    keep = generate_plan(_model(), _plan_spec(), label_overlap="keep")
    skip = generate_plan(_model(), _plan_spec(), label_overlap="skip")
    assert {p.id for p in keep.primitives if p.kind == "symbol"} == {p.id for p in skip.primitives if p.kind == "symbol"}
    assert keep.dimensions == skip.dimensions
    keep_geometry = {p.id for p in keep.primitives if p.kind != "text"}
    skip_geometry = {p.id for p in skip.primitives if p.kind != "text"}
    assert keep_geometry == skip_geometry


def test_invalid_options_raise() -> None:
    for decimals in (-1, 5, 1.5, True):
        with pytest.raises(ValueError):
            generate_plan(_model(), _plan_spec(), dimension_decimals=decimals)
    for overlap in ("drop", "KEEP", ""):
        with pytest.raises(ValueError):
            generate_plan(_model(), _plan_spec(), label_overlap=overlap)
    with pytest.raises(ValueError):
        generate_elevation(
            _model(),
            ElevationSpec(
                id="elevation:south",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
            ),
            dimension_decimals=9,
        )
    with pytest.raises(ValueError):
        generate_section(
            _model(),
            SectionSpec(
                id="section:a",
                origin=Point3(x=0, y=0, z=0),
                direction=Vector3(x=0, y=1, z=0),
            ),
            label_overlap="omit",
        )
    with pytest.raises(ValueError):
        generate_drawing_set(_model(), dimension_decimals=-2, label_overlap="skip")


def test_generation_is_deterministic_under_both_options() -> None:
    def run():
        return generate_drawing_set(
            _model(),
            plans=(_plan_spec(),),
            dimension_decimals=2,
            label_overlap="skip",
        ).to_json()

    assert run() == run()


def test_svg_without_text_height_matches_the_default_byte_for_byte() -> None:
    view = generate_plan(_model(), _plan_spec())
    default = view_to_svg(view)
    assert default == view_to_svg(view, text_height_m=None)
    assert "font-size" not in default


def test_svg_text_height_sizes_every_label_and_dimension_text() -> None:
    view = generate_plan(_model(), _plan_spec())
    svg = view_to_svg(view, pixels_per_model_unit=40, text_height_m=0.125)
    texts = [line.strip() for line in svg.splitlines() if line.strip().startswith("<text")]
    assert texts
    assert all('font-size="5"' in text for text in texts)
    assert svg.count("font-size") == len(texts)
    assert svg == view_to_svg(view, pixels_per_model_unit=40, text_height_m=0.125)
    paired = view_to_svg(view, pixels_per_model_unit=40, text_height_m=paper_text_height_m(_plan_spec().scale))
    assert paired == svg


def test_paper_text_height_m_matches_the_label_overlap_estimate() -> None:
    assert paper_text_height_m(50) == 0.125
    assert paper_text_height_m(50) == _LABEL_TEXT_HEIGHT_PAPER_M * 50
    assert paper_text_height_m(40, paper_height_m=0.005) == 0.2


@pytest.mark.parametrize("height", [0, 0.0, -0.125])
def test_svg_rejects_non_positive_text_height(height) -> None:
    view = generate_plan(_model(), _plan_spec())
    with pytest.raises(ValueError):
        view_to_svg(view, text_height_m=height)
