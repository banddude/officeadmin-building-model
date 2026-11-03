"""Readable register preserves the canonical diff, binding and presentation boundaries."""
import csv
from dataclasses import replace
import io
import json
import math

import pytest

from oabm.model import ContractError
from oabm.reports.revision_register import (
    generate_revision_register, validate_revision_register, write_revision_register, main,
)
from test_physical_canonical_revisions import mixed_snapshots
from test_physical_route_revisions import detour
from physical_route_fixture import make_physical_model


def rows(report, name):
    return list(csv.DictReader(io.StringIO(report.files()[name])))


def _assert_quantity_formats(report):
    """Read actual HTML cells, not the formatter that produced them."""
    from html.parser import HTMLParser
    class Cells(HTMLParser):
        def __init__(self):super().__init__();self.rows=[];self.row=None;self.cell=None
        def handle_starttag(self,tag,attrs):
            if tag=='tr':self.row=[]
            if tag=='td':self.cell=[]
        def handle_data(self,value):
            if self.cell is not None:self.cell.append(value)
        def handle_endtag(self,tag):
            if tag=='td':self.row.append(''.join(self.cell));self.cell=None
            if tag=='tr' and self.row:self.rows.append(self.row);self.row=None
    parser=Cells();parser.feed(report.files()['index.html'])
    csv_rows=rows(report,'quantity-deltas.csv')
    deltas=report.to_dict()['diff']['quantity_deltas']
    assert len(csv_rows)==len(deltas)
    for delta in deltas:
        csv_row,=[row for row in csv_rows if row['delta_id']==delta['delta_id']]
        html_row,=[row for row in parser.rows if len(row)==9 and row[0]==delta['delta_id']]
        assert html_row==[csv_row[key] for key in ('delta_id','category','item_type','unit','variant_json','status','before_json','after_json','delta_json')]
        for key in ('before','after','delta'):
            assert json.loads(csv_row[key+'_json'])==delta[key]
    return deltas


@pytest.mark.parametrize('increment',[0.,1e-12,-1e-12])
def test_fractional_provenance_split_register_preserves_original_totals_across_formats(increment):
    from oabm.model import BuildingModel,Level,Wall,Polyline3D,Point3,Provenance
    lengths=(.1,.2,.3);level=Level(id='level:fractions',elevation_m=0,height_m=3)
    observed=(Provenance(source_kind='synthetic',source_id='fixture:fractions',derivation='observed'),)
    walls=tuple(Wall(id=f'wall:fraction-{i}',level_id=level.id,
        centerline=Polyline3D(points=(Point3(x=0,y=i,z=0),Point3(x=length,y=i,z=0))),
        thickness_m=.1,height_m=1,provenance=observed) for i,length in enumerate(lengths))
    before=BuildingModel(model_id='model:fractional-register',levels=(level,),walls=walls)
    field=Provenance(source_kind='as-built-field-edit',source_id='field:fraction-confirmation',derivation='user',scope_paths=('centerline',))
    changed=tuple(replace(wall,provenance=(*wall.provenance,field)) if i<2 else wall for i,wall in enumerate(walls))
    changed=(replace(changed[0],centerline=replace(changed[0].centerline,
        points=(changed[0].centerline.points[0],Point3(x=.1+increment,y=0,z=0)))),*changed[1:])
    after=replace(before,walls=changed)
    frozen=(before.to_json(),after.to_json());report=generate_revision_register(before,after)
    deltas=_assert_quantity_formats(report)
    if increment==0:
        assert deltas==[] and not rows(report,'quantity-deltas.csv')
        assert rows(report,'changes.csv')  # Provenance changes remain visible.
    else:
        line,=[row for row in deltas if row['category']=='wall_length']
        assert line['before']==math.fsum(lengths)
        assert line['after']==math.fsum((.1+increment,.2,.3))
        assert line['delta']==line['after']-line['before'] and line['delta']!=0
        assert any(row['path'].startswith('centerline') for row in rows(report,'changes.csv'))
    reordered=replace(after,walls=tuple(reversed(after.walls)))
    assert generate_revision_register(before,reordered).to_dict()['diff']==report.to_dict()['diff']
    assert validate_revision_register(report.to_json()).files()==report.files()
    report.validate_against_models(before,after)
    assert frozen==(before.to_json(),after.to_json())


def test_actual_room_register_is_complete_deterministic_and_not_a_second_takeoff(tmp_path):
    before, after = mixed_snapshots()
    frozen = [model.to_json() for model in (before,after)]
    report = generate_revision_register(before,after)
    data = report.to_dict()
    _assert_quantity_formats(report)
    assert data['diff']['changed_count'] == 53
    assert len(data['diff']['changes']) == 53
    changes = rows(report,'changes.csv')
    assert len({row['change_id'] for row in changes}) == 53
    assert all(row['before_source_sha256'] == data['sources']['before']['sha256'] for row in changes)
    quantities = rows(report,'quantity-deltas.csv')
    assert len(quantities) == len(data['diff']['quantity_deltas'])
    assert len({row['delta_id'] for row in quantities}) == len(quantities)
    physical = next(row for row in data['physical_measurements'] if row['route_id'] == 'route:physical')
    exact = physical['delta']['length']
    assert exact['straight_ticks'] == 100000000
    assert exact['pi_coefficient_ticks'] == {'numerator':100000000,'denominator':1}
    assert exact['total_m_approx'] == pytest.approx(.1+.1*math.pi)
    assert 'Route length changes at a glance' in report.files()['index.html']
    assert 'same delta IDs, not additional quantities' in report.files()['index.html']
    assert any(json.loads(row['delta_json']) == .5 for row in quantities if row['category'] == 'route_length')
    assert any(row['before_json'] == 'null' and row['status'] == 'added' for row in quantities)
    assert any(row['after_json'] == 'null' and row['status'] == 'removed' for row in quantities)
    assert [model.to_json() for model in (before,after)] == frozen
    assert validate_revision_register(report.to_json()).files() == report.files()
    report.validate_against_models(before,after)
    assert generate_revision_register(before,after).files() == report.files()
    out = write_revision_register(report,tmp_path/'register')
    assert {path.name:path.read_text() for path in out.iterdir()} == report.files()
    assert '53 changed objects' in (out/'index.html').read_text()


def test_split_device_counts_and_real_removal_keep_variant_identity_in_all_formats():
    from oabm.model import Provenance
    from test_revision_diff import _routed_garage
    model=_routed_garage();original=model.electrical_devices[0]
    peer=replace(original,id='device:register-peer')
    variant=replace(original,id='device:register-other-size',size=replace(original.size,x=original.size.x+.1))
    before=replace(model,electrical_devices=(original,peer,variant))
    field=Provenance(source_kind='as-built-field-edit',source_id='field:register-count',derivation='user',scope_paths=('pose',))
    confirmed=replace(original,provenance=(*original.provenance,field))
    split=replace(before,electrical_devices=(confirmed,peer,variant))
    assert _assert_quantity_formats(generate_revision_register(before,split))==[]
    removed=replace(split,electrical_devices=(confirmed,variant))
    deltas=_assert_quantity_formats(generate_revision_register(before,removed))
    line,=[row for row in deltas if row['category']=='device']
    assert (line['before'],line['after'],line['delta'],line['unit'])==(2,1,-1,'ea')
    assert dict(line['variant'])['size_x_m']==original.size.x


def test_equal_length_moved_path_remains_visible_without_quantity_change():
    report = generate_revision_register(detour(1),detour(-1))
    assert report.to_dict()['diff']['changes']
    assert not rows(report,'quantity-deltas.csv')
    assert report.to_dict()['physical_measurements'][0]['path_changed'] is True
    assert 'No changes in this section.' in report.files()['index.html']


@pytest.mark.parametrize('owner', ['route','joint','segment'])
def test_provenance_order_only_has_consistent_canonical_status_and_explicit_snapshot_evidence(owner):
    model = make_physical_model()
    route = model.routes[0]
    entity = route if owner == 'route' else route.joints[0] if owner == 'joint' else route.segments[0]
    records = (entity.provenance[0],replace(entity.provenance[0],method='second source'))
    def snapshot(provenance):
        revised = replace(entity,provenance=provenance)
        physical = revised if owner == 'route' else (
            replace(route,joints=(revised,*route.joints[1:])) if owner == 'joint' else
            replace(route,segments=(revised,*route.segments[1:])))
        return replace(model,routes=(physical,))
    before,after = snapshot(records),snapshot(tuple(reversed(records)))
    report = generate_revision_register(before,after)
    data = report.to_dict()
    assert data['diff']['changes'] == [] and data['diff']['quantity_deltas'] == []
    assert not rows(report,'changes.csv') and not rows(report,'quantity-deltas.csv')
    measured, = data['physical_measurements']
    assert measured['status'] == 'unchanged' and measured['change_classes'] == []
    assert measured['snapshot_status'] == 'changed'
    assert measured['snapshot_field_change_categories'] == ['provenance']
    assert measured['path_changed'] is False
    assert measured['delta']['length']['total_m_approx'] == 0
    assert data['sources']['before']['sha256'] != data['sources']['after']['sha256']
    html = report.files()['index.html']
    assert '0 changed objects' in html
    assert 'Canonical status' in html and 'Ordered snapshot status' in html
    assert 'provenance order alone' in html
    assert validate_revision_register(report.to_json()).to_json() == report.to_json()


def test_unchanged_and_negative_deltas_and_external_source_binding():
    before,after = detour(1),detour(-1)
    unchanged = generate_revision_register(before,before)
    assert not rows(unchanged,'changes.csv') and not rows(unchanged,'quantity-deltas.csv')
    with pytest.raises(ContractError,match='supplied snapshots'):
        unchanged.validate_against_models(before,after)
    from oabm.reports.revision_register import _csv
    assert list(csv.reader(io.StringIO(_csv(['number'],[[-.5]]))))[1] == ['-0.5']


@pytest.mark.parametrize('payload', ['{}','[]','{"sources":{},"sources":{}}','{"bad":NaN}'])
def test_malformed_register_refuses(payload):
    with pytest.raises(ContractError):
        validate_revision_register(payload)


@pytest.mark.parametrize('target', ['hash','diff','measurement','limitation','extra'])
def test_rehashed_or_changed_report_is_not_trusted(target):
    before,after = detour(1),detour(-1)
    data = generate_revision_register(before,after).to_dict()
    if target == 'hash':
        data['sources']['before']['sha256'] = '0'*64
    elif target == 'diff':
        data['diff']['changed_count'] = 0
    elif target == 'measurement':
        data['physical_measurements'][0]['path_changed'] = False
    elif target == 'limitation':
        data['limitations'] = []
    else:
        data['extra'] = 'unbound'
    with pytest.raises(ContractError):
        validate_revision_register(json.dumps(data))


def test_html_is_escaped_and_csv_text_cannot_become_formulas():
    before = detour(1)
    hostile = '=HYPERLINK("https://example.invalid")<script>alert(1)</script>'
    before = replace(before,routes=(replace(before.routes[0],name=hostile),))
    route = before.routes[0]
    after = replace(before,routes=(replace(route,name='<img src=x onerror=alert(1)>'),))
    report = generate_revision_register(before,after)
    html = report.files()['index.html']
    assert '<script>' not in html and '<img ' not in html
    assert '&lt;script&gt;' in html and '&lt;img ' in html
    # Exercise all text cells, including leading whitespace/BOM/CR.
    from oabm.reports.revision_register import _csv
    for value in ('=1','+2','-3','@SUM(1)',' \ufeff=1','\ttext','\rtext'):
        result = list(csv.reader(io.StringIO(_csv(['text'],[[value]]))))[1][0]
        assert result.startswith("'")


def test_fresh_directory_refusal_preserves_existing_and_symlink(tmp_path):
    report = generate_revision_register(detour(1),detour(-1))
    existing = tmp_path/'existing'; existing.mkdir()
    (existing/'keep').write_text('untouched')
    link = tmp_path/'link'; link.symlink_to(existing,target_is_directory=True)
    for path in (existing,link):
        with pytest.raises(FileExistsError):
            write_revision_register(report,path)
    assert list(existing.iterdir()) == [existing/'keep']
    assert (existing/'keep').read_text() == 'untouched'


def test_output_bounds_refuse_before_any_directory_is_created(tmp_path,monkeypatch):
    import oabm.reports.revision_register as module
    report = generate_revision_register(detour(1),detour(-1))
    monkeypatch.setattr(module,'PRESENTATION_BYTES',100)
    with pytest.raises(ContractError,match='no rows truncated'):
        write_revision_register(report,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_cli_reads_full_snapshots_and_writes_offline_files(tmp_path):
    before,after = detour(1),detour(-1)
    for name,model in (('before',before),('after',after)):
        (tmp_path/f'{name}.json').write_text(model.to_json())
    assert main([str(tmp_path/'before.json'),str(tmp_path/'after.json'),str(tmp_path/'out')]) == 0
    assert sorted(path.name for path in (tmp_path/'out').iterdir()) == ['changes.csv','index.html','quantity-deltas.csv','register.json']
