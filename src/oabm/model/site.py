"""Typed plan-only site facts for the proposed opt-in utility-site contract.

These types are not admitted to BuildingModel until the versioned reader,
cross-reference rules and consumer guards are integrated. They never manufacture
Z coordinates, physical equipment extents or regulatory clearance approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from types import MappingProxyType

from .assemblies import Point2D
from .common import ContractError, Entity, _validate_id


def _number(value, label, *, positive=False):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ContractError(f'{label}: finite number required')
    if positive and value <= 0:
        raise ContractError(f'{label}: positive number required')
    return value


def _text(value,label):
    if not isinstance(value,str) or not value.strip():
        raise ContractError(f'{label}: nonempty text required')


def _turn(a,b,c):
    return _number((b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x),'site geometry orientation')


def _on_segment(a,b,p):
    return _turn(a,b,p) == 0 and min(a.x,b.x) <= p.x <= max(a.x,b.x) and min(a.y,b.y) <= p.y <= max(a.y,b.y)


def _intersects(a,b,c,d):
    turns=(_turn(a,b,c),_turn(a,b,d),_turn(c,d,a),_turn(c,d,b))
    return ((turns[0]>0) != (turns[1]>0) and (turns[2]>0) != (turns[3]>0)
            and all(value != 0 for value in turns)) or any(
                (_on_segment(a,b,c),_on_segment(a,b,d),_on_segment(c,d,a),_on_segment(c,d,b)))


@dataclass(frozen=True,slots=True,kw_only=True)
class SitePlanGeometry:
    """Explicit XY metres in the model frame. Elevation is not represented."""
    kind: str
    points: tuple[Point2D,...]

    def __post_init__(self):
        if not isinstance(self.kind,str) or self.kind not in {'point','polyline','polygon'}:
            raise ContractError('site geometry kind must be point, polyline or polygon')
        if not isinstance(self.points,tuple) or any(type(point) is not Point2D for point in self.points):
            raise ContractError('site geometry points must be a typed Point2D tuple')
        for point in self.points:
            _number(point.x,'site point.x');_number(point.y,'site point.y')
        count=len(self.points)
        if count > 256:
            raise ContractError('site geometry exceeds 256 points; no points truncated')
        if (self.kind=='point' and count!=1) or (self.kind=='polyline' and count<2) or (self.kind=='polygon' and count<3):
            raise ContractError('site geometry has incompatible point count')
        if len(set((p.x,p.y) for p in self.points)) != count:
            raise ContractError('site geometry has duplicate points; polygon closure is implicit')
        edges=list(zip(self.points,self.points[1:]))
        if self.kind=='polygon':
            edges.append((self.points[-1],self.points[0]))
            area=sum(a.x*b.y-b.x*a.y for a,b in edges)
            if not math.isfinite(area) or area==0:
                raise ContractError('site polygon must have finite nonzero area')
        for i,(a,b) in enumerate(edges):
            for j,(c,d) in enumerate(edges[i+1:],i+1):
                adjacent=j==i+1 or (self.kind=='polygon' and i==0 and j==len(edges)-1)
                if adjacent:
                    # Adjacent edges share their endpoint, but cannot backtrack.
                    u,v,w=(a,b,d) if j==i+1 else (c,d,b)
                    dot=_number((u.x-v.x)*(w.x-v.x)+(u.y-v.y)*(w.y-v.y),'site adjacent edges')
                    if _turn(u,v,w)==0 and dot>0:
                        raise ContractError('site geometry has overlapping adjacent edges')
                elif _intersects(a,b,c,d):
                    raise ContractError('site geometry must be simple, without intersecting edges')


@dataclass(frozen=True,slots=True,kw_only=True)
class SiteSourceEvidence:
    """Original source units/region and explicit source-to-model affine map.

    The affine map is authoritative for conversion to canonical XY metres;
    callers must not multiply by the printed scale a second time. File contents
    are resolved and hashed at input/export admission, not read by this record.
    """
    source_id: str
    sha256: str
    page: int
    sheet_id: str
    model_frame_id: str
    source_units: str
    drawing_units: str
    scale_denominator: float
    region: tuple[float,float,float,float]
    source_to_model: tuple[float,float,float,float,float,float]

    def __post_init__(self):
        _validate_id(self.source_id,'site source_id')
        if not isinstance(self.sha256,str) or not re.fullmatch('[0-9a-f]{64}',self.sha256):
            raise ContractError('site source sha256 must be a lowercase SHA-256 digest')
        if type(self.page) is not int or self.page<1:
            raise ContractError('site source page must be one-based')
        _text(self.sheet_id,'site source sheet_id')
        _validate_id(self.model_frame_id,'site source model_frame_id')
        if not isinstance(self.source_units,str) or not isinstance(self.drawing_units,str) or self.source_units not in {'pdf_point','m','mm','ft','in'} or self.drawing_units not in {'m','mm','ft','in'}:
            raise ContractError('site source must declare supported original and drawing units')
        _number(self.scale_denominator,'site source scale_denominator',positive=True)
        if type(self.region) is not tuple or len(self.region)!=4:
            raise ContractError('site source region must have four bounds')
        for value in self.region:_number(value,'site source region')
        x0,y0,x1,y1=self.region
        if x0>=x1 or y0>=y1:raise ContractError('site source region must have positive extent')
        if type(self.source_to_model) is not tuple or len(self.source_to_model)!=6:
            raise ContractError('site source transform must have six coefficients')
        for value in self.source_to_model:_number(value,'site source transform')
        a,b,c,d,_,_=self.source_to_model
        determinant=a*d-b*c
        if not math.isfinite(determinant) or determinant==0:
            raise ContractError('site source transform must be finite and invertible')


SITE_KINDS = MappingProxyType({
    'property_boundary': frozenset({'polygon'}),
    'building_outline': frozenset({'polygon'}),
    'street_reference': frozenset({'polyline'}),
    'gas_location': frozenset({'point','polygon'}),
    'utility_intercept': frozenset({'point'}),
    'working_surface': frozenset({'polygon'}),
})


@dataclass(frozen=True,slots=True,kw_only=True)
class SiteFeature(Entity):
    kind: str
    geometry: SitePlanGeometry
    status: str
    evidence: tuple[SiteSourceEvidence,...]
    owner_id: str | None = None
    outline_basis: str | None = None

    def __post_init__(self):
        super(SiteFeature,self).__post_init__()
        if not isinstance(self.kind,str) or self.kind not in SITE_KINDS or type(self.geometry) is not SitePlanGeometry or self.geometry.kind not in SITE_KINDS[self.kind]:
            raise ContractError('site feature kind and plan geometry are incompatible')
        if not isinstance(self.status,str) or self.status not in {'existing','proposed','unknown'}:
            raise ContractError('site feature status must be explicit existing/proposed/unknown')
        if type(self.evidence) is not tuple or not self.evidence or any(type(item) is not SiteSourceEvidence for item in self.evidence):
            raise ContractError('site feature requires typed source evidence')
        if len({item.model_frame_id for item in self.evidence})!=1:
            raise ContractError('site evidence must target one model frame')
        if self.owner_id is not None:_validate_id(self.owner_id,'site feature owner_id')
        if self.kind=='working_surface' and self.owner_id is None:
            raise ContractError('working surface requires its canonical equipment owner')
        if self.kind=='building_outline':
            if not isinstance(self.outline_basis,str) or self.outline_basis not in {'source_traced','modeled_owner_projection'}:
                raise ContractError('building outline requires explicit source-traced or modeled-owner basis')
            if self.outline_basis=='modeled_owner_projection' and self.owner_id is None:
                raise ContractError('modeled building outline requires an owner')
        elif self.outline_basis is not None:
            raise ContractError('outline_basis is only valid for building outlines')


@dataclass(frozen=True,slots=True,kw_only=True)
class SiteRequirement(Entity):
    kind: str
    source_id: str
    target_id: str
    reference_basis: str
    minimum_m: float | None = None
    width_m: float | None = None
    depth_m: float | None = None

    def __post_init__(self):
        super(SiteRequirement,self).__post_init__()
        _validate_id(self.source_id,'site requirement source_id')
        _validate_id(self.target_id,'site requirement target_id')
        if self.source_id==self.target_id:
            raise ContractError('site requirement must reference distinct owners')
        if self.kind=='minimum_horizontal_separation':
            if not isinstance(self.reference_basis,str) or self.reference_basis not in {'point_to_point','anchor_to_boundary','envelope_to_envelope'}:
                raise ContractError('site separation requires an explicit geometric reference basis')
            _number(self.minimum_m,'site separation minimum_m',positive=True)
            if self.width_m is not None or self.depth_m is not None:
                raise ContractError('site separation cannot contain working-space dimensions')
        elif self.kind=='working_surface_for':
            if self.reference_basis!='surface_to_equipment' or self.minimum_m is not None:
                raise ContractError('working surface requires surface-to-equipment basis')
            _number(self.width_m,'working surface width_m',positive=True)
            _number(self.depth_m,'working surface depth_m',positive=True)
        else:
            raise ContractError('unsupported site requirement kind')


@dataclass(frozen=True,slots=True,kw_only=True)
class ServiceEquipmentFacts(Entity):
    equipment_id: str
    equipment_rating_a: float | None = None
    continuous_meter_rating_a: float | None = None
    integrated_service_disconnect: bool | None = None
    main_lug_only: bool | None = None
    circuit_positions: int | None = None

    def __post_init__(self):
        super(ServiceEquipmentFacts,self).__post_init__()
        _validate_id(self.equipment_id,'service facts equipment_id')
        for name in ('equipment_rating_a','continuous_meter_rating_a'):
            if getattr(self,name) is not None:_number(getattr(self,name),name,positive=True)
        for name in ('integrated_service_disconnect','main_lug_only'):
            if getattr(self,name) is not None and type(getattr(self,name)) is not bool:
                raise ContractError(f'{name}: explicit boolean or null required')
        if self.integrated_service_disconnect is True and self.main_lug_only is True:
            raise ContractError('integrated service disconnect cannot also be main-lug-only')
        if self.circuit_positions is not None and (type(self.circuit_positions) is not int or self.circuit_positions<=0):
            raise ContractError('circuit_positions must be a positive integer or null')
        if self.equipment_rating_a is not None and self.continuous_meter_rating_a is not None and self.continuous_meter_rating_a>self.equipment_rating_a:
            raise ContractError('continuous meter rating exceeds equipment rating')
