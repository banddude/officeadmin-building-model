"""Source-bound scope presentation and actual optional raster/vector rendering."""
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

from oabm.model import ContractError
from oabm.reports.scope_ledger import generate_scope_ledger
from oabm.pipeline.review_drawings import generate_review_drawings
from oabm.pipeline.scope_review_plan import generate_scope_review_plan, render_scope_review_artifacts

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'examples'))
from scope_review_plan import synthetic_inputs, create_example


def marks(svg):
    root = ET.fromstring(svg)
    return {n.get('data-source-ids'): n for n in root.iter()
            if n.get('data-anchor-model-x') is not None}


def totals(ledger):
    return {s: sum(g['count'] for g in ledger['groups'] if g['status'] == s)
            for s in ('new', 'existing_to_remain', 'removed', 'relocated', 'unresolved')}


def test_geometry_schedule_identity_and_preservation():
    model, config, policy = synthetic_inputs()
    before = model.to_json()
    base, _ = generate_review_drawings(model, config)
    payloads, status = generate_scope_review_plan(model, config, policy=policy)
    assert model.to_json() == before
    assert payloads == generate_scope_review_plan(model, config, policy=policy)[0]
    assert generate_review_drawings(model, config)[0] == base
    ledger = json.loads(payloads['scope_ledger'])
    assert totals(ledger) == {'new': 1, 'existing_to_remain': 2, 'removed': 1, 'relocated': 1, 'unresolved': 1}
    marked = marks(payloads['review_plan:scope-plan'])
    expected = {o.id: o for o in (*model.electrical_devices, *model.electrical_equipment)}
    assert marked.keys() == expected.keys()
    for identity, obj in expected.items():
        node = marked[identity]
        assert float(node.get('data-anchor-model-x')) == obj.pose.position.x
        assert float(node.get('data-anchor-model-y')) == obj.pose.position.y
        assert node.get('data-scope-status') == obj.attributes['pdf_electrical']['scope_status']
    old_doc = json.loads(base['drawing_set'])['drawing_set']
    doc = json.loads(payloads['drawing_set'])['drawing_set']
    assert doc['views'] == old_doc['views']  # no alternate geometry
    assert doc['source_index'] == old_doc['source_index']
    assert {r['source_id'] for r in doc['schedules'][-2]['rows']} == expected.keys()
    assert status['scope']['views'][0]['unmarked_ids'] == []
    schedule = ET.fromstring(payloads['scope_schedule'])
    assert expected.keys() <= {n.get('data-source-ids') for n in schedule.iter()}
    svg = payloads['review_plan:scope-plan'].decode()
    assert 'origin/destination geometry unavailable' in svg
    assert 'stroke-dasharray="5 3"' in svg
    assert 'UNRESOLVED - no work assigned' in svg
    assert 'marker-end' not in svg


def test_revision_rebinds_marks_and_counts_and_rejects_stale_ledger():
    model, config, policy = synthetic_inputs()
    ledger = generate_scope_ledger(model, policy=policy)
    device = next(d for d in model.electrical_devices if d.id == 'device:removed')
    revised = replace(model, electrical_devices=tuple(replace(d,
        attributes={'pdf_electrical': {'scope_status': 'existing_to_remain'}})
        if d.id == device.id else d for d in model.electrical_devices))
    with pytest.raises(ContractError, match='does not match'):
        generate_scope_review_plan(revised, config, policy=policy, ledger=ledger)
    payloads, _ = generate_scope_review_plan(revised, config, policy=policy)
    assert marks(payloads['review_plan:scope-plan'])[device.id].get('data-scope-status') == 'existing_to_remain'
    counts = totals(json.loads(payloads['scope_ledger']))
    assert counts['removed'] == 0 and counts['existing_to_remain'] == 3
    changed_policy = replace(policy, mode='new_when_confirmed_unmarked')
    with pytest.raises(ContractError, match='does not match'):
        generate_scope_review_plan(model, config, policy=changed_policy, ledger=ledger)


@pytest.mark.parametrize('lane', [None, {'scope_status': 'unresolved', 'scope_reason': 'scope_legend_conflict'},
    {'scope_status': 'new', 'scope_reason': 'scope_legend_conflict'},
    {'scope_status': 'unresolved', 'scope_reason': 'scope_marker_undefined'}])
def test_ambiguity_never_becomes_new(lane):
    model, config, policy = synthetic_inputs()
    device = model.electrical_devices[0]
    device = replace(device, attributes={'pdf_electrical': lane})
    model = replace(model, electrical_devices=(device, *model.electrical_devices[1:]))
    payloads, _ = generate_scope_review_plan(model, config,
        policy=replace(policy, mode='new_when_confirmed_unmarked'))
    assert marks(payloads['review_plan:scope-plan'])[device.id].get('data-scope-status') == 'unresolved'


def test_incompatible_ai_scope_refuses_and_outside_geometry_not_repositioned():
    model, config, policy = synthetic_inputs()
    obj = model.electrical_devices[0]
    bad = replace(model, electrical_devices=(replace(obj,
        attributes={**obj.attributes, 'ai': {'source_scope': 'new'}}), *model.electrical_devices[1:]))
    with pytest.raises(ContractError, match='source_scope is incompatible'):
        generate_scope_review_plan(bad, config, policy=policy)
    plan = replace(config.plans[0], bounds_m=(0, 0, 5, 8))
    payloads, status = generate_scope_review_plan(model, replace(config, plans=(plan,)), policy=policy)
    assert 'device:removed' not in marks(payloads['review_plan:scope-plan'])
    assert 'device:removed' in status['scope']['views'][0]['unmarked_ids']
    assert totals(json.loads(payloads['scope_ledger']))['removed'] == 1


def test_real_pdf_png_and_example(tmp_path):
    pytest.importorskip('cairosvg')
    pypdf = pytest.importorskip('pypdf')
    Image = pytest.importorskip('PIL.Image')
    model, config, policy = synthetic_inputs()
    payloads, _ = generate_scope_review_plan(model, config, policy=policy)
    rendered = render_scope_review_artifacts(payloads)
    for key in ('review_plan:scope-plan', 'scope_schedule'):
        pdf = pypdf.PdfReader(io.BytesIO(rendered[key+':pdf']))
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
        for obj in (*model.electrical_devices, *model.electrical_equipment):
            assert obj.id in text
        png = Image.open(io.BytesIO(rendered[key+':png'])).convert('RGB')
        assert png.width >= 1000
    png = Image.open(io.BytesIO(rendered['review_plan:scope-plan:png'])).convert('RGB')
    # Real colored strokes at every supplied pose, independent of metadata.
    for obj in (*model.electrical_devices, *model.electrical_equipment):
        center = marks(payloads['review_plan:scope-plan'])[obj.id].get('data-badge-center')
        bx, by = map(float, center.split())
        x, y = round(bx), round(by+64)
        colors = set(png.crop((x-18, y-18, x+19, y+19)).getdata())
        if obj.attributes['pdf_electrical']['scope_status'] == 'existing_to_remain':
            assert any(max(c) < 180 for c in colors)
        else:
            assert any(max(c)-min(c) > 40 for c in colors)
    create_example(tmp_path/'example')
    assert (tmp_path/'example/scope-plan.pdf').read_bytes().startswith(b'%PDF')
    assert (tmp_path/'example/scope-plan.png').read_bytes().startswith(b'\x89PNG')


def _actual_ink(node):
    from oabm.drawings.company_sheets import _font
    font, _, _, _ = _font('DejaVu Sans', 400)
    size = float(node.get('font-size', '16'))
    x, y = float(node.get('x')), float(node.get('y'))
    left, top, right, bottom = font.getbbox(node.text or '', anchor='ls')
    return (x+left*size/1000, y+top*size/1000,
            x+right*size/1000, y+bottom*size/1000)


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_badges_and_id_ink_clear_existing_labels_without_moving_geometry():
    model, config, policy = synthetic_inputs()
    payloads, _ = generate_scope_review_plan(model, config, policy=policy)
    root = ET.fromstring(payloads['review_plan:scope-plan'])
    labels = [n for n in root.iter() if n.tag.endswith('}text') and n.get('data-source-ids')]
    assert any(n.text == 'RECEPTAC' for n in labels)
    badges = marks(payloads['review_plan:scope-plan'])
    obstacles = [_actual_ink(n) for n in labels]
    for identity, group in badges.items():
        badge = tuple(map(float, group.get('data-mark-bounds').split()))
        label = next(n for n in group if n.tag.endswith('}text') and n.text == identity)
        id_ink = _actual_ink(label)
        assert not _intersects(badge, id_ink)
        assert all(not _intersects(box, obstacle) for box in (badge, id_ink) for obstacle in obstacles)
        obstacles.extend((badge, id_ink))
        obj = next(o for o in (*model.electrical_devices, *model.electrical_equipment) if o.id == identity)
        assert float(group.get('data-anchor-model-x')) == obj.pose.position.x


def test_schedule_rules_are_between_actual_row_ink():
    model, config, policy = synthetic_inputs()
    payloads, _ = generate_scope_review_plan(model, config, policy=policy)
    root = ET.fromstring(payloads['scope_schedule'])
    rows = [n for n in root if n.get('data-schedule-row-id')]
    rules = {n.get('data-row-rule'): float(n.get('y1')) for n in root if n.get('data-row-rule')}
    for index, row in enumerate(rows):
        ink = [_actual_ink(n) for n in row if n.tag.endswith('}text') and n.text]
        rule = rules[row.get('data-schedule-row-id')]
        assert rule > max(b[3] for b in ink)+3
        if index+1 < len(rows):
            next_ink = [_actual_ink(n) for n in rows[index+1] if n.tag.endswith('}text') and n.text]
            assert rule < min(b[1] for b in next_ink)-3


@pytest.mark.parametrize('x,y', [(0, 0), (0, 8), (22, 0), (22, 8)])
def test_boundary_callouts_fit_above_footer_without_repositioning_source(x, y):
    model, config, policy = synthetic_inputs()
    obj = model.electrical_devices[0]
    obj = replace(obj, pose=replace(obj.pose, position=replace(obj.pose.position, x=x, y=y)))
    model = replace(model, electrical_devices=(obj,), electrical_equipment=())
    before = model.to_json()
    payloads, status = generate_scope_review_plan(model, config, policy=policy)
    assert status['scope']['views'][0]['marked_ids'] == [obj.id]
    group = marks(payloads['review_plan:scope-plan'])[obj.id]
    badge = tuple(map(float, group.get('data-mark-bounds').split()))
    label = next(n for n in group if n.tag.endswith('}text') and n.text == obj.id)
    for b in (badge, _actual_ink(label)):
        assert 0 <= b[0] < b[2] <= 22*64
        assert 0 <= b[1] < b[3] <= 8*64
    root = ET.fromstring(payloads['review_plan:scope-plan'])
    for n in root.iter():
        if n.tag.endswith('}text') and n.get('data-source-ids'):
            b = _actual_ink(n)
            assert 0 <= b[0] < b[2] <= 22*64
            assert 0 <= b[1] < b[3] <= 8*64
    assert float(group.get('data-anchor-model-x')) == x
    assert float(group.get('data-anchor-model-y')) == y
    assert model.to_json() == before
