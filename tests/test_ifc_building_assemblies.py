"""Synthetic opt-in tessellation roundtrip and native tamper refusals."""
from dataclasses import replace
import pytest
pytest.importorskip('ifcopenshell')
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.ifc import to_ifc, from_ifc, round_trip, canonical_id_to_ifc_guid
from oabm.ifc.adapter import IfcAdapterError

@pytest.fixture
def model():
    return synthetic_wall_inserts_ceiling_assembly()

def test_explicit_tessellated_roundtrip_preserves_every_physical_child(model):
    native=to_ifc(model,assembly_geometry='tessellated')
    assert from_ifc(native).to_dict()==model.to_dict()
    assert round_trip(model,assembly_geometry='tessellated').to_dict()==model.to_dict()
    assert native.to_string()==to_ifc(model,assembly_geometry='tessellated').to_string()
    for assembly in model.building_assemblies:
        if assembly.host_representation == 'aggregate_host':
            host=native.by_guid(canonical_id_to_ifc_guid(assembly.host_id))
            assert all(r.RepresentationIdentifier!='Body' for r in host.Representation.Representations)
    for child in model.assembly_components:
        p=native.by_guid(canonical_id_to_ifc_guid(child.id))
        assert p.is_a('IfcBuildingElementProxy')
        assert p.Representation.Representations[0].RepresentationType=='Tessellation'
        assert p.Decomposes[0].RelatingObject.GlobalId==canonical_id_to_ifc_guid(child.assembly_id)

@pytest.mark.parametrize('edit',['mesh','placement','parent','body','topology','name','units','aggregate-placement','aggregate-body'])
def test_native_assembly_edits_refuse_stale_canonical_claims(model,edit):
    native=to_ifc(model,assembly_geometry='tessellated')
    p=native.by_guid(canonical_id_to_ifc_guid(model.assembly_components[0].id))
    face=p.Representation.Representations[0].Items[0]
    if edit=='mesh':
        v=list(face.Coordinates.CoordList);v[0]=(v[0][0]+.01,*v[0][1:]);face.Coordinates.CoordList=v
    elif edit=='placement':p.ObjectPlacement.RelativePlacement.Location.Coordinates=(.01,0.,0.)
    elif edit=='parent':native.remove(p.Decomposes[0])
    elif edit=='body':p.Representation=None
    elif edit=='topology':face.CoordIndex=face.CoordIndex[:-1]
    elif edit=='name':p.Name='changed'
    elif edit=='units':next(u for u in native.by_type('IfcSIUnit') if u.UnitType=='LENGTHUNIT').Prefix='MILLI'
    elif edit=='aggregate-body':p.Decomposes[0].RelatingObject.Representation=p.Representation
    elif edit=='aggregate-placement':p.Decomposes[0].RelatingObject.ObjectPlacement.RelativePlacement.Location.Coordinates=(.01,0.,0.)
    with pytest.raises(IfcAdapterError):from_ifc(native)

def test_default_guard_and_unknown_mode_remain(model):
    with pytest.raises(IfcAdapterError,match='populated'):to_ifc(model)
    with pytest.raises(IfcAdapterError,match='assembly_geometry'):to_ifc(model,assembly_geometry='invented')

def test_native_ifc_schema_and_actual_shape_bounds(model):
    import ifcopenshell.validate
    import ifcopenshell.geom
    import numpy as np
    from oabm.assembly_mesh import assembly_component_mesh
    native=to_ifc(model,assembly_geometry='tessellated')
    logger=ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(native,logger,express_rules=True)
    assert logger.statements==[]
    settings=ifcopenshell.geom.settings();settings.set(settings.USE_WORLD_COORDS,True)
    seen=set()
    for component in model.assembly_components:
        kind=type(component.geometry).__name__
        if kind in seen:continue
        seen.add(kind)
        shape=ifcopenshell.geom.create_shape(settings,native.by_guid(canonical_id_to_ifc_guid(component.id)))
        actual=np.asarray(shape.geometry.verts).reshape((-1,3))
        expected=np.asarray(assembly_component_mesh(component)[0])
        assert np.allclose(actual.min(axis=0),expected.min(axis=0),atol=1e-8,rtol=0)
        assert np.allclose(actual.max(axis=0),expected.max(axis=0),atol=1e-8,rtol=0)
    assert seen=={'Box3D','PanelGeometry','ProfileExtrusion','WireSweep'}


@pytest.mark.parametrize("host_collection", ["walls", "ceilings"])
def test_native_aggregate_host_body_is_not_silently_dropped(model, host_collection):
    native = to_ifc(model, assembly_geometry="tessellated")
    candidates = {entity.id for entity in getattr(model, host_collection)}
    assembly = next(a for a in model.building_assemblies if a.host_representation == "aggregate_host" and a.host_id in candidates)
    host = native.by_guid(canonical_id_to_ifc_guid(assembly.host_id))
    child = native.by_guid(canonical_id_to_ifc_guid(assembly.component_ids[0]))
    host.Representation.Representations = (*host.Representation.Representations, child.Representation.Representations[0])
    with pytest.raises(IfcAdapterError, match="aggregate host"):
        from_ifc(native)


def test_native_aggregate_host_disguised_solid_axis_is_refused(model):
    native = to_ifc(model, assembly_geometry="tessellated")
    assembly = next(a for a in model.building_assemblies if a.host_representation == "aggregate_host")
    host = native.by_guid(canonical_id_to_ifc_guid(assembly.host_id))
    child = native.by_guid(canonical_id_to_ifc_guid(assembly.component_ids[0]))
    host.Representation.Representations[0].Items = child.Representation.Representations[0].Items
    with pytest.raises(IfcAdapterError):
        from_ifc(native)

@pytest.mark.parametrize('collection', ['walls', 'ceilings'])
@pytest.mark.parametrize('edit', ['axis-coordinate', 'host-placement', 'missing-placement'])
def test_assembly_source_axis_and_placement_edits_are_never_discarded(model, collection, edit):
    native=to_ifc(model,assembly_geometry='tessellated')
    ids={item.id for item in getattr(model,collection)}
    assembly=next(a for a in model.building_assemblies if a.host_representation=='aggregate_host' and a.host_id in ids)
    host=native.by_guid(canonical_id_to_ifc_guid(assembly.host_id))
    if edit=='axis-coordinate':
        point=host.Representation.Representations[0].Items[0].Points[0]
        xyz=list(point.Coordinates);xyz[0]+=.01;point.Coordinates=xyz
    elif edit=='host-placement':host.ObjectPlacement.RelativePlacement.Location.Coordinates=(.01,0.,0.)
    else:host.ObjectPlacement=None
    with pytest.raises(IfcAdapterError):from_ifc(native)

@pytest.mark.parametrize('target,edit',[
    ('parent','missing-placement'),('child','missing-placement'),
    ('opening','void-depth'),('opening','missing-void-link'),
    ('deck','solid-depth'),('child','extra-opening'),
])
def test_all_assembly_related_native_geometry_paths_fail_closed(model,target,edit):
    native=to_ifc(model,assembly_geometry='tessellated')
    assembly=model.building_assemblies[0]
    identity=assembly.id if target=='parent' else assembly.component_ids[0]
    if target=='opening':identity=next(a.host_id for a in model.building_assemblies if a.host_representation=='source_opening_void')
    if target=='deck':identity=next(a.deck_id for a in model.building_assemblies if a.deck_id)
    product=native.by_guid(canonical_id_to_ifc_guid(identity))
    if edit=='missing-placement':product.ObjectPlacement=None
    elif edit in {'void-depth','solid-depth'}:
        body=next(r for r in product.Representation.Representations if r.RepresentationIdentifier=='Body')
        body.Items[0].Depth+=.01
    elif edit=='missing-void-link':native.remove(product.VoidsElements[0])
    elif edit=='extra-opening':
        from ifcopenshell.guid import new
        opening=native.by_type('IfcOpeningElement')[0]
        native.create_entity('IfcRelVoidsElement',GlobalId=new(),RelatingBuildingElement=product,RelatedOpeningElement=opening)
    with pytest.raises(IfcAdapterError):from_ifc(native)

@pytest.mark.parametrize('edit', ['opening-body','opening-void','storey-placement'])
def test_wall_without_insert_still_checks_every_digest_geometry_dependency(edit):
    from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_assembly
    model=synthetic_two_opening_wall_assembly()
    native=to_ifc(model,assembly_geometry='tessellated')
    assert all(a.assembly_kind=='wall' for a in model.building_assemblies)
    if edit=='storey-placement':
        storey=native.by_guid(canonical_id_to_ifc_guid(model.levels[0].id))
        storey.ObjectPlacement.RelativePlacement.Location.Coordinates=(.01,0.,0.)
    else:
        opening=native.by_guid(canonical_id_to_ifc_guid(model.openings[0].id))
        if edit=='opening-body':opening.Representation.Representations[0].Items[0].Depth+=.01
        else:native.remove(opening.VoidsElements[0])
    with pytest.raises(IfcAdapterError):from_ifc(native)


def test_presentation_representation_labels_and_groups_are_not_geometry_locks(model):
    import ifcopenshell.api.group
    native=to_ifc(model,assembly_geometry='tessellated')
    child=native.by_guid(canonical_id_to_ifc_guid(model.assembly_components[0].id))
    child.Representation.Name='Viewer caption'
    child.Representation.Description='Display body caption'
    group=ifcopenshell.api.group.add_group(native,name='Viewer selection')
    ifcopenshell.api.group.assign_group(native,products=[child],group=group)
    assert from_ifc(native).to_dict()==model.to_dict()
