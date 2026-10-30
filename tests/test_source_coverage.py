import copy
from dataclasses import replace
import pytest
from oabm.model import BuildingModel, ContractError, Level
from oabm.reports import inventory_fingerprint, model_fingerprint, source_coverage


def setup():
    model = BuildingModel(model_id='fixture:model', levels=(Level(id='level:one', name='One', elevation_m=0),))
    source_hash = 'a' * 64
    ids = ['A', 'B', 'C']
    mapping = {'inventory_sha256': inventory_fingerprint(source_hash, ids),
               'model_sha256': model_fingerprint(model), 'entries': [
                   {'source_id': 'A', 'canonical_ids': ['level:one']},
                   {'source_id': 'B', 'canonical_ids': ['level:one']}]}
    return model, source_hash, ids, mapping


def test_unresolved_and_explicit_omission():
    model, h, ids, mapping = setup()
    report = source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping)
    assert [r['status'] for r in report['rows']] == ['mapped', 'mapped', 'unresolved']
    with pytest.raises(ContractError, match='unresolved'):
        source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping, strict=True)
    mapping['entries'].append({'source_id': 'C', 'omission_reason': 'Outside declared output scope'})
    report = source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping, strict=True)
    assert report['rows'][2]['status'] == 'omitted'


def test_bad_bindings_and_mappings():
    model, h, ids, mapping = setup()
    for bad in ['b' * 64]:
        with pytest.raises(ContractError, match='stale'):
            source_coverage(model, source_sha256=bad, object_ids=ids, mapping=mapping)
    bad = copy.deepcopy(mapping)
    bad['entries'][0]['canonical_ids'] = ['missing']
    with pytest.raises(ContractError, match='unknown or empty canonical target'):
        source_coverage(model, source_sha256=h, object_ids=ids, mapping=bad)
    for entry in ({'source_id': 'Z', 'canonical_ids': ['level:one']},
                  {'source_id': 'A', 'canonical_ids': ['level:one']},
                  {'source_id': 'C', 'omission_reason': '  '}):
        bad = copy.deepcopy(mapping)
        bad['entries'].append(entry)
        with pytest.raises(ContractError):
            source_coverage(model, source_sha256=h, object_ids=ids, mapping=bad)


def test_stale_bindings_and_duplicate_inventory():
    model, h, ids, mapping = setup()
    for changed_model, changed_ids in ((replace(model, name='changed'), ids),
                                       (model, ['A', 'B'])):
        with pytest.raises(ContractError, match='stale'):
            source_coverage(changed_model, source_sha256=h, object_ids=changed_ids, mapping=mapping)
    with pytest.raises(ContractError, match='duplicate source IDs'):
        source_coverage(model, source_sha256=h, object_ids=['A', 'A'], mapping=mapping)


def test_empty_inventory_and_entries():
    model, h, ids, mapping = setup()
    mapping['entries'] = []
    assert all(row['status'] == 'unresolved' for row in source_coverage(
        model, source_sha256=h, object_ids=ids, mapping=mapping)['rows'])
    with pytest.raises(ContractError, match='unresolved'):
        source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping, strict=True)
    mapping['inventory_sha256'] = inventory_fingerprint(h, [])
    assert source_coverage(model, source_sha256=h, object_ids=[], mapping=mapping, strict=True)['rows'] == []


@pytest.mark.parametrize('change', [
    lambda m: m.update(extra=True),
    lambda m: m.pop('entries'),
    lambda m: m.update(entries={}),
    lambda m: m['entries'][0].update(extra=True),
    lambda m: m['entries'][0].update(omission_reason='conflicting'),
    lambda m: m['entries'][0].update(canonical_ids=[]),
    lambda m: m['entries'][0].update(canonical_ids=['level:one', 'level:one']),
    lambda m: m['entries'][0].update(canonical_ids='level:one'),
    lambda m: m['entries'][0].update(source_id=1),
])
def test_malformed_mapping(change):
    model, h, ids, mapping = setup()
    change(mapping)
    with pytest.raises(ContractError):
        source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping)


@pytest.mark.parametrize('kwargs', [
    {'strict': 1}, {'source_sha256': 'X' * 64}, {'object_ids': 'A'},
    {'object_ids': ['A', 1]}, {'mapping': []},
])
def test_malformed_arguments(kwargs):
    model, h, ids, mapping = setup()
    args = dict(source_sha256=h, object_ids=ids, mapping=mapping)
    args.update(kwargs)
    with pytest.raises(ContractError):
        source_coverage(model, **args)


def test_deterministic_order_and_no_mutation():
    model, h, ids, mapping = setup()
    model = replace(model, levels=(Level(id='level:two', elevation_m=3), *model.levels))
    mapping['model_sha256'] = model_fingerprint(model)
    mapping['entries'][0]['canonical_ids'] = ['level:two', 'level:one']
    before = copy.deepcopy((model.to_dict(), ids, mapping))
    report = source_coverage(model, source_sha256=h, object_ids=ids, mapping=mapping)
    assert report == source_coverage(
        replace(model, levels=tuple(reversed(model.levels))), source_sha256=h,
        object_ids=list(reversed(ids)), mapping={**mapping, 'entries': [
            {**entry, 'canonical_ids': list(reversed(entry['canonical_ids']))}
            for entry in reversed(mapping['entries'])]})
    assert before == (model.to_dict(), ids, mapping)
