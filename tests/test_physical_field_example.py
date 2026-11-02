"""Retained public physical reroute with actual source-bound full-page images."""
import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import BuildingModel,validate_physical_context_source
from oabm.pipeline.as_built import apply_transaction,replay_history,snapshot

ROOT=Path(__file__).resolve().parents[1]


def example(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'examples'))
    spec=importlib.util.spec_from_file_location('physical_field_example',ROOT/'examples/as_built_physical_route.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_public_physical_example_changes_geometry_with_current_fixed_endpoint_bindings(monkeypatch):
    history,request=example(monkeypatch).physical_field_inputs()
    baseline=replay_history(history);current=replay_history(apply_transaction(history,request))
    old,new=baseline.routes[0],current.routes[0]
    assert old.id==new.id and old.joints!=new.joints and old.segments!=new.segments
    assert old.joints[0]==new.joints[0] and old.joints[-1]==new.joints[-1]
    assert old.context==new.context and old.requirements==new.requirements
    assert current.ports==baseline.ports and current.electrical_devices==baseline.electrical_devices
    source=replace(current,routes=tuple(route for route in current.routes if route.id!=new.id))
    validate_physical_context_source(source,new)
    assert request['installed_assertions']==[]
    assert snapshot(baseline)==history['design_json']
    from oabm.pipeline.as_built_delivery import _drawings
    outputs=_drawings(current,'field:shared-physical-endpoint')
    layout=json.loads(outputs['drawings/plan-layout.json'])
    third=next(row for row in layout['plans'] if row['level_id']=='level:physical:2')
    entries={row['owner_id']:row for row in third['entries']}
    assert entries['device:physical:b']['callout_status']=='placed'
    assert entries['route:physical-example']['callout_status']=='placed'
    assert {row['owner_id'] for row in third['shared_endpoint_symbols']}=={'device:physical:b'}


def test_public_physical_delivery_retains_all_story_previews_with_exact_source_receipts(tmp_path,monkeypatch):
    pytest.importorskip('cairosvg')
    root=tmp_path/'physical-as-built-example'
    manifest=example(monkeypatch).create_physical_field_delivery(root,preview_images=True)
    preview=json.loads((root/'preview-images/PREVIEW-MANIFEST.json').read_bytes())
    assert preview['visual_review_status']=='unreviewed'
    assert preview['model_sha256']==manifest['revised_model_sha256']
    assert preview['source_manifest_sha256']==hashlib.sha256((root/'delivery/MANIFEST.json').read_bytes()).hexdigest()
    assert {row['source_svg'] for row in preview['images']}=={
        'drawings/plan-1.svg','drawings/plan-2.svg','drawings/plan-3.svg','drawings/panel-1.svg'}
    for row in preview['images']:
        assert hashlib.sha256((root/'delivery'/row['source_svg']).read_bytes()).hexdigest()==row['source_svg_sha256']
        assert hashlib.sha256((root/'preview-images'/row['image']).read_bytes()).hexdigest()==row['sha256']
    current=BuildingModel.load(root/'delivery/canonical-model.json')
    assert current.model_id=='model:synthetic-physical-field-reroute'
    materials=json.loads((root/'delivery/materials.json').read_bytes())
    assert not materials['installed_items'] and materials['unresolved_items']


def test_unrelated_coincident_panel_keeps_symbol_collision_checks(monkeypatch):
    from oabm.model import compute_physical_source_sha256,compute_physical_request_sha256
    from oabm.pipeline.as_built_delivery import _drawings
    history,request=example(monkeypatch).physical_field_inputs()
    current=replay_history(apply_transaction(history,request))
    endpoint=next(device for device in current.electrical_devices if device.id=='device:physical:b')
    panel=replace(current.electrical_equipment[0],pose=endpoint.pose,level_id=endpoint.level_id)
    source=replace(current,routes=(),electrical_equipment=(panel,))
    route=current.routes[0]
    context=replace(route.context,source_model_sha256=compute_physical_source_sha256(source))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,route.requirements))
    model=replace(source,routes=(replace(route,context=context),))
    outputs=_drawings(model,'field:unrelated-coincident-symbol')
    layout=json.loads(outputs['drawings/plan-layout.json'])
    third=next(row for row in layout['plans'] if row['level_id']=='level:physical:2')
    assert {row['owner_id'] for row in third['shared_endpoint_symbols']}=={endpoint.id}
    entries={row['owner_id']:row for row in third['entries']}
    for identity in (panel.id,endpoint.id,route.id):
        assert entries[identity]['callout_status']=='legend_only'
