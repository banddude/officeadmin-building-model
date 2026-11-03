"""Offline source-item accounting for an explicitly inventoried takeoff.

The source bytes are hashed, not interpreted. Completeness of the manually
supplied IDs and correctness of mappings remain the reviewer's responsibility.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import stat
import sys
from importlib.resources import files
from jsonschema import Draft202012Validator
from oabm.model import BuildingModel, ContractError
from oabm.reports.source_coverage import inventory_fingerprint, model_fingerprint, source_coverage
from oabm.routing.physical_capture import SOURCE_BYTES, bounded_source_json
from oabm.routing.physical_verify_types import VerificationStop

_OUTPUT_SCHEMA=Draft202012Validator(json.loads(files("oabm.reports._contracts").joinpath("source-coverage-cli-v1.schema.json").read_text()))
_SOURCE_LIMIT=256*1024*1024
_MAX_ITEMS=2000
_MAX_TARGET_REFERENCES=10000


def _open_regular(path):
    flags=os.O_RDONLY | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('input must be a regular file')
        return os.fdopen(fd,'rb')
    except BaseException:
        os.close(fd)
        raise


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key')
        result[key]=value
    return result


def _read_json_text(path):
    with _open_regular(path) as handle:
        data=handle.read(SOURCE_BYTES+1)
    if len(data)>SOURCE_BYTES:
        raise ValueError('JSON input exceeds the size limit')
    text=bounded_source_json(data)
    json.loads(text,object_pairs_hook=_unique_object,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON number')))
    return text


def _read_json(path):
    return json.loads(_read_json_text(path),object_pairs_hook=_unique_object)


def _source_hash(path):
    digest=hashlib.sha256();total=0
    with _open_regular(path) as handle:
        before=os.fstat(handle.fileno())
        if before.st_size>_SOURCE_LIMIT:raise ValueError('source file exceeds the size limit')
        while True:
            block=handle.read(1024*1024)
            if not block:break
            total+=len(block)
            if total>_SOURCE_LIMIT:raise ValueError('source file exceeds the size limit')
            digest.update(block)
        after=os.fstat(handle.fileno())
    if total==0:raise ValueError('source file is empty')
    if total!=before.st_size:raise ValueError('source changed during reading')
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('source changed during reading')
    return digest.hexdigest()


def prepare(model, source_sha256, object_ids):
    """Return a mapping skeleton; entries must be reviewed and filled in."""
    return {'inventory_sha256':inventory_fingerprint(source_sha256,object_ids),
            'model_sha256':model_fingerprint(model),'entries':[]}


def main(argv=None):
    p=argparse.ArgumentParser(description='Audit a manually inventoried source against a canonical model; no source extraction or completeness claim.')
    p.add_argument('mode',choices=('prepare','audit'))
    p.add_argument('--model',required=True,help='canonical model JSON')
    p.add_argument('--source',required=True,help='source file whose exact bytes are hashed')
    p.add_argument('--inventory',required=True,help='JSON array of manually supplied source IDs')
    p.add_argument('--mapping',help='reviewed mapping JSON, required for audit')
    p.add_argument('--strict',action='store_true',help='reject unresolved inventoried IDs during audit')
    a=p.parse_args(argv)
    if a.mode=='audit' and not a.mapping:p.error('--mapping is required for audit')
    if a.mode=='prepare' and (a.mapping or a.strict):p.error('--mapping and --strict apply only to audit')
    try:
        model=BuildingModel.from_json(_read_json_text(a.model))
        ids=_read_json(a.inventory)
        if not isinstance(ids,list) or not ids or len(ids)>_MAX_ITEMS:raise ValueError('inventory outside supported size')
        digest=_source_hash(a.source)
        if a.mode=='prepare':
            result=prepare(model,digest,ids)
        else:
            mapping=_read_json(a.mapping)
            entries=mapping.get('entries') if isinstance(mapping,dict) else None
            if not isinstance(entries,list) or len(entries)>_MAX_ITEMS:raise ValueError('mapping outside supported size')
            targets=sum(len(entry.get('canonical_ids',[])) for entry in entries if isinstance(entry,dict) and isinstance(entry.get('canonical_ids',[]),list))
            if targets>_MAX_TARGET_REFERENCES:raise ValueError('too many mapping targets')
            report=source_coverage(model,source_sha256=digest,object_ids=ids,mapping=mapping,strict=a.strict)
            result={'version':'source-coverage-cli-v1','strict':a.strict,'report':report,'limitations':[
                'Source IDs are supplied manually; source completeness and extraction are not verified.',
                'Mappings and omissions are reviewer assertions, not independently verified.',
                'Strict mode rejects unresolved inventoried IDs but permits explicit omissions.',
                'This report does not establish design, safety, or code compliance.' ]}
        _OUTPUT_SCHEMA.validate(result)
        sys.stdout.write(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n')
        return 0
    except Exception:
        # Source IDs, private paths and parser snippets must not leak on errors.
        print('Invalid, inaccessible, stale, or unresolved source coverage input.',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
