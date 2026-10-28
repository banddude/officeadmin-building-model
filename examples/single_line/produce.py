"""Generate both synthetic canonical cases with the real shared company tool."""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw
from oabm.drawings.company_sheets import resolve_company_sheet_set
from oabm.drawings.single_line import project_topology, plan_pages, create_single_line_set
from oabm.model import BuildingModel


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=False)
    # Deliberately fictional profile and logo components, shared by both cases.
    for name,size,label in [('icon',(160,120),'DEMO'),('wordmark',(400,60),'EXAMPLE ELECTRIC')]:
        image=Image.new('RGB',size,'white');draw=ImageDraw.Draw(image)
        draw.rectangle((1,1,size[0]-2,size[1]-2),outline='black',width=2)
        draw.text((10,20),label,fill='black');image.save(args.output/(name+'.png'))
    profile={'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC',
             'license':'SYNTHETIC / NO LICENSE','project':'SYNTHETIC SINGLE LINE',
             'project_number':'DEMO','scale':'NOT TO SCALE','drawn_by':'DEMO'},
             'template':{},'logos':{'icon':'icon.png','wordmark':'wordmark.png'}}
    profile_path=args.output/'profile.json'
    profile_path.write_text(json.dumps(profile,indent=2)+'\n')
    for label in ('residential','commercial-27'):
        model_path=Path(__file__).parent/(label+'.model.json')
        model=BuildingModel.from_json(model_path.read_bytes())
        job={'version':'company-sheet-job-v1','set_id':'single-line-'+label,
             'fields':{'project':'SYNTHETIC '+('RESIDENTIAL' if label=='residential' else 'COMMERCIAL'),
                       'revision_description':'DEMO'},
             'date':'2026-10-27','revision':'A','formats':['svg','pdf'],
             'sheets':[{'id':'E601','title':'SINGLE LINE'}]}
        job_path=args.output/(label+'.job.json')
        job_path.write_text(json.dumps(job,indent=2)+'\n')
        template=resolve_company_sheet_set(profile_path,job_path).sheets[0]
        layout=plan_pages(project_topology(model),template.drawing_bounds_mm,first_sheet_id=template.id)
        job['sheets']=[{'id':p['id'],'title':{'single-line':'SINGLE LINE',
                        'distribution':'DISTRIBUTION','distribution-continuation':'DISTRIBUTION CONT.',
                        'facts-register':'FEEDER REGISTER'}[p['kind']]} for p in layout['pages']]
        job_path.write_text(json.dumps(job,indent=2)+'\n')
        delivery=create_single_line_set(model_path,profile_path,job_path,output_directory=args.output/(label+'-attempt'))
        print(label,delivery)
    return 0

if __name__=='__main__':raise SystemExit(main())
