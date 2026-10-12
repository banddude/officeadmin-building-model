"""Explicit shared installation inventory; envelopes are not exact profiles."""
from dataclasses import dataclass
import re
from .common import ContractError,Entity,Pose,Size3,_validate_id
from .physical import CatalogProduct,box_contains_point
from .raceway import RuleReference

SUPPORT_SUBSTRATES=frozenset({'wood','steel','concrete','masonry'})

def unique_refs(values,label):
    if not values or len(values)!=len(set(values)):
        raise ContractError(f'{label} requires nonempty unique references')
    for value in values:
        _validate_id(value,label)

def text(value,label):
    if not isinstance(value,str) or not value.strip():
        raise ContractError(f'{label} must be nonempty')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportAssembly(Entity):
    intent_id: str
    assembly_type: str
    level_id: str
    supported_object_ids: tuple[str,...]
    component_ids: tuple[str,...]
    interface_ids: tuple[str,...]
    basis: RuleReference

    def __post_init__(self):
        super(SupportAssembly,self).__post_init__()
        _validate_id(self.intent_id,'intent_id');_validate_id(self.level_id,'level_id')
        text(self.assembly_type,'assembly_type')
        for field in ('supported_object_ids','component_ids','interface_ids'):
            unique_refs(getattr(self,field),field)
        if not isinstance(self.basis,RuleReference):
            raise ContractError('support assembly requires typed caller rule reference')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportComponent(Entity):
    assembly_id: str
    part_key: str
    level_id: str
    category: str
    component_type: str
    product: CatalogProduct
    pose: Pose
    size: Size3
    geometry_basis: str = 'catalog_exterior_envelope'

    def __post_init__(self):
        super(SupportComponent,self).__post_init__()
        for field in ('assembly_id','part_key','level_id'):
            _validate_id(getattr(self,field),field)
        if self.category not in {'support','fastener'}:
            raise ContractError('component category must be support or fastener')
        text(self.component_type,'component_type')
        if not isinstance(self.product,CatalogProduct) or not isinstance(self.pose,Pose) or not isinstance(self.size,Size3):
            raise ContractError('component requires typed product, pose and catalog envelope')
        if self.geometry_basis!='catalog_exterior_envelope':
            raise ContractError('support component geometry is only a catalog exterior envelope')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportInterface(Entity):
    assembly_id: str
    interface_key: str
    level_id: str
    host_id: str
    substrate: str
    pose: Pose
    component_ids: tuple[str,...]

    def __post_init__(self):
        super(SupportInterface,self).__post_init__()
        for field in ('assembly_id','interface_key','level_id','host_id'):
            _validate_id(getattr(self,field),field)
        if self.substrate not in SUPPORT_SUBSTRATES:
            raise ContractError('support interface requires an explicit supported actual substrate')
        if not isinstance(self.pose,Pose):
            raise ContractError('support interface requires an explicit pose')
        unique_refs(self.component_ids,'interface.component_ids')

@dataclass(frozen=True,slots=True,kw_only=True)
class SupportSelectionOutcome(Entity):
    intent_id: str
    supported_object_ids: tuple[str,...]
    status: str
    selected_assembly_id: str | None = None
    requested_level_id: str | None = None
    requested_basis: RuleReference | None = None
    reason_codes: tuple[str,...] = ()

    def __post_init__(self):
        super(SupportSelectionOutcome,self).__post_init__()
        _validate_id(self.intent_id,'intent_id')
        unique_refs(self.supported_object_ids,'supported_object_ids')
        for field in ('selected_assembly_id','requested_level_id'):
            if getattr(self,field) is not None:
                _validate_id(getattr(self,field),field)
        if self.requested_basis is not None and not isinstance(self.requested_basis,RuleReference):
            raise ContractError('support outcome basis must be typed')
        if len(set(self.reason_codes))!=len(self.reason_codes) or any(
            not isinstance(r,str) or re.fullmatch(r'[a-z][a-z0-9_]*',r) is None for r in self.reason_codes):
            raise ContractError('support reasons must be unique tokens')
        if self.status=='selected':
            if (self.selected_assembly_id is None or self.requested_level_id is None or
                self.requested_basis is None or self.reason_codes):
                raise ContractError('selected support outcome requires assembly, level, basis and no reasons')
        elif self.status=='unresolved':
            if self.selected_assembly_id is not None or not self.reason_codes:
                raise ContractError('unresolved support outcome requires reasons and no assembly')
        else:
            raise ContractError('support outcome status must be selected or unresolved')

def component_contains_point(component,point):
    return box_contains_point(component,point)


def component_envelope_key(component):
    """World corners at nanometre precision; box symmetries share one key.

    This detects duplicate catalog envelopes, not nearby separate hardware.
    """
    q=component.pose.rotation
    u=(q.x,q.y,q.z)
    def cross(a,b):
        return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    corners=[]
    p=component.pose.position
    for x in (-component.size.x/2,component.size.x/2):
        for y in (-component.size.y/2,component.size.y/2):
            for z in (-component.size.z/2,component.size.z/2):
                v=(x,y,z);first=cross(u,v);second=cross(u,first)
                corners.append(tuple(round(origin+v[i]+2*q.w*first[i]+2*second[i],9)
                                     for i,origin in enumerate((p.x,p.y,p.z))))
    return component.product,tuple(sorted(corners))


def supported_levels(model,identity):
    """Known endpoint levels, without selecting a first host or inventing a level."""
    owners={e.id:e for e in (*model.electrical_devices,*model.electrical_equipment,*model.electrical_boxes)}
    if identity in owners:
        return {owners[identity].level_id}
    route=next((r for r in model.routes if r.id==identity),None)
    if route is None:
        raise ContractError('support references unsupported/nonphysical object')
    ports={p.id:p for p in model.ports}
    return {getattr(owners.get(ports[p].owner_id),'level_id',None)
            for p in (route.start_port_id,route.end_port_id)}
