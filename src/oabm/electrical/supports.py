"""Explicit shared assembly installation inputs; no spacing/load certification."""
from dataclasses import asdict,dataclass,replace
import json
from typing import Sequence
from oabm.model import (BuildingModel,CatalogProduct,ContractError,Pose,Provenance,RuleReference,
    Size3,SupportAssembly,SupportComponent,SupportInterface,SupportSelectionOutcome,
    SUPPORT_SCHEMA_VERSION,stable_id,provenance_applies_to)
from oabm.model.common import _validate_id
from oabm.model.supports import SUPPORT_SUBSTRATES,unique_refs,text,supported_levels

_KIND='support-selection-producer'
_METHOD='explicit-support-intent'
_CATALOG_FIELDS=('product','category','component_type','size')

def portable(value):
    return json.loads(json.dumps(asdict(value),sort_keys=True,allow_nan=False))

def field_confidence(entity,paths):
    return min((entity.confidence,*(p.confidence for p in entity.provenance
                                   if provenance_applies_to(p,paths))))

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportCatalogEntry:
    product: CatalogProduct
    category: str | None
    component_type: str | None
    size: Size3 | None
    provenance: tuple[Provenance,...]

    def __post_init__(self):
        if not isinstance(self.product,CatalogProduct):
            raise ContractError('support catalog requires typed product')
        if self.category is not None and self.category not in {'support','fastener'}:
            raise ContractError('catalog category must be explicitly support/fastener or unknown')
        if self.component_type is not None:
            text(self.component_type,'component_type')
        if self.size is not None and not isinstance(self.size,Size3):
            raise ContractError('catalog requires Size3 or an unknown envelope')
        if not self.provenance or any(p.derivation is None for p in self.provenance):
            raise ContractError('catalog requires explicit derivation provenance')
        if any(path.split('.')[0] not in _CATALOG_FIELDS for p in self.provenance for path in (p.scope_paths or ())):
            raise ContractError('support catalog evidence must name consumed catalog fields')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportRule:
    reference: RuleReference
    permitted_products: tuple[CatalogProduct,...]
    permitted_substrates: tuple[str,...]
    provenance: tuple[Provenance,...]

    def __post_init__(self):
        if not isinstance(self.reference,RuleReference):
            raise ContractError('support rule requires typed reference')
        if (not self.permitted_products or len(set(self.permitted_products))!=len(self.permitted_products) or
            any(not isinstance(p,CatalogProduct) for p in self.permitted_products)):
            raise ContractError('support rule requires unique typed product permissions')
        if (not self.permitted_substrates or len(set(self.permitted_substrates))!=len(self.permitted_substrates) or
            any(s not in SUPPORT_SUBSTRATES for s in self.permitted_substrates)):
            raise ContractError('support rule requires explicit supported substrate permissions')
        if not self.provenance or any(p.derivation is None for p in self.provenance):
            raise ContractError('support rule requires explicit provenance')
        if any(path.split('.')[0]!='basis' for p in self.provenance for path in (p.scope_paths or ())):
            raise ContractError('support rule evidence qualifies output basis fields')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportPartIntent:
    part_key: str
    product: CatalogProduct | None = None
    pose: Pose | None = None

    def __post_init__(self):
        _validate_id(self.part_key,'part_key')
        if self.product is not None and not isinstance(self.product,CatalogProduct):
            raise ContractError('support part product must be typed')
        if self.pose is not None and not isinstance(self.pose,Pose):
            raise ContractError('support part pose must be typed')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportInterfaceIntent:
    interface_key: str
    part_keys: tuple[str,...]
    host_id: str | None = None
    substrate: str | None = None
    pose: Pose | None = None

    def __post_init__(self):
        _validate_id(self.interface_key,'interface_key')
        unique_refs(self.part_keys,'interface.part_keys')
        if self.host_id is not None:
            _validate_id(self.host_id,'host_id')
        if self.substrate is not None:
            text(self.substrate,'substrate')
        if self.pose is not None and not isinstance(self.pose,Pose):
            raise ContractError('interface pose must be typed')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportIntent:
    intent_id: str
    actor_id: str
    assembly_type: str
    supported_object_ids: tuple[str,...]
    parts: tuple[SupportPartIntent,...]
    interfaces: tuple[SupportInterfaceIntent,...]
    level_id: str | None = None
    basis: RuleReference | None = None

    def __post_init__(self):
        _validate_id(self.intent_id,'intent_id');text(self.actor_id,'actor_id');text(self.assembly_type,'assembly_type')
        unique_refs(self.supported_object_ids,'supported_object_ids')
        if self.level_id is not None:
            _validate_id(self.level_id,'level_id')
        if self.basis is not None and not isinstance(self.basis,RuleReference):
            raise ContractError('support basis must be typed')
        if any(not isinstance(p,SupportPartIntent) for p in self.parts) or any(
            not isinstance(i,SupportInterfaceIntent) for i in self.interfaces):
            raise ContractError('support intent parts/interfaces must be typed')
        if len({p.part_key for p in self.parts})!=len(self.parts) or len({i.interface_key for i in self.interfaces})!=len(self.interfaces):
            raise ContractError('duplicate part/interface identity in support intent')
        keys={p.part_key for p in self.parts}
        if any(k not in keys for i in self.interfaces for k in i.part_keys):
            raise ContractError('interface references missing intent part')


def select_supports(model: BuildingModel,*,intents: Sequence[SupportIntent],
                    catalog: Sequence[SupportCatalogEntry],rules: Sequence[SupportRule]) -> BuildingModel:
    if model.schema_version!=SUPPORT_SCHEMA_VERSION:
        raise ContractError('explicit upgrade_to_v1_3 is required for support inventory')
    for entity in (*model.support_assemblies,*model.support_components,*model.support_interfaces,*model.support_selection_outcomes):
        if not any(p.source_kind==_KIND and p.method==_METHOD for p in entity.provenance):
            raise ContractError('cannot overwrite another source support inventory')
    supportable={e.id:e for e in (*model.routes,*model.electrical_boxes,*model.electrical_devices,*model.electrical_equipment)}
    hosts={e.id:e for e in (*model.walls,*model.slabs,*model.ceilings)}
    level_by_id={l.id:l for l in model.levels}
    levels=set(level_by_id)
    entries={};rule_by_ref={};intent_ids=set()
    for entry in catalog:
        if entry.product in entries:
            raise ContractError('duplicate support catalog product')
        entries[entry.product]=entry
    for rule in rules:
        if rule.reference in rule_by_ref:
            raise ContractError('duplicate support caller rule reference')
        rule_by_ref[rule.reference]=replace(rule,
            permitted_products=tuple(sorted(rule.permitted_products,key=lambda p:(p.catalog_id,p.catalog_revision,p.product_id))),
            permitted_substrates=tuple(sorted(rule.permitted_substrates)))
    assemblies=[];components=[];interfaces=[];outcomes=[]
    normalized=[]
    for original in intents:
        if original.intent_id in intent_ids:
            raise ContractError('duplicate active support intent')
        intent_ids.add(original.intent_id)
        if any(i not in supportable for i in original.supported_object_ids):
            raise ContractError('support intent references missing/nonphysical supported object')
        if original.level_id is not None and original.level_id not in levels:
            raise ContractError('support intent references missing level')
        if any(i.host_id is not None and i.host_id not in hosts for i in original.interfaces):
            raise ContractError('support interface references missing/unsupported host')
        intent=replace(original,supported_object_ids=tuple(sorted(original.supported_object_ids)),
            parts=tuple(sorted(original.parts,key=lambda p:p.part_key)),
            interfaces=tuple(sorted((replace(i,part_keys=tuple(sorted(i.part_keys))) for i in original.interfaces),key=lambda i:i.interface_key)))
        normalized.append(intent)
    for intent in sorted(normalized,key=lambda i:i.intent_id):
        rule=rule_by_ref.get(intent.basis);reasons=set()
        if intent.level_id is None: reasons.add('missing_level')
        if not intent.parts: reasons.add('missing_components')
        if not intent.interfaces: reasons.add('missing_interfaces')
        if rule is None: reasons.add('missing_rule_reference')
        if any(None in supported_levels(model,identity) for identity in intent.supported_object_ids):
            reasons.add('supported_object_level_unknown')
        if intent.level_id is not None and any(l is not None and l!=intent.level_id
            for identity in intent.supported_object_ids for l in supported_levels(model,identity)):
            reasons.add('multi_level_support_unsupported')
        for part in intent.parts:
            entry=entries.get(part.product)
            if part.pose is None: reasons.add('missing_component_pose')
            if entry is None: reasons.add('missing_catalog_entry')
            else:
                if entry.size is None: reasons.add('missing_catalog_envelope')
                if entry.category is None: reasons.add('missing_component_category')
                if entry.component_type is None: reasons.add('missing_component_type')
            if rule is not None and part.product not in rule.permitted_products:
                reasons.add('caller_product_permission_conflict')
        for interface in intent.interfaces:
            if interface.host_id is None: reasons.add('missing_host')
            elif intent.level_id is not None and hosts[interface.host_id].level_id!=intent.level_id:
                reasons.add('multi_level_support_unsupported')
            if interface.pose is None: reasons.add('missing_attachment_pose')
            if interface.substrate is None: reasons.add('unknown_substrate')
            elif interface.substrate not in SUPPORT_SUBSTRATES: reasons.add('unsupported_actual_substrate')
            elif rule is not None and interface.substrate not in rule.permitted_substrates:
                reasons.add('caller_substrate_permission_conflict')
        record=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=intent.intent_id,
            method=_METHOD,derivation='user',scope_paths=('intent_id','supported_object_ids','level_id','assembly_type'),
            attributes={'actor_id':intent.actor_id,'consumed_intent':portable(intent),
                        'spacing_loading_fastener_suitability_verification_performed':False})
        assembly_id=None
        if not reasons:
            assembly_id=stable_id('support-assembly',f'{model.model_id}:{intent.intent_id}')
            part_ids={p.part_key:stable_id('support-component',f'{model.model_id}:{intent.intent_id}:{p.part_key}') for p in intent.parts}
            interface_ids={i.interface_key:stable_id('support-interface',f'{model.model_id}:{intent.intent_id}:{i.interface_key}') for i in intent.interfaces}
            rule_records=tuple(replace(p,scope_paths=p.scope_paths or ('basis',),attributes={**p.attributes,
                'consumed_caller_rule':portable(rule),'code_verification_performed':False}) for p in rule.provenance)
            membership=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=intent.intent_id,
                method=_METHOD,derivation='inferred',scope_paths=('component_ids','interface_ids'),
                attributes={'consumed_part_keys':[p.part_key for p in intent.parts],
                            'consumed_interface_keys':[i.interface_key for i in intent.interfaces]})
            assemblies.append(SupportAssembly(id=assembly_id,name=f'Support assembly {intent.intent_id}',intent_id=intent.intent_id,assembly_type=intent.assembly_type,
                level_id=intent.level_id,supported_object_ids=intent.supported_object_ids,
                component_ids=tuple(sorted(part_ids.values())),interface_ids=tuple(sorted(interface_ids.values())),
                basis=rule.reference,provenance=(record,*rule_records,membership),
                confidence=min(p.confidence for p in (record,*rule_records))))
            for part in intent.parts:
                entry=entries[part.product]
                user=replace(record,scope_paths=('part_key','pose'))
                catalog_records=tuple(replace(p,scope_paths=p.scope_paths or _CATALOG_FIELDS,
                    attributes={**p.attributes,'consumed_catalog_entry':portable(entry)}) for p in entry.provenance)
                inferred=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=assembly_id,method=_METHOD,
                    derivation='inferred',scope_paths=('assembly_id','level_id','geometry_basis'),attributes={
                        'consumed_assembly_level_id':intent.level_id,'geometry_basis':'catalog_exterior_envelope',
                        'exact_profile_or_material_volume_verified':False})
                components.append(SupportComponent(id=part_ids[part.part_key],name=f'{entry.component_type} {part.part_key}',assembly_id=assembly_id,
                    part_key=part.part_key,level_id=intent.level_id,product=entry.product,category=entry.category,
                    component_type=entry.component_type,pose=part.pose,size=entry.size,
                    provenance=(user,*catalog_records,inferred),confidence=min(p.confidence for p in (user,*catalog_records,inferred))))
            for interface in intent.interfaces:
                user=replace(record,scope_paths=('interface_key','host_id','substrate','pose'))
                inherited=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=interface.host_id,
                    method=_METHOD,derivation='inferred',scope_paths=('level_id',),
                    confidence=min(field_confidence(hosts[interface.host_id],('level_id',)),
                                   field_confidence(level_by_id[intent.level_id],('id',))),
                    attributes={'consumed_host_level_id':hosts[interface.host_id].level_id,
                                'consumed_part_keys':list(interface.part_keys)})
                associations=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=intent.intent_id,
                    method=_METHOD,derivation='inferred',scope_paths=('assembly_id','component_ids'),
                    attributes={'consumed_part_keys':list(interface.part_keys)})
                interfaces.append(SupportInterface(id=interface_ids[interface.interface_key],name=f'Support attachment {interface.interface_key}',assembly_id=assembly_id,
                    interface_key=interface.interface_key,level_id=intent.level_id,host_id=interface.host_id,
                    substrate=interface.substrate,pose=interface.pose,component_ids=tuple(sorted(part_ids[k] for k in interface.part_keys)),
                    provenance=(user,inherited,associations),
                    confidence=min(inherited.confidence,*(c.confidence for c in components if c.id in {part_ids[k] for k in interface.part_keys}))))
        user=replace(record,scope_paths=('intent_id','supported_object_ids','requested_level_id','requested_basis'))
        decision=Provenance(source_kind=_KIND,source_id=model.model_id,source_element_id=intent.intent_id,
            method='explicit-support-selection-outcome',derivation='inferred',scope_paths=('status','selected_assembly_id','reason_codes'),
            attributes={'unresolved_reasons':sorted(reasons),'code_verification_performed':False,
                'consumed_caller_rule':None if rule is None else portable(rule),
                'consumed_catalog_entries':{p.part_key:None if entries.get(p.product) is None else portable(entries[p.product])
                                            for p in intent.parts}})
        outcomes.append(SupportSelectionOutcome(id=stable_id('support-outcome',f'{model.model_id}:{intent.intent_id}'),name=f'Support selection {intent.intent_id}',
            intent_id=intent.intent_id,supported_object_ids=intent.supported_object_ids,status='unresolved' if reasons else 'selected',
            selected_assembly_id=assembly_id,requested_level_id=intent.level_id,requested_basis=intent.basis,
            reason_codes=tuple(sorted(reasons)),provenance=(user,decision)))
    input_record=Provenance(source_kind=_KIND,source_id=model.model_id,method='explicit-support-inventory-inputs',
        derivation='user',scope_paths=('support_assemblies','support_components','support_interfaces','support_selection_outcomes'),
        attributes={'consumed_intents':[portable(i) for i in sorted(normalized,key=lambda i:i.intent_id)],
                    'code_verification_performed':False})
    provenance=tuple(p for p in model.provenance if not (p.source_kind==_KIND and p.method==input_record.method))+(input_record,)
    return replace(model,support_assemblies=tuple(sorted(assemblies,key=lambda a:a.id)),
        support_components=tuple(sorted(components,key=lambda c:c.id)),support_interfaces=tuple(sorted(interfaces,key=lambda i:i.id)),
        support_selection_outcomes=tuple(sorted(outcomes,key=lambda o:o.id)),provenance=provenance)
