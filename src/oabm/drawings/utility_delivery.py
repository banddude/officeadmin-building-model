"""One-command private utility delivery from canonical site/service/load facts."""
from __future__ import annotations
import argparse
from dataclasses import replace
from decimal import Decimal,localcontext
from fractions import Fraction
import hashlib
import json
import io
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from oabm.model import BuildingModel,ContractError,Point2D,Provenance
from oabm.model.model import _decode_dataclass
from oabm.electrical.service_site import move_plan_service_node
from oabm.reports.physical_routes import _json
from oabm.reports.source_files import hash_source_file,read_source_bytes
from oabm.reports.service_demand import bind_service_demand_inputs,validate_service_demand_report
from oabm.routing.physical_capture import bounded_source_json,SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop
from .company_sheets import resolve_company_sheet_set,render_company_sheet_svg,_pdf,_rows
from .model_utility import render_model_site_body,_source_bindings
from .single_line import project_topology,plan_pages
from .single_line.production import build_single_line_bodies
from .blank_set import _identifier

PDF_BYTES = 10_000_000
PACKAGE_BYTES = 128*1024*1024
MAX_DOCUMENT_PAGES = 20
SVG='http://www.w3.org/2000/svg'


def _sha(raw):return hashlib.sha256(raw).hexdigest()


def _read_json(path):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ContractError('duplicate utility input JSON key')
            result[key]=value
        return result
    raw=read_source_bytes(path,max_bytes=SOURCE_BYTES)
    return raw,json.loads(bounded_source_json(raw),object_pairs_hook=unique)


def _keys(value,required,optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):
        raise ContractError('utility request has missing or unsupported fields')


def _filename(value):
    if not isinstance(value,str) or len(value)>180 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]*\.pdf',value,re.IGNORECASE):
        raise ContractError('utility PDF filename must use only letters/numbers/periods/hyphens/spaces/underscores and end.pdf')
    return value


def _exact_number(value):
    """Readable exact terminating decimal, otherwise an explicit rational."""
    number=Fraction(value);denominator=number.denominator
    for factor in (2,5):
        while denominator%factor==0:denominator//=factor
    if denominator!=1:return str(number)
    with localcontext() as context:
        context.prec=len(str(abs(number.numerator)))+len(str(number.denominator))+10
        text=format(Decimal(number.numerator)/Decimal(number.denominator),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _calculation_rows(report):
    arithmetic=report['arithmetic'];policy=report['inputs']['general_demand'];scenario=report['inputs']['sensitivity']
    groups=arithmetic['group_connected_va_exact']
    sources={group:sorted({row['source_id'] for row in report['inputs']['components'] if row['group']==group}) for group in groups}
    def number(value):return _exact_number(value)
    general=number(report['general_connected_va_exact']);threshold=number(arithmetic['first_threshold_va_exact'])
    first=number(arithmetic['first_applied_va_exact']);remainder=number(arithmetic['remainder_connected_va_exact'])
    factor=number(arithmetic['remainder_factor_exact']);remainder_demand=number(arithmetic['remainder_demand_va_exact'])
    general_demand=number(report['general_demand_va_exact']);hvac=number(groups['hvac']);floor=number(groups['floor_heat'])
    total=number(report['no_supplemental_heat_va_exact']);additional=number(arithmetic['sensitivity_additional_va_exact'])
    scenario_total=number(report['sensitivity_va_exact']);voltage=number(arithmetic['voltage_v_exact'])
    all_sources=sorted(set(source for values in sources.values() for source in values)|{policy['source_id']})
    raw=[
        ('general-connected','General connected VA',general+' VA',sources['general'],'Sum supplied general component VA'),
        ('first-threshold','Supplied first-block threshold',threshold+' VA',[policy['source_id']],'Supplied threshold; not inferred'),
        ('factor','Supplied remainder factor',factor,[policy['source_id']],'Supplied multiplier; not inferred'),
        ('first-applied','First block applied',first+' VA',sources['general']+[policy['source_id']],f'min({general}, {threshold}) = {first} VA'),
        ('remainder-connected','General connected remainder',remainder+' VA',sources['general']+[policy['source_id']],f'max(0, {general} - {threshold}) = {remainder} VA'),
        ('remainder-demand','Remainder demand',remainder_demand+' VA',[policy['source_id']],f'{remainder} x {factor} = {remainder_demand} VA'),
        ('general-demand','General demand subtotal',general_demand+' VA',sources['general']+[policy['source_id']],f'{first} + {remainder_demand} = {general_demand} VA'),
        ('hvac','Supplied HVAC subtotal',hvac+' VA',sources['hvac'],'Sum supplied HVAC component VA'),
        ('floor-heat','Supplied floor-heat subtotal',floor+' VA',sources['floor_heat'],'Sum supplied floor-heat component VA'),
        ('base-va','Base demand total',total+' VA',all_sources,f'{general_demand} + {hvac} + {floor} = {total} VA'),
        ('voltage','Supplied voltage basis',voltage+' V',[report['service_node_id']],'Supplied profile; reconciled with known placed-owner voltage'),
        ('base-current','Base demand current',f'{report["no_supplemental_heat_a"]:.2f} A',all_sources,
            f'{total} VA / {voltage} V; exact A = {number(report["no_supplemental_heat_a_exact"])}'),
        ('sensitivity-addition','Supplied sensitivity addition',additional+' VA',[scenario['source_id']],'Explicit additional scenario demand'),
        ('sensitivity-total','Sensitivity demand total',scenario_total+' VA',all_sources+[scenario['source_id']],f'{total} + {additional} = {scenario_total} VA'),
        ('sensitivity-current','Sensitivity current',f'{report["sensitivity_a"]:.2f} A',all_sources+[scenario['source_id']],
            f'{scenario_total} VA / {voltage} V; exact A = {number(report["sensitivity_a_exact"])}'),
    ]
    return [(identity,[identity,label,'calculation',value,', '.join(sorted(set(source_ids))),formula]) for identity,label,value,source_ids,formula in raw]


def _load_bodies(report,template,reserved):
    """Measured, paginated source rows; no shrink-to-fit or silent truncation."""
    body_left,body_top,body_right,body_bottom=template.drawing_bounds_mm
    # The shared renderer checks actual ink, including text safety padding and
    # half the table stroke. Keep both inside its unchanged external bounds.
    inset=2.
    left,top,right,bottom=body_left+inset,body_top+inset,body_right-inset,body_bottom-inset
    if right-left<=30 or bottom-top<=110:raise ContractError('load template has insufficient inset content capacity')
    widths=[(right-left)*fraction for fraction in (.14,.20,.10,.10,.12,.24)]
    headers=['Record ID','Calculation / supplied load','Group','Value / unit','Source ID','Numeric formula / source basis']
    header_lines=[_rows(value,width-4,2.5,700) for value,width in zip(headers,widths)]
    header_height=max(len(lines) for lines in header_lines)*3.7+3
    if header_height>15:raise ContractError('load column headings exceed measured header capacity')
    records=[]
    for section,rows in (
        ('NUMERIC DEMAND CALCULATION',[(identity,values,'calculation') for identity,values in _calculation_rows(report)]),
        ('SUPPLIED LOAD COMPONENTS',[(row['id'],[row['id'],row['name'],row['group'],str(row['va'])+' VA',row['source_id'],
            row['basis']+(' [ASSUMPTION]' if row['assumption'] else '')],'component') for row in report['inputs']['components']])):
        records.append((section,None,8.,'section'))
        for identity,values,kind in rows:
            wrapped=[_rows(value,width-4,2.5) for value,width in zip(values,widths)]
            height=max(1,max(len(lines) for lines in wrapped))*3.7+4
            if height>bottom-top-108:raise ContractError('load record exceeds one measured page; no row is truncated')
            records.append((identity,wrapped,height,kind))
    pages=[];pending=[];used=0.;capacity=bottom-top-100
    for index,record in enumerate(records):
        required=record[2]+(records[index+1][2] if record[3]=='section' else 0)
        if pending and used+required>capacity:pages.append(pending);pending=[];used=0.
        pending.append(record);used+=record[2]
    if pending:pages.append(pending)
    if not pages or len(pages)>MAX_DOCUMENT_PAGES:raise ContractError('load schedule page count exceeds admission')
    sheets=[];bodies={};maps=[]
    for index,page in enumerate(pages):
        identity=template.id if index==0 else f'{template.id}-{index+1}'
        _identifier(identity,'load sheet identity')
        if identity in reserved:raise ContractError('load continuation sheet identity collides with another document')
        sheet=replace(template,id=identity,title=template.title if index==0 else template.title+' (CONTINUED)')
        root=ET.Element('{'+SVG+'}g');y=top+5
        def text(x,y,value,size=2.5,weight=400):
            ET.SubElement(root,'{'+SVG+'}text',x=str(x),y=str(y),**{'font-size':str(size),'font-family':'Liberation Sans','font-weight':str(weight)}).text=value
        for line in _rows('SUPPLIED SERVICE DEMAND / SOURCE COMPONENTS',right-left,4,700):
            text(left,y,line,4,700);y+=6
        y+=1
        summary=[f'Service owner: {report["service_node_id"]}; supplied voltage: {report["inputs"]["voltage_v"]} V',
            f'No supplemental heat: {report["no_supplemental_heat_a"]:.2f} A; sensitivity: {report["sensitivity_a"]:.2f} A',
            'Demand is separate from connected circuit VA. Supplied grouping/factors/allowances; no automatic NEC or engineering approval.',
            f'Model SHA256: {report["source_model_sha256"]}',
            'General demand policy: '+report['inputs']['general_demand']['basis'],
            'Sensitivity: '+report['inputs']['sensitivity']['basis']]
        for value in summary:
            for line in _rows(value,right-left,2.5):text(left,y,line);y+=4
        if y>top+75:raise ContractError('load report summary exceeds measured header capacity')
        y=top+80;x=left
        for lines,width in zip(header_lines,widths):
            for line_number,line in enumerate(lines):text(x+2,y+line_number*3.7,line,2.5,700)
            x+=width
        y+=header_height
        for record_id,wrapped,height,kind in page:
            if kind=='section':
                text(left+2,y+5,record_id,3,700);y+=height;continue
            x=left
            for lines,width in zip(wrapped,widths):
                ET.SubElement(root,'{'+SVG+'}rect',x=str(x),y=str(y),width=str(width),height=str(height),fill='none',stroke='#333333',**{'stroke-width':'.15'})
                for line_number,line in enumerate(lines):text(x+2,y+4+line_number*3.7,line)
                x+=width
            maps.append({('component_id' if kind=='component' else 'calculation_id'):record_id,
                         'record_kind':kind,'sheet_id':sheet.id,'bounds_mm':[left,y,right,y+height]})
            y+=height
        if y>bottom-5:raise ContractError('load table exceeds actual sheet bounds')
        sheets.append(sheet);bodies[identity]=ET.tostring(root,encoding='unicode')
    return tuple(sheets),bodies,maps


def _render_pages(pdf_path,*,document,write,remaining):
    try:import pypdfium2
    except ImportError as exc:raise ContractError('utility page proof requires the existing tables/PDF-render runtime (pypdfium2)') from exc
    receipt=[]
    with pypdfium2.PdfDocument(pdf_path) as pdf:
        if not 1<=len(pdf)<=MAX_DOCUMENT_PAGES:raise ContractError('utility PDF page count exceeds admission')
        for index in range(len(pdf)):
            page=pdf[index]
            try:
                width,height=page.get_size()
                if width*height*1.5**2>25_000_000:raise ContractError('utility page rendering exceeds pixel admission')
                bitmap=page.render(scale=1.5)
                try:
                    image=bitmap.to_pil()
                    try:
                        class CappedBuffer(io.BytesIO):
                            def write(self,data):
                                if self.tell()+len(data)>remaining():raise ContractError('utility PNG exceeds remaining package admission')
                                return super().write(data)
                        name=f'page-images/{document}-{index+1:03d}.png'
                        with CappedBuffer() as buffer:
                            image.save(buffer,format='PNG');raw=buffer.getvalue()
                        write(name,raw)
                    finally:image.close()
                finally:bitmap.close()
                receipt.append({'page':index+1,'path':name,'sha256':_sha(raw),'width_pt':width,'height_pt':height})
            finally:page.close()
    return receipt


def create_utility_delivery(request_path,*,output_directory):
    """Prepare all three PDFs, then reserve a fresh local delivery attempt.

    No upload, utility submission or public sharing is performed. The caller owns
    source interpretation and subsequent visual/engineering review.
    """
    output=Path(output_directory).absolute()
    if output.exists() or output.is_symlink():raise FileExistsError(output)
    request_path=Path(request_path).absolute();request_raw,request=_read_json(request_path)
    _keys(request,{'version','model','profile','job','sources','load_inputs','site','single_line','loads','documents'},{'field_edit'})
    if request['version']!='model-utility-set-v1':raise ContractError('unsupported utility delivery request')
    def path(value):
        if not isinstance(value,str) or not value:raise ContractError('utility input path must be explicit')
        return request_path.parent/value
    _keys(request['site'],{'sheet_id','drawing'},{'dimension_offset_mm'})
    _keys(request['single_line'],{'sheet_id','service_only'})
    _keys(request['loads'],{'sheet_id'})
    _keys(request['documents'],{'site','single_line','loads'})
    names={key:_filename(value) for key,value in request['documents'].items()}
    if len({name.casefold() for name in names.values()})!=3:raise ContractError('three distinct utility PDF filenames required')
    if not isinstance(request['sources'],dict):raise ContractError('utility source mapping required')
    sources={key:path(value) for key,value in request['sources'].items()}
    model_path=path(request['model']);model_raw,_=_read_json(model_path);model=BuildingModel.from_json(model_raw)
    profile_path,job_path=path(request['profile']),path(request['job'])
    profile_raw,_=_read_json(profile_path);job_raw,_=_read_json(job_path)
    load_path=path(request['load_inputs']);load_raw,load_profile=_read_json(load_path)
    if 'field_edit' in request:
        event=request['field_edit'];_keys(event,{'node_id','x_m','y_m','provenance'})
        model=move_plan_service_node(model,event['node_id'],Point2D(x=event['x_m'],y=event['y_m']),
            field_evidence=_decode_dataclass(Provenance,event['provenance']))
    logo_sources={}
    def logo_reader(path):
        raw=read_source_bytes(path,max_bytes=8*1024*1024);identity=str(Path(path).absolute());digest=_sha(raw)
        if identity in logo_sources and logo_sources[identity]!=digest:raise ContractError('logo source changed during utility input capture')
        logo_sources[identity]=digest
        return raw
    resolved=resolve_company_sheet_set(profile_path,job_path,_profile_raw=profile_raw,_job_raw=job_raw,_logo_reader=logo_reader)
    if 'pdf' not in resolved.formats:raise ContractError('utility job must explicitly request PDF output')
    if resolved.profile_json!=profile_raw or resolved.job_json!=job_raw:raise ContractError('company profile/job changed while resolving utility inputs')
    by_id={sheet.id:sheet for sheet in resolved.sheets}
    ids={key:request[key]['sheet_id'] for key in ('site','single_line','loads')}
    for identity in ids.values():_identifier(identity,'utility template sheet identity')
    if len(set(ids.values()))!=3 or set(ids.values())!=set(by_id):raise ContractError('utility job must contain exactly the three explicit document templates')
    site_body,site_receipt=render_model_site_body(model,by_id[ids['site']],source_files=sources,
        drawing=request['site']['drawing'],dimension_offset_mm=request['site'].get('dimension_offset_mm',8.))
    topology=project_topology(model,service_only=request['single_line']['service_only'])
    template=by_id[ids['single_line']]
    layout=plan_pages(topology,template.drawing_bounds_mm,first_sheet_id=template.id,reserved_sheet_ids=[ids['site'],ids['loads']])
    if not 1<=len(layout['pages'])<=MAX_DOCUMENT_PAGES:raise ContractError('single-line document page count exceeds admission')
    line_sheets=tuple(replace(template,id=row['id'],title=template.title if index==0 else template.title+' (CONTINUED)') for index,row in enumerate(layout['pages']))
    for sheet in line_sheets:_identifier(sheet.id,'single-line sheet identity')
    line_bodies,line_receipt=build_single_line_bodies(model,line_sheets,layout=layout,service_only=request['single_line']['service_only'])
    demand=bind_service_demand_inputs(model,load_profile,source_files=sources)
    load_sheets,load_bodies,load_map=_load_bodies(demand,by_id[ids['loads']],{ids['site'],*[sheet.id for sheet in line_sheets]})
    groups={'site':((by_id[ids['site']],),{ids['site']:site_body}),
            'single_line':(line_sheets,line_bodies),'loads':(load_sheets,load_bodies)}
    pdfs={};svgs={};pdf_receipts={}
    for role,(sheets,bodies) in groups.items():
        rendered=[render_company_sheet_svg(sheet,body_svg=bodies[sheet.id]) for sheet in sheets]
        raw,pdf_receipts[role]=_pdf(sheets,rendered,'MODEL UTILITY '+role.upper())
        if not raw.startswith(b'%PDF-') or len(raw)>PDF_BYTES:raise ContractError('utility PDF exceeds10MB portal limit or has invalid signature')
        pdfs[role]=raw
        for sheet,svg in zip(sheets,rendered):svgs[sheet.id+'.svg']=svg.encode()
    output.mkdir(exist_ok=False);pending=output/'.pending';pending.mkdir()
    total=0
    def write(name,raw):
        nonlocal total
        total+=len(raw)
        if total>PACKAGE_BYTES:raise ContractError('utility delivery exceeds package byte admission; no output truncated')
        target=pending/name;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as handle:handle.write(raw)
    try:
        for role,raw in pdfs.items():write(names[role],raw)
        for name,raw in svgs.items():write('sheets/'+name,raw)
        for name,raw in {'request.json':request_raw,'model-original.json':model_raw,'model.json':model.to_json().encode(),
                         'profile.json':profile_raw,'job.json':job_raw,'load-inputs.json':load_raw}.items():write('inputs/'+name,raw)
        for sheet in resolved.sheets:
            for _,raw in sheet.logos:
                name='assets/'+_sha(raw)+'.png'
                if not (pending/name).exists():write(name,raw)
        for name,document in {'site-projection.json':site_receipt,'single-line.json':line_receipt,'load-report.json':demand,'load-rows.json':load_map}.items():
            write('derived/'+name,(_json(document)+'\n').encode())
        page_proofs={}
        for role in groups:
            page_proofs[role]=_render_pages(pending/names[role],document=role,write=write,remaining=lambda:PACKAGE_BYTES-total)
        _source_bindings(model,sources)
        validate_service_demand_report(model,_json(demand),source_files=sources)
        for input_path,raw in ((request_path,request_raw),(model_path,model_raw),(profile_path,profile_raw),(job_path,job_raw),(load_path,load_raw)):
            if hash_source_file(input_path)!=_sha(raw):raise ContractError('utility input changed during generation')
        for path,digest in logo_sources.items():
            if hash_source_file(path,max_bytes=8*1024*1024)!=digest:raise ContractError('utility logo source changed during generation')
        files=[{'path':file.relative_to(pending).as_posix(),'bytes':file.stat().st_size,'sha256':hash_source_file(file)}
               for file in sorted(pending.rglob('*')) if file.is_file()]
        manifest={'kind':'model_utility_delivery','version':'1.0.0','source_request_sha256':_sha(request_raw),
            'source_model_sha256':site_receipt['projection']['model_sha256'],'engineering_approval':False,
            'visual_review':'pending human comparison to source architect sheet','documents':{
                role:{'filename':names[role],'bytes':len(pdfs[role]),'sha256':_sha(pdfs[role]),'pages':pdf_receipts[role]['pages']} for role in groups},
            'page_proofs':page_proofs,'service_scope':topology.to_dict().get('coverage',{'selection_mode':'whole_canonical_electrical_graph'}),
            'source_logo_sha256':logo_sources,
            'unresolved_measurements':site_receipt['projection']['unresolved_measurements'],'files':files}
        write('MANIFEST.json',(_json(manifest)+'\n').encode())
        os.rename(pending,output/'delivery')
        return output/'delivery'
    except Exception as exc:
        with (output/'failure.json').open('x') as handle:handle.write(_json({'status':'failed','error':str(exc),'delivery_published':False}))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',type=Path)
    parser.add_argument('output_directory',type=Path,help='Fresh private local attempt directory')
    args=parser.parse_args(argv)
    try:delivery=create_utility_delivery(args.request,output_directory=args.output_directory)
    except (ValueError,OSError,ImportError,VerificationStop) as exc:parser.exit(2,f'utility delivery: {exc}\n')
    print(delivery)
    return 0


if __name__=='__main__':raise SystemExit(main())
