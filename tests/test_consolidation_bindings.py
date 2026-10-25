"""Keep whole-route product/support decisions attached to their actual routes."""
from dataclasses import replace

import pytest

from oabm.electrical import RacewayCatalogEntry, RacewayIntent, RacewayRule, select_raceways
from oabm.model import CatalogProduct, Provenance, RuleReference, upgrade_to_v1_2, validate_model
from oabm.routing import consolidate_bundled_routes
from test_route_consolidation import acceptance_model
from test_shared_supports import case, selected


def product_model(count, unresolved=False):
    model = upgrade_to_v1_2(acceptance_model())
    product = CatalogProduct(catalog_id='synthetic', catalog_revision='1', product_id='emt')
    basis = RuleReference(authority='synthetic', revision='1', rule_id='explicit')
    provenance = (Provenance(source_kind='synthetic', source_id='fixture:bundle', derivation='user'),)
    entry = RacewayCatalogEntry(product=product, route_type='emt', wiring_method='raceway',
        designation_kind='trade_size', designation='synthetic', nominal_diameter_m=.021, provenance=provenance)
    rule = RacewayRule(reference=basis, product=product, route_type='emt', provenance=provenance)
    intents = tuple(RacewayIntent(intent_id=f'intent:bundle-{i}', route_id=r.id,
        actor_id='synthetic', product=None if unresolved else product, basis=basis)
        for i, r in enumerate(model.routes[:count]))
    selected_model = select_raceways(model, intents=intents, catalog=(entry,), rules=(rule,))
    # The producer reports missing-intent outcomes for every other route too.
    # This valid fixture intentionally represents decisions for only a subset;
    # otherwise those unresolved bindings must also be retained by the guard.
    bound = {r.id for r in model.routes[:count]}
    return replace(selected_model, raceway_selection_outcomes=tuple(
        outcome for outcome in selected_model.raceway_selection_outcomes
        if outcome.route_id in bound))


@pytest.mark.parametrize('unresolved', [False, True])
def test_whole_route_decisions_survive_other_routes_consolidating(unresolved):
    model = product_model(1, unresolved)
    before = model.to_json()
    result = consolidate_bundled_routes(model)
    validate_model(result.model)
    assert result.report.trunk_routes == 1
    assert model.routes[0] in result.model.routes
    assert result.model.raceway_selections == model.raceway_selections
    assert result.model.raceway_selection_outcomes == model.raceway_selection_outcomes
    assert (model.routes[0].id, 'whole_route_product_binding_requires_reselection') in result.report.untouched_route_reasons
    assert model.to_json() == before


@pytest.mark.parametrize('unresolved', [False, True])
def test_all_product_bound_routes_return_same_model(unresolved):
    model = product_model(3, unresolved)
    result = consolidate_bundled_routes(model)
    assert result.model is model
    assert result.report.trunk_routes == 0
    assert len(result.report.untouched_route_reasons) == 3


@pytest.mark.parametrize('unresolved', [False, True])
def test_support_bindings_are_not_repointed_or_discarded(case, unresolved):
    if unresolved:
        base, intent, catalog, rule = case
        case = base, replace(intent, basis=None), catalog, rule
    model = selected(case)
    result = consolidate_bundled_routes(model, tolerance_m=.35)
    assert result.model is model
    assert result.report.trunk_routes == 0
    assert dict(result.report.untouched_route_reasons) == {
        r.id:'support_binding_requires_reselection' for r in model.routes}
    validate_model(result.model)


def test_legacy_unbound_consolidation_unchanged():
    result = consolidate_bundled_routes(acceptance_model())
    assert result.report.trunk_routes == 1
    assert result.report.untouched_route_reasons == ()
    validate_model(result.model)
