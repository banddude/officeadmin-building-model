"""Known-answer triangle cuts and canonical physical-view integration."""
from dataclasses import replace
import pytest
from oabm.assembly_mesh import _index_triangle_soup, _local_box_vertices
from oabm.drawings.assembly_cuts import mesh_plane_segments
from oabm.drawings import PlanSpec, SectionSpec, Bounds2, generate_plan, generate_section
from oabm.drawings.projection import ProjectionFrame
from oabm.model import Point3,Vector3
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly

FRAME=ProjectionFrame(origin=Point3(x=0,y=0,z=0),right=Vector3(x=1,y=0,z=0),up=Vector3(x=0,y=1,z=0),normal=Vector3(x=0,y=0,z=1))
@pytest.mark.parametrize('cut',[-1.,0.,1.])
def test_cube_cut_has_four_boundary_edges_no_triangulation_diagonals(cut):
    vertices,indices=_index_triangle_soup(_local_box_vertices(2.,4.,2.))
    lines=mesh_plane_segments(vertices,indices,FRAME,cut)
    assert len(lines)==4
    assert set(lines)=={((-1.,-2.),(-1.,2.)),((-1.,-2.),(1.,-2.)),((-1.,2.),(1.,2.)),((1.,-2.),(1.,2.))}
    assert mesh_plane_segments(vertices,indices,FRAME,2.)==()

def test_physical_plan_preserves_model_and_child_identity_suppresses_aggregate_host():
    model=synthetic_wall_inserts_ceiling_assembly();before=model.to_json()
    spec=PlanSpec(id='physical',level_id=model.levels[0].id)
    view=generate_plan(model,spec,assembly_geometry='cut')
    cut=[p for p in view.primitives if p.layer=='architecture:assembly-cuts']
    assert cut
    children={c.id:c for c in model.assembly_components}
    assert all(p.source_ids[0] in children and p.source_ids[1]==children[p.source_ids[0]].assembly_id for p in cut)
    hosts={a.host_id for a in model.building_assemblies if a.host_representation=='aggregate_host'}
    assert not any(set(p.source_ids)&hosts for p in view.primitives)
    assert dict(view.metadata)['assembly_mode']=='cut-plane-only'
    assert model.to_json()==before
    assert generate_plan(model,spec,assembly_geometry='cut')==view
    hidden=generate_plan(model,replace(spec,visibility=replace(spec.visibility,architecture=False)),assembly_geometry='cut')
    assert not any(p.layer=='architecture:assembly-cuts' for p in hidden.primitives)

def test_section_and_explicit_bounds_clip_actual_parts():
    model=synthetic_wall_inserts_ceiling_assembly()
    spec=SectionSpec(id='section',origin=Point3(x=1,y=0,z=0),direction=Vector3(x=1,y=0,z=0),bounds=Bounds2(-1,-1,1,4))
    view=generate_section(model,spec,assembly_geometry='cut')
    cuts=[p for p in view.primitives if p.layer=='architecture:assembly-cuts'];assert cuts
    for p in cuts:
        assert all(-1<=q.x<=1 and -1<=q.y<=4 for q in p.points)
    with pytest.raises(ValueError,match='assembly_geometry'):generate_plan(model,PlanSpec(id='p',level_id=model.levels[0].id),assembly_geometry='silhouette')
    with pytest.raises(ValueError,match='populated'):generate_section(model,spec)
