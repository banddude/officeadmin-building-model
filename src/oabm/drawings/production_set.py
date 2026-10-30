"""One shared sheet delivery from existing cover/site/schedule/detail producers.

This coordinator reuses body APIs and canonical report/detail APIs. It does not
create design facts, a second model, or engineering approval. Single-line and
multiple-detail slots are optional explicit inputs.
"""
from __future__ import annotations
import argparse
import copy
from dataclasses import asdict
import os
from pathlib import Path
from .blank_set import _decode,_json,_sha,_runtime
from .company_sheets import resolve_company_sheet_set,create_company_sheet_set,_logos
from .cover_sheet import validate_cover,paginate_cover
from .site_plan import render_site_body
from .schedule_set import panel_schedule_drawing_set,VERSION as SCHEDULE_VERSION
from .schedule_layout import ScheduleLayout,paginate_schedules
from .details import detail_specs_from_dict,company_detail_bodies,DetailPlacement
from oabm.model import BuildingModel,stable_id

VERSION='production-sheet-set-v1'
MODEL_VIEW_VERSION='production-sheet-set-v2'


def _write_portable_inputs(root, request_path, request, inputs, resolved, paths, map_snapshot):
    """Snapshot pre-pagination inputs with relocated, content-addressed logos.

    Original source bytes remain separately retained; these editable copies keep
    the original profile/job/sheet precedence rather than flattening it.
    """
    root.mkdir()
    portable=copy.deepcopy(request)
    files={'model':'model.json','cover':'cover.json','details':'details.json',
           'site':'site.json','site_graphics':'site-graphics.json','settings':'settings.json'}
    for key,name in files.items():
        if key in inputs:(root/name).write_bytes(inputs[key])
    portable['profile']='profile.json';portable['job']='job.json';portable['model']=files['model']
    for key in ('cover','details'):
        if portable[key] is not None:portable[key]=files[key]
    if portable['site'] is not None:
        portable['site']['request']=files['site']
        if 'graphics' in portable['site']:portable['site']['graphics']=files['site_graphics']
    if portable['schedules'] is not None:portable['schedules']['request']=files['settings']
    profile=_decode(inputs['profile']);job=_decode(inputs['job'])
    def snapshot_logos(owner,directory):
        if 'logos' not in owner:return
        logos=_logos(owner['logos'],directory)
        for name,data in logos.items():
            if data is None:owner['logos'][name]=None;continue
            asset='assets/'+_sha(data)+'.png';path=root/asset;path.parent.mkdir(exist_ok=True)
            if path.exists() and path.read_bytes()!=data:raise ValueError('portable logo digest collision')
            if not path.exists():path.write_bytes(data)
            owner['logos'][name]=asset
    snapshot_logos(profile,paths['profile'].parent)
    snapshot_logos(job,paths['job'].parent)
    for sheet in job['sheets']:snapshot_logos(sheet,paths['job'].parent)
    (root/'profile.json').write_bytes(_json(profile));(root/'job.json').write_bytes(_json(job))
    portable_resolved=resolve_company_sheet_set(root/'profile.json',root/'job.json')
    if (portable_resolved.set_id,portable_resolved.sheets,portable_resolved.formats)!=(resolved.set_id,resolved.sheets,resolved.formats):
        raise ValueError('profile/job/logo changed during portable snapshot')
    map_status={'status':'not_requested','reason':'No cover map requested.'}
    if portable.get('cover_map') is not None:
        options=portable['cover_map'];source_cache=request_path.parent/options['cache_directory']
        options['mode']='cached';options['cache_directory']='map-cache'
        map_status={'status':'offline_cache_unavailable','reason':'No reusable cache receipt was present at snapshot; rebuild will not fetch a map.'}
        if map_snapshot is not None:
            from .cover_maps import _cache_key,_utc,acquire_cover_map
            query=map_snapshot.provenance['query']
            key=_cache_key(query,options['candidate_index'],options.get('candidate_confirmation'))
            receipt=source_cache/(key+'.json')
            cacheable=_utc(map_snapshot.provenance['expires_at'])>_utc(map_snapshot.provenance['fetched_at'])
            if not cacheable:
                map_status={'status':'offline_cache_unavailable','reason':'Map response has no reusable cache lifetime; rebuild will not fetch it.'}
            if cacheable and (receipt.exists() or receipt.is_symlink()):
                # Existing cache validation handles symlinks, provider, candidate,
                # freshness and asset hash; never fall back to live acquisition.
                verified=acquire_cover_map(query,cache_directory=source_cache,candidate_index=options['candidate_index'],
                    candidate_confirmation=options.get('candidate_confirmation'),offline=True)
                if verified!=map_snapshot:raise ValueError('cover map cache differs from acquired snapshot')
                target=root/'map-cache';target.mkdir()
                (target/(key+'.json')).write_bytes(receipt.read_bytes())
                (target/(map_snapshot.sha256+'.png')).write_bytes(map_snapshot.png_bytes)
                copied=acquire_cover_map(query,cache_directory=target,candidate_index=options['candidate_index'],
                    candidate_confirmation=options.get('candidate_confirmation'),offline=True)
                if copied!=map_snapshot:raise ValueError('portable cover map differs from acquired snapshot')
                map_status={'status':'offline_fresh_cache_retained','reason':'Retained cache remains subject to its original expiry and validation.'}
    (root/'production.json').write_bytes(_json(portable))
    return map_status


def create_production_sheet_set(request_path, *, output_directory, map_client=None, _check_only=False):
    """Compose all requested bodies once, then export one ordered shared packet.

    Exact inputs and derived evidence/maps are retained. Schedule pagination
    expands one explicit template slot; the cover index derives from that final
    list. All other IDs remain caller supplied. Keep exclusive attempt ownership.
    """
    request_path=Path(request_path)
    raw=request_path.read_bytes();request=_decode(raw)
    keys={'version','profile','job','model','cover','site','schedules','details','single_line'}
    v2=isinstance(request,dict) and request.get('version')==MODEL_VIEW_VERSION
    if v2:keys=keys|{'model_views'}
    if v2 and 'cover_map' in request:keys=keys|{'cover_map'}
    if not isinstance(request,dict) or set(request)!=keys or request['version'] not in {VERSION,MODEL_VIEW_VERSION}:
        raise ValueError('production request: exact production-sheet-set-v1 or v2 keys required')
    map_options=request.get('cover_map')
    if map_options is not None:
        if not isinstance(map_options,dict) or set(map_options) not in ({'mode','cache_directory','candidate_index'},{'mode','cache_directory','candidate_index','candidate_confirmation'}) or map_options['mode'] not in {'auto','cached'} or not isinstance(map_options['cache_directory'],str) or not map_options['cache_directory']:
            raise ValueError('cover_map: explicit auto/cached mode, cache_directory and candidate_index required')
        if request['cover'] is None:raise ValueError('cover_map requires a cover producer')
        from .cover_maps import validate_candidate_selection
        validate_candidate_selection(map_options['candidate_index'],map_options.get('candidate_confirmation'))
    model_views=request['model_views'] if v2 else []
    if not isinstance(model_views,list):raise ValueError('model_views: ordered list required')
    for row in model_views:
        if not isinstance(row,dict) or not isinstance(row.get('sheet_id'),str):raise ValueError('model_views: explicit sheet_id required')
    if len({row['sheet_id'] for row in model_views})!=len(model_views):raise ValueError('model_views: duplicate sheet owner')
    single_line=request['single_line']
    if single_line is not None and (not isinstance(single_line,dict) or set(single_line) not in ({'sheet_id'}, {'sheet_id','composition'} if v2 else {'sheet_id'})):
        raise ValueError('single_line: supply null or an explicit sheet_id object; v2 may add composition')
    if single_line is not None and (not isinstance(single_line['sheet_id'],str) or not single_line['sheet_id']):
        raise ValueError('single_line.sheet_id: nonempty string required')
    electrical_config=single_line.get('composition') if single_line else None
    if electrical_config is not None:
        legacy={'version','single_line_regions','panel_region_mm','panel_width_mm'}
        current=legacy|{'panel_policy','panel_placement'}
        adaptive=current|{'layout_candidates'}
        if not isinstance(electrical_config,dict) or not ((electrical_config.get('version')=='electrical-sheet-regions-v1' and set(electrical_config)==legacy) or (electrical_config.get('version')=='electrical-sheet-regions-v2' and set(electrical_config)==current) or (electrical_config.get('version')=='electrical-sheet-regions-v3' and set(electrical_config)==adaptive)):
            raise ValueError('single_line.composition: exact electrical-sheet-regions-v1, v2 or v3 settings required')
        if electrical_config['version']=='electrical-sheet-regions-v3' and (electrical_config['panel_placement']!='auto' or electrical_config['panel_region_mm'] is not None):
            raise ValueError('adaptive composition requires panel_placement=auto and panel_region_mm=null')
        if request['schedules'] is not None:raise ValueError('composed electrical panels own schedule output; set schedules=null to avoid duplicate tables')
    for key in ('site','schedules'):
        if v2 and request[key] is None:continue
        allowed=({'sheet_id','request'},{'sheet_id','request','graphics'}) if v2 and key=='site' else ({'sheet_id','request'},)
        if not isinstance(request[key],dict) or set(request[key]) not in allowed:raise ValueError(key+': sheet_id/request required; v2 site may add graphics')
    # Validate path types before joining, so missing job inputs name their owner.
    for name in ('profile', 'job', 'model', 'cover', 'details'):
        value=request[name]
        if name in ('cover', 'details') and v2 and value is None:continue
        if not isinstance(value,str) or not value:raise ValueError(f'{name}: nonempty input path required')
    for name in ('site', 'schedules'):
        if request[name] is not None:
            for field in ('sheet_id', 'request'):
                if not isinstance(request[name][field],str) or not request[name][field]:raise ValueError(f'{name}.{field}: nonempty string required')
            if 'graphics' in request[name] and (not isinstance(request[name]['graphics'],str) or not request[name]['graphics']):raise ValueError(f'{name}.graphics: nonempty input path required')
    paths={name:request_path.parent/request[name] for name in ('profile','job','model')}
    for name in ('cover','details'):
        if not(v2 and request[name] is None):paths[name]=request_path.parent/request[name]
    if request['site'] is not None:paths['site']=request_path.parent/request['site']['request']
    if request['site'] is not None and 'graphics' in request['site']:paths['site_graphics']=request_path.parent/request['site']['graphics']
    if request['schedules'] is not None:paths['settings']=request_path.parent/request['schedules']['request']
    inputs={}
    for key,path in paths.items():
        try:inputs[key]=path.read_bytes()
        except OSError as exc:raise ValueError(f'{key}: cannot read input {path}: {exc.strerror or exc}') from exc
    resolved=resolve_company_sheet_set(paths['profile'],paths['job'])
    if resolved.profile_json!=inputs['profile'] or resolved.job_json!=inputs['job']:raise ValueError('profile/job changed during snapshot')
    model=BuildingModel.from_json(inputs['model'])
    cover=validate_cover(_decode(inputs['cover'])) if 'cover' in inputs else None
    site=_decode(inputs['site']) if 'site' in inputs else None
    detail_request=_decode(inputs['details']) if 'details' in inputs else None
    specs=detail_specs_from_dict(detail_request) if detail_request is not None else ()
    placements=None
    if detail_request is not None and detail_request['version']=='detail-sheet-set@2':
        if not isinstance(detail_request['placements'],list) or not detail_request['placements']:raise ValueError('detail placements: nonempty list required')
        placements=tuple(DetailPlacement(**row) for row in detail_request['placements'])
    detail_sheet_ids=list(dict.fromkeys(p.sheet_id for p in placements)) if placements is not None else [s.id for s in specs]
    settings=_decode(inputs['settings']) if 'settings' in inputs else None
    if settings is not None and (not isinstance(settings,dict) or set(settings)!={'version','layout'} or settings['version']!=SCHEDULE_VERSION):
        raise ValueError('schedule settings: explicit version/layout required')
    layout=ScheduleLayout(**settings['layout']) if settings is not None else None
    index=[{'id':s.id,'title':s.title} for s in resolved.sheets]
    if cover is not None and cover['sheets']!=index:raise ValueError('cover source index must match ordered job template list before pagination')
    by_id={s.id:s for s in resolved.sheets}
    schedule_id=request['schedules']['sheet_id'] if request['schedules'] else None
    site_id=request['site']['sheet_id'] if request['site'] else None
    owners=([cover['id']] if cover else [])+([site_id] if site_id else [])+([schedule_id] if schedule_id else [])+detail_sheet_ids+([single_line['sheet_id']] if single_line else [])+[row['sheet_id'] for row in model_views]
    if len(set(owners))!=len(owners) or set(owners)!=set(by_id):raise ValueError('each job sheet must have exactly one content owner')
    # Explicit cover project values must agree with common titleblock fields.
    for source,key in [('name','project'),('location','address'),('number','project_number')]:
        if cover is not None and cover['project'].get(source)!=by_id[cover['id']].fields.get(key):raise ValueError('cover project '+source+' differs from shared job '+key)
    site_result=render_site_body(site,by_id[site_id],graphics=_decode(inputs['site_graphics']) if 'site_graphics' in inputs else None) if site_id else None
    selected_site_scale=site_result[3]['presentation']['selected_scale'] if site_result else None
    selected_site_label=site_result[3]['presentation'].get('sheet_scale_label',f'1:{selected_site_scale:g}') if site_result else None
    if site_result and site_result[3]['presentation']['mode']=='fixed' and by_id[site_id].fields.get('scale') not in {f'1:{selected_site_scale:g}',selected_site_label}:
        raise ValueError('site titleblock scale must match supplied drawing denominator')
    report=drawing=None;pages=();row_map=[]
    if schedule_id:
        report,drawing=panel_schedule_drawing_set(model)
        pages=paginate_schedules(drawing,by_id[schedule_id],layout=layout,empty_schedule_sources={
            stable_id('schedule',f'panel-facts:{p.panel.id}'):p.panel.id for p in report.panels if not p.circuits})
        expected=[(s.id,n,r.source_id) for s in drawing.schedules for n,r in enumerate(s.rows)]
        row_map=[r for p in pages for r in p.row_map]
        if [(r['schedule_id'],r['row_index'],r['source_id']) for r in row_map]!=expected:raise ValueError('schedule row coverage mismatch')
    model_view_results={}
    if model_views:
        from .model_view_sheets import build_model_view_body
        for row in model_views:model_view_results[row['sheet_id']]=build_model_view_body(model,by_id[row['sheet_id']],row)
    single_line_pages=[];electrical_plan=None;electrical_selection=None
    if single_line:
        from .single_line import project_topology,plan_pages
        reserved={s.id for s in resolved.sheets if s.id!=single_line['sheet_id']}|{p.spec.id for p in pages}
        if electrical_config is not None:
            from .electrical_sheet import plan_electrical_sheets
            if electrical_config['version']=='electrical-sheet-regions-v3':
                from .adaptive_electrical_sheet import plan_adaptive_electrical_sheets
                electrical_plan,electrical_selection=plan_adaptive_electrical_sheets(model,by_id[single_line['sheet_id']],
                    single_line_regions=electrical_config['single_line_regions'],layout_candidates=electrical_config['layout_candidates'],
                    panel_width_mm=electrical_config['panel_width_mm'],reserved_sheet_ids=sorted(reserved),
                    panel_policy=electrical_config['panel_policy'])
            else:
                electrical_plan=plan_electrical_sheets(model,by_id[single_line['sheet_id']],
                    single_line_regions=electrical_config['single_line_regions'],panel_region_mm=electrical_config['panel_region_mm'],
                    panel_width_mm=electrical_config['panel_width_mm'],reserved_sheet_ids=sorted(reserved),
                    panel_policy=electrical_config.get('panel_policy','all_reported'),panel_placement=electrical_config.get('panel_placement','shared'))
            single_line_pages=[{'id':identity,**({'title':'PANEL SCHEDULES (CONTINUED)'} if kind=='panel_schedules' else {})}
                for identity,kind in zip(electrical_plan.sheet_ids,electrical_plan.sheet_kinds)]
            row_map=[row for page in electrical_plan.panel_pages for row in page['row_map']]
        else:
            single_line_pages=plan_pages(project_topology(model),by_id[single_line['sheet_id']].drawing_bounds_mm,
                first_sheet_id=single_line['sheet_id'],reserved_sheet_ids=sorted(reserved))['pages']
    expanded=[]
    for sheet in resolved.sheets:
        if sheet.id==schedule_id:
            expanded.extend({'id':p.spec.id,'title':sheet.title,'source':sheet} for p in pages)
        elif single_line and sheet.id==single_line['sheet_id']:
            expanded.extend({'id':p['id'],'title':p.get('title',sheet.title),'source':sheet} for p in single_line_pages)
        else:expanded.append({'id':sheet.id,'title':sheet.title,'source':sheet})
    if len({r['id'].casefold() for r in expanded})!=len(expanded):raise ValueError('continuation IDs collide with another sheet; choose distinct template IDs')
    cover_pagination=None;map_snapshot=None
    if cover and map_options is not None and 'location_map' in cover:
        raise ValueError('cover_map conflicts with supplied location_map; remove the diagram or omit cover_map')
    if cover and map_options is not None and 'location_map' not in cover and not _check_only:
        from .cover_maps import acquire_cover_map
        map_snapshot=acquire_cover_map(cover['project']['location'],cache_directory=request_path.parent/map_options['cache_directory'],
            client=map_client,candidate_index=map_options['candidate_index'],candidate_confirmation=map_options.get('candidate_confirmation'),offline=map_options['mode']=='cached')
    if cover and not (_check_only and map_options is not None):
        cover_pagination=paginate_cover(cover,by_id[cover['id']].drawing_bounds_mm,
            sheet_index=[{'id':r['id'],'title':r['title']} for r in expanded],map_snapshot=map_snapshot)
        expanded_with_cover=[]
        for row in expanded:
            if row['id']==cover['id']:
                expanded_with_cover.extend({'id':page.id,'title':page.title,'source':row['source']} for page in cover_pagination.pages)
            else:expanded_with_cover.append(row)
        expanded=expanded_with_cover
    if _check_only:
        # Exercise the existing detail consumer against the actual canonical model;
        # unlike a build this does not reserve an attempt or write assets.
        if specs:company_detail_bodies(model,specs,tuple(by_id[sid] for sid in detail_sheet_ids),placements=placements)
        return {'status':'bounded_preflight_passed', 'model_id':model.model_id,
                'template_sheet_ids':[s.id for s in resolved.sheets],
                'limitations':['No delivery or attempt was written and no map was fetched.',
                    'Final body composition, PDF conversion, map acquisition and visual/engineering review remain unverified.']}
    output=Path(output_directory).absolute();output.parent.mkdir(parents=True,exist_ok=True);output.mkdir()
    pending=output/'.pending';pending.mkdir();stage='snapshot'
    try:
        retained=pending/'inputs';retained.mkdir()
        (retained/'production-request.json').write_bytes(raw)
        for key,value in inputs.items():(retained/(key+'.json')).write_bytes(value)
        map_portability=_write_portable_inputs(retained/'portable',request_path,request,inputs,resolved,paths,map_snapshot)
        first=resolved.sheets[0]
        profile={'version':'company-sheet-profile-v1','fields':{},'template':dict(first.template)}
        job={'version':'company-sheet-job-v1','set_id':resolved.set_id,'date':first.fields['date'],
             'revision':first.fields['revision'],'formats':list(resolved.formats),'sheets':[]}
        for row in expanded:
            sheet=row['source'];logos={}
            for name,data in sheet.logos:
                asset=f'assets/{_sha(data)}.png';(retained/'assets').mkdir(exist_ok=True)
                (retained/asset).write_bytes(data);logos[name]=asset
            fields={k:v for k,v in sheet.fields.items() if k not in {'date','revision'}}
            if row['id']==site_id:fields['scale']=selected_site_label
            if single_line and row['id'] in {page['id'] for page in single_line_pages}:fields['scale']='NOT TO SCALE'
            job['sheets'].append({'id':row['id'],'title':row['title'],'fields':fields,
                                  'template':dict(sheet.template),'logos':logos})
        (retained/'portable-profile.json').write_bytes(_json(profile));(retained/'expanded-job.json').write_bytes(_json(job))
        final=resolve_company_sheet_set(retained/'portable-profile.json',retained/'expanded-job.json')
        sheets={s.id:s for s in final.sheets};bodies={}
        stage='site body'
        site_drawing=audit=site_map=None
        if site_result:
            body,site_drawing,composition,audit,site_map=site_result;bodies[site_id]=body
        stage='detail bodies'
        if specs:bodies.update(company_detail_bodies(model,specs,tuple(sheets[sid] for sid in detail_sheet_ids),placements=placements))
        bodies.update({sid:result.body_svg for sid,(result,view) in model_view_results.items()})
        stage='schedule bodies'
        bodies.update({page.spec.id:page.body_svg(layout) for page in pages})
        single_line_map=None;electrical_map=None
        if single_line:
            stage='single-line bodies'
            if electrical_plan is not None:
                from .electrical_sheet import build_electrical_sheet_bodies
                single_line_bodies,electrical_map=build_electrical_sheet_bodies(model,tuple(sheets[p['id']] for p in single_line_pages),plan=electrical_plan)
                single_line_map=electrical_map['single_line']
            else:
                from .single_line.production import build_single_line_bodies
                single_line_bodies,single_line_map=build_single_line_bodies(model,tuple(sheets[p['id']] for p in single_line_pages))
            bodies.update(single_line_bodies)
        stage='cover body'
        sheet_images={}
        final_index=[{'id':s.id,'title':s.title} for s in final.sheets]
        cover_layout=None
        if cover_pagination:
            cover={**cover,'sheets':final_index}
            if final_index!=list(cover_pagination.sheet_index):raise ValueError('final job differs from the stable cover index')
            bodies.update({page.id:page.body_svg for page in cover_pagination.pages})
            sheet_images={page.id:page.images for page in cover_pagination.pages if page.images}
            cover_layout={'pages':[{'id':p.id,'title':p.title,'layout':p.layout} for p in cover_pagination.pages],
                          'sheet_index':cover_pagination.sheet_index,'resolution_passes':cover_pagination.iterations}
        if set(bodies)!=set(sheets):raise ValueError('final content coverage mismatch')
        maps=pending/'derived';maps.mkdir()
        for name,value in [('cover-expanded.json',cover),('cover-layout.json',cover_layout),('site-audit.json',audit),
                           ('site-body.map.json',site_map),('schedule-rows.json',row_map),('schedule-sections.json',[{'sheet_id':p.spec.id,**section} for p in pages for section in (p.sections or ({'schedule_id':p.schedule_id,'schedule_page':p.schedule_page,'schedule_pages':p.schedule_pages},))]),('schedule-layout.json',asdict(layout) if layout else None)]:
            if value is not None:(maps/name).write_bytes(_json(value))
        if map_snapshot is not None:(maps/'cover-map-source.json').write_bytes(_json({'sha256':map_snapshot.sha256,'media_type':'image/png','width_px':map_snapshot.width_px,'height_px':map_snapshot.height_px,'attribution':map_snapshot.attribution,'provenance':map_snapshot.provenance}))
        if single_line_map is not None:(maps/'single-line-topology-layout.json').write_bytes(_json(single_line_map))
        if electrical_map is not None:
            (maps/'electrical-composition.json').write_bytes(_json(electrical_map))
            if electrical_selection is not None:(maps/'electrical-layout-selection.json').write_bytes(_json(electrical_selection))
            (maps/'panel-facts.json').write_bytes(_json(electrical_map['panel_report']))
            (maps/'schedule-sections.json').write_bytes(_json([section for page in electrical_plan.panel_pages for section in page['sections']]))
            (maps/'drawing-schedules.json').write_bytes(_json(electrical_plan.drawing_schedules))
        if site_drawing is not None:(maps/'site-drawing.json').write_text(site_drawing.to_json()+'\n')
        if report is not None:(maps/'panel-facts.json').write_text(report.to_json()+'\n')
        if drawing is not None:(maps/'drawing-schedules.json').write_text(drawing.to_json()+'\n')
        for sid,(result,view) in model_view_results.items():
            (maps/(sid+'.model-view.json')).write_bytes(_json(result.coverage))
            (maps/(sid+'.view.json')).write_bytes(_json(asdict(view)))
        for page in pages:(maps/(page.spec.id+'.drawing.json')).write_text(page.drawing_set.to_json()+'\n')
        stage='shared ordered SVG/PDF production'
        shared=create_company_sheet_set(retained/'portable-profile.json',retained/'expanded-job.json',output_directory=pending/'company',bodies=bodies,images=sheet_images)
        shared_manifest=_decode((shared/'MANIFEST.json').read_bytes())
        if shared_manifest['sheet_order']!=[r['id'] for r in final_index]:raise ValueError('shared output order differs from cover index')
        manifest={'version':request['version'],'sheet_order':shared_manifest['sheet_order'],'cover_index':final_index,'model_id':model.model_id,
                  'model_sha256':_sha(inputs['model']),'status':'supplied_plan_presentation' if single_line else 'partial_plan_presentation','engineering_approval':False,
                  'single_line':{'status':'supplied_topology_only','sheet_produced':True,'sheet_ids':[p['id'] for p in single_line_pages]} if single_line else {'status':'pending_integration','sheet_produced':False},
                  'site_presentation':audit['presentation'] if audit else None,
                  'cover_pagination':{'sheet_ids':[p.id for p in cover_pagination.pages],'resolution_passes':cover_pagination.iterations} if cover_pagination else None,
                  'content_owners':{'cover':[p.id for p in cover_pagination.pages] if cover_pagination else [],'site':[site_id] if site_id else [],'schedules':[p.spec.id for p in pages],'details':detail_sheet_ids,'single_line':[p['id'] for p in single_line_pages]},
                  'schedule_row_count':len(row_map),'shared_delivery':'company/delivery',
                  'portable_request':'inputs/portable/production.json','portable_map_status':map_portability,'runtime':_runtime(),
                  'limitations':['Presentation tools do not certify plan-set completeness or engineering approval.',
                                 'Supplied site drawing evidence is not promoted to survey/model geometry.',
                                 'No demand/service sizing, installation fit or permit approval is inferred.']}
        if electrical_plan is not None:
            manifest['content_owners']['electrical_composition']=list(electrical_plan.sheet_ids)
            manifest['content_owners']['single_line']=[]
            manifest['single_line']['sheet_ids']=[page['id'] for page in electrical_plan.sld_layout['pages']]
            manifest['electrical_composition']={'sheet_ids':list(electrical_plan.sheet_ids),'panel_sheet_ids':[page['sheet_id'] for page in electrical_plan.panel_pages],
                'plan_fingerprint':electrical_plan.fingerprint,'source_model_sha256':electrical_plan.model_sha256,'panel_row_count':len(row_map)}
            if electrical_selection is not None:manifest['electrical_composition']['layout_selection']=electrical_selection
        if v2:
            if single_line is None:manifest['single_line']={'status':'not_requested','sheet_produced':False}
            manifest['content_owners']['model_views']=list(model_view_results)
            manifest['model_views']={sid:result.coverage for sid,(result,view) in model_view_results.items()}
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


def rebuild_production_sheet_set(delivery, *, output_directory):
    """Verify a portable receipt, then rerun the same coordinator offline.

    This is a reproducibility check, not a signature or concurrent-writer lock.
    For intentional edits, use the portable request through the normal command.
    """
    root=Path(delivery).resolve()
    if Path(output_directory).resolve().is_relative_to(root):raise ValueError('rebuild output must be outside the source delivery')
    manifest_path=root/'PRODUCTION-MANIFEST.json'
    if manifest_path.is_symlink():raise ValueError('production manifest symlink refused')
    manifest=_decode(manifest_path.read_bytes())
    if not isinstance(manifest,dict) or manifest.get('portable_request')!='inputs/portable/production.json' or not isinstance(manifest.get('files'),list):
        raise ValueError('delivery lacks a supported portable production receipt')
    def retained_path(name):
        if not isinstance(name,str):raise ValueError('invalid retained path')
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix()!=name:raise ValueError('invalid retained path')
        if any((root/Path(*relative.parts[:n])).is_symlink() for n in range(1,len(relative.parts)+1)):
            raise ValueError('retained path symlinks are refused')
        path=root/relative
        if not path.resolve().is_relative_to(root):raise ValueError('retained path escapes delivery')
        return path
    receipts={}
    for row in manifest['files']:
        if not isinstance(row,dict) or set(row)!={'path','bytes','sha256'}:raise ValueError('invalid production receipt row')
        name=row['path']
        if not isinstance(name,str) or name in receipts:raise ValueError('invalid or duplicate production receipt path')
        if type(row['bytes']) is not int or row['bytes']<0 or not isinstance(row['sha256'],str):raise ValueError('invalid production receipt metadata')
        raw=retained_path(name).read_bytes()
        if len(raw)!=row['bytes'] or _sha(raw)!=row['sha256']:raise ValueError(f'production receipt mismatch: {name}')
        receipts[name]=row
    prefix='inputs/portable/'
    name=prefix+'production.json'
    if name not in receipts:raise ValueError('portable production request is not receipted')
    portable_root=retained_path('inputs/portable')
    for path in portable_root.rglob('*'):
        if path.is_symlink():raise ValueError('portable input symlinks are refused')
        if path.is_file() and path.relative_to(root).as_posix() not in receipts:raise ValueError('unreceipted portable input refused')
    request=_decode(retained_path(name).read_bytes())
    if not isinstance(request,dict):raise ValueError('invalid portable production request')
    expected={'profile':'profile.json','job':'job.json','model':'model.json'}
    for key in ('cover','details'):
        if request.get(key) is not None:expected[key]=key+'.json'
    for key,value in expected.items():
        if request.get(key)!=value or prefix+value not in receipts:raise ValueError(f'portable {key}: unexpected or unreceipted path')
    for key,filename in (('site','site.json'),('schedules','settings.json')):
        row=request.get(key)
        if row is not None:
            if not isinstance(row,dict) or row.get('request')!=filename or prefix+filename not in receipts:
                raise ValueError(f'portable {key}: unexpected or unreceipted path')
            if key=='site' and 'graphics' in row and (row['graphics']!='site-graphics.json' or prefix+'site-graphics.json' not in receipts):
                raise ValueError('portable site graphics: unexpected or unreceipted path')
    map_options=request.get('cover_map')
    if map_options is not None and (not isinstance(map_options,dict) or map_options.get('mode')!='cached' or map_options.get('cache_directory')!='map-cache'):
        raise ValueError('portable rebuild requires the retained offline map cache; live acquisition is refused')
    profile=_decode(retained_path(prefix+'profile.json').read_bytes())
    job=_decode(retained_path(prefix+'job.json').read_bytes())
    if not isinstance(profile,dict) or not isinstance(job,dict) or not isinstance(job.get('sheets'),list):
        raise ValueError('invalid portable profile/job')
    import re
    for owner in [profile,job,*job['sheets']]:
        if not isinstance(owner,dict):raise ValueError('invalid portable logo owner')
        logos=owner.get('logos',{})
        if not isinstance(logos,dict):raise ValueError('invalid portable logos')
        for value in logos.values():
            if value is None:continue
            if not isinstance(value,str) or not re.fullmatch(r'assets/[0-9a-f]{64}\.png',value) or prefix+value not in receipts:
                raise ValueError('portable logo must be a receipted content-addressed asset')
            if receipts[prefix+value]['sha256']!=Path(value).stem:raise ValueError('portable logo asset hash mismatch')
    return create_production_sheet_set(retained_path(name),output_directory=output_directory)


def preflight_production_sheet_set(request_path):
    """Read-only bounded validation; no attempt, network map or output is created."""
    return create_production_sheet_set(request_path, output_directory=None, _check_only=True)


def main(argv=None):
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='read-only bounded request preflight; no output directory')
    parser.add_argument('--rebuild',action='store_true',help='verify delivery receipts and rebuild offline to a fresh output')
    parser.add_argument('request',help='production request JSON, or delivery directory with --rebuild')
    parser.add_argument('output_directory',nargs='?',help='fresh attempt directory')
    args=parser.parse_args(argv)
    if args.check and args.rebuild:parser.error('--check and --rebuild are mutually exclusive')
    if args.check and args.output_directory is not None:parser.error('--check does not take an output directory')
    if not args.check and args.output_directory is None:parser.error('output_directory is required unless --check is used')
    try:
        if args.check:result=preflight_production_sheet_set(args.request)
        elif args.rebuild:result=rebuild_production_sheet_set(args.request,output_directory=args.output_directory)
        else:result=create_production_sheet_set(args.request,output_directory=args.output_directory)
    except (ValueError,OSError,ImportError) as exc:parser.exit(1,str(exc)+'\n')
    print(json.dumps(result,indent=2) if args.check else result);return 0

if __name__=='__main__':raise SystemExit(main())
