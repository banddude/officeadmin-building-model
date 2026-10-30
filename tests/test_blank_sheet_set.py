"""Synthetic job-to-blank-sheet production, without model or fake view inputs."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from oabm.drawings import DrawingSet, SheetSpec, compose_blank_sheet, compose_sheet
from oabm.drawings.blank_set import BlankSheetSetError, create_blank_sheet_set, main, resolve_blank_sheet_set
from oabm.drawings import blank_set, sheet_svg
from oabm.drawings.sheet_svg import render_blank_sheet_svg

FIXTURE=Path(__file__).parents[1]/"fixtures/blank_sheets/v1/config.json"


def config():
    return json.loads(FIXTURE.read_bytes())


def save(tmp_path,value=None):
    path=tmp_path/"config.json"
    path.write_text(json.dumps(config() if value is None else value))
    return path


def read(root):
    return json.loads((root/"MANIFEST.json").read_bytes())


def verify(root):
    manifest=read(root)
    assert {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()} == {"MANIFEST.json",*(r['path'] for r in manifest['files'])}
    for row in manifest['files']:
        data=(root/row['path']).read_bytes()
        assert len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256']
    return manifest


def svg_text(path):
    root=ET.fromstring(path.read_bytes())
    return root,[n.text for n in root.findall('.//{http://www.w3.org/2000/svg}text')]


def test_blank_is_first_class_without_drawing_model_or_placements(monkeypatch):
    monkeypatch.setattr(sheet_svg,'compose_sheet',lambda *a,**k: pytest.fail('blank must not fake a view'))
    spec=resolve_blank_sheet_set(config())[0]['spec']
    composed=compose_blank_sheet(spec).to_dict()
    assert composed['kind']=='oabm.blank-sheet'
    assert composed['model_id'] is None and composed['drawing_set_sha256'] is None
    assert composed['source_index']==[] and composed['views']==[]
    output=render_blank_sheet_svg(spec)
    assert 'Synthetic Workshop' in output.svg
    assert output.manifest()['model_id'] is None
    assert output.manifest()['views']==[]
    assert output.manifest()['unrendered_schedule_ids']==[]


def test_ordered_set_populates_actual_svg_with_shared_and_sheet_overrides(tmp_path):
    value=config()
    value['formats']=['svg']
    value['sheets']=value['sheets'][::-1]  # order is caller supplied, not sorted IDs
    path=save(tmp_path,value)
    original=path.read_bytes()
    root=create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    manifest=verify(root)
    assert manifest['sheet_order']==['E2.0','E1.0','E0.0']
    assert manifest['status']=='blank_no_design_content'
    assert manifest['model_id'] is None and manifest['engineering_approval'] is False
    assert (root/'config.json').read_bytes()==original
    assert manifest['pdf']['status']=='not_requested'
    svg,words=svg_text(root/'sheets/001-E2.0.svg')
    assert svg.attrib['width']=='297mm' and svg.attrib['height']=='210mm'
    assert 'E2.0 | Equipment details - blank' in words
    assert 'Revision: A' in words and 'Date: 2026-02-01' in words
    assert 'Project: Synthetic Workshop' in words
    _,words=svg_text(root/'sheets/002-E1.0.svg')
    assert 'Discipline: Power' in words and 'Discipline: Electrical' not in words
    assert 'Revision: 0' in words and 'Date: 2026-01-15' in words
    assert manifest['sheets'][0]['field_sources']['Date']=='sheet.date'
    assert manifest['sheets'][1]['field_sources']['Discipline']=='sheet.fields'
    assert manifest['sheets'][2]['field_sources']['Discipline']=='job'
    assert path.read_bytes()==original
    assert not (root.parent/'.pending').exists()


def test_optional_dxf_is_editable_paperspace_and_has_no_model_content(tmp_path):
    ezdxf=pytest.importorskip('ezdxf')
    root=create_blank_sheet_set(save(tmp_path),output_directory=tmp_path/'attempt')
    manifest=verify(root)
    assert manifest['formats']==['svg','dxf']
    for stem in ('001-E0.0','002-E1.0','003-E2.0'):
        doc=ezdxf.read(io.StringIO((root/f'sheets/{stem}.dxf').read_text()))
        assert not list(doc.modelspace())
        text=[e.dxf.text for e in doc.layouts.get('Layout1').query('TEXT')]
        assert 'Project: Synthetic Workshop' in text
        assert not doc.audit().errors
        companion=json.loads((root/f'sheets/{stem}.dxf.map.json').read_bytes())
        assert companion['composition']['kind']=='oabm.blank-sheet'
        assert companion['composition']['views']==[]
        assert all(not r.get('source_ids') for r in companion['entities'])


def test_rebuild_is_byte_identical_including_manifests(tmp_path):
    value=config();value['formats']=['svg']
    path=save(tmp_path,value)
    a=create_blank_sheet_set(path,output_directory=tmp_path/'a')
    b=create_blank_sheet_set(path,output_directory=tmp_path/'b')
    tree=lambda root:{p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert tree(a)==tree(b)


def test_explicit_empty_sheet_field_clears_shared_value(tmp_path):
    value=config();value['formats']=['svg'];value['sheets'][0]['fields']={'Prepared by':''}
    root=create_blank_sheet_set(save(tmp_path,value),output_directory=tmp_path/'attempt')
    _,words=svg_text(root/'sheets/001-E0.0.svg')
    assert 'Prepared by: ' in words
    assert read(root)['sheets'][0]['field_sources']['Prepared by']=='sheet.fields'


@pytest.mark.parametrize('change,message',[
    (lambda c:c.pop('date'),'exact blank-sheet'),
    (lambda c:c.update(date='2026-02-30'),'invalid calendar'),
    (lambda c:c.update(formats=['svg','unsupported']),'include svg'),
    (lambda c:c.update(formats=['dxf']),'include svg'),
    (lambda c:c.update(sheets=[]),'ordered list'),
    (lambda c:c['job'].update(Date='2026-01-01'),'reserved'),
    (lambda c:c['sheets'].append(c['sheets'][0].copy()),'duplicate sheet id'),
    (lambda c:c['sheets'][0].update(id='../bad'),'letters/digits'),
    (lambda c:c['sheets'][0].update(fields={'project':'Ambiguous'}),'exact shared field spelling'),
    (lambda c:c['sheets'][0].update(paper={'width_mm':float('nan')}),'finite'),
    (lambda c:c['sheets'][0].update(paper={'titleblock_height_mm':0}),'must be positive'),
    (lambda c:c['sheets'][0].update(paper={'width_mm':5}),'positive drawing area'),
    (lambda c:c['job'].update(Project=42),'single-line string'),
])
def test_invalid_config_is_actionable_and_does_not_reserve_output(tmp_path,change,message):
    value=config();change(value)
    with pytest.raises(ValueError,match=message):
        create_blank_sheet_set(save(tmp_path,value),output_directory=tmp_path/'attempt')
    assert not (tmp_path/'attempt').exists()


def test_capacity_failure_names_sheet_format_and_fix_without_publishing(tmp_path):
    value=config();value['formats']=['svg'];value['sheets'][1]['paper']={'titleblock_height_mm':1}
    with pytest.raises(BlankSheetSetError,match='sheet E1.0 svg titleblock.*titleblock_height_mm') as exc:
        create_blank_sheet_set(save(tmp_path,value),output_directory=tmp_path/'attempt')
    assert exc.value.stage=='sheet E1.0 svg titleblock'
    assert (tmp_path/'attempt/.pending/sheets/001-E0.0.svg').exists()
    assert not (tmp_path/'attempt/delivery').exists()
    assert not (tmp_path/'attempt/.pending/MANIFEST.json').exists()


def test_overwide_text_refused_without_truncation(tmp_path):
    value=config();value['formats']=['svg'];value['job']['Project']='Synthetic '*100
    with pytest.raises(BlankSheetSetError,match='sheet E0.0 svg titleblock.*row capacity'):
        create_blank_sheet_set(save(tmp_path,value),output_directory=tmp_path/'attempt')
    assert not (tmp_path/'attempt/delivery').exists()
    assert json.loads((tmp_path/'attempt/.pending/config.json').read_bytes())['job']['Project']==value['job']['Project']


def test_partial_export_failure_and_existing_attempt_never_publish_or_overwrite(tmp_path,monkeypatch):
    value=config();value['formats']=['svg'];path=save(tmp_path,value)
    original=blank_set.render_blank_sheet_svg
    def fail(spec):
        if spec.id=='E1.0':raise ValueError('synthetic late failure')
        return original(spec)
    monkeypatch.setattr(blank_set,'render_blank_sheet_svg',fail)
    with pytest.raises(BlankSheetSetError):
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    snapshot=(tmp_path/'attempt/failure.json').read_bytes()
    monkeypatch.setattr(blank_set,'render_blank_sheet_svg',original)
    with pytest.raises(FileExistsError):
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert (tmp_path/'attempt/failure.json').read_bytes()==snapshot
    assert not (tmp_path/'attempt/delivery').exists()
    root=create_blank_sheet_set(path,output_directory=tmp_path/'fresh')
    before=(root/'MANIFEST.json').read_bytes()
    with pytest.raises(FileExistsError):create_blank_sheet_set(path,output_directory=root.parent)
    assert (root/'MANIFEST.json').read_bytes()==before
    verify(root)


def test_config_is_snapshotted_once_even_if_source_changes(tmp_path,monkeypatch):
    value=config();value['formats']=['svg'];path=save(tmp_path,value);raw=path.read_bytes()
    original=blank_set.render_blank_sheet_svg
    def mutate(spec):
        path.write_text('{}')
        return original(spec)
    monkeypatch.setattr(blank_set,'render_blank_sheet_svg',mutate)
    root=create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert (root/'config.json').read_bytes()==raw
    assert read(root)['sheet_order']==['E0.0','E1.0','E2.0']
    verify(root)


def test_existing_composition_and_review_safeguards_are_not_relaxed():
    empty=DrawingSet(model_id='synthetic',views=(),schedules=(),source_index=())
    spec=SheetSpec('E0',420,297,titleblock_height_mm=40,title='Blank',revision='A')
    with pytest.raises(ValueError,match='at least one view placement'):
        compose_sheet(empty,spec,())
    from oabm.drawings import ViewPlacement
    from oabm.pipeline.review_sheet_delivery import ReviewSheet
    with pytest.raises(ValueError,match='incomplete-review title'):
        ReviewSheet(paper=spec,placements=(ViewPlacement('p','v',10,10),),formats=('svg',))


def test_cli_and_duplicate_json_key(tmp_path,capsys):
    value=config();value['formats']=['svg'];path=save(tmp_path,value)
    assert main([str(path),str(tmp_path/'attempt')])==0
    assert capsys.readouterr().out.strip()==str(tmp_path/'attempt/delivery')
    path.write_text('{"version":"blank-sheet-set-v1","version":"blank-sheet-set-v1"}')
    with pytest.raises(SystemExit) as exc:main([str(path),str(tmp_path/'invalid')])
    assert exc.value.code==1
    assert not (tmp_path/'invalid').exists()


def test_failure_report_error_does_not_mask_original_render_problem(tmp_path,monkeypatch):
    value=config();value['formats']=['svg'];path=save(tmp_path,value)
    original_error=ValueError('synthetic render failure')
    def fail(spec):raise original_error
    monkeypatch.setattr(blank_set,'render_blank_sheet_svg',fail)
    original_write=Path.write_bytes
    def disk_full(target,data):
        if target.name=='failure.json':raise OSError('synthetic full disk')
        return original_write(target,data)
    monkeypatch.setattr(Path,'write_bytes',disk_full)
    with pytest.raises(BlankSheetSetError,match='synthetic render failure') as exc:
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert exc.value.__cause__.__cause__ is original_error
    assert not (tmp_path/'attempt/delivery').exists()
