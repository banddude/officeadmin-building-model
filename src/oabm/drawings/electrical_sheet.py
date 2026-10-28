"""Compose existing single-line and panel-report producers from one model.

No alternative electrical engine: topology, feeder grids and paired panel cards
remain owned by their existing modules. Explicit paper regions keep their ink
and identities separate; shared company-sheet export owns publication.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, replace
import copy
import hashlib
import html
import json
import re
from .body_regions import SheetBodyPart,compose_body_regions
from .schedule_set import panel_schedule_drawing_set
from .schedule_layout import ScheduleLayout,draft_panel_schedule_pages,draft_panel_schedule_selection
from .single_line import project_topology,plan_pages
from .single_line.production import build_single_line_bodies


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def _model_sha(model):return hashlib.sha256(model.to_json(indent=None).encode()).hexdigest()


@dataclass(frozen=True)
class ElectricalSheetPlan:
    model_sha256: str
    first_sheet_id: str
    sheet_ids: tuple[str,...]
    sheet_kinds: tuple[str,...]
    drawing_bounds_mm: tuple[float,float,float,float]
    sld_layout: dict
    panel_pages: tuple[dict,...]
    panel_report: dict
    drawing_schedules: dict
    panel_region_mm: tuple[float,float,float,float]
    panel_selection: dict
    qualification_body: str
    fingerprint: str = ''


def _fingerprint(plan):
    value=asdict(plan);value.pop('fingerprint');return _sha(value)


def _page_id(first,ordinal):
    if ordinal==0:return first
    match=re.fullmatch(r'(.*?)([0-9]+)',first)
    return (match[1]+str(int(match[2])+ordinal).zfill(len(match[2]))) if match else first+'-'+str(ordinal+1)


def plan_electrical_sheets(model,sheet,*,single_line_regions,panel_region_mm=None,panel_width_mm=300.,layout=None,reserved_sheet_ids=(),panel_policy="listed_only",panel_placement="continuation"):
    """Compile two existing producers without omitting feeder or panel facts."""
    if not isinstance(single_line_regions,dict) or set(single_line_regions)!={'diagram','notes','feeder_register'}:
        raise ValueError('electrical composition requires diagram, notes and feeder_register regions; delegated facts are unsupported')
    topology=project_topology(model)
    sld=plan_pages(topology,sheet.drawing_bounds_mm,first_sheet_id=sheet.id,reserved_sheet_ids=reserved_sheet_ids,regions=single_line_regions)
    if sld.get('delegated_feeder_connection_ids'):raise ValueError('electrical composition cannot omit feeder register coverage')
    report,drawing=panel_schedule_drawing_set(model)
    if panel_policy not in {'listed_only','include_empty','all_reported'} or panel_placement not in {'shared','continuation'}:
        raise ValueError('electrical panel policy/placement unsupported')
    if panel_placement=='continuation':
        if panel_region_mm is not None:raise ValueError('full-width panel continuations require panel_region_mm=null')
        panel_region_mm=tuple(sheet.drawing_bounds_mm)
    elif panel_region_mm is None:raise ValueError('shared panel placement requires an explicit paper region')
    if panel_policy in {'listed_only','include_empty'}:
        selected_panels=tuple(group.panel.id for group in report.panels if group.circuits or panel_policy=='include_empty')
        flags={'panel_ids':selected_panels,'include_empty_panels':panel_policy=='include_empty','include_unassigned':False}
    else:
        flags={'panel_ids':None,'include_empty_panels':True,'include_unassigned':True}
    audit=draft_panel_schedule_selection(report,**flags)
    selected_ids=set(audit['selected_schedule_ids'])
    pages=draft_panel_schedule_pages(report,drawing,sheet,layout=layout or ScheduleLayout(),
        region_bounds_mm=panel_region_mm,table_width_mm=panel_width_mm,**flags)
    expected=sorted((schedule.id,n,row.source_id) for schedule in drawing.schedules if schedule.id in selected_ids for n,row in enumerate(schedule.rows))
    actual=sorted((row['schedule_id'],row['row_index'],row['source_id']) for page in pages for row in page.row_map)
    if actual!=expected:raise ValueError('electrical composition lost or duplicated selected canonical panel rows')
    selection={'policy':panel_policy,'placement':panel_placement,'source_selection_audit':audit,'selected_schedule_ids':sorted(selected_ids),
        'not_rendered':[{'schedule_id':schedule.id,'source_ids':[row.source_id for row in schedule.rows],
            'reason':'not a selected panel with listed circuits; retained in full source report, feeder facts remain in register'}
            for schedule in drawing.schedules if schedule.id not in selected_ids]}
    count=(len(sld['pages'])+len(pages)) if panel_placement=='continuation' else max(len(sld['pages']),len(pages))
    ids=tuple(_page_id(sheet.id,n) for n in range(count))
    if any(len(sid)>64 or sid.casefold() in {x.casefold() for x in reserved_sheet_ids} for sid in ids):
        raise ValueError('electrical continuation ID collides or exceeds sheet-ID capacity')
    if tuple(page['id'] for page in sld['pages'])!=ids[:len(sld['pages'])]:raise ValueError('electrical producer sheet numbering differs')
    offset=len(sld['pages']) if panel_placement=='continuation' else 0
    kinds=tuple('combined' if n<len(sld['pages']) and offset<=n<offset+len(pages) else 'single_line' if n<len(sld['pages']) else 'panel_schedules' for n in range(count))
    l,t,r,b=sheet.drawing_bounds_mm
    qualification=''
    if panel_policy in {'listed_only','include_empty'}:
        empty=sum(not group.circuits for group in report.panels)
        prefix=(f'{empty} panelboards without listed circuits' if panel_policy=='listed_only' else 'empty panel registers explicitly requested')
        phrase=f'SUPPLIED MODEL: {prefix}; {len(report.unassigned_circuits)} circuits outside panel groups retained in feeder register. No spare/demand inference.'
        qualification=f'<g><text x="{l+3:.12g}" y="{b-17.5:.12g}" font-family="Liberation Sans" font-size="2.5" fill="#000000">{html.escape(phrase)}</text></g>'
    compiled=[]
    for ordinal,page in enumerate(pages):
        sid=ids[offset+ordinal]
        region=tuple(panel_region_mm)
        if panel_placement=='shared' and ordinal>=len(sld['pages']):
            # Existing typed paper placement moves a table-only continuation
            # to the top. Its local geometry, scale, cells and row identities
            # are unchanged; no SVG rewriting or fit-to-page rescaling.
            delta=sheet.drawing_bounds_mm[1]-region[1]
            def shifted(row):
                box=row['bounds_mm'];return {**row,'bounds_mm':[box[0],box[1]+delta,box[2],box[3]+delta]}
            page=replace(page,placement=replace(page.placement,y_mm=page.placement.y_mm+delta),
                row_map=tuple(shifted(row) for row in page.row_map),sections=tuple(shifted(row) for row in page.sections))
            region=(region[0],region[1]+delta,region[2],region[3]+delta)
        compiled.append({'sheet_id':sid,'body_svg':page.body_svg(layout or ScheduleLayout()),
            'row_map':[{**row,'sheet_id':sid} for row in page.row_map],
            'sections':[{**section,'sheet_id':sid} for section in page.sections],
            'source_spec_id':page.spec.id,'region_mm':region,'placement':asdict(page.placement),'drawing_set':page.drawing_set.to_dict()})
    plan=ElectricalSheetPlan(_model_sha(model),sheet.id,ids,kinds,tuple(sheet.drawing_bounds_mm),sld,tuple(compiled),
        report.to_dict(),drawing.to_dict(),tuple(panel_region_mm),selection,qualification)
    return replace(plan,fingerprint=_fingerprint(plan))


def build_electrical_sheet_bodies(model,sheets,*,plan):
    """Render sealed plans using the same model and shared actual sheet bounds."""
    sheets=tuple(sheets)
    if _model_sha(model)!=plan.model_sha256 or _fingerprint(plan)!=plan.fingerprint:
        raise ValueError('electrical composition plan/model changed; plan again from retained inputs')
    if tuple(s.id for s in sheets)!=plan.sheet_ids or any(tuple(s.drawing_bounds_mm)!=plan.drawing_bounds_mm for s in sheets):
        raise ValueError('electrical composition shared sheets/bounds differ from sealed plan')
    sld_count=len(plan.sld_layout['pages'])
    bodies,sld_map=build_single_line_bodies(model,sheets[:sld_count],layout=copy.deepcopy(plan.sld_layout),sheet_index=list(plan.sheet_ids))
    panel_by_id={page['sheet_id']:page for page in plan.panel_pages}
    pages={page['id']:page for page in plan.sld_layout['pages']}
    merged={};coverage={}
    l,t,r,b=plan.drawing_bounds_mm
    for sheet in sheets:
        parts=[]
        if sheet.id in bodies:
            regions={**pages[sheet.id]['effective_regions'],'footer':(l,b-14,r,b)}
            parts.append(SheetBodyPart('single_line',bodies[sheet.id],regions,
                {'model_sha256':plan.model_sha256,'connection_ids':sorted(path['connection_id'] for row in sld_map['sheets'] if row['sheet_id']==sheet.id for path in row['connection_paths'])}))
        if sheet.id in panel_by_id:
            panel=panel_by_id[sheet.id]
            parts.append(SheetBodyPart('panel_schedules',panel['body_svg'],{'panels':tuple(panel['region_mm'])},
                {'model_sha256':plan.model_sha256,'rows':panel['row_map'],'sections':panel['sections']}))
        if plan.qualification_body and sheet.id in bodies:
            parts.append(SheetBodyPart('scope_qualification',plan.qualification_body,{'qualification':(l,b-22,r,b-16)},
                {'model_sha256':plan.model_sha256,'panel_selection':plan.panel_selection}))
        composed=compose_body_regions(parts,drawing_bounds_mm=sheet.drawing_bounds_mm,clearance_mm=1.)
        merged[sheet.id]=composed.body_svg;coverage[sheet.id]=composed.coverage
    if _model_sha(model)!=plan.model_sha256:raise ValueError('electrical producer mutated the canonical model')
    return merged,{'source_model_sha256':plan.model_sha256,'plan_fingerprint':plan.fingerprint,
        'sheet_ids':list(plan.sheet_ids),'sheet_kinds':list(plan.sheet_kinds),'single_line':sld_map,'panel_report':plan.panel_report,
        'panel_pages':list(plan.panel_pages),'panel_selection':plan.panel_selection,'part_coverage':coverage,
        'limitations':['Supplied facts only; no engineering approval.',
            'Panel-only continuations retain table dimensions/scale and align at the top; composition still requires independent drafting review.']}
