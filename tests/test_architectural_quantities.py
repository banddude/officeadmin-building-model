from dataclasses import dataclass, replace
import math
import pytest
from oabm.model import (BuildingModel, Level, Wall, Slab, Ceiling, Space, Opening, Obstacle,
                        Box3D, Point3, Polygon3D, Polyline3D, Pose, Size3, Provenance, ContractError)
from oabm.quantities import extract_quantities, QuantityError
from oabm.quantities.architecture import polygon_area


def polygon(coords):
    return Polygon3D(points=tuple(Point3(x=x, y=y, z=z) for x,y,z in coords))


def base():
    observed = Provenance(source_kind="scan", source_id="synthetic:surface", derivation="observed")
    assumed = Provenance(source_kind="default", source_id="synthetic:thickness", derivation="inferred", scope_paths=("thickness_m",))
    wall = Wall(id="wall:a", level_id="level:a", centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=5,y=0,z=0))), thickness_m=.2, height_m=3, provenance=(observed, assumed))
    floor = polygon(((0,0,0),(4,0,0),(4,3,0),(0,3,0)))
    return BuildingModel(model_id="model:synthetic", levels=(Level(id="level:a", elevation_m=0),), walls=(wall,), slabs=(Slab(id="slab:a", level_id="level:a", footprint=floor, thickness_m=.1, provenance=(observed, assumed)),), spaces=(Space(id="space:a", level_id="level:a", footprint=floor, height_m=2.5, provenance=(observed,)),))


def lines(report, category):
    return [item for item in report.items if item.category == category]


def test_architectural_only_has_known_answer_quantities_and_explicit_gaps():
    report = extract_quantities(base())
    assert lines(report,"wall_length")[0].quantity == 5
    assert [x.quantity for x in lines(report,"wall_face_area")] == [15,15]
    assert {dict(x.variant)["side"] for x in lines(report,"wall_face_area")} == {"left","right"}
    assert lines(report,"wall_volume")[0].quantity == 3
    assert lines(report,"slab_area")[0].quantity == pytest.approx(12)
    assert lines(report,"slab_volume")[0].quantity == pytest.approx(1.2)
    assert lines(report,"space_volume")[0].quantity == pytest.approx(30)
    assert lines(report,"space_floor_area")[0].quantity == pytest.approx(12)
    assert {x.unit for x in report.items} == {"m","m2","m3"}
    assert "assemblies_unresolved" in {w.code for w in report.warnings}


def test_claim_scope_never_promotes_assumed_thickness_to_observed_volume():
    report = extract_quantities(base())
    face = lines(report,"wall_face_area")[0]
    volume = lines(report,"wall_volume")[0]
    assert face.to_dict()["design_status"] == "observed"
    assert volume.to_dict()["design_status"] == "inferred"
    assert len(face.provenance) == 2  # Retain variant/source evidence too.
    assert len(face.quantity_provenance) == 1
    assert lines(report,"slab_area")[0].to_dict()["design_status"] == "observed"
    assert lines(report,"slab_volume")[0].to_dict()["design_status"] == "inferred"


@pytest.mark.parametrize("attribute", [{"assumed_dimension":"thikness_m"},{"assumed_dimension":[]}])
def test_legacy_freeform_scope_cannot_drop_inferred_evidence(attribute):
    model=base()
    inferred=Provenance(source_kind="default",source_id="synthetic:legacy",derivation="inferred",attributes=attribute)
    wall=replace(model.walls[0],provenance=(model.walls[0].provenance[0],inferred))
    report=extract_quantities(replace(model,walls=(wall,)))
    assert all(x.to_dict()["design_status"] == "inferred" for x in report.items if x.category.startswith("wall_"))


def test_invalid_scope_rejected_by_canonical_model():
    model=base()
    wrong=Provenance(source_kind="default",source_id="synthetic:typo",derivation="inferred",scope_paths=("thikness_m",))
    with pytest.raises(ContractError,match="scope path"):
        replace(model,walls=(replace(model.walls[0],provenance=(wrong,)),))


@pytest.mark.parametrize("coords", [
    ((0,0,0),(1,0,0),(1,1,1),(0,1,0)),
    ((0,0,0),(2,2,0),(0,2,0),(2,0,0)),
    ((0,0,0),(1,0,0),(2,0,0)),
    ((0,0,0),(3,0,0),(1,0,0),(1,2,0),(0,2,0)),
    ((0,0,0),(3,0,0),(3,3,0),(1,0,0),(0,3,0)),
])
def test_invalid_polygon_is_warned_not_measured(coords):
    model=base()
    model=replace(model,slabs=(replace(model.slabs[0],footprint=polygon(coords)),),walls=(),spaces=())
    report=extract_quantities(model)
    assert report.items == ()
    assert {w.code for w in report.warnings} >= {"unmeasurable_polygon","empty_takeoff","unmeasured_entities"}


def test_tilted_surface_uses_its_own_plane_and_declines_undefined_volume():
    model=base()
    tilted=polygon(((0,0,0),(4,0,0),(4,3,4),(0,3,4)))
    model=replace(model,slabs=(replace(model.slabs[0],footprint=tilted),))
    report=extract_quantities(model)
    assert lines(report,"slab_area")[0].quantity == 20
    assert not lines(report,"slab_volume")
    assert "volume_direction_undefined" in {w.code for w in report.warnings}


@pytest.mark.parametrize("offset", [0,1_000_000_000])
def test_area_translation_winding_and_concave_polygon(offset):
    coords=[(0,0,0),(3,0,0),(3,1,0),(1,1,0),(1,3,0),(0,3,0)]
    coords=[(x+offset,y+offset,z+offset) for x,y,z in coords]
    assert polygon_area(polygon(coords)) == pytest.approx(5)
    assert polygon_area(polygon(tuple(reversed(coords)))) == pytest.approx(5)


def test_unknown_canonical_collection_is_not_silently_ignored():
    @dataclass(frozen=True,slots=True,kw_only=True)
    class FutureModel(BuildingModel):
        future_levels: tuple[Level,...] = ()
    with pytest.raises(QuantityError,match="future_levels"):
        extract_quantities(FutureModel(model_id="model:future",future_levels=(Level(id="future:a",elevation_m=0),)))


def test_openings_count_without_fictional_deductions():
    model=base()
    opening=Opening(id="opening:a",host_id="wall:a",opening_type="door",pose=Pose(position=Point3(x=1,y=0,z=1)),size=Size3(x=.9,y=.2,z=2))
    report=extract_quantities(replace(model,openings=(opening,)))
    assert lines(report,"opening_count")[0].quantity == 1
    assert all(x.quantity == 15 for x in lines(report,"wall_face_area"))
    assert "opening_deduction_undefined" in {w.code for w in report.warnings}


def test_bent_wall_counts_length_but_declines_ambiguous_join_solid():
    model=base()
    wall=replace(model.walls[0],centerline=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=3,y=0,z=0),Point3(x=3,y=4,z=0))))
    report=extract_quantities(replace(model,walls=(wall,)))
    assert lines(report,"wall_length")[0].quantity == 7
    assert not lines(report,"wall_face_area")
    assert "wall_solid_undefined" in {w.code for w in report.warnings}


def test_grouped_architecture_and_determinism_preserve_model():
    model=base()
    before=model.to_json()
    report=extract_quantities(model,groups={"Alternate":["wall:a"]})
    assert all(x.group == "Alternate" for x in report.items if x.category.startswith("wall_"))
    assert report.unmatched_group_ids == 0
    assert model.to_json() == before
    assert extract_quantities(model).to_json() == extract_quantities(replace(model,walls=tuple(reversed(model.walls)))).to_json()


def test_missing_height_reports_area_without_volume():
    model=base()
    report=extract_quantities(replace(model,spaces=(replace(model.spaces[0],height_m=None),)))
    assert lines(report,"space_floor_area")
    assert not lines(report,"space_volume")
    assert "missing_volume_dimension" in {w.code for w in report.warnings}


def test_user_directed_design_does_not_hide_inferred_measurement():
    model=base()
    wall=replace(model.walls[0],provenance=(Provenance(source_kind="user-override",source_id="synthetic:user",derivation="user"),model.walls[0].provenance[1]))
    report=extract_quantities(replace(model,walls=(wall,)))
    assert lines(report,"wall_volume")[0].to_dict()["quantity_derivation"] == "inferred"
    assert lines(report,"wall_face_area")[0].to_dict()["quantity_derivation"] == "user"


def test_multiple_entities_keep_warning_and_quantity_order_deterministic():
    model=base()
    second=replace(model.walls[0],id="wall:b",height_m=4)
    model=replace(model,walls=(*model.walls,second))
    assert extract_quantities(model).to_json() == extract_quantities(replace(model,walls=tuple(reversed(model.walls)))).to_json()


def test_ceiling_area_and_volume_keep_explicit_units():
    model=base()
    ceiling=Ceiling(id="ceiling:a",level_id="level:a",footprint=model.slabs[0].footprint,thickness_m=.04)
    report=extract_quantities(replace(model,ceilings=(ceiling,)))
    area=lines(report,"ceiling_area")[0]
    volume=lines(report,"ceiling_volume")[0]
    assert (area.unit,volume.unit) == ("m2","m3")
    assert area.quantity == pytest.approx(12)
    assert volume.quantity == pytest.approx(.48)
    assert area.to_dict()["quantity_derivation"] == "unknown"


def test_aggregate_does_not_mix_inferred_and_observed_equal_geometry():
    model=base()
    observed=replace(model.walls[0],id="wall:b",provenance=(model.walls[0].provenance[0],))
    report=extract_quantities(replace(model,walls=(*model.walls,observed)))
    volumes=lines(report,"wall_volume")
    assert len(volumes) == 2
    assert {v.to_dict()["quantity_derivation"] for v in volumes} == {"observed","inferred"}
    assert all(v.quantity == 3 for v in volumes)
    assert len(lines(report,"wall_face_area")) == 2
    assert all(v.quantity == 30 for v in lines(report,"wall_face_area"))


def test_unknown_only_entity_model_is_explicitly_empty_not_zero():
    report=extract_quantities(BuildingModel(model_id="model:level",levels=(Level(id="level:a",elevation_m=0),)))
    assert report.items == ()
    assert {w.code for w in report.warnings} == {"unmeasured_entities","empty_takeoff"}
    assert "contains entities" in next(w.message for w in report.warnings if w.code == "empty_takeoff")


def test_issue_80_building_only_takeoff_is_not_silently_empty():
    # Regression test for #80: a real LiDAR capture imported to 20 canonical
    # entities (1 level, 1 space, 6 walls, 1 slab, 4 openings, 7 obstacles) with
    # no electrical content. The takeoff must not be silently empty: it yields
    # building quantity lines and explicitly names what it does not measure.
    observed = Provenance(source_kind="scan", source_id="repro:issue-80", derivation="observed")
    floor = polygon(((0,0,0),(10,0,0),(10,8,0),(0,8,0)))
    runs = [(0,0,10,0),(10,0,10,8),(10,8,0,8),(0,8,0,0),(3,0,3,8),(7,0,7,8)]
    walls = tuple(Wall(id=f"wall:{i}", level_id="level:a",
                       centerline=Polyline3D(points=(Point3(x=x0,y=y0,z=0),Point3(x=x1,y=y1,z=0))),
                       thickness_m=0.2, height_m=3.0, provenance=(observed,))
                  for i,(x0,y0,x1,y1) in enumerate(runs))
    openings = tuple(Opening(id=f"opening:{i}", host_id="wall:0", opening_type="door",
                             pose=Pose(position=Point3(x=1.0+i,y=0,z=0), rotation=(0,0,0)),
                             size=Size3(x=0.9,y=0.05,z=2.1), provenance=(observed,))
                     for i in range(4))
    obstacles = tuple(Obstacle(id=f"obstacle:{i}", level_id="level:a", obstacle_type="hard",
                               geometry=Box3D(pose=Pose(position=Point3(x=i+0.5,y=4,z=0.5), rotation=(0,0,0)),
                                             size=Size3(x=1.0,y=1.0,z=1.0)),
                               provenance=(observed,)) for i in range(7))
    model = BuildingModel(model_id="model:issue-80",
                          levels=(Level(id="level:a", elevation_m=0),),
                          spaces=(Space(id="space:a", level_id="level:a", footprint=floor, height_m=3.0, provenance=(observed,)),),
                          walls=walls,
                          slabs=(Slab(id="slab:a", level_id="level:a", footprint=floor, thickness_m=0.15, provenance=(observed,)),),
                          openings=openings, obstacles=obstacles)
    report = extract_quantities(model)
    categories = {item.category for item in report.items}
    assert len(report.items) > 0  # never silently empty again
    assert {"wall_length","wall_face_area","slab_area","space_floor_area","opening_count"} <= categories
    assert "empty_takeoff" not in {w.code for w in report.warnings}
    unmeasured = [w for w in report.warnings if w.code == "unmeasured_entities"]
    assert any("obstacle:0" in w.source_entity_ids for w in unmeasured)
