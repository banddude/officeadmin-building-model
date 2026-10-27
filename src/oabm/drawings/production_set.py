"""One shared sheet delivery from existing cover/site/schedule/detail producers.

This coordinator reuses body APIs and canonical report/detail APIs. It does not
create design facts, a second model, or engineering approval. Single-line and
multiple-detail slots are optional explicit inputs.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import os
from pathlib import Path
from .blank_set import _decode,_json,_sha,_runtime
from .company_sheets import resolve_company_sheet_set,create_company_sheet_set
from .cover_sheet import validate_cover,compose_cover_body
from .site_plan import render_site_body
from .schedule_set import panel_schedule_drawing_set,VERSION as SCHEDULE_VERSION
from .schedule_layout import ScheduleLayout,paginate_schedules
from .details import detail_specs_from_dict,company_detail_bodies,DetailPlacement
from oabm.model import BuildingModel,stable_id

VERSION='production-sheet-set-v1'


def create_production_sheet_set(request_path, *, output_directory):
    """Compose all requested bodies once, then export one ordered shared packet.

    Exact inputs and derived evidence/maps are retained. Schedule pagination
    expands one explicit template slot; the cover index derives from that final
    list. All other IDs remain caller supplied. Keep exclusive attempt ownership.
    """
    request_path=Path(request_path)
    raw=request_path.read_bytes();request=_decode(raw)
    keys={'version','profile','job','model','cover','site','schedules','details','single_line'}
    if not isinstance(request,dict) or set(request)!=keys or request['version']!=VERSION:
        raise ValueError('production request: exact production-sheet-set-v1 keys required')
    single_line=request['single_line']
    if single_line is not None and (not isinstance(single_line,dict) or set(single_line)!={'sheet_id'}):
        raise ValueError('single_line: supply null or an explicit sheet_id object')
    for key in ('site','schedules'):
        if not isinstance(request[key],dict) or set(request[key])!={'sheet_id','request'}:raise ValueError(key+': sheet_id/request required')
    paths={name:request_path.parent/request[name] for name in ('profile','job','model','cover','details')}
    paths.update(site=request_path.parent/request['site']['request'],settings=request_path.parent/request['schedules']['request'])
    inputs={key:path.read_bytes() for key,path in paths.items()}
    resolved=resolve_company_sheet_set(paths['profile'],paths['job'])
    if resolved.profile_json!=inputs['profile'] or resolved.job_json!=inputs['job']:raise ValueError('profile/job changed during snapshot')
    model=BuildingModel.from_json(inputs['model'])
    cover=validate_cover(_decode(inputs['cover']));site=_decode(inputs['site'])
    detail_request=_decode(inputs['details'])
    specs=detail_specs_from_dict(detail_request)
    placements=None
    if detail_request['version']=='detail-sheet-set@2':
        if not isinstance(detail_request['placements'],list) or not detail_request['placements']:raise ValueError('detail placements: nonempty list required')
        placements=tuple(DetailPlacement(**row) for row in detail_request['placements'])
    detail_sheet_ids=list(dict.fromkeys(p.sheet_id for p in placements)) if placements is not None else [s.id for s in specs]
    settings=_decode(inputs['settings'])
    if not isinstance(settings,dict) or set(settings)!={'version','layout'} or settings['version']!=SCHEDULE_VERSION:
        raise ValueError('schedule settings: explicit version/layout required')
    layout=ScheduleLayout(**settings['layout'])
    index=[{'id':s.id,'title':s.title} for s in resolved.sheets]
    if cover['sheets']!=index:raise ValueError('cover source index must match ordered job template list before pagination')
    by_id={s.id:s for s in resolved.sheets}
    schedule_id=request['schedules']['sheet_id'];site_id=request['site']['sheet_id']
    owners=[cover['id'],site_id,schedule_id,*detail_sheet_ids]+([single_line['sheet_id']] if single_line else [])
    if len(set(owners))!=len(owners) or set(owners)!=set(by_id):raise ValueError('each job sheet must have exactly one content owner')
    # Explicit cover project values must agree with common titleblock fields.
    for source,key in [('name','project'),('location','address'),('number','project_number')]:
        if cover['project'].get(source)!=by_id[cover['id']].fields.get(key):raise ValueError('cover project '+source+' differs from shared job '+key)
    if by_id[site_id].fields.get('scale')!=f"1:{site['drawing']['scale']:g}":raise ValueError('site titleblock scale must match supplied drawing denominator')
    report,drawing=panel_schedule_drawing_set(model)
    pages=paginate_schedules(drawing,by_id[schedule_id],layout=layout,empty_schedule_sources={
        stable_id('schedule',f'panel-facts:{p.panel.id}'):p.panel.id for p in report.panels if not p.circuits})
    expected=[(s.id,n,r.source_id) for s in drawing.schedules for n,r in enumerate(s.rows)]
    row_map=[r for p in pages for r in p.row_map]
    if [(r['schedule_id'],r['row_index'],r['source_id']) for r in row_map]!=expected:raise ValueError('schedule row coverage mismatch')
    single_line_pages=[]
    if single_line:
        from .single_line import project_topology,plan_pages
        single_line_pages=plan_pages(project_topology(model),by_id[single_line['sheet_id']].drawing_bounds_mm)['pages']
    expanded=[]
    for sheet in resolved.sheets:
        if sheet.id==schedule_id:
            expanded.extend({'id':p.spec.id,'title':sheet.title,'source':sheet} for p in pages)
        elif single_line and sheet.id==single_line['sheet_id']:
            expanded.extend({'id':p['id'],'title':sheet.title,'source':sheet} for p in single_line_pages)
        else:expanded.append({'id':sheet.id,'title':sheet.title,'source':sheet})
    if len({r['id'].casefold() for r in expanded})!=len(expanded):raise ValueError('continuation IDs collide with another sheet; choose distinct template IDs')
    output=Path(output_directory).absolute();output.parent.mkdir(parents=True,exist_ok=True);output.mkdir()
    pending=output/'.pending';pending.mkdir();stage='snapshot'
    try:
        retained=pending/'inputs';retained.mkdir()
        (retained/'production-request.json').write_bytes(raw)
        for key,value in inputs.items():(retained/(key+'.json')).write_bytes(value)
        first=resolved.sheets[0]
        profile={'version':'company-sheet-profile-v1','fields':{},'template':dict(first.template)}
        job={'version':'company-sheet-job-v1','set_id':resolved.set_id,'date':first.fields['date'],
             'revision':first.fields['revision'],'formats':list(resolved.formats),'sheets':[]}
        for row in expanded:
            sheet=row['source'];logos={}
            for name,data in sheet.logos:
                asset=f'assets/{_sha(data)}.png';(retained/'assets').mkdir(exist_ok=True)
                (retained/asset).write_bytes(data);logos[name]=asset
            job['sheets'].append({'id':row['id'],'title':row['title'],'fields':{k:v for k,v in sheet.fields.items() if k not in {'date','revision'}},
                                  'template':dict(sheet.template),'logos':logos})
        (retained/'portable-profile.json').write_bytes(_json(profile));(retained/'expanded-job.json').write_bytes(_json(job))
        final=resolve_company_sheet_set(retained/'portable-profile.json',retained/'expanded-job.json')
        sheets={s.id:s for s in final.sheets};bodies={}
        stage='site body'
        body,site_drawing,composition,audit,site_map=render_site_body(site,sheets[site_id]);bodies[site_id]=body
        stage='detail bodies'
        bodies.update(company_detail_bodies(model,specs,tuple(sheets[sid] for sid in detail_sheet_ids),placements=placements))
        stage='schedule bodies'
        bodies.update({page.spec.id:page.body_svg(layout) for page in pages})
        single_line_map=None
        if single_line:
            stage='single-line bodies'
            from .single_line.production import build_single_line_bodies
            single_line_bodies,single_line_map=build_single_line_bodies(model,tuple(sheets[p['id']] for p in single_line_pages))
            bodies.update(single_line_bodies)
        stage='cover body'
        final_index=[{'id':s.id,'title':s.title} for s in final.sheets]
        cover={**cover,'sheets':final_index}
        cover_body=compose_cover_body(cover,sheets[cover['id']].drawing_bounds_mm)
        bodies[cover['id']]='<g xmlns="http://www.w3.org/2000/svg">'+cover_body.svg_elements+'</g>'
        if set(bodies)!=set(sheets):raise ValueError('final content coverage mismatch')
        maps=pending/'derived';maps.mkdir()
        for name,value in [('cover-expanded.json',cover),('cover-layout.json',cover_body.layout),('site-audit.json',audit),
                           ('site-body.map.json',site_map),('schedule-rows.json',row_map),('schedule-sections.json',[{'sheet_id':p.spec.id,**section} for p in pages for section in (p.sections or ({'schedule_id':p.schedule_id,'schedule_page':p.schedule_page,'schedule_pages':p.schedule_pages},))]),('schedule-layout.json',asdict(layout))]:
            (maps/name).write_bytes(_json(value))
        if single_line_map is not None:(maps/'single-line-topology-layout.json').write_bytes(_json(single_line_map))
        (maps/'site-drawing.json').write_text(site_drawing.to_json()+'\n')
        (maps/'panel-facts.json').write_text(report.to_json()+'\n')
        (maps/'drawing-schedules.json').write_text(drawing.to_json()+'\n')
        for page in pages:(maps/(page.spec.id+'.drawing.json')).write_text(page.drawing_set.to_json()+'\n')
        stage='shared ordered SVG/PDF production'
        shared=create_company_sheet_set(retained/'portable-profile.json',retained/'expanded-job.json',output_directory=pending/'company',bodies=bodies)
        shared_manifest=_decode((shared/'MANIFEST.json').read_bytes())
        if shared_manifest['sheet_order']!=[r['id'] for r in final_index]:raise ValueError('shared output order differs from cover index')
        manifest={'version':VERSION,'sheet_order':shared_manifest['sheet_order'],'cover_index':final_index,'model_id':model.model_id,
                  'model_sha256':_sha(inputs['model']),'status':'supplied_plan_presentation' if single_line else 'partial_plan_presentation','engineering_approval':False,
                  'single_line':{'status':'supplied_topology_only','sheet_produced':True,'sheet_ids':[p['id'] for p in single_line_pages]} if single_line else {'status':'pending_integration','sheet_produced':False},
                  'content_owners':{'cover':[cover['id']],'site':[site_id],'schedules':[p.spec.id for p in pages],'details':detail_sheet_ids,'single_line':[p['id'] for p in single_line_pages]},
                  'schedule_row_count':len(row_map),'shared_delivery':'company/delivery','runtime':_runtime(),
                  'limitations':['Presentation tools do not certify plan-set completeness or engineering approval.',
                                 'Supplied site drawing evidence is not promoted to survey/model geometry.',
                                 'No demand/service sizing, installation fit or permit approval is inferred.']}
        files=[{'path':p.relative_to(pending).as_posix(),'bytes':p.stat().st_size,'sha256':_sha(p.read_bytes())}
               for p in sorted(pending.rglob('*')) if p.is_file()]
        manifest['files']=files
        for row in files:
            if _sha((pending/row['path']).read_bytes())!=row['sha256']:raise ValueError('production artifact changed before publication')
        (pending/'PRODUCTION-MANIFEST.json').write_bytes(_json(manifest))
        os.rename(pending,output/'delivery');return output/'delivery'
    except Exception as exc:
        try:(output/'failure.json').write_bytes(_json({'stage':stage,'error_type':type(exc).__name__}))
        except OSError:pass
        raise ValueError(f'production sheet set failed at {stage}: {exc}; attempt retained at {output}') from exc


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('request');parser.add_argument('output_directory')
    args=parser.parse_args(argv)
    try:result=create_production_sheet_set(args.request,output_directory=args.output_directory)
    except (ValueError,OSError,ImportError) as exc:parser.exit(1,str(exc)+'\n')
    print(result);return 0

if __name__=='__main__':raise SystemExit(main())
