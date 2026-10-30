"""Actual shared SVG/PDF proof for branching and multiple-source graph shapes."""
import argparse
import hashlib
import json
from pathlib import Path
from make_models import make_variant_model
from produce import main as produce_standard
from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.single_line import create_single_line_set,project_topology,plan_pages


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    produce_standard([str(args.output)])
    rows=[]
    cases=[('two-transfers','multi-transfer',2,{}),
           ('two-transfers-large-explicit-rail140','multi-transfer',2,{'width_mm':1200,'height_mm':900,'rail_width_mm':140}),
           ('three-transfers','multi-transfer',3,{}),('four-transfers','multi-transfer',4,{}),
           ('two-transformers','two-transformers',2,{}),('shallow','shallow',2,{}),('deep','deep',2,{})]
    for label,shape,count,template in cases:
        model=make_variant_model(shape,transfer_count=count)
        source=args.output/(label+'.model.json')
        source.write_text(model.to_json()+'\n')
        job={'version':'company-sheet-job-v1','set_id':'synthetic-'+label,
             'fields':{'project':'SYNTHETIC GRAPH PROOF','revision_description':'DEMO'},
             'date':'2026-10-27','revision':'A','formats':['svg','pdf'],
             'template':template,'sheets':[{'id':'SL-020','title':'SINGLE LINE'}]}
        job_path=args.output/(label+'.job.json')
        job_path.write_text(json.dumps(job,indent=2)+'\n')
        resolved=resolve_company_sheet_set(args.output/'profile.json',job_path).sheets[0]
        layout=plan_pages(project_topology(model),resolved.drawing_bounds_mm,first_sheet_id=resolved.id)
        job['sheets']=[{'id':page['id'],'title':'SINGLE LINE'} for page in layout['pages']]
        job_path.write_text(json.dumps(job,indent=2)+'\n')
        delivery=create_single_line_set(source,args.output/'profile.json',job_path,output_directory=args.output/(label+'-attempt'))
        pdf=(delivery/'company-sheets/delivery/sheet-set.pdf').read_bytes()
        (args.output/(label+'.pdf')).write_bytes(pdf)
        row={'case':label,'sheets':len(layout['pages']),'equipment':len(model.electrical_equipment),
             'panels':sum(e.equipment_type=='panelboard' for e in model.electrical_equipment),
             'connections':len(project_topology(model).connections),'pdf_sha256':hashlib.sha256(pdf).hexdigest()}
        rows.append(row);print(json.dumps(row),flush=True)
    (args.output/'GRAPH-PROOFS.json').write_text(json.dumps(rows,indent=2)+'\n')
    return 0

if __name__=='__main__':raise SystemExit(main())
