"""Caller-authored product/rule decisions, not code verification or sizing."""
from dataclasses import asdict, dataclass, replace
import json
from typing import Sequence
from oabm.model import (BuildingModel,CatalogProduct,ContractError,Provenance,RuleReference,
    RacewaySelection,RacewaySelectionOutcome,RACEWAY_SCHEMA_VERSION,stable_id)
from oabm.model.common import _positive,_validate_id
from oabm.model.raceway import wiring_method_for_route_type

_KIND='raceway-selection-producer'
_METHOD='explicit-raceway-intent'
_CATALOG_FIELDS=('product','route_type','wiring_method','designation_kind','designation','nominal_diameter_m')

def _portable(value):
    return json.loads(json.dumps(asdict(value),sort_keys=True,allow_nan=False))

@dataclass(frozen=True,slots=True,kw_only=True)
class RacewayCatalogEntry:
    product: CatalogProduct
    route_type: str
    wiring_method: str
    designation_kind: str
    designation: str | None
    provenance: tuple[Provenance,...]
    nominal_diameter_m: float | None = None

    def __post_init__(self):
        if not isinstance(self.product,CatalogProduct):
            raise ContractError('catalog requires typed product')
        if not isinstance(self.route_type,str) or not self.route_type.strip():
            raise ContractError('catalog route type must be nonempty')
        if self.wiring_method not in {'raceway','cable'}:
            raise ContractError('catalog wiring method must be raceway or cable')
        if self.designation_kind != {'raceway':'trade_size','cable':'listed_assembly'}[self.wiring_method]:
            raise ContractError('catalog designation kind contradicts wiring method')
        if self.designation is not None and (not isinstance(self.designation,str) or not self.designation.strip()):
            raise ContractError('catalog designation must be nonempty or unknown')
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m,'catalog.nominal_diameter_m')
        if not self.provenance or any(p.derivation is None for p in self.provenance):
            raise ContractError('catalog requires explicit derivation provenance')
        if any(path.split('.')[0] not in _CATALOG_FIELDS for p in self.provenance for path in (p.scope_paths or ())):
            raise ContractError('catalog evidence must qualify catalog fields')

@dataclass(frozen=True,slots=True,kw_only=True)
class RacewayRule:
    """Explicit caller permission for one product/method; no code approval claim."""
    reference: RuleReference
    product: CatalogProduct
    route_type: str
    provenance: tuple[Provenance,...]

    def __post_init__(self):
        if not isinstance(self.reference,RuleReference) or not isinstance(self.product,CatalogProduct):
            raise ContractError('rule requires typed reference and product')
        if not isinstance(self.route_type,str) or not self.route_type.strip():
            raise ContractError('rule requires nonempty route type')
        if not self.provenance or any(p.derivation is None for p in self.provenance):
            raise ContractError('rule requires explicit provenance')
        # This record qualifies the reference itself; other decisions are inferred.
        if any(path.split('.')[0]!='basis' for p in self.provenance for path in (p.scope_paths or ())):
            raise ContractError('rule evidence scopes must name output basis fields')

@dataclass(frozen=True,slots=True,kw_only=True)
class RacewayIntent:
    intent_id: str
    route_id: str
    actor_id: str
    product: CatalogProduct | None = None
    basis: RuleReference | None = None
    scope: str = 'whole_route'

    def __post_init__(self):
        _validate_id(self.intent_id,'intent_id')
        _validate_id(self.route_id,'route_id')
        if not isinstance(self.actor_id,str) or not self.actor_id.strip():
            raise ContractError('intent requires actor identity')
        if self.product is not None and not isinstance(self.product,CatalogProduct):
            raise ContractError('intent product must be typed')
        if self.basis is not None and not isinstance(self.basis,RuleReference):
            raise ContractError('intent basis must be typed')
        if self.scope not in {'whole_route','partial_route','mixed_run'}:
            raise ContractError('unsupported request scope')

def select_raceways(model: BuildingModel,*,intents: Sequence[RacewayIntent],
                    catalog: Sequence[RacewayCatalogEntry],rules: Sequence[RacewayRule]) -> BuildingModel:
    if model.schema_version!=RACEWAY_SCHEMA_VERSION:
        raise ContractError('explicit upgrade_to_v1_2 is required for raceway selection')
    for entity in (*model.raceway_selections,*model.raceway_selection_outcomes):
        if not any(p.source_kind==_KIND and p.method==_METHOD for p in entity.provenance):
            raise ContractError('cannot overwrite raceway inventory owned by another source')
    routes={r.id:r for r in model.routes}
    entries={}
    for entry in catalog:
        if entry.product in entries:
            raise ContractError('duplicate catalog product')
        entries[entry.product]=entry
    rule_by_ref={}
    for rule in rules:
        if rule.reference in rule_by_ref:
            raise ContractError('duplicate caller rule reference')
        rule_by_ref[rule.reference]=rule
    work=[]
    route_ids=set()
    intent_ids=set()
    for intent in intents:
        if intent.route_id not in routes:
            raise ContractError('intent references missing route')
        if intent.route_id in route_ids or intent.intent_id in intent_ids:
            raise ContractError('duplicate active route or intent request')
        route_ids.add(intent.route_id);intent_ids.add(intent.intent_id)
        work.append((intent,False))
    for route in sorted(model.routes,key=lambda r:r.id):
        if route.id not in route_ids:
            identity=stable_id('raceway-intent',f'{model.model_id}:missing:{route.id}')
            if identity in intent_ids:
                raise ContractError('explicit intent collides with missing-intent identity')
            work.append((RacewayIntent(intent_id=identity,route_id=route.id,actor_id='unprovided'),True))
    selections=[];outcomes=[]
    for intent,missing in sorted(work,key=lambda pair:pair[0].intent_id):
        route=routes[intent.route_id]
        entry=entries.get(intent.product)
        rule=rule_by_ref.get(intent.basis)
        reasons=[]
        if missing:
            reasons=['missing_intent']
        else:
            if intent.scope!='whole_route':
                reasons.append('mixed_run_unsupported' if intent.scope=='mixed_run' else 'partial_route_unsupported')
            if entry is None:
                reasons.append('missing_catalog_entry')
            else:
                if entry.designation is None:
                    reasons.append('missing_product_designation')
                if entry.route_type!=route.route_type or entry.wiring_method!=wiring_method_for_route_type(route.route_type):
                    reasons.append('product_method_conflict')
                if route.nominal_diameter_m is not None:
                    if entry.nominal_diameter_m is None:
                        reasons.append('catalog_nominal_unknown')
                    elif abs(entry.nominal_diameter_m-route.nominal_diameter_m)>1e-9:
                        reasons.append('nominal_diameter_conflict')
            if rule is None:
                reasons.append('missing_rule_reference')
            elif rule.product!=intent.product or rule.route_type!=route.route_type:
                reasons.append('caller_rule_conflict')
        intent_record=Provenance(source_kind=_KIND,source_id=model.model_id,
            source_element_id=intent.intent_id,method=_METHOD,
            derivation='inferred' if missing else 'user',confidence=0.0 if missing else 1.0,
            scope_paths=('intent_id','route_id'),attributes={'actor_id':intent.actor_id,
            'consumed_intent':_portable(intent),'code_verification_performed':False})
        selection_id=None
        if not reasons:
            selection_id=stable_id('raceway-selection',f'{model.model_id}:{intent.intent_id}')
            claims=tuple(p for p in _CATALOG_FIELDS if p!='nominal_diameter_m' or entry.nominal_diameter_m is not None)
            catalog_records=tuple(replace(p,scope_paths=p.scope_paths or claims,
                attributes={**p.attributes,'consumed_catalog_entry':_portable(entry)}) for p in entry.provenance)
            rule_records=tuple(replace(p,scope_paths=p.scope_paths or ('basis',),
                attributes={**p.attributes,'consumed_caller_rule':_portable(rule),
                'code_verification_performed':False}) for p in rule.provenance)
            selections.append(RacewaySelection(id=selection_id,intent_id=intent.intent_id,
                route_id=route.id,product=entry.product,route_type=entry.route_type,
                wiring_method=entry.wiring_method,designation_kind=entry.designation_kind,
                designation=entry.designation,basis=rule.reference,nominal_diameter_m=entry.nominal_diameter_m,
                provenance=(intent_record,*catalog_records,*rule_records),
                confidence=min(p.confidence for p in (intent_record,*catalog_records,*rule_records))))
        outcome_intent=replace(intent_record,scope_paths=('intent_id','route_id','requested_product','requested_basis','scope'))
        decision=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=intent.intent_id,
            method='explicit-raceway-selection-outcome',derivation='inferred',confidence=intent_record.confidence,
            scope_paths=('status','selected_selection_id','reason_codes'),
            attributes={'consumed_route_type':route.route_type,'consumed_nominal_diameter_m':route.nominal_diameter_m,
                        'code_verification_performed':False})
        outcomes.append(RacewaySelectionOutcome(id=stable_id('raceway-outcome',f'{model.model_id}:{intent.intent_id}'),
            intent_id=intent.intent_id,route_id=route.id,status='unresolved' if reasons else 'selected',
            selected_selection_id=selection_id,requested_product=intent.product,requested_basis=intent.basis,
            scope=intent.scope,reason_codes=tuple(sorted(reasons)),confidence=intent_record.confidence,
            provenance=(outcome_intent,decision)))
    return replace(model,raceway_selections=tuple(sorted(selections,key=lambda s:s.id)),
                   raceway_selection_outcomes=tuple(sorted(outcomes,key=lambda o:o.id)))
