"""Explicit bounded native IFC4 intake; never the OABM metadata round-trip reader."""
from __future__ import annotations

import hashlib
import itertools
import math
import re
from dataclasses import dataclass
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.placement
import ifcopenshell.validate
import numpy as np

from oabm.model import (BuildingModel, CoordinateSystem, ContractError, Level, Wall,
    Opening, Point3, Polyline3D, Pose, Quaternion, Size3, Provenance, stable_id)

MAX_BYTES = 2_000_000
MAX_ENTITIES = 10000
MAX_PRODUCTS = 256
MAX_PLACEMENT_DEPTH = 32
MAX_CURVE_POINTS = 2048
TOLERANCE_M = 1e-7


class NativeIfcImportError(ContractError):
    """Invalid global context; message contains reason codes, never source text."""


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NativeIfcDiagnostic:
    native_guid: str
    native_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class NativeIfcImportResult:
    source_id: str
    source_sha256: str
    source_schema: str
    status: str
    model: BuildingModel | None
    diagnostics: tuple[NativeIfcDiagnostic, ...]
    supported_walls: int
    supported_openings: int

    @property
    def complete(self):
        return self.status == 'complete'

    def validate_against(self,source,*,source_id):
        current=import_native_ifc(source,source_id=source_id)
        before_model=None if self.model is None else self.model.to_json()
        current_model=None if current.model is None else current.model.to_json()
        if (self.source_id,self.source_sha256,self.source_schema,self.status,self.diagnostics,
            self.supported_walls,self.supported_openings,before_model)!=(
            current.source_id,current.source_sha256,current.source_schema,current.status,current.diagnostics,
            current.supported_walls,current.supported_openings,current_model):
            raise NativeIfcImportError('native_source_or_result_binding_mismatch')


def _number(value, *, positive=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        raise _Unsupported('invalid_numeric_evidence')
    try: value=float(value)
    except (ValueError,OverflowError):raise _Unsupported('invalid_numeric_evidence') from None
    if not math.isfinite(value) or positive and value<=0:
        raise _Unsupported('invalid_numeric_evidence')
    return value


def _coordinates(point,dimension):
    if point is None or not point.is_a('IfcCartesianPoint') or point.Coordinates is None or len(point.Coordinates)!=dimension:
        raise _Unsupported('invalid_coordinate_dimension')
    return np.array([_number(v) for v in point.Coordinates])


def _direction(direction,default,dimension):
    if direction is not None and not direction.is_a('IfcDirection'):raise _Unsupported('invalid_direction_type')
    values=default if direction is None else direction.DirectionRatios
    if values is None or len(values)!=dimension:raise _Unsupported('invalid_direction_dimension')
    vector=np.array([_number(v) for v in values])
    length=math.hypot(*vector)
    if not math.isfinite(length) or length<1e-12:raise _Unsupported('invalid_direction')
    return vector/length


def _axis(position,*,horizontal=False):
    if position is None:return np.eye(4)
    if not position.is_a('IfcAxis2Placement3D'):raise _Unsupported('unsupported_placement_type')
    location=_coordinates(position.Location,3)
    z=_direction(position.Axis,(0.,0.,1.),3)
    x=_direction(position.RefDirection,(1.,0.,0.),3)
    if abs(float(np.dot(x,z)))>1e-9:raise _Unsupported('nonorthogonal_placement')
    if horizontal and not np.allclose(z,(0,0,1),atol=1e-9,rtol=0):
        raise _Unsupported('tilted_placement')
    matrix=ifcopenshell.util.placement.get_axis2placement(position)
    if not np.isfinite(matrix).all() or not np.allclose(matrix[:3,3],location,atol=1e-9,rtol=0):
        raise _Unsupported('invalid_placement')
    return matrix


def _axis_evidence(position):
    if position is None:return None
    data={'type':position.is_a(),'location':list(position.Location.Coordinates)}
    if position.is_a('IfcAxis2Placement3D'):
        data['axis']=None if position.Axis is None else list(position.Axis.DirectionRatios)
    data['reference_direction']=None if position.RefDirection is None else list(position.RefDirection.DirectionRatios)
    return data


class _Reader:
    def __init__(self,file,source_id,source_sha):
        self.file=file;self.source_id=source_id;self.sha=source_sha
        self.cache={};self.chains={}
        self.project,self.context,self.scale,self.units=self._global_context()
        if self.context.WorldCoordinateSystem is None:raise NativeIfcImportError('missing_world_coordinate_system')
        self.world=_axis(self.context.WorldCoordinateSystem,horizontal=True)
        for placement in file.by_type('IfcLocalPlacement'):
            self.placement(placement)  # cycles/depth/tilt/finite context fail globally
        self.parents={}
        for relation in file.by_type('IfcRelAggregates'):
            for child in relation.RelatedObjects:
                self.parents.setdefault(child.id(),[]).append(relation.RelatingObject)
        self.storeys={s.GlobalId:s for s in file.by_type('IfcBuildingStorey')}
        if not self.storeys:raise NativeIfcImportError('missing_usable_storey')
        self.levels={}
        for guid,storey in sorted(self.storeys.items()):
            self._project_ownership(storey)
            if storey.ObjectPlacement is None:raise NativeIfcImportError('missing_storey_placement')
            matrix=self.world@self.placement(storey.ObjectPlacement)
            if not np.isfinite(matrix).all() or not np.allclose(matrix[:3,2],(0,0,1),atol=1e-9,rtol=0):
                raise NativeIfcImportError('tilted_or_nonfinite_storey_frame')
            raw=storey.Elevation
            if raw is not None:_number(raw)
            evidence={'guid':guid,'type':storey.is_a(),'placement_chain':self.chain(storey.ObjectPlacement),
                      'world_matrix_native':matrix.tolist(),'declared_elevation_native':raw,
                      'elevation_basis':'composed_storey_placement'}
            attrs=self.attrs(evidence)
            level=Level(id=self.identity('level',guid),elevation_m=float(matrix[2,3]*self.scale),
                height_m=None,attributes=attrs,provenance=self.provenance(guid,('elevation_m',),('placement_chain','world_matrix_native')))
            self.levels[guid]=level
        self.containment={}
        for relation in file.by_type('IfcRelContainedInSpatialStructure'):
            for product in relation.RelatedElements:
                self.containment.setdefault(product.GlobalId,[]).append(relation.RelatingStructure)
        if any(len(owners)!=1 for owners in self.containment.values()):
            raise NativeIfcImportError('conflicting_containment')

    def _global_context(self):
        projects=self.file.by_type('IfcProject')
        if len(projects)!=1:raise NativeIfcImportError('requires_one_project')
        project=projects[0]
        if project.UnitsInContext is None:raise NativeIfcImportError('missing_length_units')
        lengths=[u for u in project.UnitsInContext.Units if getattr(u,'UnitType',None)=='LENGTHUNIT']
        if len(lengths)!=1 or not lengths[0].is_a('IfcSIUnit') or lengths[0].Name!='METRE' or lengths[0].Prefix not in (None,'MILLI'):
            raise NativeIfcImportError('unsupported_or_ambiguous_length_units')
        prefix=lengths[0].Prefix
        contexts=list(project.RepresentationContexts or ())
        if len(contexts)!=1 or not contexts[0].is_a('IfcGeometricRepresentationContext') or contexts[0].is_a('IfcGeometricRepresentationSubContext') or contexts[0].CoordinateSpaceDimension!=3:
            raise NativeIfcImportError('ambiguous_or_unsupported_context')
        context=contexts[0]
        if context.TrueNorth is not None or self.file.by_type('IfcCoordinateOperation') or self.file.by_type('IfcProjectedCRS'):
            raise NativeIfcImportError('unsupported_georeferencing')
        for site in self.file.by_type('IfcSite'):
            if site.RefLatitude is not None or site.RefLongitude is not None or site.RefElevation is not None:
                raise NativeIfcImportError('unsupported_georeferencing')
        return project,context,.001 if prefix=='MILLI' else 1.,{'name':'METRE','prefix':prefix}

    def _project_ownership(self,storey):
        current=storey;seen=set()
        for _ in range(MAX_PLACEMENT_DEPTH):
            if current==self.project:return
            if current.id() in seen:raise NativeIfcImportError('spatial_ownership_cycle')
            seen.add(current.id())
            parents=self.parents.get(current.id(),[])
            if len(parents)!=1:raise NativeIfcImportError('missing_or_ambiguous_project_storey_ownership')
            current=parents[0]
        raise NativeIfcImportError('spatial_ownership_depth_exceeded')

    def placement(self,placement):
        if placement is None:raise _Unsupported('missing_product_placement')
        if placement.id() in self.cache:return self.cache[placement.id()]
        nodes=[];seen=set();current=placement
        while current is not None:
            if not current.is_a('IfcLocalPlacement'):raise NativeIfcImportError('unsupported_object_placement')
            if current.id() in seen:raise NativeIfcImportError('placement_cycle')
            seen.add(current.id());nodes.append(current)
            if len(nodes)>MAX_PLACEMENT_DEPTH:raise NativeIfcImportError('placement_depth_exceeded')
            current=current.PlacementRelTo
        matrix=np.eye(4);evidence=[]
        for node in reversed(nodes):
            if node.RelativePlacement is None:raise NativeIfcImportError('missing_relative_placement')
            matrix=matrix@_axis(node.RelativePlacement)
            evidence.append(_axis_evidence(node.RelativePlacement))
            if not np.isfinite(matrix).all():raise NativeIfcImportError('nonfinite_placement_chain')
        self.cache[placement.id()]=matrix;self.chains[placement.id()]=evidence
        return matrix

    def chain(self,placement):
        self.placement(placement)
        return self.chains[placement.id()]

    def identity(self,kind,guid):
        return stable_id(kind,f'native-ifc:{self.source_id}:{guid}')

    def attrs(self,evidence):
        return {'native_ifc':{'source_id':self.source_id,'source_sha256':self.sha,
            'schema':'IFC4','length_units':self.units,'observations':evidence}}

    def provenance(self,guid,derived_fields,consumed):
        return (Provenance(source_kind='ifc-native',source_id=self.source_id,source_element_id=guid,
                    method='native-ifc-source-evidence-v1',derivation='observed',scope_paths=('attributes.native_ifc',)),
                Provenance(source_kind='ifc-native',source_id=self.source_id,source_element_id=guid,
                    method='native-ifc-exact-prism-conversion-v1',derivation='inferred',scope_paths=derived_fields,
                    attributes={'source_sha256':self.sha,'consumed_native_evidence':list(consumed)}))

    def representation(self,product,identifier):
        if product.Representation is None:raise _Unsupported('missing_'+identifier.lower())
        reps=[r for r in product.Representation.Representations if r.RepresentationIdentifier==identifier]
        if len(reps)!=1 or not reps[0].is_a('IfcShapeRepresentation') or len(reps[0].Items)!=1:
            raise _Unsupported('missing_or_ambiguous_'+identifier.lower())
        context=reps[0].ContextOfItems
        if context is None:raise _Unsupported('missing_representation_context')
        if context.is_a('IfcGeometricRepresentationSubContext'):context=context.ParentContext
        if context!=self.context:raise _Unsupported('representation_context_conflict')
        return reps[0],reps[0].Items[0]

    def prism(self,product):
        representation,solid=self.representation(product,'Body')
        if solid.is_a()!='IfcExtrudedAreaSolid':raise _Unsupported('unsupported_body_not_single_extruded_prism')
        if representation.RepresentationType!='SweptSolid':raise _Unsupported('unsupported_body_representation_type')
        depth=_number(solid.Depth,positive=True)
        direction=_direction(solid.ExtrudedDirection,None,3)
        if abs(direction[0])>1e-9 or abs(direction[1])>1e-9:
            raise _Unsupported('oblique_extrusion')
        profile=solid.SweptArea
        if profile is None or profile.ProfileType!='AREA':raise _Unsupported('unsupported_profile')
        profile_transform=np.eye(4)
        if profile.is_a()=='IfcRectangleProfileDef':
            x=_number(profile.XDim,positive=True);y=_number(profile.YDim,positive=True)
            points=np.array([(-x/2,-y/2),(x/2,-y/2),(x/2,y/2),(-x/2,y/2)])
            if profile.Position is not None:
                if not profile.Position.is_a('IfcAxis2Placement2D'):raise _Unsupported('unsupported_profile_placement')
                location=_coordinates(profile.Position.Location,2)
                x_axis=_direction(profile.Position.RefDirection,(1.,0.),2)
                profile_transform[:2,:2]=np.column_stack((x_axis,(-x_axis[1],x_axis[0])))
                profile_transform[:2,3]=location
            profile_evidence={'type':profile.is_a(),'dimensions_native':[x,y],
                'position':_axis_evidence(profile.Position)}
        elif profile.is_a()=='IfcArbitraryClosedProfileDef':
            curve=profile.OuterCurve
            if curve is None or not curve.is_a('IfcPolyline') or curve.Points is None or len(curve.Points)!=5:
                raise _Unsupported('unsupported_profile_not_closed_rectangle')
            polygon=np.array([_coordinates(p,2) for p in curve.Points])
            if not np.allclose(polygon[0],polygon[-1],atol=TOLERANCE_M/self.scale,rtol=0):
                raise _Unsupported('profile_not_closed')
            points=polygon[:-1]
            edges=np.roll(points,-1,axis=0)-points
            lengths=np.linalg.norm(edges,axis=1)
            if np.any(lengths*self.scale<=TOLERANCE_M) or any(abs(float(np.dot(edges[i]/lengths[i],edges[(i+1)%4]/lengths[(i+1)%4])))>1e-9 for i in range(4)) or not np.allclose(points[0]+points[2],points[1]+points[3],atol=TOLERANCE_M/self.scale,rtol=0):
                raise _Unsupported('profile_not_rectangle')
            profile_evidence={'type':profile.is_a(),'closed_polyline_native':polygon.tolist()}
        else:raise _Unsupported('unsupported_profile')
        product_matrix=self.world@self.placement(product.ObjectPlacement)
        solid_matrix=_axis(solid.Position)
        total=product_matrix@solid_matrix@profile_transform
        vertices=np.array([[p[0],p[1],0.,1.] for p in points])
        top=vertices.copy();top[:,:3]+=direction*depth
        with np.errstate(over='ignore',invalid='ignore'):
            corners=(total@np.vstack((vertices,top)).T).T[:,:3]*self.scale
        if not np.isfinite(corners).all():raise _Unsupported('nonfinite_prism')
        evidence={'guid':product.GlobalId,'type':product.is_a(),
            'placement_chain':self.chain(product.ObjectPlacement),
            'product_world_matrix_native':product_matrix.tolist(),
            'body':{'representation_type':representation.RepresentationType,'profile':profile_evidence,
                'solid_position':_axis_evidence(solid.Position),'depth_native':depth,
                'extrusion_direction_native':list(solid.ExtrudedDirection.DirectionRatios)},
            'composed_matrix_native':total.tolist()}
        return corners,total,direction,evidence

    def wall(self,product):
        owners=self.containment.get(product.GlobalId,[])
        if len(owners)!=1 or not owners[0].is_a('IfcBuildingStorey') or owners[0].GlobalId not in self.levels:
            raise _Unsupported('missing_usable_storey_ownership')
        corners,total,direction,evidence=self.prism(product)
        world_direction=total[:3,:3]@direction
        if abs(world_direction[0])>1e-9 or abs(world_direction[1])>1e-9:
            raise _Unsupported('wall_extrusion_not_vertical')
        rep,axis=self.representation(product,'Axis')
        if not axis.is_a('IfcPolyline') or len(axis.Points)!=2:
            raise _Unsupported('wall_axis_not_straight_polyline')
        points=[]
        for p in axis.Points:
            coords=list(p.Coordinates)
            if len(coords) not in (2,3):raise _Unsupported('invalid_axis_coordinates')
            coords=[_number(v) for v in coords]
            if len(coords)==2:coords.append(0.)
            points.append(coords)
        native_axis=np.array(points)
        world_axis=(self.world@self.placement(product.ObjectPlacement)@np.column_stack((native_axis,np.ones(2))).T).T[:,:3]*self.scale
        delta=world_axis[1]-world_axis[0];length=math.hypot(*delta)
        if not math.isfinite(length) or length<=TOLERANCE_M or abs(delta[2])>TOLERANCE_M:
            raise _Unsupported('wall_axis_not_horizontal')
        u=delta/length
        if u[0]<-1e-9 or abs(u[0])<=1e-9 and u[1]<0:u=-u
        basis=np.column_stack((u,(-u[1],u[0],0),(0,0,1)))
        low,high=_box_bounds(corners,basis)
        axis_coords=world_axis@basis
        if not np.allclose(sorted(axis_coords[:,0]),(low[0],high[0]),atol=TOLERANCE_M,rtol=0) or not np.allclose(axis_coords[:,2],low[2],atol=TOLERANCE_M,rtol=0) or np.ptp(axis_coords[:,1])>TOLERANCE_M or not low[1]-TOLERANCE_M<=axis_coords[0,1]<=high[1]+TOLERANCE_M:
            raise _Unsupported('wall_axis_body_disagreement')
        middle=(low[1]+high[1])/2
        endpoints=[basis@np.array([x,middle,low[2]]) for x in (low[0],high[0])]
        evidence['axis']={'representation_type':rep.RepresentationType,'points_native':native_axis.tolist()}
        evidence['storey_guid']=owners[0].GlobalId
        wall=Wall(id=self.identity('wall',product.GlobalId),level_id=self.levels[owners[0].GlobalId].id,
            centerline=Polyline3D(points=tuple(Point3(x=float(p[0]),y=float(p[1]),z=float(p[2])) for p in endpoints)),
            thickness_m=float(high[1]-low[1]),height_m=float(high[2]-low[2]),construction=None,
            attributes=self.attrs(evidence),provenance=self.provenance(product.GlobalId,
                ('centerline','thickness_m','height_m','level_id'),('body','axis','placement_chain','storey_guid')))
        return wall,basis,low,high,owners[0].GlobalId

    def opening(self,product,hosts):
        relations=list(product.VoidsElements or ())
        if len(relations)!=1:raise _Unsupported('opening_requires_exactly_one_host')
        host=relations[0].RelatingBuildingElement
        if host is None or not host.is_a('IfcWall') or host.GlobalId not in hosts:raise _Unsupported('opening_host_not_imported')
        wall,basis,host_low,host_high,storey=hosts[host.GlobalId]
        owners=self.containment.get(product.GlobalId,[])
        if owners and owners[0].GlobalId!=storey:raise _Unsupported('opening_storey_conflict')
        if product.PredefinedType=='RECESS':raise _Unsupported('recess_not_supported')
        corners,_,_,evidence=self.prism(product)
        low,high=_box_bounds(corners,basis)
        # Through-wall boxes must cover host depth; asymmetric oversized cutters
        # are refused because the existing IFC view centers its cutter on pose.
        if low[1]>host_low[1]+TOLERANCE_M or high[1]<host_high[1]-TOLERANCE_M or abs((low[1]+high[1])-(host_low[1]+host_high[1]))>TOLERANCE_M:
            raise _Unsupported('opening_not_centered_through_host')
        if low[0]<host_low[0]-TOLERANCE_M or high[0]>host_high[0]+TOLERANCE_M or low[2]<host_low[2]-TOLERANCE_M or high[2]>host_high[2]+TOLERANCE_M:
            raise _Unsupported('opening_outside_host_extent')
        fills=list(product.HasFillings or ())
        if len(fills)>1:raise _Unsupported('ambiguous_opening_fill')
        opening_type='opening';filling=None
        if fills:
            element=fills[0].RelatedBuildingElement
            if element is None:raise _Unsupported('missing_opening_fill')
            if element.is_a('IfcWindow'):opening_type='window'
            elif element.is_a('IfcDoor'):opening_type='door'
            else:raise _Unsupported('unsupported_opening_fill')
            filling={'guid':element.GlobalId,'type':element.is_a(),'geometry_modeled':False}
        evidence.update(host_guid=host.GlobalId,void_relationship_guid=relations[0].GlobalId,
            filling=filling,storey_guid=storey)
        center=basis@((low+high)/2);angle=math.atan2(basis[1,0],basis[0,0])
        return Opening(id=self.identity('opening',product.GlobalId),host_id=wall.id,
            opening_type=opening_type,pose=Pose(position=Point3(x=float(center[0]),y=float(center[1]),z=float(center[2])),
                rotation=Quaternion(z=math.sin(angle/2),w=math.cos(angle/2))),
            size=Size3(x=float(high[0]-low[0]),y=float(high[1]-low[1]),z=float(high[2]-low[2])),
            attributes=self.attrs(evidence),provenance=self.provenance(product.GlobalId,
                ('pose','size','host_id','opening_type'),('body','placement_chain','host_guid','filling')))


def _box_bounds(corners,basis):
    coordinates=corners@basis
    low=coordinates.min(axis=0);high=coordinates.max(axis=0)
    if np.any(high-low<=TOLERANCE_M):raise _Unsupported('degenerate_box')
    expected=np.array(list(itertools.product(*zip(low,high))))
    distances=np.linalg.norm(coordinates[:,None,:]-expected[None,:,:],axis=2)
    if not np.all(np.min(distances,axis=0)<=TOLERANCE_M) or not np.all(np.min(distances,axis=1)<=TOLERANCE_M):
        raise _Unsupported('oblique_or_non_box_geometry')
    return low,high


def import_native_ifc(source: bytes | str | Path,*,source_id: str) -> NativeIfcImportResult:
    """Intake native bytes/path explicitly; all unsupported products remain visible."""
    if not isinstance(source_id,str) or not source_id.strip() or len(source_id)>128:
        raise NativeIfcImportError('stable_source_identity_required')
    if isinstance(source,bytes):data=source
    elif isinstance(source,(str,Path)):
        try:
            with Path(source).open('rb') as handle:data=handle.read(MAX_BYTES+1)
        except OSError:raise NativeIfcImportError('native_source_read_failed') from None
    else:raise NativeIfcImportError('source_requires_bytes_or_path')
    if len(data)>MAX_BYTES:raise NativeIfcImportError('source_byte_budget_exceeded')
    if len(re.findall(rb'(?m)^\s*#\d+\s*=',data))>MAX_ENTITIES:
        raise NativeIfcImportError('source_entity_budget_exceeded')
    sha=hashlib.sha256(data).hexdigest()
    try:file=ifcopenshell.file.from_string(data.decode('utf-8'))
    except Exception:raise NativeIfcImportError('native_ifc_parse_failed') from None
    if file.schema!='IFC4':raise NativeIfcImportError('unsupported_ifc_schema')
    entities=list(file)
    if len(entities)>MAX_ENTITIES:raise NativeIfcImportError('source_entity_budget_exceeded')
    elements=file.by_type('IfcProduct')
    if len(elements)>MAX_PRODUCTS:raise NativeIfcImportError('source_product_budget_exceeded')
    guids=[]
    for root in file.by_type('IfcRoot'):
        if not isinstance(root.GlobalId,str) or ifcopenshell.validate.validate_guid(root.GlobalId) is not None:
            raise NativeIfcImportError('invalid_native_guid')
        guids.append(root.GlobalId)
    if len(guids)!=len(set(guids)):raise NativeIfcImportError('duplicate_native_guid')
    if any(curve.Points is None for curve in file.by_type('IfcPolyline')):
        raise NativeIfcImportError('malformed_source_primitive')
    if sum(len(curve.Points) for curve in file.by_type('IfcPolyline'))>MAX_CURVE_POINTS:
        raise NativeIfcImportError('source_primitive_budget_exceeded')
    try:reader=_Reader(file,source_id,sha)
    except _Unsupported as exc:raise NativeIfcImportError(str(exc)) from None
    except (AttributeError,TypeError,OverflowError):raise NativeIfcImportError('malformed_global_context') from None
    hosts={};openings=[];diagnostics=[];processed=set()
    for product in sorted(file.by_type('IfcWall'),key=lambda e:e.GlobalId):
        processed.add(product.GlobalId)
        try:hosts[product.GlobalId]=reader.wall(product)
        except _Unsupported as exc:diagnostics.append(NativeIfcDiagnostic(product.GlobalId,product.is_a(),str(exc)))
        except (AttributeError,TypeError,OverflowError,ContractError):diagnostics.append(NativeIfcDiagnostic(product.GlobalId,product.is_a(),'malformed_product_geometry'))
    for product in sorted(file.by_type('IfcOpeningElement'),key=lambda e:e.GlobalId):
        processed.add(product.GlobalId)
        try:openings.append(reader.opening(product,hosts))
        except _Unsupported as exc:diagnostics.append(NativeIfcDiagnostic(product.GlobalId,product.is_a(),str(exc)))
        except (AttributeError,TypeError,OverflowError,ContractError):diagnostics.append(NativeIfcDiagnostic(product.GlobalId,product.is_a(),'malformed_product_geometry'))
    for product in sorted(elements,key=lambda e:e.GlobalId):
        if product.GlobalId not in processed:
            if product.is_a() in {'IfcSite','IfcBuilding','IfcBuildingStorey'} and product.Representation is None:
                continue
            reason='filling_geometry_unmodeled' if product.is_a('IfcWindow') or product.is_a('IfcDoor') else 'unsupported_product_type'
            diagnostics.append(NativeIfcDiagnostic(product.GlobalId,product.is_a(),reason))
    diagnostics=tuple(sorted(diagnostics,key=lambda d:(d.native_guid,d.reason)))
    status='no_supported_geometry' if not hosts else 'partial' if diagnostics else 'complete'
    model=None
    if hosts:
        attrs=reader.attrs({'project_guid':reader.project.GlobalId,'world_coordinate_system':_axis_evidence(reader.context.WorldCoordinateSystem)})
        attrs['native_ifc'].update(status=status,diagnostics=[{'guid':d.native_guid,'type':d.native_type,'reason':d.reason} for d in diagnostics],
            quantity_limitations={'wall_quantities':'gross','opening_deductions_unresolved':bool(file.by_type('IfcOpeningElement'))},
            rendering_limitations={'glb_opening_cuts':False,'filling_geometry_modeled':False})
        model=BuildingModel(model_id=reader.identity('model',reader.project.GlobalId),name=None,
            coordinate_system=CoordinateSystem(frame_id=reader.identity('frame',reader.project.GlobalId)),
            levels=tuple(sorted(reader.levels.values(),key=lambda e:e.id)),
            walls=tuple(sorted((value[0] for value in hosts.values()),key=lambda e:e.id)),
            openings=tuple(sorted(openings,key=lambda e:e.id)),attributes=attrs,
            provenance=(Provenance(source_kind='ifc-native',source_id=source_id,source_element_id=reader.project.GlobalId,
                derivation='observed',method='native-ifc-source-context-v1',scope_paths=('attributes.native_ifc',)),))
    return NativeIfcImportResult(source_id,sha,file.schema,status,model,diagnostics,len(hosts),len(openings))
