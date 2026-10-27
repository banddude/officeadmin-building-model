"""Local single-line package using the shared company sheet production tool."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
from oabm.model import BuildingModel
from .topology import project_topology, json_bytes
from .layout import plan_pages
from .render import render_body


def build_single_line_bodies(model, sheets):
    """Resolve first-sheet body bounds; require exact ordered page identities.

    Call plan_pages(project_topology(model), template.drawing_bounds_mm) first
    to obtain the deterministic page list for the shared job request. Every
    generated sheet must use the same body bounds; no fit-to-page scaling.
    """
    sheets=tuple(sheets)
    if not sheets:
        raise ValueError('single-line requires at least one resolved shared company sheet')
    topology=project_topology(model)
    layout=plan_pages(topology,sheets[0].drawing_bounds_mm)
    expected=[p['id'] for p in layout['pages']]
    if [s.id for s in sheets]!=expected:
        raise ValueError('single-line shared job sheets must be exactly this ordered deterministic page list: '+', '.join(expected))
    if any(tuple(s.drawing_bounds_mm)!=tuple(sheets[0].drawing_bounds_mm) for s in sheets):
        raise ValueError('single-line v1 requires identical drawing bounds on all sheets')
    bodies={}
    maps=[]
    for page,sheet in zip(layout['pages'],sheets):
        body,mapping=render_body(topology,layout,page,sheet)
        bodies[sheet.id]=body
        maps.append(mapping)
    occurrences=[row for mapping in maps for row in mapping['terminal_occurrences']]
    for edge in topology.connections:
        endpoints=[row['endpoint'] for row in occurrences if row['connection_id']==edge['id']]
        if sorted(endpoints)!=['load','source']:
            raise ValueError(f'{edge["id"]}: incomplete terminal pair')
    return bodies, {'kind':'oabm.single-line','version':'1.0.0',
        'engineering_approval':False,'topology':topology.to_dict(),'layout':layout,'sheets':maps}


def create_single_line_set(model_path, profile_path, job_path, *, output_directory):
    """Fresh atomic package; exact source snapshots and a complete hash manifest.

    Uses company_sheets without modifying its profile/titleblock/PDF behavior.
    Validation happens before creating the attempt. Failed rendering retains a
    pending attempt; only a complete verified package becomes delivery/.
    """
    from ..company_sheets import resolve_company_sheet_set, create_company_sheet_set
    model_raw=Path(model_path).read_bytes()
    model=BuildingModel.from_dict(json.loads(model_raw))
    resolved=resolve_company_sheet_set(profile_path,job_path)
    bodies,document=build_single_line_bodies(model,resolved.sheets)
    attempt=Path(output_directory).absolute()
    attempt.mkdir(parents=True,exist_ok=False)
    pending=attempt/'.pending'
    pending.mkdir()
    try:
        (pending/'model.json').write_bytes(model_raw)
        (pending/'topology-layout.json').write_bytes(json_bytes(document))
        for key,body in bodies.items():
            (pending/(key+'.body.svg')).write_text(body)
        shared=create_company_sheet_set(profile_path,job_path,output_directory=pending/'company-sheets',bodies=bodies)
        if (Path(shared)/'inputs/profile.json').read_bytes()!=resolved.profile_json or (Path(shared)/'inputs/job.json').read_bytes()!=resolved.job_json:
            raise ValueError('shared profile/job changed during generation; retry with stable inputs')
        for sheet in resolved.sheets:
            for _,raw in sheet.logos:
                asset=Path(shared)/('inputs/assets/'+hashlib.sha256(raw).hexdigest()+'.png')
                if not asset.is_file() or asset.read_bytes()!=raw:
                    raise ValueError('shared logo bytes changed during generation; retry with stable inputs')
        if not Path(shared).is_dir() or not list(Path(shared).rglob('*.svg')):
            raise ValueError('shared company renderer returned no SVG delivery')
        files=[]
        for path in sorted(pending.rglob('*')):
            if path.is_file():
                data=path.read_bytes()
                files.append({'path':path.relative_to(pending).as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        manifest={'kind':'oabm.single-line-package','version':'1.0.0','status':'complete_supplied_facts_only',
                  'engineering_approval':False,'sheet_order':list(bodies),
                  'model_sha256':hashlib.sha256(model_raw).hexdigest(),'files':files}
        for row in files:
            data=(pending/row['path']).read_bytes()
            if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
                raise ValueError('single-line output changed before publication')
        (pending/'MANIFEST.json').write_bytes(json_bytes(manifest))
        os.rename(pending,attempt/'delivery')
        return attempt/'delivery'
    except Exception as exc:
        (attempt/'failure.json').write_bytes(json_bytes({'status':'failed','error':str(exc)}))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model',type=Path)
    parser.add_argument('profile',type=Path)
    parser.add_argument('job',type=Path)
    parser.add_argument('output_directory',type=Path)
    args=parser.parse_args(argv)
    try:
        path=create_single_line_set(args.model,args.profile,args.job,output_directory=args.output_directory)
    except (ValueError,OSError,ImportError) as exc:
        parser.exit(1,str(exc)+'\n')
    print(path)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
