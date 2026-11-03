"""Explicit atomic field edit for an already source-located plan-only service."""
from dataclasses import replace

from oabm.model import ContractError,Point2D,Provenance,validate_model
from oabm.model.site import SitePlanGeometry


def move_plan_service_node(model,node_id,position,*,field_evidence):
    """Preserve IDs/evidence, update owned proposed surfaces and lateral start.

    No route search, 3D placement, existing-construction relocation or hidden
    source-hash rebinding occurs. Invalid resulting geometry refuses atomically.
    """
    validate_model(model)
    if type(position) is not Point2D:raise ContractError('service field edit requires explicit canonical XY metres')
    if type(field_evidence) is not Provenance or field_evidence.derivation!='user':
        raise ContractError('service field edit requires supplied user-derived evidence')
    node=next((item for item in model.service_nodes if item.id==node_id),None)
    if node is None or node.location_basis!='plan_only':
        raise ContractError('service field edit supports an already located plan-only node, not unknown or placed3D equipment')
    dx,dy=position.x-node.plan_position.x,position.y-node.plan_position.y
    def record(path):
        return replace(field_evidence,scope_paths=(path,))
    revised_node=replace(node,plan_position=position,provenance=(*node.provenance,record('plan_position')))
    features=[]
    for feature in model.site_features:
        if feature.owner_id==node_id and feature.kind=='working_surface':
            if feature.status!='proposed':raise ContractError('existing or unknown working surface cannot be silently relocated')
            geometry=SitePlanGeometry(kind=feature.geometry.kind,points=tuple(Point2D(x=p.x+dx,y=p.y+dy) for p in feature.geometry.points))
            feature=replace(feature,geometry=geometry,provenance=(*feature.provenance,record('geometry')))
        features.append(feature)
    laterals=[]
    for lateral in model.plan_service_laterals:
        if lateral.source_node_id==node_id:
            geometry=SitePlanGeometry(kind='polyline',points=(position,*lateral.geometry.points[1:]))
            lateral=replace(lateral,geometry=geometry,provenance=(*lateral.provenance,record('geometry')))
        laterals.append(lateral)
    return replace(model,service_nodes=tuple(revised_node if item.id==node_id else item for item in model.service_nodes),
                   site_features=tuple(features),plan_service_laterals=tuple(laterals))
