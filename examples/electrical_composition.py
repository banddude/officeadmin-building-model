"""Prepare synthetic inputs then run the existing production CLI/API boundary.

PYTHONPATH=src python examples/electrical_composition.py FRESH_ROOT
No source recognition, engineering approval or raster rendering is performed.
"""
import argparse
import json
from pathlib import Path
from oabm.drawings.single_line.demo_drafting import make_model
from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.production_set import create_production_sheet_set


def prepare(root,*,pdf=True):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    (root/'model.json').write_text(make_model().to_json())
    profile={'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC','project':'SYNTHETIC ELECTRICAL SET',
        'project_number':'DEMO-E3','scale':'NOT TO SCALE'},'template':{}}
    job={'version':'company-sheet-job-v1','set_id':'synthetic-electrical-composition','date':'2026-01-01','revision':'DEMO',
        'formats':['svg','pdf'] if pdf else ['svg'],'sheets':[{'id':'E3.1','title':'ELECTRICAL ONE-LINE AND PANEL SCHEDULES'}]}
    for name,data in [('profile',profile),('job',job)]:
        (root/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
    sheet=resolve_company_sheet_set(root/'profile.json',root/'job.json').sheets[0]
    l,t,r,b=sheet.drawing_bounds_mm
    # Explicit synthetic style regions, all in paper mm. They are editable
    # inputs, not inferred construction facts or job/page-count special cases.
    width,height=r-l,b-t;right_column=min(170,width*.24);gap=8;split=b-220
    request={'version':'production-sheet-set-v2','profile':'profile.json','job':'job.json','model':'model.json',
        'cover':None,'site':None,'schedules':None,'details':None,'model_views':[],
        'single_line':{'sheet_id':sheet.id,'composition':{'version':'electrical-sheet-regions-v2',
            'single_line_regions':{'diagram':[l,t,r-right_column-gap,split],
                'notes':[r-right_column,t,r,split],
                'feeder_register':[l,split+gap,r,b-24]},
            'panel_policy':'listed_only','panel_placement':'continuation',
            'panel_region_mm':None,'panel_width_mm':300}}}
    (root/'production.json').write_text(json.dumps(request,indent=2)+'\n')
    return root/'production.json'

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');a=p.parse_args();root=Path(a.root)
    root.mkdir(parents=True,exist_ok=False)
    request=prepare(root/'inputs')
    print(create_production_sheet_set(request,output_directory=root/'attempt'))
