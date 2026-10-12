"""Identified whole-route products; no automatic sizing or mixed-run inference."""
from dataclasses import dataclass
import re
from .common import ContractError, Entity, _positive, _validate_id
from .physical import CatalogProduct

def wiring_method_for_route_type(route_type: str) -> str | None:
    token=route_type.lower()
    if token=='cable':
        return 'cable'
    if token in {'emt','pvc','rmc','imc','ent','fmc','lfmc','lfnc','rigid',
                 'tray','cabletray','cable-tray','trunking','wireway'}:
        return 'raceway'
    return None

@dataclass(frozen=True, slots=True, kw_only=True)
class RuleReference:
    authority: str
    revision: str
    rule_id: str

    def __post_init__(self):
        for field in ('authority','revision','rule_id'):
            if not isinstance(getattr(self,field),str) or not getattr(self,field).strip():
                raise ContractError(f'RuleReference.{field} must be nonempty')

@dataclass(frozen=True, slots=True, kw_only=True)
class RacewaySelection(Entity):
    intent_id: str
    route_id: str
    product: CatalogProduct
    route_type: str
    wiring_method: str
    designation_kind: str
    designation: str
    basis: RuleReference
    nominal_diameter_m: float | None = None

    def __post_init__(self):
        super(RacewaySelection,self).__post_init__()
        _validate_id(self.intent_id,'intent_id')
        _validate_id(self.route_id,'route_id')
        if not isinstance(self.product,CatalogProduct) or not isinstance(self.basis,RuleReference):
            raise ContractError('raceway selection requires typed product and rule reference')
        if not isinstance(self.route_type,str) or not self.route_type.strip():
            raise ContractError('raceway route_type must be nonempty')
        expected={'raceway':'trade_size','cable':'listed_assembly'}
        if self.wiring_method not in expected or self.designation_kind != expected[self.wiring_method]:
            raise ContractError('raceway requires trade-size identity; cable requires listed-assembly identity')
        if not isinstance(self.designation,str) or not self.designation.strip():
            raise ContractError('raceway/cable designation must be explicitly supplied')
        if self.nominal_diameter_m is not None:
            _positive(self.nominal_diameter_m,'selection.nominal_diameter_m')

@dataclass(frozen=True, slots=True, kw_only=True)
class RacewaySelectionOutcome(Entity):
    intent_id: str
    route_id: str
    status: str
    selected_selection_id: str | None = None
    requested_product: CatalogProduct | None = None
    requested_basis: RuleReference | None = None
    scope: str = 'whole_route'
    reason_codes: tuple[str,...] = ()

    def __post_init__(self):
        super(RacewaySelectionOutcome,self).__post_init__()
        _validate_id(self.intent_id,'intent_id')
        _validate_id(self.route_id,'route_id')
        if self.scope not in {'whole_route','partial_route','mixed_run'}:
            raise ContractError('unsupported selection scope')
        if self.requested_product is not None and not isinstance(self.requested_product,CatalogProduct):
            raise ContractError('outcome product must be typed')
        if self.requested_basis is not None and not isinstance(self.requested_basis,RuleReference):
            raise ContractError('outcome basis must be typed')
        if self.selected_selection_id is not None:
            _validate_id(self.selected_selection_id,'selected_selection_id')
        if len(set(self.reason_codes))!=len(self.reason_codes) or any(
            not isinstance(r,str) or re.fullmatch(r'[a-z][a-z0-9_]*',r) is None for r in self.reason_codes):
            raise ContractError('outcome reasons must be unique tokens')
        if self.status=='selected':
            if (self.selected_selection_id is None or self.requested_product is None or
                self.requested_basis is None or self.reason_codes or self.scope!='whole_route'):
                raise ContractError('selected outcome requires whole route, product, basis and selection')
        elif self.status=='unresolved':
            if self.selected_selection_id is not None or not self.reason_codes:
                raise ContractError('unresolved outcome requires reasons and no selection')
        else:
            raise ContractError('selection status must be selected or unresolved')
