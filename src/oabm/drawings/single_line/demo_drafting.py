"""Public-safe explicit electrical facts for the first drafting-style proof."""
from dataclasses import replace
from pathlib import Path
import argparse,json
from oabm.model import BuildingModel,ElectricalEquipment,Circuit,Conductor,Port,Pose,Point3,Vector3,Provenance


def make_model():
    provenance=(Provenance(source_kind='synthetic',source_id='electrical-drafting-demo',derivation='user',method='explicit-supplied-demo-facts'),)
    pose=Pose(position=Point3(x=0,y=0,z=0))
    equipment=[];ports=[];circuits=[];conductors=[]
    rows=[('U1','source',480,'Utility supply','existing',None),('M1','meter',480,'Service meter','existing',None),
          ('MS1','switchboard',480,'Main switchboard','existing',400),('HP1','panelboard',480,'High voltage panel','new',100),
          ('T1','transformer',None,'Distribution transformer','new',None),('LP1','panelboard',120,'Low voltage panel','new',125),
          ('N1','neutral_bus',None,'Neutral terminal bar','new',None),('G1','ground_electrode',None,'Supplied electrode connection','new',None)]
    for identity,kind,voltage,name,status,current in rows:
        facts={'status':status}
        if current is not None:facts['bus_current_a']=current
        if identity=='MS1':facts['neutral_ground_bond']='isolated';facts['notes']=['Neutral isolation is a supplied demo fact. No neutral-ground bond is inferred.']
        equipment.append(ElectricalEquipment(id=identity,name=name,equipment_type=kind,pose=pose,rated_voltage_v=voltage,
                                           provenance=provenance,attributes={'single_line_drafting':facts}))
    def edge(identity,source,target,voltage,kind='power',protection=None,status='new',size='6 AWG',wire_role='phase',count=3):
        a,b=identity+'-source',identity+'-load'
        ports.extend((Port(id=a,owner_id=source,domain='electrical',role='feeder' if kind=='power' else kind,pose=pose,direction=Vector3(x=1,y=0,z=0),provenance=provenance),
                      Port(id=b,owner_id=target,domain='electrical',role='incoming' if kind=='power' else kind,pose=pose,direction=Vector3(x=-1,y=0,z=0),provenance=provenance)))
        facts={'status':status,'connection_kind':kind}
        if protection is not None:facts['protection']=protection
        circuits.append(Circuit(id=identity,source_port_id=a,load_port_ids=(b,),voltage_v=voltage,
            poles=(1 if voltage==120 else 3) if kind=='power' else None,phase=('A' if voltage==120 else 'ABC') if kind=='power' else None,
            provenance=provenance,attributes={'single_line_drafting':facts}))
        conductors.append(Conductor(id=identity+'-wire',circuit_id=identity,role=wire_role,material='copper',size=size,count=count,insulation='THHN',provenance=provenance))
    edge('F01','U1','M1',480,status='existing',size='3/0 AWG')
    edge('F02','M1','MS1',480,protection={'kind':'breaker','rating_a':200,'poles':3,'interrupting_rating_a':22000},status='existing',size='3/0 AWG')
    edge('F03','MS1','HP1',480,protection={'kind':'breaker','rating_a':60,'poles':3},size='6 AWG')
    edge('F04','MS1','T1',480,protection={'kind':'fused_disconnect','rating_a':30,'poles':3},size='8 AWG')
    edge('F05','T1','LP1',120,protection={'kind':'fuse','rating_a':100,'poles':1},size='3 AWG',count=1)
    edge('F06','MS1','N1',None,kind='neutral',size='6 AWG',wire_role='neutral',count=1)
    edge('F07','MS1','G1',None,kind='grounding',size='6 AWG',wire_role='grounding_electrode',count=1)
    return BuildingModel(model_id='synthetic-electrical-drafting-facts',electrical_equipment=tuple(equipment),
                         ports=tuple(ports),circuits=tuple(circuits),conductors=tuple(conductors))


def main(argv=None):
    from .topology import project_topology
    from .layout import plan_pages
    from .production import create_single_line_set
    from ..company_sheets import resolve_company_sheet_set
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    args=parser.parse_args(argv);root=args.output;root.mkdir(parents=True,exist_ok=False)
    model=make_model();(root/'model.json').write_text(model.to_json()+'\n')
    profile={'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC','project':'SYNTHETIC ONE LINE','project_number':'DEMO','scale':'NOT TO SCALE','drawn_by':'DEMO'},'template':{}}
    job={'version':'company-sheet-job-v1','set_id':'synthetic-electrical-drafting','date':'2026-10-27','revision':'A','formats':['svg','pdf'],'fields':{'revision_description':'DEMO'},'sheets':[{'id':'E3.1','title':'POWER ONE LINE'}]}
    (root/'profile.json').write_text(json.dumps(profile,indent=2)+'\n');(root/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    template=resolve_company_sheet_set(root/'profile.json',root/'job.json').sheets[0]
    plan=plan_pages(project_topology(model),template.drawing_bounds_mm,first_sheet_id=template.id)
    print('PLAN',len(plan['pages']),[(p['id'],len(p['nodes']),len(p['groups'])) for p in plan['pages']],flush=True)
    job['sheets']=[{'id':p['id'],'title':'POWER ONE LINE'} for p in plan['pages']]
    (root/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    delivery=create_single_line_set(root/'model.json',root/'profile.json',root/'job.json',output_directory=root/'attempt')
    print(delivery,flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
