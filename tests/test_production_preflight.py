"""The operator check uses production validators without creating an attempt."""
import json
import shutil
from pathlib import Path

import pytest
from oabm.drawings import production_set as ps

FIXTURE=Path(__file__).parents[1]/'fixtures/production_set/v1'


def copy_inputs(tmp_path):
    dest=tmp_path/'inputs'
    shutil.copytree(FIXTURE,dest)
    return dest


def snapshot(root):
    return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_check_valid_is_read_only_and_cli_needs_no_destination(tmp_path, capsys):
    inputs=copy_inputs(tmp_path);before=snapshot(tmp_path)
    assert ps.main(['--check',str(inputs/'production.json')])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='bounded_preflight_passed'
    assert result['model_id']=='model:synthetic-production-set'
    assert snapshot(tmp_path)==before


def test_check_missing_path_names_the_producer_and_does_not_create_attempt(tmp_path):
    inputs=copy_inputs(tmp_path);(inputs/'model.json').unlink();before=snapshot(tmp_path)
    with pytest.raises(ValueError, match='model: cannot read input .*model.json'):
        ps.preflight_production_sheet_set(inputs/'production.json')
    assert snapshot(tmp_path)==before


def test_check_exercises_detail_consumer_against_model(tmp_path):
    inputs=copy_inputs(tmp_path)
    path=inputs/'details.json';details=json.loads(path.read_bytes())
    details['details'][0]['source_ids']=['equipment:does-not-exist']
    path.write_text(json.dumps(details));before=snapshot(tmp_path)
    with pytest.raises(ValueError,match='equipment:does-not-exist: missing or unsupported detail source'):
        ps.preflight_production_sheet_set(inputs/'production.json')
    assert snapshot(tmp_path)==before


def test_check_does_not_fetch_or_cache_cover_map(tmp_path, monkeypatch):
    inputs=copy_inputs(tmp_path);path=inputs/'production.json'
    request=json.loads(path.read_bytes());request.update(version=ps.MODEL_VIEW_VERSION,model_views=[],cover_map={
        'mode':'auto','cache_directory':'map-cache','candidate_index':None})
    path.write_text(json.dumps(request))
    cover_path=inputs/'cover.json';cover=json.loads(cover_path.read_bytes());cover.pop('location_map',None)
    cover_path.write_text(json.dumps(cover));before=snapshot(tmp_path)
    import oabm.drawings.cover_maps as maps
    def forbidden(*args,**kwargs):raise AssertionError('preflight fetched a map')
    monkeypatch.setattr(maps,'acquire_cover_map',forbidden)
    assert ps.preflight_production_sheet_set(path)['status']=='bounded_preflight_passed'
    assert snapshot(tmp_path)==before
    assert not (inputs/'map-cache').exists()


def test_check_rejects_unknown_request_or_invalid_single_line(tmp_path):
    inputs=copy_inputs(tmp_path);path=inputs/'production.json'
    request=json.loads(path.read_bytes());request['single_line']={'sheet_id':None}
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError,match='single_line.sheet_id'):
        ps.preflight_production_sheet_set(path)
    request['extra']=True;path.write_text(json.dumps(request))
    with pytest.raises(ValueError,match='exact production-sheet-set'):
        ps.preflight_production_sheet_set(path)
