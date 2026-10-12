"""Public synthetic PDF scope observations through an explicit caller decision."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from oabm.model import BuildingModel, ContractError, Provenance, RuleReference
from oabm.reports.scope_ledger import ScopePolicy, ScopeLedger, generate_scope_ledger
from oabm.drawings.scope_ledger import scope_ledger_to_drawing_schedules
from oabm.qa import device_scope_counts
from test_scope_and_takeoff_comparison import _model, _variant, FIXTURE, FIELD_MARKER, LEGEND_R_ROW

ROOT = Path(__file__).resolve().parents[1]


def policy(mode='preserve_unresolved', confidence=.8):
    return ScopePolicy(version='1.0.0', mode=mode,
        basis=RuleReference(authority='synthetic caller', revision='r1', rule_id='scope.unmarked'),
        actor='synthetic reviewer', provenance=(Provenance(source_kind='user', source_id='fixture:policy',
            confidence=confidence, derivation='user', scope_paths=('mode', 'basis', 'actor')),))


@pytest.fixture(scope='module')
def imported():
    return _model(FIXTURE)


def totals(report):
    return {status: sum(g.count for g in report.groups if g.status == status)
            for status in ('new', 'relocated', 'existing_to_remain', 'removed', 'unresolved')}


def test_actual_pdf_ledger_and_adapter(imported):
    original = imported.to_json()
    rule = policy()
    ledger = generate_scope_ledger(imported, policy=rule)
    assert totals(ledger) == device_scope_counts(imported)['totals']
    assert totals(ledger)['new'] == totals(ledger)['existing_to_remain'] == 8
    assert len(ledger.rows) == 16
    assert sum(g.count for g in ledger.groups) == 16
    for row in ledger.rows:
        lane = row.source.entity['attributes']['pdf_electrical']
        assert lane['scope_marker_source_element_id']
        assert len(lane['scope_legend_source_element_ids']) == 2
        assert lane['source_page'] == 1
    decoded = ScopeLedger.from_json(ledger.to_json())
    decoded.validate_against(imported, rule)
    schedules = scope_ledger_to_drawing_schedules(decoded)
    assert len(schedules) == 2
    assert len(schedules[0].rows) == 16
    assert {r.source_id for r in schedules[0].rows} == {r.source.id for r in ledger.rows}
    assert imported.to_json() == original


@pytest.mark.parametrize('case', ['unmarked', 'undefined', 'conflict', 'removed', 'relocated'])
def test_actual_pdf_variants(tmp_path, case):
    edits = []
    append = b''
    if case == 'unmarked':
        edits.append((FIELD_MARKER, FIELD_MARKER.replace(b'(N)', b'( )')))
    elif case == 'conflict':
        append = (b'BT /F1 5.5 Tf 1 0 0 1 596 106 Tm (N) Tj ET\n'
                  b'BT /F1 5.5 Tf 1 0 0 1 608 106 Tm (EXISTING TO REMAIN) Tj ET\n')
    else:
        edits.append((FIELD_MARKER, FIELD_MARKER.replace(b'(N)', b'(R)')))
        if case == 'undefined':
            edits.append((LEGEND_R_ROW, LEGEND_R_ROW.replace(b'EXISTING TO BE REMOVED', b'SEE GENERAL NOTES')))
        if case == 'relocated':
            edits.append((LEGEND_R_ROW, LEGEND_R_ROW.replace(b'EXISTING TO BE REMOVED', b'EXISTING TO BE REMOVED AND SALVAGED FOR RELOCATION')))
    model = _model(_variant(tmp_path, case, *edits, append=append))
    before = model.to_json()
    preserved = generate_scope_ledger(model, policy=policy())
    effective = generate_scope_ledger(model, policy=policy('new_when_confirmed_unmarked'))
    assert totals(preserved) == device_scope_counts(model)['totals']
    if case == 'unmarked':
        changed = [r for r in effective.rows if r.derivation == 'inferred_caller_policy']
        assert len(changed) == 1
        assert changed[0].source.entity['attributes']['pdf_electrical']['scope_status'] == 'unresolved'
        assert changed[0].effective_status == 'new'
        assert changed[0].effective_confidence <= .8
        assert totals(effective)['new'] == totals(preserved)['new'] + 1
    else:
        assert all(r.derivation == 'source_metadata' for r in effective.rows)
        assert totals(effective) == totals(preserved)
        if case in {'removed', 'relocated'}:
            assert totals(effective)[case] == 1
    ScopeLedger.from_json(effective.to_json()).validate_against(model, policy('new_when_confirmed_unmarked'))
    scope_ledger_to_drawing_schedules(effective)
    assert model.to_json() == before


def changed_lane(model, lane):
    device = model.electrical_devices[0]
    updated = replace(device, attributes={**device.attributes, 'pdf_electrical': lane})
    return replace(model, electrical_devices=(updated, *model.electrical_devices[1:]))


@pytest.mark.parametrize('lane', [None, {}, {'scope_status':'invented'}, {'scope_status':True},
    {'scope_status':'unresolved'}, {'scope_status':'unresolved','scope_reason':'scope_marker_undefined','scope_marker':'R'},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_marker':'N'},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_marker':''},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','status':'N'},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_marker_source_element_id':'element:x'},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_legend_source_element_ids':['element:x']},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_legend_text':'NEW'},
    {'scope_status':'new','scope_reason':'no_scope_marker'},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_marker':42},
    {'scope_status':'unresolved','scope_reason':'no_scope_marker','scope_legend_source_element_ids':[{}]},
])
def test_ambiguous_malformed_missing_never_new(imported, lane):
    model = changed_lane(imported, lane)
    row = next(r for r in generate_scope_ledger(model, policy=policy('new_when_confirmed_unmarked')).rows
               if r.source.id == model.electrical_devices[0].id)
    assert row.effective_status == 'unresolved'
    assert row.derivation == 'source_metadata'
    assert row.source.entity['attributes']['pdf_electrical'] == lane


def test_confidence_and_owner_relative_evidence(imported):
    model = changed_lane(imported, {'scope_status':'unresolved','scope_reason':'no_scope_marker'})
    device = model.electrical_devices[0]
    source_record = Provenance(source_kind='pdf',source_id='fixture:weak',confidence=.3,
        derivation='inferred',scope_paths=('attributes.pdf_electrical.scope_reason','attributes.pdf_electrical.scope_status'))
    pose_record = Provenance(source_kind='user',source_id='fixture:pose',confidence=.01,scope_paths=('pose',))
    device = replace(device,confidence=.7,provenance=(source_record,pose_record))
    model = replace(model,electrical_devices=(device,*model.electrical_devices[1:]),provenance=(
        Provenance(source_kind='pdf',source_id='fixture:collection',confidence=.2,scope_paths=('electrical_devices',)),
        Provenance(source_kind='user',source_id='fixture:frame',confidence=.001,scope_paths=('coordinate_system',))))
    rule = policy('new_when_confirmed_unmarked', .1)
    ledger = generate_scope_ledger(model,policy=rule)
    row = next(r for r in ledger.rows if r.source.id == device.id)
    assert row.effective_confidence == .1
    assert row.source.entity['confidence'] == .7
    assert row.source.entity['provenance'][0]['scope_paths'] == sorted(source_record.scope_paths)
    assert ledger.model_source_context.provenance[0].scope_paths == ('electrical_devices',)
    assert ledger.policy.provenance[0].scope_paths == ('actor','basis','mode')
    preserved = generate_scope_ledger(model,policy=policy())
    assert next(r for r in preserved.rows if r.source.id == device.id).effective_confidence == .2


def test_binding_permutation_mutation_and_revisions(imported):
    rule = policy()
    first = generate_scope_ledger(imported,policy=rule)
    permuted = replace(imported,electrical_devices=tuple(reversed(imported.electrical_devices)),
        electrical_equipment=tuple(reversed(imported.electrical_equipment)))
    permuted_rule = replace(rule,provenance=(replace(rule.provenance[0],scope_paths=tuple(reversed(rule.provenance[0].scope_paths))),))
    assert generate_scope_ledger(permuted,policy=permuted_rule).to_json() == first.to_json()
    changed = changed_lane(imported, {'scope_status':'removed'})
    with pytest.raises(ContractError,match='source model/policy'):
        first.validate_against(changed,rule)
    revised = replace(rule,basis=replace(rule.basis,revision='r2'))
    with pytest.raises(ContractError,match='source model/policy'):
        first.validate_against(imported,revised)
    data = first.to_dict()
    data['rows'][0]['source']['entity']['attributes']['forged']=True
    assert first.to_dict() != data
    first.rows[0].source.entity['attributes']['forged']=True
    with pytest.raises(ContractError,match='binding'):
        first.to_dict()


@pytest.mark.parametrize('version',['1.0.0','1.1.0','1.2.0','1.3.0'])
def test_null_name_all_versions(version):
    model = BuildingModel(schema_version=version,model_id='fixture:empty')
    report = generate_scope_ledger(model,policy=policy())
    assert report.model_source_context.name is None
    assert report.rows == report.groups == ()
    ScopeLedger.from_json(report.to_json()).validate_against(model,policy())


@pytest.mark.parametrize('mutate',[
    lambda d:d.update(version='2.0.0'), lambda d:d.update(extra=1),
    lambda d:d.update(rows='x'), lambda d:d.update(groups='x'),
    lambda d:d.update(source_model_fingerprint='z'*64), lambda d:d.update(code_verification_performed=0),
    lambda d:d['policy'].update(mode='default'),lambda d:d['policy'].update(actor=''),
    lambda d:d['policy'].update(provenance='x'),lambda d:d['policy']['basis'].update(revision='r2'),
    lambda d:d['rows'][0].update(effective_status='removed'),
    lambda d:d['rows'][0].update(effective_confidence=True),
    lambda d:d['rows'][0].update(effective_confidence=float('inf')),
    lambda d:d['rows'][0].update(policy_fingerprint='z'*64),
    lambda d:d['rows'][0]['source']['entity'].update(confidence=.1),
    lambda d:d['rows'].append(copy.deepcopy(d['rows'][0])),
    lambda d:d['groups'][0].update(object_ids='x'),lambda d:d['groups'][0].update(count=True),
    lambda d:d['groups'][0].update(count=999),lambda d:d['groups'].reverse(),
])
def test_strict_portability(imported,mutate):
    data = generate_scope_ledger(imported,policy=policy()).to_dict()
    mutate(data)
    with pytest.raises(ContractError):ScopeLedger.from_dict(data)


@pytest.mark.parametrize('bad',[None,42,[], 'value'])
def test_policy_attributes_object(bad):
    with pytest.raises(ContractError):
        replace(policy(),provenance=(replace(policy().provenance[0],attributes=bad),))


def test_constructor_shapes_and_duplicate_json(imported):
    report = generate_scope_ledger(imported,policy=policy())
    for changes in ({'rows':'x'},{'rows':report.rows+report.rows},{'groups':'x'}):
        with pytest.raises(ContractError):replace(report,**changes)
    with pytest.raises(ContractError):replace(policy(),provenance='x')
    with pytest.raises(ContractError):replace(report.groups[0],object_ids='x')
    with pytest.raises(ContractError):ScopePolicy.from_json('{"version":"1.0.0","version":"1.0.0"}')


def test_schemas(imported):
    documents=[json.loads((ROOT/'contracts'/name).read_text()) for name in (
        'oabm-model-v1.3.schema.json','panel-schedule-report-v1.schema.json',
        'scope-policy-v1.schema.json','scope-ledger-v1.schema.json')]
    registry=Registry().with_resources((d['$id'],Resource.from_contents(d)) for d in documents)
    report=generate_scope_ledger(imported,policy=policy())
    Draft202012Validator(documents[-1],registry=registry).validate(report.to_dict())
    Draft202012Validator(documents[-2],registry=registry).validate(policy().to_dict())


def test_default_outputs_unchanged(imported, tmp_path):
    from dataclasses import asdict
    from oabm.ifc import to_ifc
    from oabm.exports.gltf import to_glb
    from oabm.quantities import extract_quantities
    from oabm.drawings import generate_schedules
    def outputs(path):
        to_glb(imported, path)
        return (imported.to_json(), to_ifc(imported).to_string(), path.read_bytes(),
                extract_quantities(imported).to_json(),
                json.dumps([asdict(s) for s in generate_schedules(imported)], sort_keys=True))
    before = outputs(tmp_path/'before.glb')
    report = generate_scope_ledger(imported, policy=policy('new_when_confirmed_unmarked'))
    scope_ledger_to_drawing_schedules(report)
    assert outputs(tmp_path/'after.glb') == before


@pytest.mark.parametrize('upgrade_name',[None,'upgrade_to_v1_1','upgrade_to_v1_2','upgrade_to_v1_3'])
def test_populated_legacy_and_equipment_counts(upgrade_name):
    import oabm.model as canonical
    model=BuildingModel.load(ROOT/'fixtures/model/v1/garage-route.json')
    if upgrade_name:model=getattr(canonical,upgrade_name)(model)
    before=model.to_json()
    report=generate_scope_ledger(model,policy=policy('new_when_confirmed_unmarked'))
    assert totals(report)['unresolved']==len(model.electrical_devices)+len(model.electrical_equipment)
    assert all(r.derivation=='source_metadata' for r in report.rows)
    assert {r.source.collection for r in report.rows}=={'electrical_devices','electrical_equipment'}
    ScopeLedger.from_json(report.to_json()).validate_against(model,policy('new_when_confirmed_unmarked'))
    assert model.to_json()==before
    equipment=replace(model.electrical_equipment[0],attributes={'pdf_electrical':{'scope_status':'relocated'}})
    revised=replace(model,electrical_equipment=(equipment,*model.electrical_equipment[1:]))
    report=generate_scope_ledger(revised,policy=policy())
    group=next(g for g in report.groups if g.collection=='electrical_equipment' and g.status=='relocated')
    assert group.object_ids==(equipment.id,) and group.count==1
    assert totals(report)['new']==0


def test_detached_sources_and_policy_attributes(imported):
    model=changed_lane(imported,{'scope_status':'unresolved','scope_reason':'no_scope_marker'})
    rule=replace(policy('new_when_confirmed_unmarked'),provenance=(
        replace(policy().provenance[0],attributes={'ordered_values':[2,1]}),))
    ledger=generate_scope_ledger(model,policy=rule)
    before=ledger.to_json()
    model.electrical_devices[0].attributes['pdf_electrical']['scope_status']='removed'
    rule.provenance[0].attributes['ordered_values'].reverse()
    assert ledger.to_json()==before
    with pytest.raises(ContractError):ledger.validate_against(model,rule)


def test_legend_evidence_confidence_and_policy_reference(imported):
    device=imported.electrical_devices[0]
    device=replace(device,provenance=(*device.provenance,Provenance(source_kind='pdf',source_id='fixture:legend',
        confidence=.12,scope_paths=('attributes.pdf_electrical.scope_legend_text',))))
    model=replace(imported,electrical_devices=(device,*imported.electrical_devices[1:]))
    report=generate_scope_ledger(model,policy=policy())
    assert next(r for r in report.rows if r.source.id==device.id).effective_confidence==.12
    model=changed_lane(imported,{'scope_status':'unresolved','scope_reason':'no_scope_marker'})
    rule=replace(policy('new_when_confirmed_unmarked'),provenance=(
        Provenance(source_kind='user',source_id='fixture:action',confidence=.8,scope_paths=('mode','actor')),
        Provenance(source_kind='user',source_id='fixture:rule',confidence=.09,scope_paths=('basis.revision',))))
    report=generate_scope_ledger(model,policy=rule)
    assert next(r for r in report.rows if r.source.id==model.electrical_devices[0].id).effective_confidence==.09
