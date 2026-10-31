"""Native receipt -> exact-scale shared company SVG/PDF sheets, locally."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from .blank_set import _decode,_json,_sha
from .company_sheets import resolve_company_sheet_set,render_company_sheet_svg,create_company_sheet_set,assert_company_delivery_matches
from .native_vectors import native_view_asset

VERSION='native-review-sheet-set-v1'


def _plan(request_path):
    path=Path(request_path);raw=path.read_bytes();request=_decode(raw)
    if not isinstance(request,dict) or set(request)!={'version','profile','job','native_delivery','sheets'} or request['version']!=VERSION:
        raise ValueError('exact native-review-sheet-set-v1 request required')
    def local(key):
        value=request[key]
        if not isinstance(value,str) or not value:raise ValueError('explicit local '+key+' path required')
        return path.parent/value
    profile,job,native=local('profile'),local('job'),local('native_delivery')
    resolved=resolve_company_sheet_set(profile,job)
    native=native.resolve();receipt_raw=(native/'receipt.json').read_bytes();receipt=_decode(receipt_raw)
    if receipt.get('status')!='review_only_success' or not isinstance(receipt.get('files'),dict):raise ValueError('completed receipted native delivery required')
    snapshot={'receipt.json':receipt_raw}
    for name,digest in receipt['files'].items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix()!=name:raise ValueError('invalid native retained path')
        source=native/relative
        if any((native/Path(*relative.parts[:n])).is_symlink() for n in range(1,len(relative.parts)+1)) or not source.resolve().is_relative_to(native):raise ValueError('native retained symlink/escape refused')
        data=source.read_bytes()
        if _sha(data)!=digest:raise ValueError('native retained file receipt mismatch')
        snapshot[name]=data
    rows=request['sheets']
    if not isinstance(rows,list) or [r.get('sheet_id') for r in rows if isinstance(r,dict)]!=[s.id for s in resolved.sheets]:
        raise ValueError('native sheet owners must exactly match ordered shared job')
    vectors={};bodies={};bindings={}
    for row,sheet in zip(rows,resolved.sheets,strict=True):
        if set(row)!={'sheet_id','views','body'} or not isinstance(row['views'],list):raise ValueError('native sheet: exact sheet_id/views/body required')
        assets=[]
        for index,view in enumerate(row['views']):
            if not isinstance(view,dict) or set(view)!={'view_id','origin_mm'}:raise ValueError('native view: exact view_id/origin_mm required')
            asset=native_view_asset(native,view['view_id'],origin_mm=view['origin_mm'],asset_id=sheet.id+'-'+str(index))
            if json.loads(asset.provenance_json)['native_receipt_sha256']!=_sha(receipt_raw):raise ValueError('native delivery changed during snapshot')
            view_path=receipt['views'][view['view_id']]['svg']
            if asset.svg_bytes!=snapshot[view_path]:raise ValueError('native view changed during snapshot')
            scale=json.loads(asset.provenance_json)['scale'].replace('/',':')
            if sheet.fields.get('scale','').upper().replace(' ','') not in {'ASNOTED',scale}:
                raise ValueError('shared titleblock scale must match native view or explicitly say AS NOTED')
            assets.append(asset)
        body=row['body']
        if body is not None:
            if not isinstance(body,str) or not body:raise ValueError('body: explicit local SVG body path or null required')
            bodies[sheet.id]=(path.parent/body).read_text()
        if not assets and sheet.id not in bodies:raise ValueError('empty native sheets are not a completed delivery')
        vectors[sheet.id]=tuple(assets)
        render_company_sheet_svg(sheet,body_svg=bodies.get(sheet.id),vectors=assets)
        bindings[sheet.id]=[json.loads(a.provenance_json) for a in assets]
    return raw,request,profile,job,snapshot,resolved,vectors,bodies,bindings


def preflight_native_sheet_set(request_path):
    *_,resolved,vectors,bodies,bindings=_plan(request_path)
    return {'status':'presentation_preflight_passed','sheet_order':[s.id for s in resolved.sheets],
            'native_views':bindings,'limitations':['PDF conversion and independent visual acceptance remain unverified.']}


def create_native_sheet_set(request_path, *, output_directory):
    raw,request,profile,job,snapshot,resolved,vectors,bodies,bindings=_plan(request_path)
    attempt=Path(output_directory).absolute();attempt.mkdir(parents=True,exist_ok=False)
    pending=attempt/'.pending';pending.mkdir()
    try:
        company=create_company_sheet_set(profile,job,output_directory=pending/'company',bodies=bodies,vectors=vectors)
        assert_company_delivery_matches(company,resolved)
        # The shared renderer is the sole SVG/PDF composition/assembly path.
        for name,data in snapshot.items():
            dest=pending/'native'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
        inputs=pending/'inputs';inputs.mkdir();(inputs/'request.json').write_bytes(raw)
        portable=dict(request,profile='../company/delivery/inputs/profile-portable.json',job='../company/delivery/inputs/job-portable.json',native_delivery='../native')
        portable['sheets']=[]
        for row in request['sheets']:
            item=dict(row)
            if item['body'] is not None:
                body_name='bodies/'+row['sheet_id']+'.svg';dest=inputs/body_name;dest.parent.mkdir(exist_ok=True);dest.write_text(bodies[row['sheet_id']]);item['body']=body_name
            portable['sheets'].append(item)
        (inputs/'portable.json').write_bytes(_json(portable))
        manifest={'version':'native-review-sheet-delivery-v1','status':'review_only','engineering_approval':False,
                  'sheet_order':[s.id for s in resolved.sheets],'native_view_bindings':bindings,
                  'shared_delivery':'company/delivery','files':{p.relative_to(pending).as_posix():_sha(p.read_bytes()) for p in sorted(pending.rglob('*')) if p.is_file()},
                  'limitations':['Original native viewport retained at unchanged scale; original native clipping remains.',
                                 'Source graph preservation and canonical interpretation are separate qualified proofs.',
                                 'Visual review and agency/engineering acceptance are not established by generation.']}
        (pending/'NATIVE-SHEET-MANIFEST.json').write_bytes(_json(manifest))
        os.rename(pending,attempt/'delivery');return attempt/'delivery'
    except Exception as exc:
        (attempt/'failure.json').write_bytes(_json({'status':'failed','exception':type(exc).__name__,'reason':str(exc)}));raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');parser.add_argument('request',type=Path);parser.add_argument('output',type=Path,nargs='?');args=parser.parse_args()
    if args.check:
        if args.output:parser.error('--check takes no output')
        print(json.dumps(preflight_native_sheet_set(args.request),indent=2))
    else:
        if not args.output:parser.error('fresh output required')
        print(create_native_sheet_set(args.request,output_directory=args.output))


if __name__=='__main__':main()
