"""Finite event-plane proposals; only live solid verification proves clearance."""
from fractions import Fraction as F
from dataclasses import fields
from oabm.model import Obstacle, RouteConstraint, StructuralMember, Box3D
from oabm.model.physical_route_context import physical_geometry_inventory, physical_box_bounds
from oabm.model.physical_routes import point_ticks, exact_ticks
from .physical_verify_types import VerificationStop, reserved_sort

GRID_PROFILE='physical-event-planes-single-offset-v1'
SCOPE=1000000000000


def candidate_coordinates(source, context, requirements, ledger):
    ledger.set_cursor('candidate_coordinate_construction')
    ledger.debit('grid_port_lookup',len(source.ports))
    ports={p.id:p for p in source.ports}
    start=point_ticks(ports[context.start_port_id].pose.position)
    end=point_ticks(ports[context.end_port_id].pose.position)
    radius=exact_ticks(requirements.centerline_radius_m,'radius',minimum='.001',maximum='10')
    tube=F(exact_ticks(requirements.outside_diameter_m,'OD',minimum='.001',maximum='1'),2)
    clearance=exact_ticks(requirements.clearance_m,'clearance',minimum='0',maximum='100')
    q=tube+clearance+1000  # fixed one-micrometre margin, in nanometres
    minimum=lambda name: exact_ticks(getattr(requirements,name),'straight minimum',minimum='0',maximum='2000')
    ledger.debit('grid_domain_adaptation',len(context.allowed_domain.volumes))
    domain=tuple(physical_box_bounds(b) for b in context.allowed_domain.volumes)
    ledger.debit('grid_domain_bounds_scan',6*len(domain))
    lo=tuple(min(b[0][i] for b in domain) for i in range(3))
    hi=tuple(max(b[1][i] for b in domain) for i in range(3))
    seeds=[set() for _ in range(3)]
    def add(axis,value):
        ledger.debit('grid_seed_proposal')
        seeds[axis].add(F(value))
    def floor(v):return v.numerator//v.denominator
    def ceil(v):return -((-v.numerator)//v.denominator)
    for i in range(3):
        add(i,start[i]);add(i,end[i])
        add(i,lo[i]);add(i,hi[i])
    for endpoint,port,sign,minimum_length in ((start,ports[context.start_port_id],1,minimum('start_straight_min_m')),(end,ports[context.end_port_id],1,minimum('end_straight_min_m'))):
        # End outward direction is negative final traversal tangent.
        for i,axis in enumerate('xyz'):
            add(i,endpoint[i]+int(getattr(port.direction,axis))*sign*(radius+max(1,minimum_length)))
    for lower,upper in domain:
        for i in range(3):
            add(i,lower[i]);add(i,upper[i])
            add(i,floor(lower[i]+q)+1);add(i,ceil(upper[i]-q)-1)
    descriptors=fields(source)
    ledger.debit('grid_inventory_fields',len(descriptors))
    ledger.debit('grid_inventory_scan',sum(len(getattr(source,f.name)) for f in descriptors if isinstance(getattr(source,f.name),tuple)))
    inventory=physical_geometry_inventory(source,context.route_type)
    ledger.debit('grid_coverage_lookup',len(context.coverage))
    coverage={r.source_entity_id:r for r in context.coverage}
    for identity in reserved_sort(tuple(inventory),ledger):
        ledger.debit('grid_blocker_adaptation')
        if coverage[identity].decision=='outside_proof_scope':continue
        item=inventory[identity]
        if isinstance(item,(Obstacle,RouteConstraint)):
            lower,upper=physical_box_bounds(item.geometry)
            extra=exact_ticks(item.clearance_m,'blocker clearance',minimum='0',maximum='100')
        elif isinstance(item,StructuralMember):
            lower,upper=physical_box_bounds(Box3D(pose=item.pose,size=item.size));extra=0
        else:raise VerificationStop('unsupported_input','grid_blocker_adapter',(identity,))
        for i in range(3):
            add(i,lower[i]);add(i,upper[i])
            add(i,ceil(lower[i]-q-extra)-1);add(i,floor(upper[i]+q+extra)+1)
    offset=2*radius+max(1,minimum('between_bends_straight_min_m'))
    axes=[]
    for i in range(3):
        points={start[i],end[i]}
        for value in reserved_sort(tuple(seeds[i]),ledger):
            for shift in (0,-radius,radius,-offset,offset):
                ledger.debit('grid_offset_proposal')
                proposal=value+shift
                for tick in (floor(proposal),ceil(proposal)):
                    ledger.debit('grid_integer_proposal')
                    if -SCOPE<=tick<=SCOPE and lo[i]<=tick<=hi[i]:points.add(tick)
            if len(points)>context.execution_limits.candidate_grid_nodes:
                raise VerificationStop('resource_limit','candidate_grid_node_limit')
        axes.append(tuple(reserved_sort(tuple(points),ledger)))
    if len(axes[0])*len(axes[1])*len(axes[2])>context.execution_limits.candidate_grid_nodes:
        raise VerificationStop('resource_limit','candidate_grid_node_limit')
    return tuple(axes)
