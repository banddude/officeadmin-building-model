"""Physical room projections and selected supported details keep canonical context."""
from dataclasses import replace
import pytest
from oabm.assembly_mesh import assembly_component_mesh
from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
from oabm.drawings import generate_elevation, generate_drawing_set, ElevationSpec, VisibilityPolicy, view_to_svg
from oabm.drawings.details import DetailSpec, generate_detail
from oabm.drawings.model import Bounds2
from oabm.drawings.projection import frame_from_view_direction
from oabm.model import ElectricalEquipment, Point3, Pose, Size3, Vector3


@pytest.mark.parametrize('keep_above,sign', [(True,1),(False,-1)])
def test_depth_clipping_grazing_edges_never_extrapolate(keep_above,sign):
    from oabm.drawings.assembly_cuts import _clip_mesh_face_depth
    # Both are outside the exact plane, even though one is inside the old EPS.
    face = [(0.,0.,sign*-5e-9),(1.,0.,sign*-2e-8),(0.,1.,sign*-3e-8)]
    assert _clip_mesh_face_depth(face,0.,keep_above) == []
    for tiny in (5e-9,1e-20):
        face = [(0.,0.,sign*tiny),(1.,0.,sign*-tiny),(0.,1.,sign*-tiny)]
        result = _clip_mesh_face_depth(face,0.,keep_above)
        assert result
        assert all(0 <= x <= 1 and 0 <= y <= 1 and x+y <= 1
                   and (z >= 0 if keep_above else z <= 0) for x,y,z in result)
        assert any(x == pytest.approx(.5) and y == 0 for x,y,z in result)


@pytest.fixture(scope='module')
def room():
    return synthetic_enclosed_room_assembly()


def test_elevation_projects_every_physical_child_with_actual_bounds_and_no_host_body(room):
    spec = ElevationSpec(id='elevation:room', origin=Point3(x=0,y=0,z=0),
        direction=Vector3(x=0,y=1,z=0), near_m=-10, far_m=10,
        visibility=VisibilityPolicy(labels=False,dimensions=False))
    frozen = room.to_json()
    view = generate_elevation(room,spec,assembly_geometry='projection')
    physical = [p for p in view.primitives if p.layer == 'architecture:assembly-projection']
    assert {p.source_ids[0] for p in physical} == {c.id for c in room.assembly_components}
    aggregate = {a.host_id for a in room.building_assemblies if a.host_representation == 'aggregate_host'}
    assert not any(aggregate.intersection(p.source_ids) for p in view.primitives)
    frame = frame_from_view_direction(origin=spec.origin,direction=spec.direction)
    expected = [frame.project(Point3(x=x,y=y,z=z))[0] for c in room.assembly_components for x,y,z in assembly_component_mesh(c)[0]]
    points = [point for p in physical for point in p.points]
    for axis in ('x','y'):
        assert min(getattr(p,axis) for p in points) == pytest.approx(min(getattr(p,axis) for p in expected),abs=1e-8)
        assert max(getattr(p,axis) for p in points) == pytest.approx(max(getattr(p,axis) for p in expected),abs=1e-8)
    assert 'no hidden-line' in dict(view.metadata)['assembly_projection_limitation']
    assert 'architecture:assembly-projection' in view_to_svg(view)
    assert room.to_json() == frozen
    drawing = generate_drawing_set(room,elevations=(spec,),assembly_geometry='cut')
    assert drawing.views and any(s.id=='schedule:assembly-components' for s in drawing.schedules)
    assert {c.id for c in room.assembly_components} <= {s.canonical_id for s in drawing.source_index}


def test_elevation_respects_depth_and_architecture_visibility(room):
    spec = ElevationSpec(id='elevation:excluded',origin=Point3(x=0,y=0,z=0),direction=Vector3(x=0,y=1,z=0),near_m=100,far_m=101)
    assert not any(p.layer=='architecture:assembly-projection' for p in generate_elevation(room,spec,assembly_geometry='projection').primitives)
    spec = replace(spec,near_m=-10,far_m=10,visibility=VisibilityPolicy(architecture=False))
    assert not any(p.layer=='architecture:assembly-projection' for p in generate_elevation(room,spec,assembly_geometry='projection').primitives)
    with pytest.raises(ValueError,match='explicit'):
        generate_elevation(room,spec,assembly_geometry='guessed')


def test_supported_equipment_detail_works_with_unrelated_room_assemblies(room):
    equipment = ElectricalEquipment(id='equipment:detail',equipment_type='panelboard',level_id=room.levels[0].id,
        pose=Pose(position=Point3(x=2,y=1,z=1)),size=Size3(x=.2,y=.2,z=.4))
    model = replace(room,electrical_equipment=(equipment,))
    frozen = model.to_json()
    spec = DetailSpec(id='detail:equipment',number='1',title='Supplied panel envelope',recipe='equipment-mounting-elevation@1',
        source_ids=(equipment.id,),origin=Point3(x=2,y=0,z=1),direction=Vector3(x=0,y=1,z=0),bounds=Bounds2(-1,-1,1,1),
        scale=10,near_m=0,far_m=2,mounting_spec='Not designed',connection_spec='Not supplied')
    detail = generate_detail(model,spec)
    assert detail.view.primitives
    assert all(set(p.source_ids) <= {equipment.id} for p in detail.view.primitives)
    assert model.to_json() == frozen
    with pytest.raises(ValueError,match='missing or unsupported detail source'):
        generate_detail(model,replace(spec,source_ids=(room.assembly_components[0].id,)))


def test_thin_front_depth_slab_does_not_project_back_or_side_wall_geometry(room):
    spec = ElevationSpec(id='elevation:front-slab',origin=Point3(x=0,y=0,z=0),
        direction=Vector3(x=0,y=1,z=0),near_m=-.06,far_m=.05,visibility=VisibilityPolicy(labels=False,dimensions=False))
    view = generate_elevation(room,spec,assembly_geometry='projection')
    physical = [p for p in view.primitives if p.layer=='architecture:assembly-projection']
    assert physical
    assert {p.source_ids[1] for p in physical} <= {'assembly:wall-proof','assembly:door-proof','assembly:window-proof'}
    assert not any(p.source_ids[0] in {'wall:back','wall:left','wall:right'} for p in view.primitives)
