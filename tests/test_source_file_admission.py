"""Small source-only regressions for descriptor and growing-stream admission."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from oabm.model import ContractError
from oabm.reports.source_files import hash_source_file,read_source_bytes


def test_swapped_path_checks_the_descriptor_actually_opened(tmp_path,monkeypatch):
    source=tmp_path/'small';source.write_bytes(b'ab')
    replacement=tmp_path/'large';replacement.write_bytes(b'0123456789')
    original=os.open
    def swapped(path,flags):
        return original(replacement if Path(path)==source else path,flags)
    monkeypatch.setattr(os,'open',swapped)
    with pytest.raises(ContractError,match='descriptor'):
        hash_source_file(source,max_bytes=4)


def test_growing_opened_file_cannot_bypass_stream_byte_counter(tmp_path,monkeypatch):
    source=tmp_path/'growing';source.write_bytes(b'ab')
    original=os.fstat;grew=False
    def growing(descriptor):
        nonlocal grew
        info=original(descriptor)
        if not grew:
            grew=True
            with source.open('ab') as handle:handle.write(b'0123456789')
        return info
    monkeypatch.setattr(os,'fstat',growing)
    with pytest.raises(ContractError,match='stream'):
        hash_source_file(source,max_bytes=4)


def test_json_fifo_refuses_before_any_blocking_read(tmp_path):
    fifo=tmp_path/'request.json';os.mkfifo(fifo)
    script='''import sys
from oabm.drawings.utility_delivery import _read_json
from oabm.model import ContractError
try: _read_json(sys.argv[1])
except ContractError as exc:
    assert "regular file" in str(exc)
    raise SystemExit(0)
raise SystemExit("FIFO was not refused")
'''
    environment={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')}
    result=subprocess.run([sys.executable,'-c',script,str(fifo)],env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr+result.stdout


def test_bounded_byte_capture_checks_the_replaced_opened_descriptor(tmp_path,monkeypatch):
    source=tmp_path/'small.json';source.write_bytes(b'{}')
    oversized=tmp_path/'large.json';oversized.write_bytes(b'0123456789')
    original=os.open
    def swapped(path,flags):return original(oversized if Path(path)==source else path,flags)
    monkeypatch.setattr(os,'open',swapped)
    with pytest.raises(ContractError,match='descriptor'):read_source_bytes(source,max_bytes=4)


def test_utility_resolver_uses_admitted_json_snapshots_without_reopening_paths(tmp_path,monkeypatch):
    from oabm.drawings.company_sheets import resolve_company_sheet_set
    fixture=Path(__file__).parents[1]/'fixtures/company_sheets/v1'
    profile=(fixture/'profile.json').read_bytes();job=(fixture/'job.json').read_bytes()
    profile_fifo=tmp_path/'profile.json';job_fifo=tmp_path/'job.json'
    os.mkfifo(profile_fifo);os.mkfifo(job_fifo)
    def unexpected_read(path):raise AssertionError('admitted profile/job snapshots must not reopen JSON paths')
    monkeypatch.setattr(Path,'read_bytes',unexpected_read)
    resolved=resolve_company_sheet_set(profile_fifo,job_fifo,_profile_raw=profile,_job_raw=job)
    assert resolved.profile_json==profile and resolved.job_json==job


@pytest.mark.parametrize('role',['request.json','model.json','profile.json','job.json','loads.json'])
def test_utility_entry_refuses_each_json_role_fifo_before_any_delivery(tmp_path,role):
    from test_utility_delivery import utility_inputs
    request=utility_inputs(tmp_path)
    target=tmp_path/role;target.unlink();os.mkfifo(target)
    script='''import sys
from oabm.drawings.utility_delivery import create_utility_delivery
from oabm.model import ContractError
try: create_utility_delivery(sys.argv[1],output_directory=sys.argv[2])
except ContractError as exc:
    assert "regular file" in str(exc)
    raise SystemExit(0)
raise SystemExit("nonregular utility JSON input was accepted")
'''
    environment={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')}
    output=tmp_path/'refused'
    result=subprocess.run([sys.executable,'-c',script,str(request),str(output)],env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr+result.stdout
    assert not output.exists()
