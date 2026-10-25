"""Explicit synthetic support station screening, never engineering approval."""
from dataclasses import replace
import json
import pytest

from oabm.model import BuildingModel, ContractError, Point3, Polyline3D, Provenance, RuleReference, SupportSelectionOutcome
from oabm.reports.support_coverage import (SupportSpacingRule, SupportStation,
    report_support_coverage, support_model_fingerprint)
from test_shared_supports import case as make_support_case, selected

PROV = (Provenance(source_kind='synthetic', source_id='fixture:coverage', derivation='user'),)
BASIS = RuleReference(authority='synthetic-policy', revision='one', rule_id='spacing-screen')


@pytest.fixture
def model():
    return selected(make_support_case.__wrapped__())


def inputs(model, end=2.1):
    rules = tuple(SupportSpacingRule(route_id=r.id, route_type=r.route_type, basis=BASIS,
                                   max_span_m=3, max_end_distance_m=end, provenance=PROV) for r in model.routes)
    contacts = tuple(SupportStation(route_id=r.id, component_id=next(c.id for c in model.support_components
        if c.part_key in ('part:clip-a', 'part:clip-b') and c.pose.position.y == r.centerline.points[0].y),
        distance_m=2, provenance=PROV) for r in model.routes)
    return rules, contacts


def run(model, rules=None, stations=None):
    defaults = inputs(model)
    return report_support_coverage(model, expected_model_sha256=support_model_fingerprint(model),
        rules=defaults[0] if rules is None else rules, stations=defaults[1] if stations is None else stations)


def test_valid_screen_preserves_model_quantities_and_does_not_certify(model):
    from oabm.quantities import extract_quantities
    before, quantities = model.to_json(), extract_quantities(model)
    result = run(model)
    assert result['status'] == 'within_supplied_limits'
    assert all(r['route_length_m'] == pytest.approx(4) for r in result['routes'])
    assert all(len(r['accepted_stations']) == 1 for r in result['routes'])
    assert not result['physical_contact_verified']
    assert not result['attachment_capacity_verified']
    assert not result['code_compliance_verified']
    assert not result['installation_completeness_verified']
    assert model.to_json() == before and extract_quantities(model) == quantities
    assert run(model) == result


def test_missing_rules_and_stations_remain_unresolved(model):
    missing = run(model, rules=(), stations=())
    assert missing['status'] == 'unresolved'
    for row in missing['routes']:
        assert row['reason_codes'] == ['spacing_rule_missing', 'support_stations_missing']
        assert row['unlocated_model_span_m'] == pytest.approx(4)
        assert row['gaps'] == []


def test_supplied_end_distance_violation_reports_exact_spans(model):
    rules, contacts = inputs(model, end=1)
    result = run(model, rules, contacts)
    assert result['status'] == 'gaps_found'
    for row in result['routes']:
        assert [(g['from_m'], g['to_m'], g['limit_m']) for g in row['gaps']] == [(0, 2, 1), (2, 4, 1)]


def test_envelope_check_cannot_accept_arbitrary_station(model):
    rules, contacts = inputs(model)
    wrong = replace(contacts[0], distance_m=.5)
    result = run(model, rules, (wrong, contacts[1]))
    row = next(r for r in result['routes'] if r['route_id'] == wrong.route_id)
    assert row['status'] == 'unresolved'
    assert 'station_outside_support_envelope' in row['reason_codes']
    assert not row['accepted_stations']


def test_fastener_cannot_masquerade_as_route_support(model):
    rules, contacts = inputs(model)
    anchor = next(c for c in model.support_components if c.category == 'fastener')
    wrong = replace(contacts[0], component_id=anchor.id)
    result = run(model, rules, (wrong, contacts[1]))
    assert any('fastener_is_not_support_station' in r['reason_codes'] for r in result['routes'])


def test_component_must_belong_to_assembly_that_names_route(model):
    remaining = (model.routes[1].id,)
    changed = replace(model,
        support_assemblies=tuple(replace(a, supported_object_ids=remaining) for a in model.support_assemblies),
        support_selection_outcomes=tuple(replace(o, supported_object_ids=remaining) for o in model.support_selection_outcomes))
    result = run(changed)
    first = next(r for r in result['routes'] if r['route_id'] == model.routes[0].id)
    assert 'component_assembly_does_not_support_route' in first['reason_codes']
    assert first['status'] == 'unresolved' and not first['accepted_stations']


def test_existing_unresolved_attachment_work_remains_open(model):
    pending = SupportSelectionOutcome(id='support-outcome:pending', intent_id='intent:pending',
        supported_object_ids=(model.routes[0].id,), status='unresolved', reason_codes=('missing_attachment',), provenance=PROV)
    changed = replace(model, support_selection_outcomes=(*model.support_selection_outcomes, pending))
    result = run(changed)
    assert result['status'] == 'unresolved'
    assert any('support_selection_unresolved' in row['reason_codes'] for row in result['routes'])
    assert any(s['entity']['id'] == pending.id for s in result['source_snapshots'])


def test_wrong_route_type_does_not_apply_rule(model):
    rules, contacts = inputs(model)
    result = run(model, (replace(rules[0], route_type='cable'), rules[1]), contacts)
    assert result['status'] == 'unresolved'
    assert any('spacing_rule_route_type_conflict' in r['reason_codes'] for r in result['routes'])


def test_model_binding_is_mandatory_and_stale_revision_is_rejected(model):
    rules, contacts = inputs(model)
    for binding in (None, '', 'f'*64):
        with pytest.raises(ContractError, match='binding'):
            report_support_coverage(model, expected_model_sha256=binding, rules=rules, stations=contacts)
    changed = replace(model, name='Different selected revision')
    with pytest.raises(ContractError, match='binding'):
        report_support_coverage(changed, expected_model_sha256=support_model_fingerprint(model), rules=rules, stations=contacts)


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), 10**400, '2'])
def test_invalid_station_numbers(value):
    with pytest.raises(ContractError):
        SupportStation(route_id='route:a', component_id='support:a', distance_m=value, provenance=PROV)


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), '3'])
def test_invalid_spacing_numbers(value):
    with pytest.raises(ContractError):
        SupportSpacingRule(route_id='route:a', route_type='emt', basis=BASIS,
                           max_span_m=value, max_end_distance_m=0, provenance=PROV)


def test_missing_or_unrelated_evidence_is_refused():
    with pytest.raises(ContractError):
        SupportStation(route_id='route:a', component_id='support:a', distance_m=1, provenance=())
    with pytest.raises(ContractError):
        SupportStation(route_id='route:a', component_id='support:a', distance_m=1,
                       provenance=(replace(PROV[0], scope_paths=('pose',)),))


def test_duplicate_and_dangling_references_refused(model):
    rules, contacts = inputs(model)
    for bad_rules, bad_contacts in ((rules+rules[:1], contacts), (rules, contacts+contacts[:1]),
         (rules, (replace(contacts[0], component_id='support:absent'),)),
         ((replace(rules[0], route_id='route:absent'),), contacts)):
        with pytest.raises(ContractError):
            run(model, bad_rules, bad_contacts)
    with pytest.raises(ContractError, match='beyond'):
        run(model, rules, (replace(contacts[0], distance_m=99),))


def test_permutation_and_field_evidence_preservation(model):
    rules, contacts = inputs(model)
    scoped = replace(PROV[0], scope_paths=('distance_m',), confidence=.25,
                     attributes={'operator_note':'Explicit synthetic assertion, not measurement proof'})
    contacts = (replace(contacts[0], provenance=(scoped,)), contacts[1])
    first = run(model, rules, contacts)
    assert first == run(model, tuple(reversed(rules)), tuple(reversed(contacts)))
    retained = next(s['station']['provenance'][0] for r in first['routes'] for s in r['accepted_stations']
                    if s['station']['component_id'] == contacts[0].component_id)
    assert retained['scope_paths'] == ['distance_m'] and retained['confidence'] == .25
    assert retained['attributes'] == scoped.attributes
    assert first['source_snapshots']


def test_arclength_screen_and_between_station_gap(model):
    route = model.routes[0]
    # Existing ports stay fixed; the caller explicitly supplies a longer path.
    points = (Point3(x=1,y=2,z=2), Point3(x=3,y=2,z=2), Point3(x=3,y=2.3,z=2),
              Point3(x=5,y=2.3,z=2), Point3(x=5,y=2,z=2))
    changed = replace(model, routes=(replace(route, centerline=Polyline3D(points=points)), model.routes[1]))
    rules, contacts = inputs(changed, end=3)
    rule = replace(rules[0], max_span_m=.2)
    clip_b = next(c for c in model.support_components if c.part_key == 'part:clip-b')
    extra = SupportStation(route_id=route.id, component_id=clip_b.id, distance_m=2.3, provenance=PROV)
    result = run(changed, (rule,rules[1]), (*contacts,extra))
    row = next(r for r in result['routes'] if r['route_id'] == route.id)
    assert row['route_length_m'] == pytest.approx(4.6)
    assert row['gaps'][0]['kind'] == 'between_supports'
    assert row['gaps'][0]['length_m'] == pytest.approx(.3)


def test_empty_model_is_not_an_installation_pass():
    model = BuildingModel(model_id='model:empty')
    assert run(model, (), ())['status'] == 'unresolved'


@pytest.mark.parametrize('owner,path', [
    ('rule', 'max_span_m.does_not_exist'),
    ('rule', 'basis.does_not_exist'),
    ('rule', 'basis.authority.does_not_exist'),
    ('station', 'distance_m.does_not_exist'),
    ('station', 'component_id.does_not_exist'),
])
def test_input_evidence_must_resolve_complete_owner_path(model, owner, path):
    rules, stations = inputs(model)
    evidence = (replace(PROV[0], scope_paths=(path,)),)
    with pytest.raises(ContractError, match='unknown input-owner field'):
        replace(rules[0] if owner == 'rule' else stations[0], provenance=evidence)


def test_valid_nested_rule_reference_evidence_is_retained(model):
    rules, stations = inputs(model)
    evidence = (replace(PROV[0], scope_paths=('basis.authority', 'basis.revision', 'basis.rule_id')),)
    rule = replace(rules[0], provenance=evidence)
    result = run(model, (rule, rules[1]), stations)
    assert result['status'] == 'within_supplied_limits'
    retained = next(row['rule'] for row in result['routes'] if row['route_id'] == rule.route_id)
    assert retained['provenance'][0]['scope_paths'] == ['basis.authority', 'basis.revision', 'basis.rule_id']
