"""Offline CLI tests use only synthetic model facts and temporary files."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from oabm.electrical.manual_device_intake import admit_manual_devices, manual_intake_request_sha256
from oabm.electrical.manual_preliminary_design import manual_design_decision_sha256
from oabm.model import BuildingModel
from oabm.reports.source_coverage import model_fingerprint
from test_manual_device_intake import item
from test_manual_preliminary_design import setup


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'oabm.electrical.manual_design_cli', *map(str, args)],
                          capture_output=True, text=True, timeout=25)


@pytest.fixture
def data(tmp_path):
    base, intake, decision = setup()
    model_path = tmp_path / 'model.json'
    model_path.write_text(base.to_json(), encoding='utf-8')
    intake_path = write_json(tmp_path / 'intake.json', intake)
    unsigned_path = write_json(tmp_path / 'unsigned.json', {k: v for k, v in intake.items() if k != 'request_sha256'})
    decision_path = write_json(tmp_path / 'decision.json', decision)
    return base, intake, decision, model_path, intake_path, unsigned_path, decision_path


def design_args(data, output, digest=None):
    _, _, decision, model, intake, _, choice = data
    return ('design', '--model', model, '--intake', intake, '--decision', choice,
            '--decision-sha256', digest or manual_design_decision_sha256(decision), '--output', output)


def test_all_modes_private_deterministic_bundle_and_no_input_mutation(data, tmp_path):
    base, intake, decision, model, request, unsigned, choice = data
    snapshots = {p: p.read_bytes() for p in (model, request, unsigned, choice)}
    for args, expected in [
        (('model-digest', '--model', model), model_fingerprint(base)),
        (('intake-digest', '--intake', unsigned), intake['request_sha256']),
        (('decision-digest', '--decision', choice), manual_design_decision_sha256(decision)),
    ]:
        result = cli(*args)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
        assert result.stderr == ''
    prep = tmp_path / 'prep.json'
    p = cli('prepare', '--model', model, '--intake', request, '--output', prep)
    assert p.returncode == 0, p.stderr
    receipt = json.loads(prep.read_bytes())
    assert receipt['version'] == 'manual-intake-preparation-cli-v1'
    assert receipt['report']['source_authentication'] == 'not_verified'
    assert receipt['report']['registration'] == 'not_verified'
    assert receipt['report']['applied_to_original'] is False
    assert any(row['status'] == 'unresolved' for row in receipt['report']['items'])
    outputs = [tmp_path / 'result1.json', tmp_path / 'result2.json']
    for output in outputs:
        p = cli(*design_args(data, output))
        assert p.returncode == 0, p.stderr
        assert not p.stdout and not p.stderr
        assert stat.S_IMODE(output.stat().st_mode) & 0o177 == 0
    assert stat.S_IMODE(prep.stat().st_mode) & 0o177 == 0
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    bundle = json.loads(outputs[0].read_bytes())
    assert bundle['version'] == 'manual-preliminary-design-cli-v1'
    assert bundle['report']['source_observed'] is False
    assert bundle['report']['applied_to_original'] is False
    assert bundle['report']['decision_authentication'] == 'not_verified'
    assert bundle['report']['unresolved_items']
    assert bundle['report']['limitations']
    result = BuildingModel.from_dict(bundle['designed_model'])
    assert result.circuits and result.routes
    assert model_fingerprint(result) == bundle['report']['designed_model_sha256']
    assert {p: p.read_bytes() for p in snapshots} == snapshots
    assert not list(tmp_path.glob('.oabm-manual-*.tmp'))


@pytest.mark.parametrize('which', ['decision_digest', 'decision_candidate', 'intake_digest', 'model_binding', 'groups'])
def test_stale_and_incomplete_bindings_never_publish(data, tmp_path, which):
    _, intake, decision, model, request, _, choice = data
    intake, decision = copy.deepcopy(intake), copy.deepcopy(decision)
    digest = manual_design_decision_sha256(decision)
    if which == 'decision_digest':
        decision['decision_id'] = 'changed'
    elif which == 'decision_candidate':
        decision['candidate_model_sha256'] = '0' * 64
        digest = manual_design_decision_sha256(decision)
    elif which == 'intake_digest':
        intake['request_sha256'] = '0' * 64
    elif which == 'model_binding':
        intake['base_model_sha256'] = '0' * 64
        intake['request_sha256'] = manual_intake_request_sha256(**{k: v for k, v in intake.items() if k != 'request_sha256'})
    else:
        decision['groups'] = [[decision['groups'][0][0]]]
        digest = manual_design_decision_sha256(decision)
    write_json(request, intake)
    write_json(choice, decision)
    output = tmp_path / 'failure.json'
    result = cli(*design_args(data, output, digest))
    assert result.returncode != 0 and not output.exists()
    assert not list(tmp_path.glob('.oabm-manual-*.tmp'))


@pytest.mark.parametrize('content', [b'{', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'\xff', b'[' * 70 + b']' * 70, b' ' * (1024 * 1024 + 1)])
def test_bad_json_is_rejected_without_leaking_content_or_path(data, tmp_path, content):
    private = tmp_path / 'private-path-sentinel.json'
    private.write_bytes(content + (b' sentinel-private-content' if content == b'{' else b''))
    result = cli('decision-digest', '--decision', private)
    assert result.returncode != 0
    assert not result.stdout
    assert 'sentinel' not in result.stderr and str(tmp_path) not in result.stderr
    assert 'Traceback' not in result.stderr


def test_output_no_overwrite_input_alias_or_symlink(data, tmp_path):
    _, _, _, model, _, _, _ = data
    before = model.read_bytes()
    result = cli(*design_args(data, model))
    assert result.returncode != 0 and model.read_bytes() == before
    existing = tmp_path / 'existing.json'
    existing.write_bytes(b'preserve')
    result = cli(*design_args(data, existing))
    assert result.returncode != 0 and existing.read_bytes() == b'preserve'
    link = tmp_path / 'dangling.json'
    link.symlink_to(tmp_path / 'missing.json')
    result = cli(*design_args(data, link))
    assert result.returncode != 0 and link.is_symlink() and not (tmp_path / 'missing.json').exists()
    assert not list(tmp_path.glob('.oabm-manual-*.tmp'))


def test_symlink_fifo_and_directory_inputs_fail_without_hanging(data, tmp_path):
    model = data[3]
    link = tmp_path / 'model-link'
    link.symlink_to(model)
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    for bad in (link, fifo, tmp_path):
        result = cli('model-digest', '--model', bad)
        assert result.returncode != 0 and not result.stdout


def test_atomic_link_failure_cleans_temporary_and_final(data, tmp_path, monkeypatch, capsys):
    from oabm.electrical import manual_design_cli
    output = tmp_path / 'unpublished.json'
    def fail(*args, **kwargs):
        raise OSError('private path and secret')
    monkeypatch.setattr(manual_design_cli.os, 'link', fail)
    assert manual_design_cli.main(list(map(str, design_args(data, output)))) != 0
    assert not output.exists()
    assert not list(tmp_path.glob('.oabm-manual-*.tmp'))
    printed = capsys.readouterr()
    assert not printed.out and 'secret' not in printed.err and str(tmp_path) not in printed.err


def test_five_admitted_devices_refused(data, tmp_path):
    base, intake, decision, _, request, _, choice = data
    intake, decision = copy.deepcopy(intake), copy.deepcopy(decision)
    for n in (3, 4, 5):
        row = item(n)
        row['port_direction'] = {'x': -1, 'y': 0, 'z': 0}
        intake['items'].append(row)
    intake['request_sha256'] = manual_intake_request_sha256(**{k: v for k, v in intake.items() if k != 'request_sha256'})
    _, receipt = admit_manual_devices(base, **intake)
    decision['candidate_model_sha256'] = receipt['candidate_model_sha256']
    decision['intake_request_sha256'] = receipt['request_sha256']
    write_json(request, intake)
    write_json(choice, decision)
    prep = tmp_path / 'five-preparation.json'
    prepared = cli('prepare', '--model', data[3], '--intake', request, '--output', prep)
    assert prepared.returncode == 0, prepared.stderr
    rows = json.loads(prep.read_bytes())['report']['items']
    assert sum(row['status'] == 'admitted' for row in rows) == 5
    output = tmp_path / 'five.json'
    result = cli(*design_args(data, output, manual_design_decision_sha256(decision)))
    assert result.returncode != 0 and not output.exists()


def test_extract_model_roundtrip_and_bundle_tamper_refusal(data, tmp_path):
    output=tmp_path/'bundle.json'
    produced=cli(*design_args(data,output))
    assert produced.returncode==0,produced.stderr
    extracted=tmp_path/'extracted.json'
    result=cli('extract-model','--bundle',output,'--output',extracted)
    assert result.returncode==0,result.stderr
    model=BuildingModel.from_json(extracted.read_text())
    bundle=json.loads(output.read_text())
    assert model_fingerprint(model)==bundle['report']['designed_model_sha256']
    assert stat.S_IMODE(extracted.stat().st_mode) & 0o177 == 0
    digest=cli('model-digest','--model',extracted)
    assert digest.returncode==0 and digest.stdout.strip()==bundle['report']['designed_model_sha256']
    bundle['designed_model']['model_id']='model:tampered'
    output.write_text(json.dumps(bundle))
    rejected=tmp_path/'refused.json'
    bad=cli('extract-model','--bundle',output,'--output',rejected)
    assert bad.returncode!=0 and not rejected.exists()
    before=extracted.read_bytes()
    repeated=cli('extract-model','--bundle',output,'--output',extracted)
    assert repeated.returncode!=0 and extracted.read_bytes()==before


def test_extract_uses_original_physical_number_tokens(data, tmp_path):
    from physical_route_fixture import make_physical_model
    original=make_physical_model()
    raw=original.to_json().replace('"centerline_radius_m": 0.3',
                                    '"centerline_radius_m": 0.30000000000000000001')
    assert raw != original.to_json()
    # This forged consistency digest matches the float-rounded model. Exact
    # canonical token admission must still reject the off-grid authored value.
    valid_bundle=tmp_path/'valid-design.json'
    produced=cli(*design_args(data,valid_bundle))
    assert produced.returncode==0,produced.stderr
    report=json.loads(valid_bundle.read_text())['report']
    report['designed_model_sha256']=model_fingerprint(original)
    bundle='{"version":"manual-preliminary-design-cli-v1","designed_model":'+raw+\
           ',"report":'+json.dumps(report)+'}'
    source=tmp_path/'physical-bundle.json';source.write_text(bundle)
    output=tmp_path/'must-not-exist.json'
    result=cli('extract-model','--bundle',source,'--output',output)
    assert result.returncode!=0 and not output.exists() and not result.stdout


def test_extract_reordered_escaped_keys_and_malformed_report(data, tmp_path):
    bundle_path=tmp_path/'design-bundle.json'
    assert cli(*design_args(data,bundle_path)).returncode==0
    bundle=json.loads(bundle_path.read_text())
    # The member scanner must recognize escaped, reordered top-level keys.
    text=' { "report": '+json.dumps(bundle['report'])+', "designed_\\u006dodel": '+\
         json.dumps(bundle['designed_model'])+', "version": "manual-preliminary-design-cli-v1" } '
    bundle_path.write_text(text)
    model_path=tmp_path/'extracted.json'
    good=cli('extract-model','--bundle',bundle_path,'--output',model_path)
    assert good.returncode==0,good.stderr
    bad_bundle=json.loads(text)
    del bad_bundle['report']['limitations']
    bundle_path.write_text(json.dumps(bad_bundle))
    bad_out=tmp_path/'bad-report.json'
    bad=cli('extract-model','--bundle',bundle_path,'--output',bad_out)
    assert bad.returncode!=0 and not bad_out.exists()


def test_extract_rejects_duplicate_model_key_and_oversized_bundle(data, tmp_path):
    bundle_path=tmp_path/'bundle.json'
    assert cli(*design_args(data,bundle_path)).returncode==0
    source=bundle_path.read_text()
    assert source.startswith('{')
    bundle_path.write_text(source[:-1]+',"designed_model":{}}')
    out=tmp_path/'duplicate-output.json'
    assert cli('extract-model','--bundle',bundle_path,'--output',out).returncode!=0
    assert not out.exists()
    bundle_path.write_bytes(b' '*(8*1024*1024+1))
    assert cli('extract-model','--bundle',bundle_path,'--output',out).returncode!=0
    assert not out.exists()


@pytest.mark.parametrize('depth,success', [(61, True), (62, False)])
def test_bundle_is_readable_or_not_published_at_depth_boundary(data,tmp_path,depth,success):
    from dataclasses import replace
    base,intake,decision,model_path,request_path,_,decision_path=data
    nested='value'
    for _ in range(depth): nested=[nested]
    base=replace(base,attributes={'nested':nested})
    model_path.write_text(base.to_json())
    intake=copy.deepcopy(intake)
    intake['base_model_sha256']=model_fingerprint(base)
    intake['request_sha256']=manual_intake_request_sha256(**{k:v for k,v in intake.items() if k!='request_sha256'})
    _,receipt=admit_manual_devices(base,**intake)
    decision=copy.deepcopy(decision)
    decision['candidate_model_sha256']=receipt['candidate_model_sha256']
    decision['intake_request_sha256']=receipt['request_sha256']
    write_json(request_path,intake);write_json(decision_path,decision)
    bundle=tmp_path/'deep-bundle.json'
    result=cli('design','--model',model_path,'--intake',request_path,'--decision',decision_path,
        '--decision-sha256',manual_design_decision_sha256(decision),'--output',bundle)
    if success:
        assert result.returncode==0,result.stderr
        model_out=tmp_path/'deep-extracted.json'
        extracted=cli('extract-model','--bundle',bundle,'--output',model_out)
        assert extracted.returncode==0,extracted.stderr
    else:
        assert result.returncode!=0 and not bundle.exists()
        assert not list(tmp_path.glob('.oabm-manual-*.tmp'))


def test_cleanup_failure_after_publication_reports_uncertain_outcome(data,tmp_path,monkeypatch,capsys):
    from oabm.electrical import manual_design_cli
    output=tmp_path/'published.json'
    def fail_unlink(*args,**kwargs): raise OSError('private sentinel')
    monkeypatch.setattr(manual_design_cli.os,'unlink',fail_unlink)
    result=manual_design_cli.main(list(map(str,design_args(data,output))))
    assert result==3 and output.exists()
    assert json.loads(output.read_text())['version']=='manual-preliminary-design-cli-v1'
    assert stat.S_IMODE(output.stat().st_mode) & 0o177 == 0
    assert len(list(tmp_path.glob('.oabm-manual-*.tmp')))==1
    messages=capsys.readouterr()
    assert not messages.out and 'sentinel' not in messages.err and str(tmp_path) not in messages.err
    assert 'Output was created' in messages.err
