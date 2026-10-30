"""Public synthetic source-identity + native mesh/plumbing + shared sheet proof.

Uses an explicitly supplied installed runtime. No installation or downloads.
"""
from pathlib import Path
import argparse
import hashlib
import json
import copy
import numpy as np
import ifcopenshell
import ifcopenshell.api
from oabm.ifc import canonical_id_to_ifc_guid
from oabm.model import BuildingModel
from oabm.native_bonsai import NativeRuntime,export_native_ifc
from oabm.native_bonsai.source_bindings import fingerprint
from oabm.drawings.native_sheet_set import create_native_sheet_set,preflight_native_sheet_set
from synthetic_native_plumbing import build_fixture


def prepare_inputs(root):
    mp,source,old_request=build_fixture(root/'inputs')
    native=ifcopenshell.open(str(source));model=BuildingModel.load(mp).to_dict()
    alternate=canonical_id_to_ifc_guid('synthetic-native-source:independent-light-id')
    native.by_guid(canonical_id_to_ifc_guid('device:L1')).GlobalId=alternate
    bindings=[]
    for rows in model.values():
        if not isinstance(rows,list):continue
        for entity in rows:
            if not isinstance(entity,dict) or 'id' not in entity:continue
            cid=entity['id'];guid=alternate if cid=='device:L1' else canonical_id_to_ifc_guid(cid)
            entity.setdefault('provenance',[]).append({'source_kind':'ifc','source_id':'synthetic-native-source',
                'source_element_id':guid,'method':'Explicit synthetic source interpretation','confidence':.8,'derivation':'inferred','attributes':{}})
    model=BuildingModel.from_dict(model);model.dump(mp)
    for rows in model.to_dict().values():
        if isinstance(rows,list):
            for entity in rows:
                if isinstance(entity,dict) and 'id' in entity:
                    cid=entity['id'];guid=alternate if cid=='device:L1' else canonical_id_to_ifc_guid(cid)
                    bindings.append({'canonical_id':cid,'source_guid':guid,'canonical_sha256':fingerprint(entity),
                        'qualification':'Synthetic source geometry authoritative; no manufacturer or field claims.'})
    body=next(c for c in native.by_type('IfcGeometricRepresentationSubContext') if c.ContextIdentifier=='Body')
    level=native.by_type('IfcBuildingStorey')[0]
    preserve=[]
    # This L-shaped native wall has no canonical box/centerline equivalent.
    outline=[(0.,0.),(1.,0.),(1.,.2),(.2,.2),(.2,1.),(0.,1.)]
    vertices=[(x,y,z) for z in (0.,2.) for x,y in outline]
    faces=[(5,4,3,2,1,0),(6,7,8,9,10,11)]+[(i,(i+1)%6,(i+1)%6+6,i+6) for i in range(6)]
    for kind,key,xyz in [('IfcWall','mesh-wall',(2.8,1.6,0.)),('IfcDoor','mesh-door',(2.8,1.6,0.)),('IfcBoiler','native-boiler',(3.,.5,0.))]:
        product=ifcopenshell.api.run('root.create_entity',native,ifc_class=kind,name='Synthetic '+key)
        product.GlobalId=canonical_id_to_ifc_guid('synthetic-native-source:'+key)
        if kind=='IfcWall':rep=ifcopenshell.api.run('geometry.add_mesh_representation',native,context=body,vertices=[vertices],faces=[faces])
        else:rep=ifcopenshell.api.run('geometry.add_wall_representation',native,context=body,length=.3,thickness=.15,height=.6 if kind=='IfcBoiler' else 1.8)
        ifcopenshell.api.run('geometry.assign_representation',native,product=product,representation=rep)
        matrix=np.eye(4);matrix[:3,3]=xyz
        ifcopenshell.api.run('geometry.edit_object_placement',native,product=product,matrix=matrix,is_si=True)
        ifcopenshell.api.run('spatial.assign_container',native,products=[product],relating_structure=level)
        preserve.append({'source_guid':product.GlobalId,'ifc_class':kind,'qualification':'Explicit synthetic native-only product; exact source geometry retained.'})
    # Deterministic synthetic new relationship GUIDs; source geometry/identity
    # assertions use the resulting source snapshot rather than assuming byte
    # equality between separately authored fixtures.
    native.write(str(source))
    request=json.loads(old_request.read_text());request['version']='native-bonsai-ifc-v2'
    request['source_contract']={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'bindings':bindings,'preserve_products':preserve}
    original=json.loads((Path(__file__).parent/'synthetic-native-bonsai-request.json').read_text())
    plan=original['views'][0];plan['id']='plan';plan['native_annotations']=[]
    rcp=original['views'][1];rcp['id']='rcp';rcp['native_annotations']=[]
    plumbing=request['views'][0];plumbing['id']='plumbing'
    request['views']=[plan,rcp,plumbing]
    request['acknowledged_native_body_omissions']=[]
    old_request.write_text(json.dumps(request,indent=2)+'\n')
    return mp,source,old_request


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path);p.add_argument('--blender',type=Path,required=True);p.add_argument('--bonsai-python',type=Path,required=True);args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    mp,source,request=prepare_inputs(args.output)
    native=export_native_ifc(mp,source,request,args.output/'native',NativeRuntime(args.blender,args.bonsai_python))
    inputs=args.output/'sheet-inputs';inputs.mkdir()
    profile={'version':'company-sheet-profile-v1','fields':{'company':'SYNTHETIC COMPANY'},'template':{'width_mm':279.4,'height_mm':215.9,'rail_width_mm':65}}
    job={'version':'company-sheet-job-v1','set_id':'synthetic-native-shared-review','date':'2026-01-01','revision':'R1','formats':['svg','pdf'],'fields':{'project':'SYNTHETIC NATIVE REVIEW','scale':'1:48','issue':'REVIEW ONLY'},'sheets':[{'id':sid,'title':title} for sid,title in [('A1','SOURCE NATIVE FLOOR VIEW'),('R1','SOURCE NATIVE REFLECTED VIEW'),('P1','SOURCE NATIVE PLUMBING VIEW')]]}
    sheets=[]
    for sid,view in [('A1','plan'),('R1','rcp'),('P1','plumbing')]:
        body=sid+'-body.svg';(inputs/body).write_text('<g><text x="12" y="150" font-family="Liberation Sans" font-size="3" fill="#000000">SYNTHETIC REVIEW: SOURCE GEOMETRY RETAINED</text><text x="12" y="157" font-family="Liberation Sans" font-size="2.5" fill="#000000">No manufacturer, field, engineering or code acceptance implied.</text></g>')
        sheets.append({'sheet_id':sid,'views':[{'view_id':view,'origin_mm':[12,25]}],'body':body})
    data={'version':'native-review-sheet-set-v1','profile':'profile.json','job':'job.json','native_delivery':'../native','sheets':sheets}
    for name,value in [('profile.json',profile),('job.json',job),('native-sheets.json',data)]: (inputs/name).write_text(json.dumps(value,indent=2)+'\n')
    preflight_native_sheet_set(inputs/'native-sheets.json')
    print(create_native_sheet_set(inputs/'native-sheets.json',output_directory=args.output/'sheets'))


if __name__=='__main__':main()
