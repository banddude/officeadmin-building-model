"""Relocatable production requests and receipt-verified offline rebuilds."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime,timedelta,timezone

import pytest
from PIL import Image
from oabm.drawings import production_set as ps
from oabm.drawings.company_sheets import resolve_company_sheet_set
from test_cover_maps import AddressClient

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'examples'))
from reference_production_set import prepare as prepare_reference
from assembly_production_sheets import prepare as prepare_assembly


def custom_paths(base):
    """Give each logical input an arbitrary colliding basename in a subdir."""
    request_path=base/'production.json';request=json.loads(request_path.read_bytes())
    paths=[]
    for key in ('profile','job','model','cover','details'):
        if request[key] is not None:paths.append((request,key,request[key],key))
    for key in ('site','schedules'):
        if request[key] is not None:
            paths.append((request[key],'request',request[key]['request'],key))
            if 'graphics' in request[key]:paths.append((request[key],'graphics',request[key]['graphics'],'graphics'))
    for owner,key,old,folder in paths:
        new=Path(folder)/'data.json';(base/new).parent.mkdir()
        shutil.move(base/old,base/new);owner[key]=new.as_posix()
    request_path.write_text(json.dumps(request,indent=2)+'\n')
    return request_path


def add_logos(request_path):
    request=json.loads(request_path.read_bytes());p=request_path.parent/request['profile'];j=request_path.parent/request['job']
    def logo(path,color):
        path.parent.mkdir(exist_ok=True);Image.new('RGB',(16,8),color).save(path,format='PNG')
    logo(p.parent/'logos/profile.png','red');logo(p.parent/'logos/wordmark.png','yellow')
    logo(j.parent/'logos/job.png','blue');logo(j.parent/'logos/sheet.png','green')
    profile=json.loads(p.read_bytes());profile['logos']={'icon':'logos/profile.png','wordmark':'logos/wordmark.png'};p.write_text(json.dumps(profile))
    job=json.loads(j.read_bytes());job['logos']={'icon':'logos/job.png'}
    job['sheets'][0]['logos']={'icon':None};job['sheets'][1]['logos']={'wordmark':'logos/sheet.png'}
    j.write_text(json.dumps(job))
    return p,j


def compare_sheets(a,b):
    shared=lambda root:root/'company/delivery'
    for path in (shared(a)/'sheets').glob('*.svg'):
        assert path.read_bytes()==(shared(b)/'sheets'/path.name).read_bytes()
    if (shared(a)/'sheet-set.pdf').exists():
        assert (shared(a)/'sheet-set.pdf').read_bytes()==(shared(b)/'sheet-set.pdf').read_bytes()
    assert json.loads((a/'PRODUCTION-MANIFEST.json').read_bytes())['sheet_order']==json.loads((b/'PRODUCTION-MANIFEST.json').read_bytes())['sheet_order']


def test_v1_custom_paths_logo_precedence_original_removal_and_verified_rebuild(tmp_path):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/v1',base)
    request=custom_paths(base);profile,job=add_logos(request)
    config=json.loads(request.read_bytes());config['model']=str((base/config['model']).absolute());request.write_text(json.dumps(config))
    before=resolve_company_sheet_set(profile,job);raw=request.read_bytes()
    first=ps.create_production_sheet_set(request,output_directory=tmp_path/'first')
    assert (first/'inputs/production-request.json').read_bytes()==raw
    portable=first/'inputs/portable';after=resolve_company_sheet_set(portable/'profile.json',portable/'job.json')
    assert before.sheets==after.sheets and before.formats==after.formats
    assert json.loads((portable/'job.json').read_bytes())['sheets'][0]['logos']['icon'] is None
    assert len(list((portable/'assets').glob('*.png')))==4  # Includes overridden profile logo.
    assert not dict(after.sheets[0].logos).get('icon') and dict(after.sheets[1].logos)['icon'] != dict(after.sheets[1].logos)['wordmark']
    moved=tmp_path/'relocated';shutil.move(first,moved);shutil.rmtree(base)
    result=ps.preflight_production_sheet_set(moved/'inputs/portable/production.json')
    assert result['status']=='bounded_preflight_passed'
    rerun=ps.create_production_sheet_set(moved/'inputs/portable/production.json',output_directory=tmp_path/'rerun')
    rebuild=ps.rebuild_production_sheet_set(moved,output_directory=tmp_path/'rebuild')
    compare_sheets(moved,rerun);compare_sheets(moved,rebuild)
    assert (rebuild/'inputs/model.json').read_bytes()==(moved/'inputs/model.json').read_bytes()


def test_v2_composition_graphics_relocates_without_original_inputs(tmp_path):
    base=tmp_path/'source';prepare_reference(base,variant='reference',pdf=False)
    request=custom_paths(base);original=json.loads(request.read_bytes())
    first=ps.create_production_sheet_set(request,output_directory=tmp_path/'a')
    shutil.rmtree(base);moved=tmp_path/'moved';shutil.move(first,moved)
    portable=json.loads((moved/'inputs/portable/production.json').read_bytes())
    assert portable['site']['graphics']=='site-graphics.json' and portable['single_line']==original['single_line']
    second=ps.rebuild_production_sheet_set(moved,output_directory=tmp_path/'b')
    compare_sheets(moved,second)
    assert json.loads((second/'PRODUCTION-MANIFEST.json').read_bytes())['electrical_composition']==json.loads((moved/'PRODUCTION-MANIFEST.json').read_bytes())['electrical_composition']


def test_v2_optional_model_view_only_relocates(tmp_path):
    request=prepare_assembly(tmp_path/'source');first=ps.create_production_sheet_set(request,output_directory=tmp_path/'a')
    shutil.rmtree(request.parent);moved=tmp_path/'moved';shutil.move(first,moved)
    portable=json.loads((moved/'inputs/portable/production.json').read_bytes())
    assert portable['cover'] is None and portable['site'] is None and portable['details'] is None and portable['schedules'] is None
    second=ps.rebuild_production_sheet_set(moved,output_directory=tmp_path/'b')
    compare_sheets(moved,second)
    assert json.loads((second/'PRODUCTION-MANIFEST.json').read_bytes())['content_owners']['model_views']==['A101','A301']


def prepare_map(tmp_path,client):
    path=prepare_reference(tmp_path/'source',variant='reference',pdf=False)
    request=json.loads(path.read_bytes());request['cover_map']={'mode':'auto','cache_directory':'private-cache','candidate_index':None};path.write_text(json.dumps(request))
    cover_path=path.parent/'cover.json';cover=json.loads(cover_path.read_bytes());cover.pop('location_map',None);cover_path.write_text(json.dumps(cover))
    return path,ps.create_production_sheet_set(path,output_directory=tmp_path/'a',map_client=client)


def test_cached_map_rebuild_is_relocated_offline_and_expires(tmp_path,monkeypatch):
    client=AddressClient('SYNTHETIC SITE');path,first=prepare_map(tmp_path,client)
    manifest=json.loads((first/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['portable_map_status']['status']=='offline_fresh_cache_retained'
    portable=json.loads((first/'inputs/portable/production.json').read_bytes())
    assert portable['cover_map']['mode']=='cached'
    shutil.rmtree(path.parent);moved=tmp_path/'moved';shutil.move(first,moved)
    import oabm.drawings.cover_maps as maps
    def forbidden(*args,**kwargs):raise AssertionError('network map client constructed')
    monkeypatch.setattr(maps,'OfficeAdminMapClient',forbidden)
    second=ps.rebuild_production_sheet_set(moved,output_directory=tmp_path/'b');compare_sheets(moved,second)
    class Future(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(timezone.utc)+timedelta(days=2)
    monkeypatch.setattr(maps,'datetime',Future)
    with pytest.raises(ValueError,match='no fresh verified Apple map'):
        ps.rebuild_production_sheet_set(moved,output_directory=tmp_path/'expired')
    assert not (tmp_path/'expired').exists()


def test_no_store_map_records_offline_block_without_network(tmp_path,monkeypatch):
    class NoStore(AddressClient):
        def request(self,path,query,*,binary=False):
            value,receipt=super().request(path,query,binary=binary)
            if binary:receipt['headers']['Cache-Control']='no-store'
            return value,receipt
    path,first=prepare_map(tmp_path,NoStore('SYNTHETIC SITE'))
    manifest=json.loads((first/'PRODUCTION-MANIFEST.json').read_bytes())
    assert manifest['portable_map_status']['status']=='offline_cache_unavailable'
    assert 'no reusable cache lifetime' in manifest['portable_map_status']['reason']
    shutil.rmtree(path.parent)
    import oabm.drawings.cover_maps as maps
    def forbidden(*args,**kwargs):raise AssertionError('network forbidden')
    monkeypatch.setattr(maps,'OfficeAdminMapClient',forbidden)
    with pytest.raises(ValueError,match='no fresh verified Apple map'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'blocked')
    assert not (tmp_path/'blocked').exists()


def test_rebuild_rejects_receipt_tampering_and_unreceipted_cache(tmp_path):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/compact',base)
    first=ps.create_production_sheet_set(base/'production.json',output_directory=tmp_path/'a')
    target=first/'inputs/portable/model.json';data=target.read_bytes();target.write_bytes(data+b' ')
    with pytest.raises(ValueError,match='production receipt mismatch'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'tampered')
    target.write_bytes(data)
    (first/'inputs/portable/unreceipted').write_text('unexpected')
    with pytest.raises(ValueError,match='unreceipted portable input'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'unreceipted')
    assert not (tmp_path/'tampered').exists() and not (tmp_path/'unreceipted').exists()


def test_rebuild_refuses_edited_outbound_paths_even_if_receipt_rewritten(tmp_path):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/compact',base)
    first=ps.create_production_sheet_set(base/'production.json',output_directory=tmp_path/'a')
    path=first/'inputs/portable/production.json';request=json.loads(path.read_bytes())
    request['model']='/tmp/outside-model.json';raw=(json.dumps(request)+'\n').encode();path.write_bytes(raw)
    manifest_path=first/'PRODUCTION-MANIFEST.json';manifest=json.loads(manifest_path.read_bytes())
    receipt=next(row for row in manifest['files'] if row['path']=='inputs/portable/production.json')
    receipt.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest());manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='portable model.*unexpected'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()


def test_no_store_ignores_older_expired_receipt_without_reusing_it(tmp_path):
    from oabm.drawings.cover_maps import acquire_cover_map
    path=prepare_reference(tmp_path/'source',variant='reference',pdf=False)
    request=json.loads(path.read_bytes());request['cover_map']={'mode':'auto','cache_directory':'private-cache','candidate_index':None};path.write_text(json.dumps(request))
    cover_path=path.parent/'cover.json';cover=json.loads(cover_path.read_bytes());cover.pop('location_map',None);cover_path.write_text(json.dumps(cover))
    old=datetime.now(timezone.utc)-timedelta(hours=3)
    acquire_cover_map('SYNTHETIC SITE',cache_directory=path.parent/'private-cache',client=AddressClient('SYNTHETIC SITE'),now=old)
    class NoStore(AddressClient):
        def request(self,path,query,*,binary=False):
            value,receipt=super().request(path,query,binary=binary)
            if binary:receipt['headers']['Cache-Control']='no-store'
            return value,receipt
    result=ps.create_production_sheet_set(path,output_directory=tmp_path/'a',map_client=NoStore('SYNTHETIC SITE'))
    status=json.loads((result/'PRODUCTION-MANIFEST.json').read_bytes())['portable_map_status']
    assert status['status']=='offline_cache_unavailable' and 'no reusable cache lifetime' in status['reason']
    assert not (result/'inputs/portable/map-cache').exists()
    with pytest.raises(ValueError,match='no fresh verified Apple map'):
        ps.rebuild_production_sheet_set(result,output_directory=tmp_path/'blocked')


def test_editable_portable_job_reruns_but_verified_rebuild_detects_edits(tmp_path):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/compact',base)
    first=ps.create_production_sheet_set(base/'production.json',output_directory=tmp_path/'a')
    portable=first/'inputs/portable';job_path=portable/'job.json';job=json.loads(job_path.read_bytes())
    job['sheets'][0].setdefault('fields',{})['drawn_by']='PUBLIC SYNTHETIC EDIT';job_path.write_text(json.dumps(job))
    with pytest.raises(ValueError,match='production receipt mismatch'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'verified')
    ps.preflight_production_sheet_set(portable/'production.json')
    revised=ps.create_production_sheet_set(portable/'production.json',output_directory=tmp_path/'edited')
    assert any('PUBLIC SYNTHETIC EDIT' in p.read_text() for p in (revised/'company/delivery/sheets').glob('*.svg'))
    with pytest.raises(ValueError,match='outside the source delivery'):
        ps.rebuild_production_sheet_set(first,output_directory=first/'unsafe-new-attempt')


def test_cli_rebuilds_relocated_input_without_originals(tmp_path,capsys):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/compact',base)
    original=ps.create_production_sheet_set(base/'production.json',output_directory=tmp_path/'a')
    shutil.rmtree(base);moved=tmp_path/'moved';shutil.move(original,moved)
    assert ps.main(['--check',str(moved/'inputs/portable/production.json')])==0
    assert json.loads(capsys.readouterr().out)['status']=='bounded_preflight_passed'
    assert ps.main(['--rebuild',str(moved),str(tmp_path/'b')])==0
    assert capsys.readouterr().out.strip()==str(tmp_path/'b/delivery')
    compare_sheets(moved,tmp_path/'b/delivery')


def test_rebuild_refuses_live_map_even_if_request_receipt_is_rewritten(tmp_path):
    base=tmp_path/'source';shutil.copytree(ROOT/'fixtures/production_set/compact',base)
    first=ps.create_production_sheet_set(base/'production.json',output_directory=tmp_path/'a')
    path=first/'inputs/portable/production.json';request=json.loads(path.read_bytes())
    request['version']=ps.MODEL_VIEW_VERSION;request['model_views']=[]
    request['cover_map']={'mode':'auto','cache_directory':'outside','candidate_index':None}
    raw=(json.dumps(request)+'\n').encode();path.write_bytes(raw)
    manifest_path=first/'PRODUCTION-MANIFEST.json';manifest=json.loads(manifest_path.read_bytes())
    receipt=next(row for row in manifest['files'] if row['path']=='inputs/portable/production.json')
    receipt.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest());manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='live acquisition is refused'):
        ps.rebuild_production_sheet_set(first,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
