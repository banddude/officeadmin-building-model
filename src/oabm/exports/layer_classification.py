"""Automatic presentation membership from canonical kinds and explicit facts.

No source recognition, spatial inference, system guessing from device names,
construction-status fallback, or canonical mutation. Unknown axes stay explicit.
"""
from dataclasses import dataclass
import hashlib
import json

from oabm.model import BuildingModel, ContractError, LayerDefinition, LayerSelectionConfig, SavedPreset, stable_id
from oabm.model.layers import _universe
from oabm.reports.scope_ledger import scope_metadata_facts


@dataclass(frozen=True, slots=True)
class LayerClassification:
    source_model_json: str
    config_json: str
    manifest_json: str

    @property
    def configuration(self):
        return LayerSelectionConfig.from_json(self.config_json)

    def to_dict(self):
        return json.loads(self.manifest_json)


def classify_model_layers(model: BuildingModel, *, include_obstacles: bool = False) -> LayerClassification:
    """Generate saved all-visible layers, retaining unknown system/status facts.

    Trade/category follow canonical collections and assembly component kinds.
    Explicit device/equipment ``system`` is preserved verbatim; routes/fittings
    are raceway categories, and shared supports are independently classified.
    Construction status consumes only validated existing pdf_electrical scope
    metadata on devices/equipment, with no unmarked-object fallback. Circuit
    ``phase`` means electrical phase and is never construction status.
    """
    from oabm.routing.physical_capture import capture, SOURCE_BYTES
    from oabm.routing.physical_verify_types import VerificationStop
    if type(model) is not BuildingModel or type(include_obstacles) is not bool:
        raise ContractError('classification requires a canonical model and boolean obstacle inclusion')
    try:
        snapshot=BuildingModel.from_json(capture(model,byte_limit=SOURCE_BYTES).to_json(indent=None))
    except VerificationStop as exc:
        raise ContractError(f'layer classification {exc.status}: {exc.reason}') from exc
    universe=_universe(snapshot,include_obstacles=include_obstacles)
    rows={}
    def add(entities,collection,trade,category):
        for entity in entities:
            system=category
            basis=[f'{collection}:canonical_kind']
            missing=[]
            status='unresolved'
            if collection in ('electrical_devices','electrical_equipment'):
                if isinstance(entity.system,str) and entity.system.strip():
                    system=entity.system
                    basis.append(f'{collection}.{entity.id}.system')
                else:
                    system='unclassified'
                    missing.append('system')
                status,reason,_=scope_metadata_facts(entity.attributes.get('pdf_electrical'))
                basis.append(f'{collection}.{entity.id}.attributes.pdf_electrical.scope_status')
                if status=='unresolved':missing.append('construction_status:'+str(reason))
            else:
                missing.append('construction_status:no_canonical_fact')
            rows[entity.id]={'id':entity.id,'collection':collection,'trade':trade,'system':system,
                'construction_status':status,'level_id':universe.get(entity.id),
                'basis':basis,'missing_facts':missing}
    for collection,trade,category in (
        ('walls','architecture','walls'),('slabs','architecture','slabs'),
        ('spaces','architecture','spaces'),('ceilings','architecture','ceilings'),
        ('openings','architecture','openings'),('building_assemblies','architecture','assemblies'),
        ('assembly_components','architecture','assembly_components'),
        ('electrical_devices','electrical','unclassified'),('electrical_equipment','electrical','unclassified'),
        ('electrical_boxes','electrical','boxes'),('routes','electrical','raceway'),
        ('route_fittings','electrical','raceway'),('structural_members','structure','members'),
        ('support_assemblies','electrical','shared_supports'),('support_components','electrical','shared_supports'),
        ('support_interfaces','electrical','shared_supports'),
    ):
        add(getattr(snapshot,collection),collection,trade,category)
    if include_obstacles:
        add(snapshot.obstacles,'obstacles','unclassified','obstacles')
        for entity in snapshot.obstacles:rows[entity.id]['missing_facts'].append('trade')
    for component in snapshot.assembly_components:
        row=rows[component.id]
        row['system']='ceiling_grid' if component.component_kind in ('tbar_main_runner','tbar_cross_runner') else component.component_kind
        row['basis'].append(f'assembly_components.{component.id}.component_kind')
    # Membership is source-explicit in the resulting portable configuration.
    # Normal resolver ownership expansion still applies to selected host layers.
    memberships={}
    for entity_id in sorted(universe):
        row=rows[entity_id]
        for axis,key in (('trade','trade'),('system','system'),('phase','construction_status')):
            memberships.setdefault((axis,row[key]),[]).append(entity_id)
    layers=[]
    for (axis,value),members in sorted(memberships.items()):
        layers.append(LayerDefinition(id=stable_id('layer',json.dumps([axis,value],ensure_ascii=True)),
            name=f'{axis}: {value}',member_ids=tuple(members),**{axis:value}))
    config=LayerSelectionConfig(layers=tuple(layers),presets=(SavedPreset(id='preset:classified-all',
        name='Classified all visible',visible_layer_ids=tuple(layer.id for layer in layers)),))
    source=snapshot.to_json(indent=None)
    manifest={'kind':'oabm.canonical-layer-classification','version':'1.0.0',
        'source':{'model_id':snapshot.model_id,'sha256':hashlib.sha256(source.encode()).hexdigest()},
        'include_obstacles':include_obstacles,'rows':[rows[i] for i in sorted(universe)],
        'disclosures':['Missing facts remain unclassified/unresolved; labels do not assert missing systems exist.',
            'Canonical kind categories are presentation categories, not inferred engineering systems.',
            'Shared supports do not inherit supported systems; circuit electrical phase is not construction status.',
            'Resolver host/assembly membership expands through canonical ownership, so layers can overlap.']}
    return LayerClassification(source,config.to_json(indent=None),json.dumps(manifest,sort_keys=True,indent=2))
