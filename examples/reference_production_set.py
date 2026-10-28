"""Complete generic sample inputs through the existing production coordinator.

All geometry/ratings are synthetic explicit design inputs, not manufacturer fit,
survey or engineering approval. No raster/Blender/browser process is launched.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from oabm.model import BuildingModel,upgrade_to_v1_3,validate_model
from oabm.drawings.single_line.demo_drafting import make_model
from oabm.drawings.production_set import create_production_sheet_set
from electrical_composition import prepare as prepare_electrical

ROOT=Path(__file__).parents[1]


def source_model(variant):
    if variant=='reference':return make_model(),('MS1','HP1','F03')
    spec=importlib.util.spec_from_file_location('reference_set_models',ROOT/'examples/single_line/make_models.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.make_model(variant=='commercial'),(('MAIN','SUB','F003') if variant=='residential' else ('MAIN','ATS','F002'))


def with_detail_geometry(model,targets):
    """Author known synthetic envelopes/supports into this demo's ONE model.

    These are declared example design facts. No source drawing is interpreted,
    no manufacturer/internal geometry is invented, and no customer input changes.
    """
    first,second,circuit_id=targets
    mapping={'EQ-A':first,'EQ-B':second}
    def renamed(value):
        if isinstance(value,str):return mapping.get(value,value)
        if isinstance(value,list):return [renamed(x) for x in value]
        if isinstance(value,dict):return {k:renamed(v) for k,v in value.items()}
        return value
    detail=renamed(json.loads((ROOT/'fixtures/detail_sheets/drafted/model.json').read_bytes()))
    data=upgrade_to_v1_3(model).to_dict()
    for key in ('levels','spaces','walls','slabs','ceilings','openings','obstacles','support_assemblies','support_components','support_interfaces','support_selection_outcomes','routes'):
        data[key]=detail.get(key,[])
    envelopes={e['id']:e for e in detail['electrical_equipment']}
    for equipment in data['electrical_equipment']:
        equipment['attributes']['synthetic_geometry_qualification']='Design intent only; not surveyed or manufacturer-fit evidence.'
        if equipment['id'] in envelopes:
            envelope=envelopes[equipment['id']]
            for key in ('pose','size','level_id'):equipment[key]=envelope[key]
            equipment['provenance']+=envelope['provenance']
            equipment['attributes'].setdefault('single_line_drafting',{}).setdefault('notes',[]).append('Exterior envelope is synthetic design intent; no manufacturer/internal fit verified.')
    circuit=next(c for c in data['circuits'] if c['id']==circuit_id)
    if len(circuit['load_port_ids'])!=1:raise ValueError('synthetic detail pair must be a single explicit load')
    old_source,old_load=circuit['source_port_id'],circuit['load_port_ids'][0]
    originals={p['id']:p for p in data['ports']}
    physical=[]
    for p,old in zip(detail['ports'],(old_source,old_load)):
        p={**p,'domain':'electrical','role':originals[old]['role']};physical.append(p)
    data['ports']=[p for p in data['ports'] if p['id'] not in {old_source,old_load}]+physical
    circuit['source_port_id']='P-A';circuit['load_port_ids']=['P-B'];circuit['route_ids']=['R1']
    data['model_id']='synthetic-reference-production-'+model.model_id
    data['name']='Synthetic production model with explicit external detail envelopes'
    data['attributes']['qualification']='Partial synthetic design model, not full-building, survey or engineering approval.'
    result=BuildingModel.from_dict(data);validate_model(result)
    return result,mapping


def prepare(root,*,variant='reference',pdf=True,profile=None,adaptive_layout=False):
    root=Path(root)
    request=prepare_electrical(root,pdf=pdf)
    model,targets=source_model(variant);model,mapping=with_detail_geometry(model,targets)
    (root/'model.json').write_text(model.to_json())
    data=json.loads(request.read_bytes())
    if profile is not None:data['profile']=str(Path(profile).absolute())
    data.update(cover='cover.json',site={'sheet_id':'E1.0','request':'site.json','graphics':'site-graphics.json'},details='details.json')
    job=json.loads((root/'job.json').read_bytes())
    job['set_id']='synthetic-reference-'+variant
    job['fields']={'project':'SYNTHETIC '+variant.upper()+' PRODUCTION SET','address':'SYNTHETIC SITE',
        'project_number':'DEMO-'+variant.upper(),'client':'EXAMPLE OWNER','drawn_by':'EXAMPLE','checked_by':'NOT REVIEWED',
        'issue':'SYNTHETIC DRAFT - NOT FOR CONSTRUCTION','scale':'AS NOTED'}
    job['sheets']=[{'id':'G0.0','title':'COVER SHEET','fields':{'scale':'NOT TO SCALE'}},
        {'id':'E1.0','title':'SITE AND CONNECTION PLAN','fields':{'scale':'AS NOTED'}},
        {'id':'E3.1','title':'POWER ONE-LINE AND SCHEDULES' if adaptive_layout else 'POWER ONE-LINE AND FEEDER SCHEDULE','fields':{'scale':'NOT TO SCALE'}},
        {'id':'ED1','title':'SUPPLIED INSTALLATION DETAILS','fields':{'scale':'AS NOTED'}}]
    (root/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    if adaptive_layout:
        from oabm.drawings.company_sheets import resolve_company_sheet_set
        sheet=next(s for s in resolve_company_sheet_set(Path(data['profile']) if profile is not None else root/'profile.json',root/'job.json').sheets if s.id=='E3.1')
        l,t,r,b=sheet.drawing_bounds_mm;height=b-t-24
        right_column=min(170,(r-l)*.24)
        compact={'id':'shared-bottom','single_line_regions':{
            'diagram':[l,t,r-right_column-8,t+height*.5],
            'notes':[r-right_column,t,r,t+height*.5],
            'feeder_register':[l,t+height*.5+8,r,t+height*.69]},
            'panel_region_mm':[l,t+height*.69+8,r,b-24]}
        data['single_line']['composition'].update(version='electrical-sheet-regions-v3',
            panel_placement='auto',layout_candidates=[compact])
    cover={'version':'cover-sheet-v1','id':'G0.0','project':{'name':job['fields']['project'],'location':job['fields']['address'],'number':job['fields']['project_number']},
        'scope':['Present supplied canonical electrical topology, explicit external-envelope details and separately qualified synthetic site information.',
            'Site coordinates are a separate supplied design ledger; they are not surveyed canonical equipment locations.'],
        'sheets':[{'id':s['id'],'title':s['title']} for s in job['sheets']],
        'contacts':[{'role':'Design example','name':'EXAMPLE DESIGN TEAM'}],
        'notes':['SYNTHETIC DEMONSTRATION. No manufacturer/internal fit, attachment capacity, code, survey or engineering approval.',
            'Unspecified branch circuits, equipment internals and protective ratings remain unknown. No spares or demand are inferred.']}
    site=json.loads((ROOT/'fixtures/reference_production/site.json').read_bytes())
    site['features']=site['features']
    for f in site['features']:
        if f['id']=='main-panel':f['label']=targets[0]
        if f['id']=='annex-panel':f['label']=targets[1]
    graphics=json.loads((ROOT/'fixtures/reference_production/site-graphics.json').read_bytes())
    details=json.loads((ROOT/'fixtures/detail_sheets/drafted/details.json').read_bytes())
    def remap(value):
        if isinstance(value,str):return mapping.get(value,value)
        if isinstance(value,list):return [remap(x) for x in value]
        if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
        return value
    details=remap(details)
    for detail in details['details']:
        detail['connection_spec']='Supplied R1 route and P-A/P-B locations only; no internal terminal/fitting/wire geometry or manufacturer fit verified.'
    for name,value in [('cover',cover),('site',site),('site-graphics',graphics),('details',details),('production',data)]:
        (root/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    return request

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--variant',choices=('reference','residential','commercial'),default='reference');p.add_argument('--profile',type=Path);p.add_argument('--adaptive-layout',action='store_true')
    a=p.parse_args();root=Path(a.root);root.mkdir(parents=True,exist_ok=False)
    request=prepare(root/'inputs',variant=a.variant,profile=a.profile,adaptive_layout=a.adaptive_layout)
    print(create_production_sheet_set(request,output_directory=root/'attempt'))
