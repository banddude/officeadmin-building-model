"""Typed plan-only site facts for the proposed opt-in utility-site contract.

These records never manufacture Z coordinates, physical equipment extents or
regulatory clearance approval. Unresolved service topology has no spatial ports.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from types import MappingProxyType

from .assemblies import Point2D
from .common import ContractError, Entity, _validate_id, provenance_applies_to


def _number(value, label, *, positive=False):
    if type(value) not in (int,float):
        raise ContractError(f'{label}: finite number required')
    try:finite=math.isfinite(value)
    except (OverflowError,ValueError) as exc:raise ContractError(f'{label}: finite numerical range required') from exc
    if not finite:raise ContractError(f'{label}: finite number required')
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
            origin=self.points[0]
            area=math.fsum((a.x-origin.x)*(b.y-origin.y)-(b.x-origin.x)*(a.y-origin.y) for a,b in edges)
            if not math.isfinite(area) or area==0:
                raise ContractError('site polygon must have finite nonzero area')
        for a,b in edges:
            _number(b.x-a.x,'site edge span');_number(b.y-a.y,'site edge span')
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
    coordinate_convention: str = 'page-top-left-x-right-y-down'

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
        # (a,b,c,d,tx,ty): X=a*x+b*y+tx; Y=c*x+d*y+ty.
        # Source coordinates are normalized, displayed-page coordinates with
        # top-left origin, +x right, +y down; PDF points are exactly 1/72 inch.
        if self.coordinate_convention!='page-top-left-x-right-y-down':
            raise ContractError('site source requires normalized displayed-page top-left coordinates')
        expected={'pdf_point':.0254/72,'m':1.,'mm':.001,'ft':.3048,'in':.0254}[self.source_units]*self.scale_denominator
        first=math.hypot(a,c);second=math.hypot(b,d)
        if (determinant>=0 or not math.isclose(first,expected,rel_tol=1e-10,abs_tol=0.) or
                not math.isclose(second,expected,rel_tol=1e-10,abs_tol=0.) or
                not math.isclose((a/first)*(b/second)+(c/first)*(d/second),0.,rel_tol=0.,abs_tol=1e-10)):
            raise ContractError('site source transform must be a metric similarity/reflection matching supplied printed scale; shear and unequal-axis scale are unsupported')


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
    extent_basis: str | None = None

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
        if self.kind=='gas_location':
            allowed=('source_marker',) if self.geometry.kind=='point' else ('source_outline','confirmed_envelope')
            if self.extent_basis not in allowed:raise ContractError('gas extent basis must distinguish marker, source outline and confirmed enclosure')
        elif self.extent_basis is not None:raise ContractError('extent_basis is only valid for gas location')


@dataclass(frozen=True,slots=True,kw_only=True)
class SiteRequirement(Entity):
    kind: str
    source_id: str
    target_id: str
    reference_basis: str
    minimum_m: float | None = None
    width_m: float | None = None
    depth_m: float | None = None
    orientation_radians: float | None = None

    def __post_init__(self):
        super(SiteRequirement,self).__post_init__()
        _validate_id(self.source_id,'site requirement source_id')
        _validate_id(self.target_id,'site requirement target_id')
        if self.source_id==self.target_id:
            raise ContractError('site requirement must reference distinct owners')
        if self.orientation_radians is not None:_number(self.orientation_radians,'working surface orientation_radians')
        if self.kind=='horizontal_reference_distance':
            if self.reference_basis!='point_to_point':
                raise ContractError('horizontal reference distance requires point-to-point anchors')
            if any(value is not None for value in (self.minimum_m,self.width_m,self.depth_m,self.orientation_radians)):
                raise ContractError('horizontal reference distance forbids minimum, width, depth and orientation')
        elif self.kind=='minimum_horizontal_separation':
            if not isinstance(self.reference_basis,str) or self.reference_basis not in {'point_to_point','anchor_to_boundary','envelope_to_envelope'}:
                raise ContractError('site separation requires an explicit geometric reference basis')
            _number(self.minimum_m,'site separation minimum_m',positive=True)
            if self.width_m is not None or self.depth_m is not None:
                raise ContractError('site separation cannot contain working-space dimensions')
            if self.orientation_radians is not None:raise ContractError('orientation is only applicable to working surfaces')
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
    front_normal_radians: float | None = None

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
        if self.front_normal_radians is not None:_number(self.front_normal_radians,'service equipment front_normal_radians')
        if self.equipment_rating_a is not None and self.continuous_meter_rating_a is not None and self.continuous_meter_rating_a>self.equipment_rating_a:
            raise ContractError('continuous meter rating exceeds equipment rating')


@dataclass(frozen=True,slots=True,kw_only=True)
class ServiceNode(Entity):
    """A supplied service-diagram owner, optionally tied to placed equipment.

    An unplaced node is a reference, not a fabricated ElectricalEquipment/Port.
    Plan position is XY only. A linked equipment owner supplies its own geometry.
    """
    equipment_type: str
    location_basis: str
    equipment_id: str | None = None
    plan_position: Point2D | None = None
    unresolved_reason: str | None = None
    evidence: tuple[SiteSourceEvidence,...] = ()

    def __post_init__(self):
        super(ServiceNode,self).__post_init__()
        if type(self.evidence) is not tuple or any(type(item) is not SiteSourceEvidence for item in self.evidence):
            raise ContractError('service node evidence must be a typed tuple')
        if not isinstance(self.equipment_type,str) or self.equipment_type not in {'source','meter_main','panelboard'}:
            raise ContractError('service node requires source, meter_main or panelboard type')
        if self.location_basis not in ('placed_equipment','plan_only','unresolved'):
            raise ContractError('unsupported service node location basis')
        if self.location_basis=='placed_equipment':
            _validate_id(self.equipment_id,'service node equipment_id')
            if self.plan_position is not None or self.unresolved_reason is not None:
                raise ContractError('placed service node cannot override equipment placement')
        else:
            if self.equipment_id is not None:raise ContractError('unplaced service reference cannot alias equipment')
            _text(self.unresolved_reason,'service node unresolved_reason')
            if self.location_basis=='plan_only':
                if not self.evidence:raise ContractError('plan-only service location requires source evidence')
                if type(self.plan_position) is not Point2D:raise ContractError('plan-only service node needs typed XY position')
                _number(self.plan_position.x,'service node x');_number(self.plan_position.y,'service node y')
            elif self.plan_position is not None:
                raise ContractError('unresolved service reference must not invent an origin or position')


@dataclass(frozen=True,slots=True,kw_only=True)
class ServiceConnection(Entity):
    """Explicit service arrangement; no invented ports, feeder size or route."""
    source_node_id: str
    target_node_id: str
    specification_status: str
    circuit_id: str | None = None

    def __post_init__(self):
        super(ServiceConnection,self).__post_init__()
        for name in ('source_node_id','target_node_id'):_validate_id(getattr(self,name),name)
        if self.source_node_id==self.target_node_id:raise ContractError('service connection must have distinct ends')
        if self.specification_status not in ('unresolved','canonical_circuit'):
            raise ContractError('service connection needs explicit specification status')
        if self.specification_status=='canonical_circuit':_validate_id(self.circuit_id,'service circuit_id')
        elif self.circuit_id is not None:raise ContractError('unresolved specification cannot claim a circuit')


@dataclass(frozen=True,slots=True,kw_only=True)
class PlanServiceLateral(Entity):
    """A plan-only private lateral, never a measured-depth 3D route."""
    source_node_id: str
    intercept_id: str
    property_boundary_id: str
    geometry: SitePlanGeometry
    nominal_diameter_m: float
    raceway_description: str

    def __post_init__(self):
        super(PlanServiceLateral,self).__post_init__()
        for name in ('source_node_id','intercept_id','property_boundary_id'):_validate_id(getattr(self,name),name)
        if type(self.geometry) is not SitePlanGeometry or self.geometry.kind!='polyline':
            raise ContractError('plan lateral needs an open plan-only polyline')
        _number(self.nominal_diameter_m,'lateral nominal_diameter_m',positive=True)
        _text(self.raceway_description,'lateral raceway_description')


SITE_FIELDS = frozenset({'site_features','site_requirements','service_equipment_facts',
                        'service_nodes','service_connections','plan_service_laterals'})


def has_site_inventory(model):
    return any(getattr(model,name,()) for name in SITE_FIELDS)


def reject_site_inventory(model,consumer):
    if has_site_inventory(model):
        raise ContractError(f'{consumer}: populated site/service inventory is unsupported; no geometry may be silently omitted')


def service_node_position(model,node):
    if node.equipment_id is not None:
        equipment=next(item for item in model.electrical_equipment if item.id==node.equipment_id)
        return Point2D(x=equipment.pose.position.x,y=equipment.pose.position.y)
    return node.plan_position


def validate_site_inventory(model):
    """Cross-record admission; does not open files or certify clearance."""
    features={item.id:item for item in model.site_features}
    nodes={item.id:item for item in model.service_nodes}
    equipment={item.id:item for item in model.electrical_equipment}
    owners={**features,**equipment,**nodes}
    def require(key,index,label):
        if key not in index:raise ContractError(f'{label}: missing or incompatible owner {key!r}')
        return index[key]
    def region_covers(owner,points,path):
        if any(record.derivation=='user' and provenance_applies_to(record,(path,)) for record in owner.provenance):
            return  # Explicit field override retains original source evidence.
        for point in points:
            covered=False
            for proof in owner.evidence:
                a,b,c,d,tx,ty=proof.source_to_model;det=a*d-b*c
                x=(d*(point.x-tx)-b*(point.y-ty))/det
                y=(-c*(point.x-tx)+a*(point.y-ty))/det
                x0,y0,x1,y1=proof.region
                if math.isfinite(x) and math.isfinite(y) and x0-1e-8<=x<=x1+1e-8 and y0-1e-8<=y<=y1+1e-8:
                    covered=True;break
            if not covered:raise ContractError('site position is outside supplied source region without explicit user override evidence')
    placed=[]
    for node in nodes.values():
        if any(item.model_frame_id!=model.coordinate_system.frame_id for item in node.evidence):
            raise ContractError('service node source frame differs from model frame')
        if node.location_basis=='plan_only':region_covers(node,(node.plan_position,),'plan_position')
        if node.equipment_id is not None:
            owner=require(node.equipment_id,equipment,node.id)
            if owner.equipment_type!=node.equipment_type:raise ContractError('service node contradicts placed equipment type')
            placed.append(owner.id)
    if len(set(placed))!=len(placed):raise ContractError('placed equipment cannot have duplicate service node owners')
    for feature in features.values():
        if any(evidence.model_frame_id!=model.coordinate_system.frame_id for evidence in feature.evidence):
            raise ContractError('site evidence frame differs from model frame')
        region_covers(feature,feature.geometry.points,'geometry')
        if feature.owner_id is not None:
            if feature.kind=='working_surface':require(feature.owner_id,{**nodes,**equipment},feature.id)
            elif feature.kind=='building_outline' and feature.outline_basis=='modeled_owner_projection':
                owner=require(feature.owner_id,{item.id:item for item in (*model.spaces,*model.slabs)},feature.id)
                expected=tuple((point.x,point.y) for point in owner.footprint.points)
                if tuple((point.x,point.y) for point in feature.geometry.points)!=expected:
                    raise ContractError('modeled building outline differs from canonical owner footprint')
            else:raise ContractError('site feature owner is unsupported for its kind')
    claimed=set()
    for facts in model.service_equipment_facts:
        owner=require(facts.equipment_id,{**nodes,**equipment},facts.id)
        identity=getattr(owner,'equipment_id',None) or owner.id
        if identity in claimed:raise ContractError('duplicate service facts for the same physical owner')
        claimed.add(identity)
        if owner.equipment_type=='meter_main' and facts.integrated_service_disconnect is False:
            raise ContractError('meter_main cannot contradict its integrated disconnect function')
        physical_owner=equipment.get(getattr(owner,'equipment_id',None)) if isinstance(owner,ServiceNode) else owner
        if physical_owner is not None and facts.equipment_rating_a is not None:
            annotation=physical_owner.attributes.get('single_line_drafting',{})
            if isinstance(annotation,dict) and annotation.get('bus_current_a') is not None and annotation['bus_current_a']!=facts.equipment_rating_a:
                raise ContractError('service rating contradicts existing single-line annotation')
        if facts.integrated_service_disconnect is True and owner.equipment_type!='meter_main':
            raise ContractError('integrated service disconnect must belong to one meter_main')
        if facts.main_lug_only is True and owner.equipment_type!='panelboard':
            raise ContractError('main-lug-only service facts require a panelboard')
        if facts.continuous_meter_rating_a is not None and owner.equipment_type not in {'meter','meter_main'}:
            raise ContractError('continuous metering rating requires meter equipment')
        if facts.circuit_positions is not None and owner.equipment_type not in {'panelboard','meter_main'}:
            raise ContractError('circuit positions require panel equipment')
    pairs=set();incoming={key:0 for key in nodes};outgoing={key:[] for key in nodes}
    ports={item.id:item for item in model.ports};circuits={item.id:item for item in model.circuits}
    for edge in model.service_connections:
        source=require(edge.source_node_id,nodes,edge.id);target=require(edge.target_node_id,nodes,edge.id)
        pair=(source.id,target.id)
        if pair in pairs:raise ContractError('duplicate service connection')
        pairs.add(pair);incoming[target.id]+=1;outgoing[source.id].append(target.id)
        if target.equipment_type=='source' or incoming[target.id]>1:
            raise ContractError('service graph supports explicit radial supply only')
        if edge.circuit_id is not None:
            circuit=require(edge.circuit_id,circuits,edge.id)
            if source.equipment_id is None or target.equipment_id is None:
                raise ContractError('unplaced service reference cannot claim canonical spatial ports')
            matching=[key for key in circuit.load_port_ids if require(key,ports,edge.id).owner_id==target.equipment_id]
            if require(circuit.source_port_id,ports,edge.id).owner_id!=source.equipment_id or len(matching)!=1:
                raise ContractError('service edge contradicts canonical circuit ownership')
        elif source.equipment_id is not None and target.equipment_id is not None:
            for circuit in model.circuits:
                if require(circuit.source_port_id,ports,edge.id).owner_id==source.equipment_id and target.equipment_id in {
                        require(key,ports,edge.id).owner_id for key in circuit.load_port_ids}:
                    raise ContractError('existing canonical circuit must be referenced, not duplicated as unresolved')
    pending=sorted(key for key,count in incoming.items() if count==0);visited=0
    while pending:
        key=pending.pop();visited+=1
        for target in outgoing[key]:
            incoming[target]-=1
            if incoming[target]==0:pending.append(target)
    if visited!=len(nodes):raise ContractError('service graph contains a cycle')
    for requirement in model.site_requirements:
        source=require(requirement.source_id,owners,requirement.id)
        target=require(requirement.target_id,owners,requirement.id)
        def shape(owner):
            if isinstance(owner,SiteFeature):return owner.geometry.kind
            if isinstance(owner,ServiceNode):return 'point' if service_node_position(model,owner) is not None else 'unknown'
            return 'envelope' if owner.size is not None else 'point'
        if requirement.kind=='working_surface_for':
            if not isinstance(source,SiteFeature) or source.kind!='working_surface' or source.owner_id!=target.id:
                raise ContractError('working requirement contradicts canonical surface owner')
        elif requirement.reference_basis=='point_to_point' and any(shape(owner) not in {'point','unknown'} for owner in (source,target)):
            raise ContractError('point separation requires two known point references')
        elif requirement.reference_basis=='anchor_to_boundary' and (shape(source) not in {'point','unknown'} or shape(target) not in {'polygon','envelope','unknown','point'}):
            raise ContractError('anchor separation requires a known anchor and boundary')
        # Unknown extents/working orientation are admissible requirements,
        # but their derived measurement must remain unresolved, never certified.
    for lateral in model.plan_service_laterals:
        node=require(lateral.source_node_id,nodes,lateral.id)
        intercept=require(lateral.intercept_id,features,lateral.id)
        boundary=require(lateral.property_boundary_id,features,lateral.id)
        if intercept.kind!='utility_intercept' or boundary.kind!='property_boundary':raise ContractError('lateral requires intercept and property boundary')
        start=service_node_position(model,node)
        if start is None or lateral.geometry.points[0]!=start or lateral.geometry.points[-1]!=intercept.geometry.points[0]:
            raise ContractError('stale plan lateral endpoints differ from current canonical owners')
        point=intercept.geometry.points[0];polygon=boundary.geometry.points
        # Explicit small metre tolerance only for source-derived point-on-line admission.
        def touches(a,b):
            dx,dy=b.x-a.x,b.y-a.y
            t=max(0.,min(1.,((point.x-a.x)*dx+(point.y-a.y)*dy)/(dx*dx+dy*dy)))
            return math.hypot(point.x-a.x-t*dx,point.y-a.y-t*dy)<=1e-8
        if not any(touches(a,b) for a,b in zip(polygon,(*polygon[1:],polygon[0]))):
            raise ContractError('utility intercept is not on the supplied property boundary')
