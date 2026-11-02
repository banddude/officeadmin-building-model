"""Synthetic three-document workflow; written under execution hold, not yet run."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from oabm.model import ContractError,SiteFeature,SiteRequirement
from oabm.drawings.utility_delivery import create_utility_delivery,_filename
from test_site_model_integration import site_model
from test_site_contract_types import geometry

FIXTURE=Path(__file__).parents[1]/'fixtures/company_sheets/v1'


def utility_inputs(tmp_path,*,field_edit=False):
    source=tmp_path/'source.bin';source.write_bytes(b'Public synthetic declared source; no customer data')
    digest=hashlib.sha256(source.read_bytes()).hexdigest();model=site_model(digest)
    gas=SiteFeature(id='site:gas',kind='gas_location',status='existing',name='Synthetic source marker',
        geometry=geometry('point',[(6,3)]),extent_basis='source_marker',evidence=model.site_features[0].evidence)
    requirement=SiteRequirement(id='requirement:separation',kind='minimum_horizontal_separation',source_id=gas.id,
        target_id='service:main',reference_basis='point_to_point',minimum_m=1.)
    model=replace(model,site_features=(*model.site_features,gas),site_requirements=(requirement,))
    (tmp_path/'model.json').write_text(model.to_json())
    (tmp_path/'profile.json').write_bytes((FIXTURE/'profile.json').read_bytes())
    job=json.loads((FIXTURE/'job.json').read_text());job['formats']=['svg','pdf']
    job['sheets']=[{'id':'U1','title':'SYNTHETIC SITE','fields':{'scale':'1:100'}},
                   {'id':'U2','title':'SYNTHETIC SERVICE','fields':{'scale':'DIAGRAMMATIC'}},
                   {'id':'U3','title':'SUPPLIED SYNTHETIC DEMAND','fields':{'scale':'N/A'}}]
    (tmp_path/'job.json').write_text(json.dumps(job))
    components=[dict(id='load:'+group,name='Synthetic '+group,group=group,va=va,
        source_id='source:synthetic',basis='Explicit synthetic allowance',assumption=True)
        for group,va in [('general',20000),('hvac',2000),('floor_heat',500)]]
    profile=dict(kind='supplied_service_demand_inputs_v1',service_node_id='service:main',voltage_v=240,
        sources={'source:synthetic':digest},components=components,
        general_demand={'first_va':10000,'remainder_factor':.4,'basis':'Supplied synthetic policy','source_id':'source:synthetic'},
        sensitivity={'additional_va':4000,'basis':'Supplied synthetic scenario','source_id':'source:synthetic'})
    (tmp_path/'loads.json').write_text(json.dumps(profile))
    request=dict(version='model-utility-set-v1',model='model.json',profile='profile.json',job='job.json',
        sources={'source:synthetic':'source.bin'},load_inputs='loads.json',
        site={'sheet_id':'U1','drawing':{'scale':100.,'placement_mm':[15.,20.],'padding_mm':5.,'legend_width_mm':95.}},
        single_line={'sheet_id':'U2','service_only':True},loads={'sheet_id':'U3'},
        documents={'site':'Synthetic Site.pdf','single_line':'Synthetic Service.pdf','loads':'Synthetic Loads.pdf'})
    if field_edit:
        request['field_edit']={'node_id':'service:main','x_m':4.,'y_m':3.,
            'provenance':{'source_kind':'manual','source_id':'field:synthetic-event','derivation':'user','method':'explicit field edit'}}
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize('name',['../outside.pdf','bad/name.pdf','bad?.pdf','bad.xlsx','.hidden.pdf','bad\\name.pdf'])
def test_portal_filename_refusal(name):
    with pytest.raises(ContractError):_filename(name)


def test_actual_three_document_command_regenerates_from_field_edit_and_preserves_inputs(tmp_path):
    request=utility_inputs(tmp_path)
    original=(tmp_path/'model.json').read_bytes()
    first=create_utility_delivery(request,output_directory=tmp_path/'first')
    manifest=json.loads((first/'MANIFEST.json').read_text())
    assert set(manifest['documents'])=={'site','single_line','loads'}
    assert manifest['visual_review'].startswith('pending') and manifest['engineering_approval'] is False
    for row in manifest['documents'].values():
        path=first/row['filename']
        assert path.stat().st_size==row['bytes']<=10_000_000
        assert len(PdfReader(path).pages)==len(row['pages'])>=1
    assert all((first/page['path']).is_file() for pages in manifest['page_proofs'].values() for page in pages)
    before=json.loads((first/'derived/site-projection.json').read_text())
    assert before['projection']['measurement_requirements'][0]['distance_m']==3
    assert before['projection']['dimension_bindings'][0]['requirement_id']=='requirement:separation'
    changed=json.loads(request.read_text());changed['field_edit']={'node_id':'service:main','x_m':4.,'y_m':3.,
        'provenance':{'source_kind':'manual','source_id':'field:synthetic-event','derivation':'user'}}
    request.write_text(json.dumps(changed))
    second=create_utility_delivery(request,output_directory=tmp_path/'second')
    after=json.loads((second/'derived/site-projection.json').read_text())
    assert after['projection']['measurement_requirements'][0]['distance_m']==2
    assert before['projection']['model_sha256']!=after['projection']['model_sha256']
    service=json.loads((second/'derived/single-line.json').read_text())['topology']
    load=json.loads((second/'derived/load-report.json').read_text())
    assert service['model_sha256']==load['source_model_sha256']==after['projection']['model_sha256']
    edited=json.loads((second/'inputs/model.json').read_text())
    assert next(row for row in edited['service_nodes'] if row['id']=='service:main')['plan_position']=={'x':4.,'y':3.}
    assert edited['plan_service_laterals'][0]['geometry']['points'][0]=={'x':4.,'y':3.}
    assert (tmp_path/'model.json').read_bytes()==original
    assert service['coverage']['selection_mode']=='service_only_explicit'
    with pytest.raises(FileExistsError):create_utility_delivery(request,output_directory=tmp_path/'second')


def test_stale_source_and_filename_errors_refuse_before_reserving_output(tmp_path):
    request=utility_inputs(tmp_path)
    data=json.loads(request.read_text());data['documents']['site']='bad/name.pdf';request.write_text(json.dumps(data))
    with pytest.raises(ContractError):create_utility_delivery(request,output_directory=tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    data['documents']['site']='Synthetic Site.pdf';request.write_text(json.dumps(data))
    (tmp_path/'source.bin').write_bytes(b'changed source')
    with pytest.raises(ContractError,match='stale'):create_utility_delivery(request,output_directory=tmp_path/'stale')
    assert not (tmp_path/'stale').exists()
