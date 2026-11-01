"""Native analytic physical route solids and fail-closed readback.

Native edits are not silently applied to source-bound physical requests. Axis,
disk sweep and placement must agree with the canonical line/arc contract.
"""
import math
import json
from dataclasses import replace
import ifcopenshell.api.geometry
import ifcopenshell.util.placement
from oabm.model import PhysicalRoute, physical_segment_facts


def validate_current_source(model):
    """Bounded IFC profile: all physical routes share the retained source.

    Only these known generated routes are removed for source reconstruction;
    every legacy route, architectural object and caller fact stays present.
    Dependent post-routing edits cannot be silently stripped to match a hash.
    """
    from .adapter import IfcAdapterError
    from oabm.model import compute_physical_source_sha256
    physical=tuple(route for route in model.routes if isinstance(route,PhysicalRoute))
    if not physical:return
    try:
        source=replace(model,routes=tuple(route for route in model.routes if not isinstance(route,PhysicalRoute)))
        digest=compute_physical_source_sha256(source)
        if any(route.context.source_model_sha256!=digest for route in physical):
            raise ValueError('current reconstructed source differs from retained physical route binding')
    except (ValueError,TypeError,AttributeError,KeyError) as error:
        raise IfcAdapterError(f'physical IFC source binding refused: {error}; requires unchanged shared pre-routing source') from error


def validate_native_inventory(ifc,canonical_items,route_segments):
    from .adapter import IfcAdapterError
    physical_ids={identity for identity,(_,data,_,kind) in canonical_items.items()
                  if kind=='route' and data.get('geometry_kind')=='line_arc_v1'}
    if not physical_ids:return
    route_ids={identity for identity,(_,_,_,kind) in canonical_items.items() if kind=='route'}
    if set(route_segments)-route_ids:
        raise IfcAdapterError('physical IFC inventory has segments for an unknown route')
    allowed={product for pairs in route_segments.values() for _,product in pairs}
    allowed.update(product for product,_,_,kind in canonical_items.values() if kind=='conductor')
    for product in (*ifc.by_type('IfcCableCarrierSegment'),*ifc.by_type('IfcCableSegment')):
        if product not in allowed:
            raise IfcAdapterError('physical IFC inventory contains an untagged or unowned conduit/cable product')


def decode_route_payload(raw):
    from oabm.routing.physical_capture import bounded_source_json
    from oabm.model.physical_routes import _physical_json_float, validate_physical_json_numbers
    from .adapter import IfcAdapterError
    from oabm.routing.physical_verify_types import VerificationStop
    try:
        raw=bounded_source_json(raw)
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result: raise ValueError('duplicate route metadata key')
                result[key]=value
            return result
        exact=json.loads(raw,parse_float=_physical_json_float,object_pairs_hook=unique)
        if isinstance(exact,dict) and exact.get('geometry_kind')=='line_arc_v1':
            validate_physical_json_numbers({'routes':[exact]})
        return json.loads(raw)
    except (ValueError,RecursionError,OverflowError,VerificationStop) as error:
        raise IfcAdapterError(f'IFC route payload refused: {error}') from error


def _point(ifc, coordinates):
    return ifc.create_entity('IfcCartesianPoint',Coordinates=tuple(float(v) for v in coordinates))


def _direction(ifc, coordinates):
    return ifc.create_entity('IfcDirection',DirectionRatios=tuple(float(v) for v in coordinates))


def segment_curve(ifc,route,index):
    segment=route.segments[index]
    points={joint.id:joint.position for joint in route.joints}
    a,b=points[segment.start_joint_id],points[segment.end_joint_id]
    start=tuple(getattr(a,axis) for axis in 'xyz')
    end=tuple(getattr(b,axis) for axis in 'xyz')
    if segment.kind=='line':
        return ifc.create_entity('IfcPolyline',Points=(_point(ifc,start),_point(ifc,end)))
    fact=physical_segment_facts(route)[index]
    centre=tuple(float(value/1_000_000_000) for value in fact['centre_ticks'])
    radius=fact['radius_ticks']/1_000_000_000
    sign=1 if segment.turn=='positive_quarter' else -1
    normal=tuple(sign*getattr(segment.plane_normal,axis) for axis in 'xyz')
    radial=tuple((value-origin)/radius for value,origin in zip(start,centre))
    position=ifc.create_entity('IfcAxis2Placement3D',Location=_point(ifc,centre),
        Axis=_direction(ifc,normal),RefDirection=_direction(ifc,radial))
    circle=ifc.create_entity('IfcCircle',Position=position,Radius=radius)
    return ifc.create_entity('IfcTrimmedCurve',BasisCurve=circle,
        Trim1=(ifc.create_entity('IfcParameterValue',0.),),
        Trim2=(ifc.create_entity('IfcParameterValue',math.pi/2),),
        SenseAgreement=True,MasterRepresentation='PARAMETER')


def _representation(ifc,product,context,identifier,representation_type,item):
    representation=ifc.create_entity('IfcShapeRepresentation',ContextOfItems=context,
        RepresentationIdentifier=identifier,RepresentationType=representation_type,Items=(item,))
    ifcopenshell.api.geometry.assign_representation(ifc,product=product,representation=representation)


def assign_segment(ifc,product,axis_context,body_context,route,index):
    curve=segment_curve(ifc,route,index)
    _representation(ifc,product,axis_context,'Axis','Curve3D',curve)
    solid=ifc.create_entity('IfcSweptDiskSolid',Directrix=curve,
        Radius=route.requirements.outside_diameter_m/2)
    _representation(ifc,product,body_context,'Body','AdvancedSweptSolid',solid)


def assign_conductor_axis(ifc,product,axis_context,route):
    curves=tuple(ifc.create_entity('IfcCompositeCurveSegment',Transition='CONTSAMEGRADIENT',
        SameSense=True,ParentCurve=segment_curve(ifc,route,index)) for index in range(len(route.segments)))
    curve=ifc.create_entity('IfcCompositeCurve',Segments=curves,SelfIntersect=False)
    _representation(ifc,product,axis_context,'Axis','Curve3D',curve)


def validate_native_route(data, segments):
    from .adapter import IfcAdapterError, canonical_id_to_ifc_guid, _property_set, ADAPTER_PSET, _segment_ifc_type
    from oabm.model.model import _decode_dataclass
    try:
        route=_decode_dataclass(PhysicalRoute,data)
        if len(segments)!=len(route.segments) or [index for index,_ in segments]!=list(range(len(route.segments))):
            raise ValueError('native physical route segment inventory differs')
        facts=physical_segment_facts(route)
        def close(actual,expected):
            return len(actual)==len(expected) and all(math.isfinite(float(a)) and
                math.isclose(float(a),float(b),abs_tol=1e-12,rel_tol=0) for a,b in zip(actual,expected))
        for index,product in segments:
            segment=route.segments[index]
            expected_class,expected_type=_segment_ifc_type(route.route_type)
            if product.is_a()!=expected_class or product.PredefinedType!=expected_type:
                raise ValueError('native physical segment class/type differs')
            if product.GlobalId!=canonical_id_to_ifc_guid(f'{route.id}#segment:{index}'):
                raise ValueError('native physical segment identity differs')
            meta=_property_set(product,ADAPTER_PSET)
            if meta.get('PhysicalSegmentId')!=segment.id:
                raise ValueError('native physical segment canonical identity differs')
            matrix=ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
            if any(abs(float(matrix[i,j])-(1 if i==j else 0))>1e-12 for i in range(4) for j in range(4)):
                raise ValueError('native physical segment placement changed')
            reps=product.Representation.Representations if product.Representation else ()
            if len(reps)!=2 or {rep.RepresentationIdentifier for rep in reps}!={'Axis','Body'}:
                raise ValueError('native physical segment Axis/Body inventory differs')
            axis=next(rep for rep in reps if rep.RepresentationIdentifier=='Axis')
            body=next(rep for rep in reps if rep.RepresentationIdentifier=='Body')
            if len(axis.Items)!=1 or len(body.Items)!=1 or axis.RepresentationType!='Curve3D' or body.RepresentationType!='AdvancedSweptSolid':
                raise ValueError('native physical representation type differs')
            curve,solid=axis.Items[0],body.Items[0]
            if not solid.is_a('IfcSweptDiskSolid') or solid.Directrix!=curve or solid.InnerRadius is not None or solid.StartParam is not None or solid.EndParam is not None:
                raise ValueError('native physical disk sweep differs')
            if not close((solid.Radius,),(route.requirements.outside_diameter_m/2,)):
                raise ValueError('native physical outside diameter differs')
            a=tuple(getattr(route.joints[index].position,key) for key in 'xyz')
            b=tuple(getattr(route.joints[index+1].position,key) for key in 'xyz')
            if segment.kind=='line':
                if not curve.is_a('IfcPolyline') or len(curve.Points)!=2 or not close(curve.Points[0].Coordinates,a) or not close(curve.Points[1].Coordinates,b):
                    raise ValueError('native physical line geometry differs')
                continue
            if not curve.is_a('IfcTrimmedCurve') or not curve.BasisCurve.is_a('IfcCircle'):
                raise ValueError('native physical arc must remain a trimmed circle')
            if curve.MasterRepresentation!='PARAMETER' or curve.SenseAgreement is not True:
                raise ValueError('native physical arc trim semantics differ')
            if len(curve.Trim1)!=1 or len(curve.Trim2)!=1 or not curve.Trim1[0].is_a('IfcParameterValue') or not curve.Trim2[0].is_a('IfcParameterValue'):
                raise ValueError('native physical arc parameter trims differ')
            if not close((curve.Trim1[0].wrappedValue,curve.Trim2[0].wrappedValue),(0,math.pi/2)):
                raise ValueError('native physical arc trim angle differs')
            fact=facts[index];circle=curve.BasisCurve
            centre=tuple(float(value/1_000_000_000) for value in fact['centre_ticks'])
            radius=fact['radius_ticks']/1_000_000_000
            sign=1 if segment.turn=='positive_quarter' else -1
            normal=tuple(sign*getattr(segment.plane_normal,key) for key in 'xyz')
            radial=tuple((value-origin)/radius for value,origin in zip(a,centre))
            position=circle.Position
            if (not position.is_a('IfcAxis2Placement3D') or not close((circle.Radius,),(radius,))
                or not close(position.Location.Coordinates,centre)
                or position.Axis is None or not close(position.Axis.DirectionRatios,normal)
                or position.RefDirection is None or not close(position.RefDirection.DirectionRatios,radial)):
                raise ValueError('native physical arc centre/radius/orientation differs')
    except (ValueError,TypeError,AttributeError,KeyError,IndexError,OverflowError) as error:
        raise IfcAdapterError(f'{data.get("id")}: physical route readback refused: {error}') from error
