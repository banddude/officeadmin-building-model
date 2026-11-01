"""Saved physical-route display is source-bound, complete and geometry preserving."""
from dataclasses import replace
import hashlib
import json

import pytest

from oabm.exports import build_layer_glb, layer_glb_plan
from oabm.exports.gltf import build_glb_bytes
from oabm.model import (
    Box3D, BuildingModel, ContractError, LayerDefinition, LayerSelectionConfig,
    Level, Obstacle, Point3, Polyline3D, Pose, SavedPreset, Size3, resolve_preset,
)
from test_physical_route_glb import curved_model, unpack
from oabm.reports.physical_routes import generate_physical_route_schedule
from oabm.exports.review_bundle import build_review_bundle
from oabm.qa.confidence_review import build_confidence_review, ReviewConfig

OPTIONS = dict(physical_routes=True, include_obstacles=True)


def model_fixture():
    # Add the supplied box before rebuilding the source-bound physical context.
    from oabm.model import compute_physical_source_sha256, compute_physical_request_sha256
    model = curved_model()
    source = replace(model, routes=(), obstacles=(Obstacle(id='obstacle:box',
        geometry=Box3D(pose=Pose(position=Point3(x=.5,y=.5,z=0)),
                       size=Size3(x=.2,y=.2,z=.2))),))
    route = model.routes[0]
    context = replace(route.context, source_model_sha256=compute_physical_source_sha256(source))
    context = replace(context, request_sha256=compute_physical_request_sha256(context,route.requirements))
    return replace(source,routes=(replace(route, context=context),))


def config_fixture():
    return LayerSelectionConfig(layers=(
        LayerDefinition(id='layer:route', name='Route', member_ids=('route:physical',)),
        LayerDefinition(id='layer:box', name='Supplied boxes', member_ids=('obstacle:box',)),
    ), presets=(
        SavedPreset(id='preset:ghost-box',name='Ghost boxes',visible_layer_ids=('layer:route',),ghosted_layer_ids=('layer:box',)),
        SavedPreset(id='preset:hide-route',name='Hide route',hidden_layer_ids=('layer:route',),visible_layer_ids=('layer:box',)),
        SavedPreset(id='preset:ghost-route',name='Ghost route',ghosted_layer_ids=('layer:route',),visible_layer_ids=('layer:box',)),
    ))


def delivery(model=None, config=None, **kwargs):
    return build_layer_glb(model or model_fixture(), config or config_fixture(),
        preset=kwargs.pop('preset','preset:ghost-box'),unassigned=kwargs.pop('unassigned','visible'),
        **OPTIONS,**kwargs)


def test_geometry_identity_measurements_and_materials_across_presets():
    model=model_fixture();before=model.to_json(); config=config_fixture()
    baseline=build_glb_bytes(model,**OPTIONS); doc,binary=unpack(baseline)
    expected_measurements=generate_physical_route_schedule(model).to_json()
    for preset in config.presets:
        output=delivery(model,config,preset=preset.id)
        data=output.to_dict(); got,body=unpack(output.glb_bytes)
        assert body==binary
        assert got['accessors']==doc['accessors'] and got['bufferViews']==doc['bufferViews']
        assert data['source']['sha256']==hashlib.sha256(output.source_model_json.encode()).hexdigest()
        assert data['unmatched_group_ids']==0 and data['unsupported_ghost_ids']==[]
        assert generate_physical_route_schedule(BuildingModel.from_json(output.source_model_json)).to_json()==expected_measurements
        for node in doc['nodes']:
            actual=next(n for n in got['nodes'] if n['name']==node['name'])
            assert actual.get('extras')=={**node.get('extras',{}),**({'display':actual['extras']['display']} if 'display' in actual.get('extras',{}) else {})}
            assert actual.get('translation')==node.get('translation')
        assert output==delivery(model,config,preset=preset.id)
        # Existing review bundle accepts the exact saved-layer display options,
        # including hidden nodes and the explicit whole-model quantity limit.
        snapshot=BuildingModel.from_json(output.source_model_json)
        confidence=build_confidence_review(snapshot,ReviewConfig(.9),physical_routes=True)
        bundle=build_review_bundle(snapshot,glb_bytes=output.glb_bytes,model_reference='model.json',
            glb_reference='model.glb',confidence_manifest=confidence,confidence_reference='confidence.json',
            source_reference_mapping_reference='sources.json',source_references={('model','model:physical-fixture'):'synthetic/model-source'},
            glb_options=data['export_options'])
        assert bundle.to_dict()['artifacts']['glb']['sha256']==data['glb_sha256']
    assert model.to_json()==before


def test_unassigned_obstacle_is_visible_in_coverage_even_when_isolated():
    model=model_fixture();config=config_fixture()
    config=replace(config,layers=(config.layers[0],),presets=(SavedPreset(id='preset:route',name='Route',visible_layer_ids=('layer:route',)),))
    output=delivery(model,config,preset='preset:route',isolate=('layer:route',))
    report=output.to_dict()['resolution']
    assert 'obstacle:box' in report['unassigned_ids'] and 'obstacle:box' in report['hidden_ids']
    with pytest.raises(ContractError,match='unassigned'):
        delivery(model,config,preset='preset:route',isolate=('layer:route',),unassigned='error')
    default=resolve_preset(config,model,preset='preset:route',unassigned='visible')
    assert 'obstacle:box' not in default.visible_ids


def test_unknown_level_disclosure_and_nested_entities_are_not_drawables():
    model=model_fixture()
    # This does not alter route source facts: the resolver alone can inspect an
    # independently constructed unconnected level; the export rejects stale context.
    model=replace(model,routes=(),levels=(Level(id='level:one',name='One',elevation_m=0),))
    config=LayerSelectionConfig(layers=(LayerDefinition(id='layer:box',name='Box',member_ids=('obstacle:box',)),),presets=(SavedPreset(id='preset:box',name='Box',visible_layer_ids=('layer:box',)),))
    output=delivery(model,config,preset='preset:box',level_ids=('level:one',))
    assert 'obstacle:box' in output.to_dict()['unknown_level_hidden_ids']
    model=model_fixture();route=model.routes[0];config=config_fixture()
    config=replace(config,layers=(replace(config.layers[0],member_ids=('route:physical',route.joints[0].id,route.segments[0].id,route.requirements.id)),config.layers[1]))
    report=delivery(model,config).to_dict()['resolution']
    assert report['out_of_scope_member_ids']==sorted((route.joints[0].id,route.segments[0].id,route.requirements.id))


def test_default_guards_and_stale_incomplete_overlapping_foreign_partitions():
    model=model_fixture();config=config_fixture()
    resolution=resolve_preset(config,model,preset='preset:ghost-box',unassigned='visible',include_obstacles=True)
    with pytest.raises(ContractError,match='does not support physical routes'):
        layer_glb_plan(model,resolution)
    for invalid in (
        replace(resolution,visible_ids=()),
        replace(resolution,hidden_ids=resolution.visible_ids),
        replace(resolution,visible_ids=resolution.visible_ids+('obstacle:foreign',)),
        replace(resolution,visible_ids=resolution.visible_ids+resolution.visible_ids),
    ):
        with pytest.raises(ValueError,match='complete disjoint partition'):
            layer_glb_plan(model,invalid,**OPTIONS)
    with pytest.raises(ValueError,match='complete disjoint partition'):
        layer_glb_plan(model,resolution,physical_routes=True,include_obstacles=False)


def test_hidden_unsupported_obstacle_refuses_and_snapshot_is_detached():
    model=model_fixture();config=config_fixture()
    invalid=replace(model,routes=(),obstacles=(replace(model.obstacles[0],geometry=Polyline3D(points=(Point3(x=0,y=0,z=0),Point3(x=1,y=0,z=0)))),))
    config=replace(config,layers=(config.layers[1],),presets=(SavedPreset(id='preset:hidden',name='Hidden',hidden_layer_ids=('layer:box',)),))
    with pytest.raises(ContractError,match='Box3D'):
        delivery(invalid,config,preset='preset:hidden',unassigned='hidden')
    output=delivery(model)
    model.electrical_devices[0].attributes['later']='changed'
    assert 'later' not in output.source_model_json
    assert json.loads(output.manifest_json)['configuration']==config_fixture().to_dict()


def test_overlap_strictest_isolate_and_permutation_are_preserved():
    model=model_fixture();config=config_fixture()
    overlap=LayerDefinition(id='layer:overlap',name='Overlap',member_ids=('route:physical',))
    preset=SavedPreset(id='preset:overlap',name='Overlap states',visible_layer_ids=('layer:route',),
                       ghosted_layer_ids=('layer:box',),hidden_layer_ids=('layer:overlap',))
    config=replace(config,layers=(*config.layers,overlap),presets=(preset,))
    first=delivery(model,config,preset=preset.id)
    assert 'route:physical' in first.to_dict()['resolution']['hidden_ids']
    reordered=replace(config,layers=tuple(reversed(config.layers)))
    assert delivery(model,reordered,preset=preset.id).glb_bytes==first.glb_bytes
    isolated=delivery(model,config,preset=preset.id,isolate=('layer:route',))
    assert isolated.to_dict()['resolution']['visible_ids']==['route:physical']
    assert 'obstacle:box' in isolated.to_dict()['resolution']['hidden_ids']


def test_support_hierarchy_and_populated_building_assemblies_remain_explicitly_unsupported():
    from test_layer_selections import _supports_model
    model=_supports_model()
    config=LayerSelectionConfig(layers=(),presets=(SavedPreset(id='preset:empty',name='Empty'),))
    with pytest.raises(ValueError,match='shared support hierarchy'):
        build_layer_glb(model,config,preset='preset:empty',unassigned='visible',**OPTIONS)
    # Check the compatibility guard at the boundary without inventing a valid
    # producer input or weakening canonical assembly-source validation.
    from types import SimpleNamespace
    from oabm.exports.layers import _validate_partition
    with pytest.raises(ValueError,match='populated canonical building assemblies'):
        _validate_partition(SimpleNamespace(building_assemblies=(object(),)),None,include_obstacles=True)


def test_connected_example_actual_hidden_nodes_materials_render_and_replay(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'examples'))
    from physical_route_layers import create_layer_example, hidden_nodes, mesh_triangles, read_glb
    reports=create_layer_example(tmp_path/'example')
    root=tmp_path/'example'
    assert len({item['source']['sha256'] for item in reports})==1
    assert reports[-1]['resolution']['unassigned_ids']==['device:a','device:b','obstacle:demo']
    assert reports[-1]['resolution']['hidden_ids']==['device:a','device:b','obstacle:demo']
    for index,report in enumerate(reports):
        doc,binary=read_glb((root/f'{index+1:02d}.glb').read_bytes())
        hidden=hidden_nodes(doc)
        hidden_names={doc['nodes'][i]['name'] for i in hidden}
        assert set(report['resolution']['hidden_ids'])<=hidden_names
        for node in doc['nodes']:
            if node.get('name') in ('route:demo','obstacle:demo'):
                triangles,colour=mesh_triangles(doc,binary,node)
                assert len(triangles)>0
                if node['name'] in report['resolution']['ghosted_ids']:
                    assert colour[3]==.3
    # Existing review-bundle exact-byte replay was checked above; this render
    # additionally proves the extension actually removes the mesh from display.
    from PIL import Image
    image=Image.open(root/'presets.png')
    assert image.size==(660,1650)
    assert image.getpixel((325,237)) != image.getpixel((325,330+237))
