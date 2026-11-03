import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def invoke(*args):
    return subprocess.run([sys.executable,'-m','oabm.reports.source_coverage_cli',*map(str,args)],
                          text=True,capture_output=True,timeout=10)


def setup(tmp_path):
    model=ROOT/'fixtures/model/v1/garage-route.json'
    source=tmp_path/'synthetic-takeoff.dat';source.write_bytes(b'synthetic takeoff source')
    inventory=tmp_path/'ids.json';inventory.write_text(json.dumps(['item:a','item:b']))
    base=['--model',model,'--source',source,'--inventory',inventory]
    prepared=invoke('prepare',*base);assert prepared.returncode==0,prepared.stderr
    return model,source,inventory,base,json.loads(prepared.stdout)


def test_prepare_and_audit_reports_unresolved_then_complete(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    document=json.loads(model.read_text()); entity=document['electrical_devices'][0]['id']
    mapping['entries']=[{'source_id':'item:a','canonical_ids':[entity]}]
    path=tmp_path/'mapping.json';path.write_text(json.dumps(mapping))
    result=invoke('audit',*base,'--mapping',path);assert result.returncode==0,result.stderr
    assert [r['status'] for r in json.loads(result.stdout)['report']['rows']]==['mapped','unresolved']
    assert invoke('audit',*base,'--mapping',path,'--strict').returncode==2
    mapping['entries'].append({'source_id':'item:b','omission_reason':'outside reviewed scope'})
    path.write_text(json.dumps(mapping)); result=invoke('audit',*base,'--mapping',path,'--strict')
    assert result.returncode==0 and [r['status'] for r in json.loads(result.stdout)['report']['rows']]==['mapped','omitted']


def test_changed_source_or_inventory_invalidates_mapping(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    path=tmp_path/'mapping.json';path.write_text(json.dumps(mapping))
    source.write_bytes(b'different synthetic source')
    result=invoke('audit',*base,'--mapping',path)
    assert result.returncode==2 and str(tmp_path) not in result.stderr


def test_fifo_input_rejected_without_blocking(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    fifo=tmp_path/'fifo';os.mkfifo(fifo)
    result=invoke('prepare','--model',model,'--source',fifo,'--inventory',inventory)
    assert result.returncode==2 and 'fifo' not in result.stderr


def test_duplicate_inventory_and_invalid_mapping_fail_closed(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    inventory.write_text('["item:a","item:a"]')
    assert invoke('prepare',*base).returncode==2


def test_duplicate_json_keys_and_empty_inventory_are_rejected(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    inventory.write_text('[]'); assert invoke('prepare',*base).returncode==2
    inventory.write_text('["item:a"]'); mapping_path=tmp_path/'mapping.json'
    mapping_path.write_text('{"entries":[],"entries":[],"inventory_sha256":"x","model_sha256":"x"}')
    assert invoke('audit',*base,'--mapping',mapping_path).returncode==2


def test_empty_source_and_symlink_are_rejected(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    source.write_bytes(b''); assert invoke('prepare',*base).returncode==2
    source.write_bytes(b'private synthetic bytes')
    link=tmp_path/'link';link.symlink_to(source)
    if hasattr(os,'O_NOFOLLOW'):
        result=invoke('prepare','--model',model,'--source',link,'--inventory',inventory)
        assert result.returncode==2 and 'private synthetic bytes' not in result.stdout+result.stderr


def test_inventory_limit_rejects_before_accounting(tmp_path):
    model,source,inventory,base,mapping=setup(tmp_path)
    inventory.write_text(json.dumps([f'item:{i}' for i in range(2001)]))
    assert invoke('prepare',*base).returncode==2


def test_output_schema_is_packaged_and_matches_public_copy():
    from importlib.resources import files
    from oabm.reports.source_coverage_cli import _OUTPUT_SCHEMA
    published=json.loads((ROOT/'contracts/source-coverage-cli-v1.schema.json').read_text())
    packaged=json.loads(files('oabm.reports._contracts').joinpath('source-coverage-cli-v1.schema.json').read_text())
    assert published==packaged
    assert _OUTPUT_SCHEMA.is_valid({'inventory_sha256':'a'*64,'model_sha256':'b'*64,'entries':[]})


def test_schema_rejects_strict_unresolved_report(tmp_path):
    from oabm.reports.source_coverage_cli import _OUTPUT_SCHEMA
    model,source,inventory,base,mapping=setup(tmp_path)
    path=tmp_path/'mapping.json';path.write_text(json.dumps(mapping))
    result=invoke('audit',*base,'--mapping',path);report=json.loads(result.stdout)
    assert _OUTPUT_SCHEMA.is_valid(report)
    report['strict']=True
    assert not _OUTPUT_SCHEMA.is_valid(report)
