"""Glazed-wall presentation on derived drawings.

A wall whose canonical ``construction`` is ``glazed`` projects onto its own
``architecture:glazing`` layer with the lightest existing line weight and one
dashed centre line per wall segment in plan cuts; sections and elevations use
the same layer. Every other construction token keeps ``architecture:walls``
and carries its token in each primitive's ``metadata``. Walls without a token
stay byte-identical to the pre-token output: the pinned drawing fixture is
tokenless, so its golden fingerprint and the absence of any primitive
metadata prove that. All content is synthetic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from oabm.drawings import (
    Bounds2,
    DrawingSet,
    ElevationSpec,
    PlanSpec,
    SectionSpec,
    VisibilityPolicy,
    generate_drawing_set,
    generate_elevation,
    generate_plan,
    generate_section,
)
from oabm.model import BuildingModel, Level, Point3, Polyline3D, Vector3, Wall

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "drawings" / "v1" / "drawing-model.json"
GOLDEN = ROOT / "fixtures" / "drawings" / "v1" / "expected-drawing-set.sha256"

GLAZING_LAYER = "architecture:glazing"
WALLS_LAYER = "architecture:walls"
_QUIET = VisibilityPolicy(labels=False)


def _wall(wall_id: str, *, y: float, thickness: float, token: str | None) -> Wall:
    values: dict[str, object] = dict(
        id=wall_id,
        level_id="level:ground",
        centerline=Polyline3D(points=(Point3(x=0, y=y, z=0), Point3(x=4, y=y, z=0))),
        thickness_m=thickness,
        height_m=2.7,
    )
    if token is not None:
        values["construction"] = token
    return Wall(**values)  # type: ignore[arg-type]


def _model() -> BuildingModel:
    return BuildingModel(
        model_id="model:drawings-glazed",
        name="Synthetic Glazed Drawings Model",
        levels=(Level(id="level:ground", elevation_m=0.0),),
        walls=(
            _wall("wall:glass-run", y=0.0, thickness=0.12, token="glazed"),
            _wall("wall:stud-run", y=3.0, thickness=0.15, token="framed"),
        ),
    )


def _plan_spec() -> PlanSpec:
    return PlanSpec(
        id="plan:glazed",
        level_id="level:ground",
        bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=4.5, max_y=3.5),
        visibility=_QUIET,
    )


def _section_spec() -> SectionSpec:
    return SectionSpec(
        id="section:glazed",
        origin=Point3(x=2, y=0, z=0),
        direction=Vector3(x=0, y=1, z=0),
        depth_m=4.0,
        back_depth_m=0.2,
        bounds=Bounds2(min_x=-2.5, min_y=-0.5, max_x=2.5, max_y=4.5),
        visibility=_QUIET,
    )


def test_plan_glazed_and_framed_walls_split_layers_with_thin_style_and_one_centre_line() -> None:
    view = generate_plan(_model(), _plan_spec())

    glazed = [p for p in view.primitives if p.source_ids == ("wall:glass-run",)]
    assert glazed, "the glazed wall must project"
    assert {p.layer for p in glazed} == {GLAZING_LAYER}
    assert all(p.style.weight == "normal" for p in glazed), "glazing reads thin even where cut"

    cut_solid = next(p for p in glazed if p.kind == "polygon")
    assert cut_solid.style.stroke == "cut"
    assert {round(p.y, 6) for p in cut_solid.points} == {-0.06, 0.06}

    centres = [p for p in glazed if p.kind == "polyline"]
    assert len(centres) == 1, "exactly one centre line per glazed segment"
    centre = centres[0]
    assert centre.layer == GLAZING_LAYER
    assert centre.style.weight == "normal"
    assert centre.style.pattern == "dash"
    assert all(round(p.y, 6) == 0.0 for p in centre.points), "the centre line runs between the two faces"
    assert min(p.x for p in centre.points) == 0 and max(p.x for p in centre.points) == 4

    framed = [p for p in view.primitives if p.source_ids == ("wall:stud-run",)]
    assert framed, "the framed wall must project"
    assert {p.layer for p in framed} == {WALLS_LAYER}
    assert all(p.metadata == (("construction", "framed"),) for p in framed)
    framed_cut = next(p for p in framed if p.kind == "polygon")
    assert framed_cut.style.stroke == "cut" and framed_cut.style.weight == "heavy"
    assert not [p for p in framed if p.kind == "polyline"], "only glazed walls get a centre line"


def test_section_through_glazed_wall_uses_glazing_layer() -> None:
    view = generate_section(_model(), _section_spec())

    glazed = [p for p in view.primitives if p.source_ids == ("wall:glass-run",)]
    assert glazed
    assert {p.layer for p in glazed} == {GLAZING_LAYER}
    cut_profile = next(p for p in glazed if p.style.stroke == "cut")
    assert cut_profile.style.weight == "normal"
    assert cut_profile.metadata == (("construction", "glazed"),)

    framed = [p for p in view.primitives if p.source_ids == ("wall:stud-run",)]
    assert framed
    assert {p.layer for p in framed} == {WALLS_LAYER}


def test_elevation_of_glazed_wall_uses_glazing_layer() -> None:
    view = generate_elevation(
        _model(),
        ElevationSpec(
            id="elevation:glazed",
            origin=Point3(x=0, y=0, z=0),
            direction=Vector3(x=0, y=1, z=0),
            near_m=-0.5,
            far_m=4.5,
            bounds=Bounds2(min_x=-0.5, min_y=-0.5, max_x=4.5, max_y=3.5),
            visibility=_QUIET,
        ),
    )
    glazed = [p for p in view.primitives if p.source_ids == ("wall:glass-run",)]
    assert glazed
    assert {p.layer for p in glazed} == {GLAZING_LAYER}
    assert all(p.metadata == (("construction", "glazed"),) for p in glazed)


def test_tokenless_models_stay_byte_identical_to_main() -> None:
    # The pinned drawing fixture predates construction tokens: regenerating its
    # golden fingerprint proves byte identity, and no primitive may gain
    # metadata from a tokenless model.
    model = BuildingModel.load(FIXTURE)
    drawing_set = generate_drawing_set(
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
    assert hashlib.sha256(drawing_set.to_json().encode("utf-8")).hexdigest() == GOLDEN.read_text(encoding="utf-8").strip()
    assert all(p.metadata == () for view in drawing_set.views for p in view.primitives)
    assert not [p for view in drawing_set.views for p in view.primitives if p.layer == GLAZING_LAYER]


def test_generation_is_deterministic_byte_for_byte() -> None:
    first = generate_drawing_set(_model(), plans=(_plan_spec(),), sections=(_section_spec(),)).to_json()
    second = generate_drawing_set(_model(), plans=(_plan_spec(),), sections=(_section_spec(),)).to_json()
    assert first == second
