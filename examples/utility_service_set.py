"""Independent synthetic utility source/model and one-command field-edit proof.

No customer plan, actual address, private equipment selection or accepted
project load data is used. Source SVG is explicit known-answer paper geometry.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from oabm.model import (BuildingModel,CoordinateSystem,Point2D,Provenance,SitePlanGeometry,
    SiteSourceEvidence,SiteFeature,SiteRequirement,ServiceNode,ServiceConnection,
    ServiceEquipmentFacts,PlanServiceLateral,upgrade_to_v1_8)
from oabm.drawings.utility_delivery import create_utility_delivery

ROOT=Path(__file__).parents[1]


def _json(value):return json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n'
def _sha(raw):return hashlib.sha256(raw).hexdigest()


def _geometry(kind,points):return SitePlanGeometry(kind=kind,points=tuple(Point2D(x=x,y=y) for x,y in points))


def _source_svg():
    ns='http://www.w3.org/2000/svg'
    canvas=ET.Element('{'+ns+'}svg',width='220mm',height='160mm',viewBox='0 0 220 160')
    root=ET.SubElement(canvas,'{'+ns+'}g',transform='translate(10 10)')
    def text(x,y,value):ET.SubElement(root,'{'+ns+'}text',x=str(x),y=str(y),**{'font-family':'sans-serif','font-size':'2.7'}).text=value
    ET.SubElement(root,'{'+ns+'}rect',x='0',y='20',width='100',height='80',fill='none',stroke='#111111',**{'stroke-width':'.3'})
    ET.SubElement(root,'{'+ns+'}rect',x='50',y='30',width='40',height='60',fill='none',stroke='#111111',**{'stroke-width':'.5'})
    ET.SubElement(root,'{'+ns+'}line',x1='0',y1='110',x2='100',y2='110',stroke='#111111',**{'stroke-width':'.3'})
    ET.SubElement(root,'{'+ns+'}rect',x='24',y='73',width='12',height='12',fill='none',stroke='#111111',**{'stroke-width':'.2','stroke-dasharray':'1 1'})
    for x,label in ((50,'Gas source marker'),(30,'Proposed service anchor')):
        ET.SubElement(root,'{'+ns+'}line',x1=str(x-1.5),y1='70',x2=str(x+1.5),y2='70',stroke='#111111')
        ET.SubElement(root,'{'+ns+'}line',x1=str(x),y1='68.5',x2=str(x),y2='71.5',stroke='#111111')
        text(x,75,label)
    text(105,15,'SYNTHETIC REFERENCE PLAN / 1:100')
    text(105,25,'Paper XY millimetres; model XY metres.')
    text(105,30,'X = 0.1*x - 1; Y = 11 - 0.1*y.')
    text(105,40,'All geometry is fictional design input.')
    text(105,50,'200 A equipment / 160 A meter basis.')
    text(105,60,'Meter-main > MLO panel > subpanel.')
    text(105,70,'Utility location and terminal geometry TBD.')
    text(105,80,'No survey or engineering approval.')
    text(105,90,'Proposed1.2m surface; front/interface unknown.')
    text(5,116,'EXAMPLE SERVICE WAY')
    ET.SubElement(root,'{'+ns+'}line',x1='180',y1='110',x2='180',y2='95',stroke='#111111')
    text(178,93,'N')
    return ET.tostring(canvas,encoding='utf-8')


def _model(digest):
    proof=SiteSourceEvidence(source_id='source:synthetic-utility',sha256=digest,page=1,sheet_id='SYN-PLAN',
        model_frame_id='site',source_units='mm',drawing_units='m',scale_denominator=100.,
        region=(0.,0.,220.,160.),source_to_model=(.1,0.,0.,-.1,-1.,11.))
    def provenance(path):return (Provenance(source_kind='synthetic',source_id=proof.source_id,
        page=1,method='explicit known-answer synthetic facts',derivation='user',scope_paths=(path,)),)
    features=(
        SiteFeature(id='site:property',name='Synthetic property boundary',kind='property_boundary',status='existing',
            geometry=_geometry('polygon',[(0,0),(10,0),(10,8),(0,8)]),evidence=(proof,),provenance=provenance('geometry')),
        SiteFeature(id='site:building',name='Source-traced fictional building',kind='building_outline',status='existing',
            outline_basis='source_traced',geometry=_geometry('polygon',[(5,1),(9,1),(9,7),(5,7)]),evidence=(proof,),provenance=provenance('geometry')),
        SiteFeature(id='site:street',name='EXAMPLE SERVICE WAY',kind='street_reference',status='existing',
            geometry=_geometry('polyline',[(0,-1),(10,-1)]),evidence=(proof,),provenance=provenance('geometry')),
        SiteFeature(id='site:gas',name='Synthetic gas marker; extent unknown',kind='gas_location',status='existing',
            extent_basis='source_marker',geometry=_geometry('point',[(5,3)]),evidence=(proof,),provenance=provenance('geometry')),
        SiteFeature(id='site:intercept',name='Private-side boundary intercept',kind='utility_intercept',status='proposed',
            geometry=_geometry('point',[(0,3)]),evidence=(proof,),provenance=provenance('geometry')),
        SiteFeature(id='site:surface',name='Proposed surface / interface unverified',kind='working_surface',status='proposed',owner_id='service:meter-main',
            geometry=_geometry('polygon',[(2.4,1.5),(3.6,1.5),(3.6,2.7),(2.4,2.7)]),evidence=(proof,),provenance=provenance('geometry')),
    )
    nodes=(
        ServiceNode(id='service:utility',name='Utility reference / location unknown',equipment_type='source',
            location_basis='unresolved',unresolved_reason='Utility-side route and physical location not supplied.'),
        ServiceNode(id='service:meter-main',name='Synthetic integrated meter-main',equipment_type='meter_main',location_basis='plan_only',
            plan_position=Point2D(x=3,y=3),evidence=(proof,),unresolved_reason='Elevation, enclosure extent and terminal directions unknown.',provenance=provenance('plan_position')),
        ServiceNode(id='service:panel',name='Synthetic main-lug-only panel',equipment_type='panelboard',location_basis='unresolved',
            unresolved_reason='Panel position and feeder specification unknown.'),
        ServiceNode(id='service:subpanel',name='Existing subpanel / rating unknown',equipment_type='panelboard',location_basis='unresolved',
            unresolved_reason='Existing subpanel location, rating and feeder unknown.'),
    )
    links=tuple(ServiceConnection(id='service-link:'+name,source_node_id=a,target_node_id=b,specification_status='unresolved')
        for name,a,b in [('utility','service:utility','service:meter-main'),('main','service:meter-main','service:panel'),('subpanel','service:panel','service:subpanel')])
    return replace(upgrade_to_v1_8(BuildingModel(model_id='model:synthetic-utility-example',name='FICTIONAL UTILITY SITE',
        coordinate_system=CoordinateSystem(frame_id='site',true_north_radians=0.))),
        site_features=features,service_nodes=nodes,service_connections=links,
        service_equipment_facts=(ServiceEquipmentFacts(id='service-facts:meter-main',equipment_id='service:meter-main',
            equipment_rating_a=200,continuous_meter_rating_a=160,integrated_service_disconnect=True),
            ServiceEquipmentFacts(id='service-facts:panel',equipment_id='service:panel',equipment_rating_a=200,main_lug_only=True,circuit_positions=12)),
        site_requirements=(SiteRequirement(id='requirement:gas-anchor',kind='minimum_horizontal_separation',source_id='site:gas',
            target_id='service:meter-main',reference_basis='point_to_point',minimum_m=.5),
            SiteRequirement(id='requirement:surface',kind='working_surface_for',source_id='site:surface',target_id='service:meter-main',
                reference_basis='surface_to_equipment',width_m=.8,depth_m=.8)),
        plan_service_laterals=(PlanServiceLateral(id='lateral:private',source_node_id='service:meter-main',intercept_id='site:intercept',
            property_boundary_id='site:property',geometry=_geometry('polyline',[(3,3),(0,3)]),nominal_diameter_m=.05,
            raceway_description='Synthetic private raceway; depth unknown'),))


def prepare(root):
    """Write only independent public synthetic inputs, without PDF execution."""
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    source=_source_svg();(root/'reference-plan.svg').write_bytes(source)
    model=_model(_sha(source));(root/'model.json').write_text(model.to_json())
    load_source={'voltage_v':240,'loads':[{'id':'load:'+group,'group':group,'va':va} for group,va in [('general',18000),('hvac',1500),('floor_heat',600)]],
                 'first_va':8000,'remainder_factor':.5,'sensitivity_additional_va':3000,'qualification':'Fictional supplied example policy, not an automatic code calculation.'}
    raw=_json(load_source).encode();(root/'load-source.json').write_bytes(raw)
    profile={'kind':'supplied_service_demand_inputs_v1','service_node_id':'service:meter-main','voltage_v':240,
        'sources':{'source:synthetic-loads':_sha(raw)},
        'components':[{'id':row['id'],'name':'Synthetic '+row['group'],'group':row['group'],'va':row['va'],
            'source_id':'source:synthetic-loads','basis':'Declared fictional component allowance','assumption':True} for row in load_source['loads']],
        'general_demand':{'first_va':8000,'remainder_factor':.5,'basis':'Explicit fictional policy','source_id':'source:synthetic-loads'},
        'sensitivity':{'additional_va':3000,'basis':'Explicit fictional sensitivity','source_id':'source:synthetic-loads'}}
    (root/'load-inputs.json').write_text(_json(profile))
    (root/'profile.json').write_bytes((ROOT/'fixtures/company_sheets/v1/profile.json').read_bytes())
    job=json.loads((ROOT/'fixtures/company_sheets/v1/job.json').read_text())
    job['set_id']='synthetic-utility-example';job['formats']=['svg','pdf']
    job['fields'].update(project='FICTIONAL UTILITY EXAMPLE',client='SYNTHETIC OWNER',address='NO REAL ADDRESS',issue='SYNTHETIC / NOT FOR CONSTRUCTION')
    job['sheets']=[{'id':'U1','title':'SYNTHETIC SITE','fields':{'scale':'1:25'}},
                   {'id':'U2','title':'SYNTHETIC SERVICE ARRANGEMENT','fields':{'scale':'DIAGRAMMATIC'}},
                   {'id':'U3','title':'SUPPLIED SYNTHETIC LOADS','fields':{'scale':'N/A'}}]
    (root/'job.json').write_text(_json(job))
    request={'version':'model-utility-set-v1','model':'model.json','profile':'profile.json','job':'job.json',
        'sources':{'source:synthetic-utility':'reference-plan.svg','source:synthetic-loads':'load-source.json'},'load_inputs':'load-inputs.json',
        'site':{'sheet_id':'U1','drawing':{'scale':25.,'placement_mm':[15.,20.],'padding_mm':5.,'legend_width_mm':95.}},
        'single_line':{'sheet_id':'U2','service_only':True},'loads':{'sheet_id':'U3'},
        'documents':{'site':'Synthetic Site.pdf','single_line':'Synthetic Service.pdf','loads':'Synthetic Loads.pdf'}}
    (root/'baseline-request.json').write_text(_json(request))
    edited={**request,'field_edit':{'node_id':'service:meter-main','x_m':4.,'y_m':3.,
        'provenance':{'source_kind':'synthetic','source_id':'field:synthetic-location-edit','derivation':'user','method':'explicit fictional XY edit'}}}
    (root/'field-edit-request.json').write_text(_json(edited))
    return root/'baseline-request.json',root/'field-edit-request.json'


def create_example(root):
    """Generate real three-document baseline/edit packages through the public API."""
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    before,after=prepare(root/'inputs')
    baseline=create_utility_delivery(before,output_directory=root/'baseline')
    edited=create_utility_delivery(after,output_directory=root/'field-edit')
    first=json.loads((baseline/'derived/site-projection.json').read_text())['projection']
    second=json.loads((edited/'derived/site-projection.json').read_text())['projection']
    if first['measurement_requirements'][0]['distance_m']!=2 or second['measurement_requirements'][0]['distance_m']!=1:
        raise ValueError('synthetic canonical anchor edit did not regenerate its distance')
    original=json.loads((baseline/'inputs/model.json').read_text());changed=json.loads((edited/'inputs/model.json').read_text())
    def identities(document):
        return {(key,row['id']) for key,value in document.items() if isinstance(value,list) for row in value if isinstance(row,dict) and 'id' in row}
    if identities(original)!=identities(changed):raise ValueError('synthetic field edit changed stable IDs')
    for delivery in (baseline,edited):
        source=json.loads((delivery/'derived/site-projection.json').read_text())['projection']['model_sha256']
        if json.loads((delivery/'derived/load-report.json').read_text())['source_model_sha256']!=source or json.loads((delivery/'derived/single-line.json').read_text())['topology']['model_sha256']!=source:
            raise ValueError('synthetic document source bindings disagree')
    summary={'synthetic_only':True,'model_id':original['model_id'],'baseline_distance_m':2,'edited_distance_m':1,'identities_preserved':True,
             'document_source_bindings_verified':True,'engineering_approval':False,'private_project_acceptance':False,
             'files':{path.relative_to(root).as_posix():_sha(path.read_bytes()) for path in sorted(root.rglob('*')) if path.is_file()}}
    (root/'EXAMPLE-ACCEPTANCE.json').write_text(_json(summary))
    (root/'README.txt').write_text('FICTIONAL PUBLIC EXAMPLE ONLY. Baseline and field-edit each contain three real utility PDFs and page PNGs.\n'
        'The SVG is explicit synthetic source geometry at1:100. Anchor separation is not enclosure clearance.\n'
        'Model edit updates the private plan lateral and all document bindings without inventing utility/subpanel poses or terminal directions.\n'
        'No customer drawing, address or accepted project load data is included. Private acceptance remains separate.\n')
    return root


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    args=parser.parse_args();print(create_example(args.output))
