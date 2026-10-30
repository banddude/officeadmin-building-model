"""Synthetic physical model -> one-command shared scaled plan/section packet.

python examples/assembly_production_sheets.py FRESH_ROOT [--pdf]
Writes editable inputs, then calls the existing production coordinator. No fake
cover/site/schedule/detail inputs are required for the explicit view-only packet.
"""
import argparse
import json
from pathlib import Path
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.drawings.production_set import create_production_sheet_set


def prepare(root,*,pdf=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    model=synthetic_wall_inserts_ceiling_assembly();(root/'model.json').write_text(model.to_json())
    profile={'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC'},'template':{}}
    job={'version':'company-sheet-job-v1','set_id':'synthetic-physical-model-views','date':'2026-01-01','revision':'DEMO',
        'fields':{'project':'SYNTHETIC PHYSICAL ASSEMBLIES','project_number':'DEMO-01','scale':'1:20','issue':'SYNTHETIC REVIEW'},
        'formats':['svg','pdf'] if pdf else ['svg'],
        'sheets':[{'id':'A101','title':'PHYSICAL ASSEMBLY PLAN CUT'},{'id':'A301','title':'PHYSICAL ASSEMBLY SECTION CUT'}]}
    request={'version':'production-sheet-set-v2','profile':'profile.json','job':'job.json','model':'model.json',
        'cover':None,'site':None,'schedules':None,'details':None,'single_line':None,'model_views':[
            {'sheet_id':'A101','type':'plan','options':{'level_id':model.levels[0].id,'scale':20},
                'assembly_geometry':'cut','annotations':'none','origin_mm':[20,30]},
            {'sheet_id':'A301','type':'section','options':{'origin':{'x':1,'y':0,'z':0},'direction':{'x':1,'y':0,'z':0},'scale':20},
                'assembly_geometry':'cut','annotations':'none','origin_mm':[20,30]}]}
    for name,value in [('profile',profile),('job',job),('production',request)]:
        (root/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    return root/'production.json'

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--pdf',action='store_true');a=p.parse_args()
    Path(a.root).mkdir(parents=True,exist_ok=False)
    request=prepare(Path(a.root)/'inputs',pdf=a.pdf)
    print(create_production_sheet_set(request,output_directory=Path(a.root)/'attempt'))
