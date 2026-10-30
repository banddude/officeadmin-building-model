"""Explicit canonical model plus checked-route request to a retained delivery."""
from __future__ import annotations
import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
from oabm.model import BuildingModel
from .checked_assembly import AssemblyRouteCandidate, NoCheckedAssemblyRoute, route_checked_assembly
from .router import RoutingOptions


def _json(value):
    return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()


def _decode(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate checked routing request key: '+key)
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def deliver_checked_assembly_route(model_path, request_path, *, output_directory):
    """Snapshot inputs and publish a fresh result only when a candidate passes."""
    model_bytes=Path(model_path).read_bytes();request_bytes=Path(request_path).read_bytes()
    model=BuildingModel.from_json(model_bytes);request=_decode(request_bytes)
    keys={'version','start_port_id','end_port_id','route_type','assembly_id','domain_kind',
          'nominal_diameter_m','centerline_radius_m','candidates'}
    if not isinstance(request,dict) or set(request)!=keys or request['version']!='checked-assembly-route-v1':
        raise ValueError('Exact checked-assembly-route-v1 request required')
    if not isinstance(request['candidates'],list) or not 1<=len(request['candidates'])<=8:
        raise ValueError('Supply one to eight candidates')
    option_keys={f.name for f in fields(RoutingOptions)};candidates=[]
    for row in request['candidates']:
        if not isinstance(row,dict) or set(row)!={'id','options'} or not isinstance(row['options'],dict):
            raise ValueError('Candidate requires id and options')
        if not {'clearance_m','port_stub_m'}<=row['options'].keys() or row['options'].keys()-option_keys:
            raise ValueError('Explicit clearance_m and port_stub_m required; unknown routing options refused')
        candidates.append(AssemblyRouteCandidate(row['id'],RoutingOptions(**row['options'])))
    args={key:value for key,value in request.items() if key not in {'version','candidates'}}
    output=Path(output_directory).absolute();output.parent.mkdir(parents=True,exist_ok=True);output.mkdir()
    pending=output/'.pending';pending.mkdir();(pending/'inputs').mkdir()
    (pending/'inputs/model.json').write_bytes(model_bytes)
    (pending/'inputs/request.json').write_bytes(request_bytes)
    try:
        selected=route_checked_assembly(model,**args,candidates=tuple(candidates))
        (pending/'model.json').write_text(selected.model.to_json())
        (pending/'bend-report.json').write_text(selected.bend_report.to_json())
        (pending/'selection.json').write_bytes(_json({
            'version':'checked-assembly-route-v1','selected_candidate_id':selected.selected_candidate_id,
            'route_id':selected.route.id,'attempts':selected.attempts,
            'status':'selected_route_passed_bounded_checks',
            'limitations':['Checks apply to the selected route, not every route in the job.',
                           'No curved swept-clearance, manufacturer-fit or engineering approval.',
                           'No penetrations or circuit reassignment were created.']}))
        records=[{'path':p.relative_to(pending).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                  'bytes':p.stat().st_size} for p in sorted(pending.rglob('*')) if p.is_file()]
        (pending/'MANIFEST.json').write_bytes(_json({'version':'checked-assembly-route-delivery-v1','files':records}))
        result=output/'delivery';pending.rename(result);return result
    except Exception as exc:
        failure={'status':'failed','reason':str(exc)}
        if isinstance(exc,NoCheckedAssemblyRoute):failure['attempts']=exc.attempts
        (output/'failure.json').write_bytes(_json(failure))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model');parser.add_argument('request');parser.add_argument('output_directory')
    args=parser.parse_args(argv)
    print(deliver_checked_assembly_route(args.model,args.request,output_directory=args.output_directory))


if __name__=='__main__':main()
