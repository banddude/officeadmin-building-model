"""Actual physical-sheet extents and owner links; no screenshot substitution."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from oabm.drawings import PlanSpec, Symbol, generate_drawing_set
from oabm.drawings.label_layout import _inflate, _overlap, _segment_hits_box
from oabm.model import stable_id
from oabm.pipeline.as_built import apply_transaction,replay_history,snapshot
from oabm.pipeline.as_built_delivery import _drawings
from oabm.pipeline.as_built_presentation import ROUTE_COLOR

ROOT=Path(__file__).resolve().parents[1]
NS={'s':'http://www.w3.org/2000/svg'}


def example():
    spec=importlib.util.spec_from_file_location('as_built_presentation_example',ROOT/'examples/as_built_quick_edit.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_small_model_physical_callouts_legend_route_and_paper_are_bound_to_unchanged_geometry():
    history,request=example().synthetic_inputs()
    model=replay_history(apply_transaction(history,request));before=snapshot(model)
    outputs=_drawings(model,request['transaction_id'])
    audit=json.loads(outputs['drawings/plan-layout.json'])
    assert audit['model_sha256']==hashlib.sha256(before.encode()).hexdigest()
    view=json.loads(outputs['drawings/plans.json'])['views'][0]
    layout=audit['plans'][0]
    assert layout['paper_mm'][0]<=420 and layout['paper_mm'][1]<=297
    assert layout['scale'] in {25,50} and layout['text_height_mm']==3
    entries={row['owner_id']:row for row in layout['entries']}
    for identity in ('device:evse','device:spare','equip:panel',model.routes[0].id):
        assert entries[identity]['callout_status']=='placed'
    boxes=[row['text_box_m'] for row in entries.values() if row['callout_status']=='placed']
    for index,box in enumerate(boxes):
        assert not any(_overlap(box,other) for other in boxes[index+1:])
        assert not any(_overlap(box,dimension) for dimension in layout['dimension_text_boxes_m'])
        for primitive in view['primitives']:
            if primitive['kind'] not in {'line','polyline','polygon'} or primitive['layer'].startswith('annotations:'):
                continue
            points=primitive['points']+([primitive['points'][0]] if primitive['closed'] else [])
            from oabm.drawings import Point2
            assert not any(_segment_hits_box(Point2(**a),Point2(**b),_inflate(box,.5*layout['scale']/1000))
                           for a,b in zip(points,points[1:]))
    original=generate_drawing_set(model,plans=(PlanSpec(stable_id('as-built-plan',model.levels[0].id),
        model.levels[0].id,cut_height_m=model.levels[0].height_m,view_depth_below_m=0,
        view_depth_above_m=0,route_level_policy='traversed_stories'),),include_schedules=False,
        symbol_provider=lambda entity,view_type:Symbol('as-built-location'),physical_geometry='xy-centerline',assembly_geometry='cut')
    projected={p['id']:p for p in view['primitives']}
    for primitive in original.views[0].primitives:
        if primitive.kind=='text':
            assert all(any((primitive.text or '') in label for label in entries[identity]['labels'])
                       for identity in primitive.source_ids)
            assert all(any(row['id']==primitive.id and row['text']==primitive.text
                           for row in entries[identity]['original_labels']) for identity in primitive.source_ids)
            continue
        assert projected[primitive.id]['points']==[{'x':p.x,'y':p.y} for p in primitive.points]
        assert projected[primitive.id]['source_ids']==list(primitive.source_ids)
    dimensions={row['id']:row for row in view['dimensions']}
    for dimension in original.views[0].dimensions:
        assert dimensions[dimension.id]['value_m']==dimension.value_m
        assert dimensions[dimension.id]['start']=={'x':dimension.start.x,'y':dimension.start.y}
        assert dimensions[dimension.id]['end']=={'x':dimension.end.x,'y':dimension.end.y}
    svg=ET.fromstring(outputs['drawings/plan-1.svg'])
    assert svg.attrib['width']!='1189mm'
    paths=[node for node in svg.iter() if model.routes[0].id in (node.get('data-source-ids') or '')
           and node.tag.rsplit('}',1)[-1] in {'line','polyline','path'}]
    assert any(node.get('stroke')==ROUTE_COLOR and node.get('stroke-dasharray') for node in paths)
    assert json.loads(outputs['drawings/plan-1.manifest.json'])['styles']['stroke_colors']['route']==ROUTE_COLOR
    assert snapshot(model)==before


def test_panel_cards_use_readable_font_unknown_positions_and_complete_source_rows():
    history,request=example().synthetic_inputs()
    model=replay_history(apply_transaction(history,request));outputs=_drawings(model,'field:presentation')
    svg=ET.fromstring(outputs['drawings/panel-1.svg'])
    assert svg.get('width')=='420mm' and svg.get('height')=='297mm'
    texts=svg.findall('.//s:text',NS)
    assert min(float(node.get('font-size')) for node in texts)>=3
    text=' '.join(node.text or '' for node in texts)
    assert 'UNPOSITIONED' in text and 'TBD' in text and '9600' in text
    assert any(node.get('font-weight')=='600' and float(node.get('font-size'))>3 for node in texts)
    layout=json.loads(outputs['drawings/panel-layout.json'])
    assert {row['source_id'] for row in layout['rows']}=={model.circuits[0].id,model.electrical_equipment[0].id}
    circuit=next(row for row in layout['rows'] if row['source_id']==model.circuits[0].id)
    assert circuit['position_status']=='not_supplied_display_order_only' and circuit['display_side']=='unpositioned'


def test_actual_full_sheet_previews_bind_exact_svg_bytes_without_crop(tmp_path):
    pytest.importorskip('cairosvg')
    root=tmp_path/'preview-example'
    manifest=example().create_synthetic_delivery(root,preview_images=True)
    preview=json.loads((root/'preview-images/PREVIEW-MANIFEST.json').read_bytes())
    assert preview['visual_review_status']=='unreviewed'
    assert preview['model_sha256']==manifest['revised_model_sha256']
    assert preview['source_manifest_sha256']==hashlib.sha256((root/'delivery/MANIFEST.json').read_bytes()).hexdigest()
    assert preview['images']
    for row in preview['images']:
        raw=(root/'delivery'/row['source_svg']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==row['source_svg_sha256']
        image=(root/'preview-images'/row['image']).read_bytes()
        assert hashlib.sha256(image).hexdigest()==row['sha256']
        assert struct.unpack('>II',image[16:24])==(row['width'],row['height'])
        svg=ET.fromstring(raw)
        assert row['width']==round(float(svg.get('width').removesuffix('mm'))*96/25.4*1.5)
        assert row['height']==round(float(svg.get('height').removesuffix('mm'))*96/25.4*1.5)


def test_retained_three_story_sheets_print_their_actual_level_in_elevation_order(tmp_path):
    module=example();root=tmp_path/'three-story'
    module.create_three_story_delivery(root)
    from oabm.model import BuildingModel
    model=BuildingModel.load(root/'delivery/canonical-model.json')
    audit=json.loads((root/'delivery/drawings/plan-layout.json').read_text())
    levels=sorted(model.levels,key=lambda level:(level.elevation_m,level.id))
    assert [row['level_id'] for row in audit['plans']]==[level.id for level in levels]
    for ordinal,(level,row) in enumerate(zip(levels,audit['plans']),1):
        svg=ET.fromstring((root/f'delivery/drawings/plan-{ordinal}.svg').read_bytes())
        manifest=json.loads((root/f'delivery/drawings/plan-{ordinal}.manifest.json').read_text())
        ids={item['svg_id'] for item in manifest['items'] if ':level-heading:' in item['source_item_id']}
        heading=[node.text or '' for node in svg.findall('.//s:text',NS) if node.get('id') in ids]
        assert heading==row['printed_level_heading_lines']
        assert ''.join(heading)==f'{level.name or level.id} PlanLevel ID: {level.id}Elevation: {level.elevation_m:g} m'


def test_long_level_names_wrap_losslessly_within_selected_sheet_bounds():
    history,request=example().synthetic_inputs()
    model=replay_history(apply_transaction(history,request))
    name='Long canonical level name retained in full / '*8
    model=replace(model,levels=(replace(model.levels[0],name=name),))
    outputs=_drawings(model,'field:long-level')
    row=json.loads(outputs['drawings/plan-layout.json'])['plans'][0]
    assert ''.join(row['printed_level_heading_lines']).startswith(name+' Plan')
    svg=ET.fromstring(outputs['drawings/plan-1.svg'])
    manifest=json.loads(outputs['drawings/plan-1.manifest.json'])
    ids={item['svg_id'] for item in manifest['items'] if ':level-heading:' in item['source_item_id']}
    printed=''.join(node.text or '' for node in svg.findall('.//s:text',NS) if node.get('id') in ids)
    assert printed==''.join(row['printed_level_heading_lines'])


def test_panel_row_rectangles_match_exact_svg_anchor_coordinates():
    from oabm.drawings import SheetSpec
    from oabm.drawings.schedule_set import panel_schedule_drawing_set
    from oabm.drawings.schedule_layout import ScheduleLayout,draft_panel_schedule_pages
    history,request=example().synthetic_inputs()
    model=replay_history(apply_transaction(history,request));outputs=_drawings(model,'field:row-bounds')
    report,schedules=panel_schedule_drawing_set(model)
    pages=draft_panel_schedule_pages(report,schedules,SheetSpec('AB-P',420,297,titleblock_height_mm=30,
        title='As-built supplied panel facts',revision='field:row-bounds'),
        layout=ScheduleLayout(text_height_mm=3,line_height_mm=4.5,packing='compact'),table_width_mm=360)
    recorded=json.loads(outputs['drawings/panel-layout.json'])['rows']
    assert recorded==[row for page in pages for row in page.row_map]
    for ordinal,page in enumerate(pages,1):
        svg=ET.fromstring(outputs[f'drawings/panel-{ordinal}.svg'])
        manifest=json.loads(outputs[f'drawings/panel-{ordinal}.manifest.json'])
        ids={item['source_item_id']:item['svg_id'] for item in manifest['items']}
        nodes={node.get('id'):node for node in svg.findall('.//s:text',NS)}
        view=page.drawing_set.views[0];primitives={p.id:p for p in view.primitives}
        for row in page.row_map:
            left,top,right,bottom=row['bounds_mm']
            for identity in row['primitive_ids']:
                primitive=primitives[identity]
                if primitive.kind!='text':
                    continue
                point=primitive.points[0];node=nodes[ids[identity]]
                x=float(node.get('x'));y=float(node.get('y'))
                assert x==pytest.approx(page.placement.x_mm+(point.x-view.bounds.min_x)*1000,abs=1e-5)
                assert y==pytest.approx(page.placement.y_mm+(view.bounds.max_y-point.y)*1000,abs=1e-5)
                assert left<=x<=right and top<=y<=bottom
