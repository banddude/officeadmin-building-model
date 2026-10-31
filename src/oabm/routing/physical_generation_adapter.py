"""Internal adapter prototype: exact realization to existing canonical records.
No verifier status is asserted or saved by this function.
"""
from dataclasses import replace
from oabm.model import (BuildingModel, PhysicalRoute, PhysicalRouteJoint,
    LineSegment3D, QuarterArc3D, Point3, Vector3, Provenance, stable_id)
from oabm.model.physical_routes import point_ticks
from .physical_capture import capture, CANDIDATE_BYTES, COMBINED_BYTES
from .physical_verify_types import VerificationStop


def canonical_candidate(source, requirements, context, realization, *, reserved_ids=None):
    route_id = requirements.route_id
    if len(route_id)*(2*len(realization.pieces)+2)>CANDIDATE_BYTES:
        raise VerificationStop('resource_limit','candidate_identity_expansion_limit')
    method = 'physical-cardinal-finite-family-v1'
    if reserved_ids is None:
        from oabm.model.model import _entity_collections
        reserved_ids={source.model_id,route_id,requirements.id,context.allowed_domain.id}
        for collection in _entity_collections(source):reserved_ids.update(e.id for e in collection)
        for old in source.routes:
            if type(old) is PhysicalRoute:
                reserved_ids.update(e.id for e in (*old.joints,*old.segments,old.requirements,old.context.allowed_domain))
    occupied=set()
    def identity(kind,slot):
        for nonce in range(32):
            value=stable_id(kind,f'{route_id}:{slot}:collision-probe:{nonce}')
            if value not in reserved_ids and value not in occupied:
                occupied.add(value);return value
        raise VerificationStop('resource_limit','generated_identity_probe_limit')
    def evidence(*paths):
        return (Provenance(source_kind='model', source_id=source.model_id,
            source_element_id=context.request_id, method=method,
            derivation='inferred', scope_paths=tuple(paths)),)
    points = (realization.pieces[0].start,) + tuple(p.end for p in realization.pieces)
    joints=[]
    for i,p in enumerate(points):
        # Role slots are scoped to the stable caller route identity. They do not
        # depend on input collection order or source/context hash.
        joint_id=identity('physical-route-joint',f'joint-slot:{i}')
        values=tuple(float(f'{"-" if v < 0 else ""}{abs(v)//1000000000}.{abs(v)%1000000000:09d}') for v in p)
        pos=Point3(**dict(zip('xyz',values)))
        if point_ticks(pos)!=p:raise ValueError('candidate serialization closure')
        joints.append(PhysicalRouteJoint(id=joint_id,route_id=route_id,position=pos,
            provenance=evidence('position')))
    segments=[]
    for i,p in enumerate(realization.pieces):
        args=dict(id=identity('physical-route-segment',f'segment-slot:{i}'),
            route_id=route_id,start_joint_id=joints[i].id,end_joint_id=joints[i+1].id,
            provenance=evidence('start_joint_id','end_joint_id'))
        if p.kind=='line':segments.append(LineSegment3D(**args))
        else:
            args['provenance']=evidence('start_joint_id','end_joint_id','plane_normal','turn')
            segments.append(QuarterArc3D(**args,plane_normal=Vector3(**dict(zip('xyz',p.normal))),
                turn='positive_quarter' if p.turn==1 else 'negative_quarter'))
    route=PhysicalRoute(id=route_id,route_type=context.route_type,
        start_port_id=context.start_port_id,end_port_id=context.end_port_id,
        joints=tuple(joints),segments=tuple(segments),requirements=requirements,
        context=context,provenance=evidence('joints','segments'))
    route=capture(route,byte_limit=CANDIDATE_BYTES)
    combined=capture(replace(source,routes=(*source.routes,route)),byte_limit=COMBINED_BYTES,node_limit=500000)
    serialized=combined.to_json(indent=None)
    if len(serialized.encode('utf-8'))>COMBINED_BYTES:
        raise VerificationStop('resource_limit','combined_snapshot_byte_limit')
    restored=BuildingModel.from_json(serialized).routes[-1]
    if tuple(point_ticks(j.position) for j in restored.joints)!=points:
        raise ValueError('candidate serialized tick closure')
    return restored
