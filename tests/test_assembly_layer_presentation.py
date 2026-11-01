"""Same canonical room/selection across actual GLB, drawings, CAD and IFC."""
import io
import json
from pathlib import Path
import sys

import pytest

from oabm.model import LayerDefinition, LayerSelectionConfig, SavedPreset, resolve_preset, ContractError
from oabm.model.layers import _universe
from oabm.exports import build_layer_glb
from oabm.ifc import to_ifc, from_ifc, canonical_id_to_ifc_guid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
from assembly_layer_selections import synthetic_model, saved_config, run
from physical_route_layers import read_glb, hidden_nodes


@pytest.fixture(scope='module')
def room():
    model=synthetic_model()
    return model,saved_config(model)


def test_owned_membership_strict_child_override_shared_support_and_levels(room):
    model,config=room
    all_visible=resolve_preset(config,model,preset='preset:all',unassigned='error')
    assert set(all_visible.visible_ids)==set(_universe(model))
    assert not all_visible.unassigned_ids
    wall=next(a for a in model.building_assemblies if a.assembly_kind=='wall')
    child=wall.component_ids[0]
    layers=(LayerDefinition(id='layer:wall',name='Wall',member_ids=(wall.host_id,)),
        LayerDefinition(id='layer:child',name='Child',member_ids=(child,)))
    conflict=LayerSelectionConfig(layers=layers,presets=(SavedPreset(id='preset:conflict',name='Conflict',
        visible_layer_ids=('layer:wall',),hidden_layer_ids=('layer:child',)),))
    resolved=resolve_preset(conflict,model,preset='preset:conflict',unassigned='visible')
    assert child in resolved.hidden_ids
    assert set(wall.component_ids)-{child} <= set(resolved.visible_ids)
    assert wall.id in resolved.visible_ids
    # A shared support is separately classified; it does not inherit either load's system.
    isolated=resolve_preset(config,model,preset='preset:all',isolate=('layer:power',),unassigned='error')
    assert 'support:shared' in isolated.hidden_ids
    assert 'support:part' in isolated.hidden_ids
    selected=resolve_preset(config,model,preset='preset:review',level_ids=('level:proof',),unassigned='error')
    assert 'device:unknown' in selected.hidden_ids
    with pytest.raises(ContractError,match='missing level'):
        resolve_preset(config,model,preset='preset:all',level_ids=('level:missing',),unassigned='error')


def test_every_physical_mesh_ghosts_without_changing_geometry_or_ownership(room):
    model,config=room
    before=model.to_json()
    all_ids=tuple(_universe(model))
    ghosts=LayerSelectionConfig(layers=(LayerDefinition(id='layer:all',name='All',member_ids=all_ids),),
        presets=(SavedPreset(id='preset:ghost',name='Ghost all',ghosted_layer_ids=('layer:all',)),))
    reset=build_layer_glb(model,config,preset='preset:all',unassigned='error')
    ghost=build_layer_glb(model,ghosts,preset='preset:ghost',unassigned='error')
    normal,normal_bin=read_glb(reset.glb_bytes);doc,binary=read_glb(ghost.glb_bytes)
    assert binary==normal_bin
    assert ghost.to_dict()['unsupported_ghost_ids']==[]
    for node in doc['nodes']:
        if 'mesh' not in node:continue
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            mat=doc['materials'][primitive['material']]
            assert mat['alphaMode']=='BLEND'
            assert mat['pbrMetallicRoughness']['baseColorFactor'][3]==pytest.approx(.3)
    assert len({n['name'] for n in doc['nodes']})==len(doc['nodes'])
    assert model.to_json()==before
    parent=next(n for n in doc['nodes'] if n['name']=='assembly:ceiling-proof')
    assert set(parent['extras']['component_ids'])==set(next(a for a in model.building_assemblies if a.id==parent['name']).component_ids)
    assert build_layer_glb(model,ghosts,preset='preset:ghost',unassigned='error').glb_bytes==ghost.glb_bytes


def test_native_ifc_selection_retains_geometry_roundtrip_and_deterministic_bytes(room):
    model,config=room
    resolution=resolve_preset(config,model,preset='preset:review',unassigned='error')
    native=to_ifc(model,assembly_geometry='tessellated',layer_resolution=resolution)
    layers={l.Name.rsplit(':',1)[1]:l for l in native.by_type('IfcPresentationLayerWithStyle')}
    assert layers['hidden'].LayerOn is False and layers['hidden'].LayerFrozen is True
    assert layers['visible'].LayerOn is True
    assert layers['ghosted'].LayerStyles[0].Styles[0].Transparency==pytest.approx(.82)
    hidden=native.by_guid(canonical_id_to_ifc_guid('device:lighting'))
    assert {item for rep in hidden.Representation.Representations for item in rep.Items} <= set(layers['hidden'].AssignedItems)
    ghost=native.by_guid(canonical_id_to_ifc_guid('support:part'))
    assert {item for rep in ghost.Representation.Representations for item in rep.Items} <= set(layers['ghosted'].AssignedItems)
    assert from_ifc(native).to_dict()==model.to_dict()
    assert to_ifc(model,assembly_geometry='tessellated',layer_resolution=resolution).to_string()==native.to_string()


def test_actual_saved_room_delivery_drawings_dxf_ifc_and_unknown_disclosure(tmp_path):
    ezdxf=pytest.importorskip('ezdxf')
    output=tmp_path/'delivery'
    reports=run(output)
    assert len(reports)==5
    assert (output/'presets.png').is_file()
    config=LayerSelectionConfig.load(output/'layers.json')
    assert len(config.presets)==2
    report={r['name']:r for r in reports}
    assert report['other-level']['drawing_primitives']=={'plan':0,'ceiling-cut':0}
    assert json.loads((output/'level'/'selection.json').read_text())['unknown_level_hidden_ids']==['device:unknown']
    import ifcopenshell
    import ifcopenshell.validate
    for label in ('reset','review','isolate','level','other-level'):
        native = ifcopenshell.open(str(output/label/'model.ifc'))
        for layer in native.by_type('IfcPresentationLayerWithStyle'):
            assert layer.AssignedItems
            assert all(item.is_a('IfcGeometricRepresentationItem') or item.is_a('IfcMappedItem')
                for item in layer.AssignedItems)
            assert len({item.id() for item in layer.AssignedItems}) == len(layer.AssignedItems)
        logger = ifcopenshell.validate.json_logger()
        ifcopenshell.validate.validate(native, logger, express_rules=True)
        assert not logger.statements, (label, logger.statements)
        manifest=json.loads((output/label/'selection.json').read_text())
        hidden=set(manifest['resolution']['hidden_ids'])
        for name in ('plan','ceiling-cut'):
            dxf=ezdxf.read(io.StringIO((output/label/f'{name}.dxf').read_text()))
            assert not dxf.audit().errors
            companion=json.loads((output/label/f'{name}-dxf-manifest.json').read_text())
            # Actual plotted objects are traced to retained canonical source IDs.
            assert (output/label/f'{name}.svg').is_file()
            plotted = companion['composition']['views'][0]['primitives']
            assert not hidden.intersection(p['source_ids'][0] for p in plotted)
            if label=='review' and name=='plan':
                by_source={p['source_ids'][0]:p for p in plotted}
                for entity_id in ('support:part','member:room'):
                    assert by_source[entity_id]['style']['stroke']=='ghost'
                assert 'device:lighting' not in by_source
            if label=='review':
                assert any(getattr(e.dxf,'true_color',None)==0xA6ADB8 for e in dxf.layouts.get('Layout1'))
        if label=='review':
            assert '#a6adb8' in (output/label/'plan.svg').read_text()
    doc,_=read_glb((output/'isolate'/'model.glb').read_bytes())
    hidden=hidden_nodes(doc)
    assert next(i for i,n in enumerate(doc['nodes']) if n['name']=='support:part') in hidden
