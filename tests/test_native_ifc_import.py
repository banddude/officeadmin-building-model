"""Independent anonymous IFC4 source -> bounded canonical walls/openings."""
import hashlib
import itertools
import math
from dataclasses import replace
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.validate
import numpy as np
import pytest

from oabm.ifc import (import_native_ifc,NativeIfcImportError,IfcAdapterError,
    from_ifc,to_ifc,round_trip_differences)
from oabm.model import BuildingModel,validate_model
from oabm.quantities import extract_quantities
from native_ifc_fixtures import (author_fixture,build_native_ifc_fixture,native_world_vertices,
    native_volume,WALL_GUID,OPENING_GUID,STOREY_GUID)

ROOT=Path(__file__).resolve().parents[1]


def intake(file,source_id='fixture:native-ifc'):
    return import_native_ifc(file.to_string().encode(),source_id=source_id)


def missing_required_source(file, entity, field):
    original=entity.to_string()
    reference=getattr(entity,field)
    malformed=original.replace('#'+str(reference.id()), '$', 1)
    assert malformed != original
    return file.to_string().replace(original,malformed).encode()


def body(file,guid):
    return next(r for r in file.by_guid(guid).Representation.Representations if r.RepresentationIdentifier=='Body').Items[0]


def axis(file):
    return next(r for r in file.by_guid(WALL_GUID).Representation.Representations if r.RepresentationIdentifier=='Axis').Items[0]


def canonical_wall_vertices(wall):
    a,b=wall.centerline.points
    tangent=np.array((b.x-a.x,b.y-a.y,0));tangent/=np.linalg.norm(tangent)
    normal=np.array((-tangent[1],tangent[0],0))*wall.thickness_m/2
    return np.array([np.array((p.x,p.y,p.z))+normal*side+np.array((0,0,z))
        for p,side,z in itertools.product((a,b),(-1,1),(0,wall.height_m))])


def canonical_opening_vertices(opening):
    q=opening.pose.rotation;angle=2*math.atan2(q.z,q.w)
    u=np.array((math.cos(angle),math.sin(angle),0));v=np.array((-u[1],u[0],0));z=np.array((0,0,1))
    center=np.array((opening.pose.position.x,opening.pose.position.y,opening.pose.position.z))
    return np.array([center+u*x+v*y+z*h for x,y,h in itertools.product(
        (-opening.size.x/2,opening.size.x/2),(-opening.size.y/2,opening.size.y/2),(-opening.size.z/2,opening.size.z/2))])


def same_vertices(a,b):
    distances=np.linalg.norm(np.array(a)[:,None,:]-np.array(b)[None,:,:],axis=2)
    assert np.max(np.min(distances,axis=0))<1e-7
    assert np.max(np.min(distances,axis=1))<1e-7


@pytest.mark.parametrize('options',[
    {},{'mm':True},{'nested':True},{'edge_axis':True,'reverse_axis':True},
    {'poly_profile':True,'edge_axis':True,'reversed_winding':True},
    {'horizontal_opening':True},
    {'mm':True,'nested':True,'edge_axis':True,'horizontal_opening':True,'window':True},
])
def test_native_fixture_exact_geometry_units_and_existing_consumers(options):
    file=author_fixture(**options)
    logger=ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(file,logger,express_rules=True)
    assert not logger.statements
    source=file.to_string().encode();result=import_native_ifc(source,source_id='fixture:native-exact')
    assert result.status==('partial' if options.get('window') else 'complete')
    assert result.supported_walls==result.supported_openings==1
    assert result.source_sha256==hashlib.sha256(source).hexdigest()
    model=result.model;validate_model(model)
    wall=model.walls[0];opening=model.openings[0]
    same_vertices(canonical_wall_vertices(wall),native_world_vertices(file.by_guid(WALL_GUID)))
    same_vertices(canonical_opening_vertices(opening),native_world_vertices(file.by_guid(OPENING_GUID)))
    assert wall.thickness_m==pytest.approx(.3) and wall.height_m==pytest.approx(2)
    assert opening.host_id==wall.id and opening.size.x==pytest.approx(1)
    assert opening.size.y==pytest.approx(.3) and opening.size.z==pytest.approx(1)
    assert model.levels[0].height_m is None and wall.construction is None
    assert model.name is None and wall.name is None
    assert wall.attributes['native_ifc']['observations']['axis']['points_native']
    assert wall.provenance[0].scope_paths==('attributes.native_ifc',)
    assert wall.provenance[0].derivation=='observed'
    assert wall.provenance[1].derivation=='inferred' and 'centerline' in wall.provenance[1].scope_paths
    result.validate_against(source,source_id='fixture:native-exact')
    with pytest.raises(IfcAdapterError,match='requires a Name'):to_ifc(model)
    named=replace(model,name='Anonymous synthetic native intake')
    assert round_trip_differences(named,from_ifc(to_ifc(named)))==()
    quantities=extract_quantities(model)
    volumes=[q for q in quantities.items if q.category=='wall_volume']
    assert sum(q.quantity for q in volumes)==pytest.approx(1.8)
    assert native_volume(file.by_guid(WALL_GUID),subtract_openings=False)==pytest.approx(1.8)
    assert native_volume(file.by_guid(WALL_GUID),subtract_openings=True)==pytest.approx(1.5)
    assert model.attributes['native_ifc']['quantity_limitations']['opening_deductions_unresolved']
    if options.get('window'):
        assert opening.opening_type=='window'
        assert result.diagnostics[0].reason=='filling_geometry_unmodeled'
        assert opening.attributes['native_ifc']['observations']['filling']['geometry_modeled'] is False


def test_equivalent_opening_rotation_decomposition():
    file=author_fixture(horizontal_opening=True)
    original=intake(file)
    solid=body(file,OPENING_GUID);opening=file.by_guid(OPENING_GUID)
    rotation=solid.Position
    product_position=opening.ObjectPlacement.RelativePlacement
    product_position.Axis=rotation.Axis;product_position.RefDirection=rotation.RefDirection
    rotation.Axis=file.create_entity('IfcDirection',DirectionRatios=(0.,0.,1.))
    rotation.RefDirection=file.create_entity('IfcDirection',DirectionRatios=(1.,0.,0.))
    result=intake(file)
    assert result.status=='complete'
    assert result.model.openings[0].pose==original.model.openings[0].pose
    assert result.model.openings[0].size==original.model.openings[0].size
    same_vertices(canonical_opening_vertices(result.model.openings[0]),native_world_vertices(opening))


def test_stable_ids_step_order_and_revision_source_freshness():
    file=author_fixture();before=intake(file)
    lines=file.to_string().splitlines();start=lines.index('DATA;')+1;end=next(i for i in range(start,len(lines)) if lines[i]=='ENDSEC;')
    reordered='\n'.join(lines[:start]+list(reversed(lines[start:end]))+lines[end:])+'\n'
    after=import_native_ifc(reordered.encode(),source_id=before.source_id)
    assert after.source_sha256!=before.source_sha256
    assert after.model.walls[0].id==before.model.walls[0].id
    assert after.model.openings[0].id==before.model.openings[0].id
    assert after.model.walls[0].centerline==before.model.walls[0].centerline
    body(file,WALL_GUID).Depth=2.5
    revision=intake(file)
    assert revision.model.walls[0].id==before.model.walls[0].id
    assert revision.model.walls[0].height_m==2.5
    with pytest.raises(NativeIfcImportError,match='binding'):before.validate_against(file.to_string().encode(),source_id=before.source_id)
    with pytest.raises(NativeIfcImportError,match='binding'):before.validate_against(author_fixture().to_string().encode(),source_id='fixture:different')
    before.model.walls[0].attributes['native_ifc']['forged']=True
    with pytest.raises(NativeIfcImportError,match='binding'):before.validate_against(author_fixture().to_string().encode(),source_id=before.source_id)


@pytest.mark.parametrize('kind',['no-axis','multiple-body','axis-too-short','axis-outside','wall-tilted','nonrectangle','open-profile','no-storey','missing-direction','missing-profile'])
def test_unsupported_wall_diagnostics_and_no_orphan_openings(kind):
    file=author_fixture(poly_profile=kind in {'nonrectangle','open-profile'})
    wall=file.by_guid(WALL_GUID);solid=body(file,WALL_GUID)
    if kind=='no-axis':wall.Representation.Representations=tuple(r for r in wall.Representation.Representations if r.RepresentationIdentifier!='Axis')
    elif kind=='multiple-body':wall.Representation.Representations=(*wall.Representation.Representations,wall.Representation.Representations[-1])
    elif kind=='axis-too-short':axis(file).Points[-1].Coordinates=(2.,0.,0.)
    elif kind=='axis-outside':
        for point in axis(file).Points:point.Coordinates=(point.Coordinates[0],1.,0.)
    elif kind=='wall-tilted':solid.Position.Axis=file.create_entity('IfcDirection',DirectionRatios=(0.,1.,0.))
    elif kind=='nonrectangle':solid.SweptArea.OuterCurve.Points[1].Coordinates=(3.,.8)
    elif kind=='open-profile':solid.SweptArea.OuterCurve.Points=solid.SweptArea.OuterCurve.Points[:-1]
    elif kind=='no-storey':file.remove(wall.ContainedInStructure[0])
    elif kind=='missing-direction':pass
    else:pass
    source=missing_required_source(file,solid,'ExtrudedDirection' if kind=='missing-direction' else 'SweptArea') if kind in {'missing-direction','missing-profile'} else file.to_string().encode()
    result=import_native_ifc(source,source_id='fixture:native-ifc')
    assert result.status=='no_supported_geometry' and result.model is None
    assert result.supported_walls==result.supported_openings==0
    assert any(d.native_guid==WALL_GUID for d in result.diagnostics)
    assert any(d.native_guid==OPENING_GUID and d.reason=='opening_host_not_imported' for d in result.diagnostics)


@pytest.mark.parametrize('kind',['oblique','outside','shallow','recess','two-hosts'])
def test_opening_refusals_leave_host_partial(kind):
    file=author_fixture();opening=file.by_guid(OPENING_GUID);solid=body(file,OPENING_GUID)
    if kind=='oblique':solid.ExtrudedDirection=file.create_entity('IfcDirection',DirectionRatios=(.1,0.,1.))
    elif kind=='outside':opening.ObjectPlacement.RelativePlacement.Location.Coordinates=(4.,0.,.5)
    elif kind=='shallow':solid.SweptArea.YDim=.1
    elif kind=='recess':opening.PredefinedType='RECESS'
    else:file.create_entity('IfcRelVoidsElement',GlobalId=ifcopenshell.guid.new(),RelatingBuildingElement=file.by_guid(WALL_GUID),RelatedOpeningElement=opening)
    result=intake(file)
    assert result.status=='partial' and len(result.model.walls)==1 and not result.model.openings
    assert any(d.native_guid==OPENING_GUID for d in result.diagnostics)


@pytest.mark.parametrize('kind',['duplicate-guid','missing-units','unsupported-units','context','georeferencing','cycle','tilted-storey','missing-relative','missing-world','conflicting-containment','missing-project-storey'])
def test_invalid_global_context_fails_without_source_text(kind):
    file=author_fixture();project=file.by_type('IfcProject')[0];storey=file.by_guid(STOREY_GUID)
    if kind=='duplicate-guid':file.by_guid(OPENING_GUID).GlobalId=WALL_GUID
    elif kind=='missing-units':project.UnitsInContext=None
    elif kind=='unsupported-units':project.UnitsInContext.Units[0].Prefix='CENTI'
    elif kind=='context':project.RepresentationContexts=(*project.RepresentationContexts,project.RepresentationContexts[0])
    elif kind=='georeferencing':file.by_type('IfcSite')[0].RefLatitude=(1,2,3)
    elif kind=='cycle':storey.ObjectPlacement.PlacementRelTo=storey.ObjectPlacement
    elif kind=='tilted-storey':storey.ObjectPlacement.RelativePlacement.Axis=file.create_entity('IfcDirection',DirectionRatios=(0.,1.,0.))
    elif kind in {'missing-relative','missing-world'}:pass
    elif kind=='conflicting-containment':file.create_entity('IfcRelContainedInSpatialStructure',GlobalId=ifcopenshell.guid.new(),RelatingStructure=storey,RelatedElements=(file.by_guid(WALL_GUID),))
    else:file.remove(storey.Decomposes[0])
    if kind=='missing-relative':source=missing_required_source(file,storey.ObjectPlacement,'RelativePlacement')
    elif kind=='missing-world':source=missing_required_source(file,project.RepresentationContexts[0],'WorldCoordinateSystem')
    else:source=file.to_string().encode()
    with pytest.raises(NativeIfcImportError) as exc:import_native_ifc(source,source_id='fixture:native-ifc')
    assert 'Synthetic project' not in str(exc.value) and '#'+str(storey.id()) not in str(exc.value)


def test_unsupported_product_partial_and_strict_reader_unchanged(tmp_path):
    file=author_fixture();file.create_entity('IfcSlab',GlobalId=ifcopenshell.guid.new(),PredefinedType='FLOOR')
    result=intake(file)
    assert result.status=='partial' and any(d.native_type=='IfcSlab' for d in result.diagnostics)
    path=tmp_path/'anonymous.ifc';path.write_text(file.to_string())
    with pytest.raises(IfcAdapterError,match='missing OABM_Canonical'):from_ifc(path)
    assert import_native_ifc(path,source_id='fixture:native-ifc').model.to_json()==result.model.to_json()
    legacy=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
    before=to_ifc(legacy).to_string()
    intake(file)
    assert to_ifc(legacy).to_string()==before


@pytest.mark.parametrize('kind',['taper','hollow','profile-void'])
def test_subtypes_never_become_filled_boxes(kind):
    file=author_fixture(poly_profile=kind=='profile-void');solid=body(file,WALL_GUID)
    wall=file.by_guid(WALL_GUID)
    representation=next(r for r in wall.Representation.Representations if r.RepresentationIdentifier=='Body')
    if kind=='taper':
        replacement=file.create_entity('IfcExtrudedAreaSolidTapered',SweptArea=solid.SweptArea,Position=solid.Position,
            ExtrudedDirection=solid.ExtrudedDirection,Depth=solid.Depth,EndSweptArea=solid.SweptArea)
        representation.Items=(replacement,)
    elif kind=='hollow':solid.SweptArea=file.create_entity('IfcRectangleHollowProfileDef',ProfileType='AREA',Position=solid.SweptArea.Position,XDim=3.,YDim=.3,WallThickness=.05)
    else:solid.SweptArea=file.create_entity('IfcArbitraryProfileDefWithVoids',ProfileType='AREA',OuterCurve=solid.SweptArea.OuterCurve,InnerCurves=(solid.SweptArea.OuterCurve,))
    result=intake(file)
    assert result.model is None and result.status=='no_supported_geometry'
    assert result.supported_walls==0


def test_preflight_limits_and_read_error_redaction(tmp_path,monkeypatch):
    import oabm.importers.ifc_native as native
    def forbidden(*args,**kwargs):raise AssertionError('parser must not run')
    monkeypatch.setattr(native.ifcopenshell.file,'from_string',forbidden)
    with pytest.raises(NativeIfcImportError,match='byte_budget'):import_native_ifc(b'x'*(native.MAX_BYTES+1),source_id='fixture:bounded')
    with pytest.raises(NativeIfcImportError,match='entity_budget'):import_native_ifc(b'#1=FAKE();\n'*(native.MAX_ENTITIES+1),source_id='fixture:bounded')
    with pytest.raises(NativeIfcImportError,match='stable_source'):import_native_ifc(b'',source_id='')
    with pytest.raises(NativeIfcImportError,match='source_read_failed') as exc:
        import_native_ifc(tmp_path/'synthetic-sensitive-name-missing.ifc',source_id='fixture:bounded')
    assert 'sensitive' not in str(exc.value)


@pytest.mark.parametrize('walls',[True,False])
def test_unsupported_space_inventory_is_visible(walls):
    file=author_fixture()
    space=file.create_entity('IfcSpace',GlobalId=ifcopenshell.guid.new(),PredefinedType='INTERNAL')
    if not walls:
        file.remove(file.by_guid(OPENING_GUID));file.remove(file.by_guid(WALL_GUID))
    result=intake(file)
    assert result.status==('partial' if walls else 'no_supported_geometry')
    assert any(d.native_guid==space.GlobalId and d.native_type=='IfcSpace' and d.reason=='unsupported_product_type' for d in result.diagnostics)


def test_container_geometry_is_not_silently_ignored():
    file=author_fixture()
    building=file.by_type('IfcBuilding')[0]
    building.Representation=file.by_guid(WALL_GUID).Representation
    result=intake(file)
    assert result.status=='partial'
    assert any(d.native_guid==building.GlobalId and d.reason=='unsupported_product_type' for d in result.diagnostics)


@pytest.mark.parametrize('name',['native-metre','native-mm-nested-horizontal','native-edge-polyline-reverse'])
def test_committed_anonymous_native_sources(name):
    source=(ROOT/'fixtures/ifc_native'/f'{name}.ifc').read_bytes()
    file=ifcopenshell.file.from_string(source.decode())
    logger=ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(file,logger,express_rules=True)
    assert not logger.statements
    result=import_native_ifc(source,source_id='fixture:'+name)
    assert result.supported_walls==result.supported_openings==1
    same_vertices(canonical_wall_vertices(result.model.walls[0]),native_world_vertices(file.by_guid(WALL_GUID)))
    same_vertices(canonical_opening_vertices(result.model.openings[0]),native_world_vertices(file.by_guid(OPENING_GUID)))
