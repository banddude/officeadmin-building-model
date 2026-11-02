"""Named overlapping memberships survive actual native exports, once per solid."""
from dataclasses import replace,fields
import io,json,sys
from pathlib import Path
import pytest
import ifcopenshell
import ifcopenshell.validate
import ezdxf
from oabm.exports import (build_layer_glb,read_ifc_layer_memberships,read_dxf_layer_memberships)
from oabm.model import LayerDefinition,LayerSelectionConfig,SavedPreset,BuildingModel
from oabm.ifc import to_ifc,from_ifc
from oabm.quantities import extract_quantities
from oabm.drawings import generate_plan,PlanSpec,VisibilityPolicy
from oabm.drawings.generator import _source_reference
from oabm.drawings.model import DrawingSet
from oabm.drawings.sheets import SheetSpec,ViewPlacement
from oabm.drawings.sheet_dxf import sheet_to_dxf
from oabm.exports.layer_memberships import membership_payload,verify_layer_delivery
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
from assembly_layer_selections import synthetic_model,saved_config

@pytest.fixture(scope='module')
def room():
    model=synthetic_model();base=saved_config(model)
    # Explicit overlapping membership, an empty named system and nonphysical metadata.
    layers=base.layers+(LayerDefinition(id='layer:overlap',name='Fire alarm / existing – synthetic',system='fire_alarm',phase='existing',
        member_ids=('device:power','support:shared',model.ports[0].id)),LayerDefinition(id='layer:empty',name='Unmodeled security',system='security'))
    config=replace(base,layers=layers,presets=tuple(replace(p,visible_layer_ids=p.visible_layer_ids+('layer:overlap','layer:empty')) for p in base.presets))
    delivery=build_layer_glb(model,config,preset='preset:review',unassigned='error',level_ids=('level:proof',))
    return model,config,delivery


def drawing(model):
    view=generate_plan(model,PlanSpec('plan','level:proof',view_depth_above_m=2.5,
        visibility=VisibilityPolicy(labels=False,dimensions=False)),assembly_geometry='cut',symbol_provider=lambda e,v:None)
    sources=tuple(_source_reference(e) for field in fields(model)
        for e in (getattr(model,field.name) if isinstance(getattr(model,field.name),tuple) else ()) if hasattr(e,'id'))
    return DrawingSet(model.model_id,(view,),(),sources),SheetSpec('L1',300,220),(ViewPlacement('main','plan',15,15),)


def test_ifc_native_named_groups_preserve_overlap_empty_metadata_and_geometry(room):
    model,config,delivery=room
    native=to_ifc(model,assembly_geometry='tessellated',layer_delivery=delivery)
    wrapper=read_ifc_layer_memberships(ifcopenshell.file.from_string(native.to_string()))
    assert wrapper['memberships']==membership_payload(model,delivery)
    assert wrapper['memberships']['selection']['configuration']==config.to_dict()
    overlap=next(row for row in wrapper['memberships']['layers'] if row['id']=='layer:overlap')
    assert overlap['out_of_scope_member_ids']==[model.ports[0].id]
    empty=next(row for row in wrapper['native_groups'] if row['layer_id']=='layer:empty')
    assert empty['member_global_ids']==[]
    actual=from_ifc(native)
    assert actual.to_dict()==model.to_dict()
    assert extract_quantities(actual).to_json()==extract_quantities(model).to_json()
    baseline=to_ifc(model,assembly_geometry='tessellated',layer_resolution=verify_layer_delivery(model,delivery)[2])
    for kind in ('IfcTriangulatedFaceSet','IfcExtrudedAreaSolid','IfcProductDefinitionShape'):
        assert len(native.by_type(kind))==len(baseline.by_type(kind))
    logger=ifcopenshell.validate.json_logger();ifcopenshell.validate.validate(native,logger)
    assert not logger.statements
    assert to_ifc(model,assembly_geometry='tessellated',layer_delivery=delivery).to_string()==native.to_string()


def test_ifc_rejects_native_group_edits_and_stale_or_malformed_layer_source(room):
    model,config,delivery=room
    native=to_ifc(model,assembly_geometry='tessellated',layer_delivery=delivery)
    wrapper=read_ifc_layer_memberships(native)
    group=native.by_guid(next(row['global_id'] for row in wrapper['native_groups'] if len(row['member_global_ids'])>2))
    rel=group.IsGroupedBy[0];rel.RelatedObjects=list(rel.RelatedObjects)[:-1]
    with pytest.raises(ValueError,match='membership mismatch'):read_ifc_layer_memberships(native)
    with pytest.raises(ValueError,match='stale|mismatch'):
        to_ifc(replace(model,name='Other source'),assembly_geometry='tessellated',layer_delivery=delivery)
    manifest=delivery.to_dict();manifest['configuration']['layers'][0]['unknown_field']='not allowed'
    bad=replace(delivery,manifest_json=json.dumps(manifest))
    with pytest.raises(ValueError):to_ifc(model,assembly_geometry='tessellated',layer_delivery=bad)


def test_dxf_named_native_groups_share_actual_handles_and_keep_primitive_layers(room):
    model,config,delivery=room
    args=drawing(model)
    output=sheet_to_dxf(*args,layer_model=model,layer_delivery=delivery)
    wrapper=read_dxf_layer_memberships(output.dxf,model)
    assert wrapper['memberships']==membership_payload(model,delivery)
    native=ezdxf.read(io.StringIO(output.dxf.decode()))
    assert not native.audit().fixes
    nonempty=[g for g in wrapper['native_groups'] if g['handles']]
    assert any(set(a['handles'])&set(b['handles']) for i,a in enumerate(nonempty) for b in nonempty[i+1:])
    # Original primitive layers remain native layers, not overwritten by membership.
    assert any(entity.dxf.layer.startswith('OABM_') and not entity.dxf.layer.startswith('OABM_LAYER_')
        for entity in native.layouts.get('Layout1') if entity.dxftype() in ('LINE','LWPOLYLINE'))
    hidden=set(delivery.to_dict()['resolution']['hidden_ids'])
    assert not hidden.intersection(row['source_id'] for row in wrapper['handle_sources'])
    assert next(row for row in wrapper['native_groups'] if row['layer_id']=='layer:empty')['handles']==[]
    # No geometry duplication: the same selected DrawingView without metadata exports the same plotted entities.
    from oabm.exports import apply_layer_selection
    selection=verify_layer_delivery(model,delivery)[2]
    selected=replace(args[0],views=tuple(apply_layer_selection(v,model,selection) for v in args[0].views))
    baseline=ezdxf.read(io.StringIO(sheet_to_dxf(selected,*args[1:]).dxf.decode()))
    def plot(doc):return [(e.dxftype(),e.dxfattribs()) for e in doc.layouts.get('Layout1')]
    assert plot(native)==plot(baseline)
    assert output.dxf==sheet_to_dxf(*args,layer_model=model,layer_delivery=delivery).dxf


def test_dxf_rejects_native_group_edits_and_unknown_sources(room):
    model,_,delivery=room
    output=sheet_to_dxf(*drawing(model),layer_model=model,layer_delivery=delivery)
    wrapper=read_dxf_layer_memberships(output.dxf,model)
    native=ezdxf.read(io.StringIO(output.dxf.decode()))
    row=next(row for row in wrapper['native_groups'] if len(row['handles'])>2)
    group=native.groups.get(row['name']);group.set_data(list(group)[:-1])
    stream=io.StringIO();native.write(stream)
    with pytest.raises(ValueError,match='membership mismatch'):read_dxf_layer_memberships(stream.getvalue().encode(),model)
    args=drawing(model)
    bad_primitive=replace(args[0].views[0].primitives[0],source_ids=('missing:entity',))
    bad=replace(args[0],views=(replace(args[0].views[0],primitives=(bad_primitive,)),))
    with pytest.raises(ValueError,match='unknown canonical'):sheet_to_dxf(bad,*args[1:],layer_model=model,layer_delivery=delivery)


def test_empty_configuration_retains_explicit_unassigned_disclosure(room):
    model,_,_=room
    config=LayerSelectionConfig(layers=(),presets=(SavedPreset(id='preset:empty',name='Empty'),))
    delivery=build_layer_glb(model,config,preset='preset:empty',unassigned='visible')
    native=to_ifc(model,assembly_geometry='tessellated',layer_delivery=delivery)
    wrapper=read_ifc_layer_memberships(native)
    assert wrapper['native_groups']==[]
    assert len(wrapper['memberships']['selection']['resolution']['unassigned_ids'])==168
    output=sheet_to_dxf(*drawing(model),layer_model=model,layer_delivery=delivery)
    assert read_dxf_layer_memberships(output.dxf,model)['native_groups']==[]


def test_dxf_membership_reader_does_not_claim_coordinate_tamper_detection(room):
    model,_,delivery=room
    output=sheet_to_dxf(*drawing(model),layer_model=model,layer_delivery=delivery)
    wrapper=read_dxf_layer_memberships(output.dxf,model)
    native=ezdxf.read(io.StringIO(output.dxf.decode()))
    handle=next(row['handle'] for row in wrapper['handle_sources'] if native.entitydb[row['handle']].dxftype()=='LINE')
    entity=native.entitydb[handle];entity.dxf.start=(123.,456.,0.)
    stream=io.StringIO();native.write(stream)
    # Native identity/group references remain valid even after an editable CAD
    # coordinate changes. Geometry freshness is deliberately outside this API.
    assert read_dxf_layer_memberships(stream.getvalue().encode(),model)==wrapper
