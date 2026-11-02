"""Exact bounded XY centerline presentation, never swept clearance geometry."""
import math
import json
from .model import DrawingPrimitive, LineStyle, Point2
from oabm.model import ContractError, PhysicalRoute, Point3, validate_model, physical_segment_facts, stable_id


def admit_plan_routes(model, mode):
    if mode not in (None, 'xy-centerline'):
        raise ContractError("physical_geometry: use None or explicit 'xy-centerline'")
    physical = [route for route in model.routes if isinstance(route,PhysicalRoute)]
    if not physical:
        return
    if mode is None:
        from oabm.model import reject_physical_routes
        reject_physical_routes(model,'generate_plan')
    validate_model(model)


def physical_story_segments(route, min_z, max_z):
    """Analytic cardinal line/quarter-arc intersections with a closed Z slab.

    Vertical-plane quarter arcs have monotone Z under line_arc_v1. Their XY
    projection is a straight locus, not a 3D chord: recover its varying planar
    coordinate from r² = u² + z². Retain the analytic source and clipping planes.
    No sampling, tessellation, swept envelope or invented bend is involved.
    """
    if not math.isfinite(min_z) or not math.isfinite(max_z) or min_z>max_z:
        raise ContractError('physical story clipping requires a finite ordered Z interval')
    joints={joint.id:joint.position for joint in route.joints}
    result=[]
    for segment,fact in zip(route.segments,physical_segment_facts(route)):
        start,end=joints[segment.start_joint_id],joints[segment.end_joint_id]
        low,high=max(min(start.z,end.z),min_z),min(max(start.z,end.z),max_z)
        if low>high:
            continue
        center=None
        if segment.kind=='quarter_arc':
            center=Point3(**dict(zip(('x','y','z'),(float(value/1_000_000_000) for value in fact['centre_ticks']))))
        def at_z(z):
            if z==start.z:
                return start
            if z==end.z:
                return end
            if segment.kind=='line':
                if start.x!=end.x or start.y!=end.y:
                    raise ContractError(f'{segment.id}: noncardinal nonhorizontal line projection is unsupported')
                return Point3(x=start.x,y=start.y,z=z)
            if segment.plane_normal.z==1:
                raise ContractError(f'{segment.id}: horizontal arc cannot intersect a different Z plane')
            axis='y' if segment.plane_normal.x==1 else 'x'
            radius=float(fact['radius_ticks']/1_000_000_000)
            radical=radius*radius-(z-center.z)**2
            if radical < -1e-12*max(1,radius*radius):
                raise ContractError(f'{segment.id}: clipping plane is outside analytic arc')
            delta=getattr(start,axis)-getattr(center,axis) or getattr(end,axis)-getattr(center,axis)
            value=getattr(center,axis)+math.copysign(math.sqrt(max(0,radical)),delta)
            return Point3(x=value if axis=='x' else center.x,y=value if axis=='y' else center.y,z=z)
        if start.z==end.z:
            a,b=start,end
        else:
            a,b=(at_z(low),at_z(high)) if start.z<end.z else (at_z(high),at_z(low))
            if a==b:  # slab boundary contact has no positive-length portion
                continue
        result.append((segment,fact,a,b,center))
    return tuple(result)


def quarter_arc_geometry(points):
    """Start, centre, end; finite quarter-circle only, in the supplied frame."""
    if len(points) != 3:
        raise ValueError('quarter_arc requires start, centre, end')
    a,c,b = points
    ax,ay,bx,by = a.x-c.x,a.y-c.y,b.x-c.x,b.y-c.y
    radius,other = math.hypot(ax,ay),math.hypot(bx,by)
    if radius <= 0 or other <= 0 or abs(radius-other) > max(1e-12,1e-8*radius) or abs(ax*bx+ay*by)/(radius*other) > 1e-7:
        raise ValueError('quarter_arc requires equal positive radii and perpendicular radial endpoints')
    return radius, 1 if ax*by-ay*bx > 0 else -1


def physical_plan_primitives(route, frame, bounds, *, depth_interval=None):
    joints = {joint.id:joint.position for joint in route.joints}
    result=[]
    minimum,maximum=depth_interval or (min(p.z for p in joints.values())-frame.origin.z,
                                       max(p.z for p in joints.values())-frame.origin.z)
    for segment,fact,start,end,center in physical_story_segments(route,minimum+frame.origin.z,maximum+frame.origin.z):
        a = frame.project(start)[0]
        b = frame.project(end)[0]
        points=(a,b)
        kind='line'
        if segment.kind == 'quarter_arc' and segment.plane_normal.z==1:
            points=(a,frame.project(center)[0],b)
            kind='quarter_arc'
        # Exact quarter-circle extrema in XY are endpoints. The centre is
        # inside their bounding rectangle. Partial crop needs analytic trimming;
        # refuse instead of returning a chord or dropping part of the curve.
        if any(not (bounds.min_x <= p.x <= bounds.max_x and bounds.min_y <= p.y <= bounds.max_y) for p in points):
            raise ContractError(f'{segment.id}: explicit plan bounds crop physical geometry; enlarge bounds')
        nonhorizontal=start.z!=end.z
        meta=(('geometry_scope','horizontal analytic centerline; OD/clearance not drawn'),)
        if nonhorizontal:
            source={'kind':segment.kind,'start':vars_point(joints[segment.start_joint_id]),
                'end':vars_point(joints[segment.end_joint_id]),'clip_interval_z_m':[minimum+frame.origin.z,maximum+frame.origin.z],
                'clipped_start':vars_point(start),'clipped_end':vars_point(end)}
            if center is not None:
                source.update(centre_ticks=[str(value) for value in fact['centre_ticks']],radius_ticks=str(fact['radius_ticks']),
                    plane_normal=vars_point(segment.plane_normal),turn=segment.turn,
                    projection='analytic vertical-plane arc XY locus, not a 3D chord')
            meta=(('geometry_scope','finite-story XY centerline projection only; no OD/clearance; 3D curve retained'),
                  ('analytic_source_and_clip',json.dumps(source,sort_keys=True,allow_nan=False)),
                  ('clipped_min_z_m',str(min(start.z,end.z))),('clipped_max_z_m',str(max(start.z,end.z))))
        vertical=nonhorizontal and a==b
        if a==b and (start.x!=end.x or start.y!=end.y):
            raise ContractError(f'{segment.id}: projected extent collapses at drawing precision; enlarge the story interval')
        result.append(DrawingPrimitive(id=stable_id('drawing-primitive',f'{segment.id}:xy'),kind='symbol' if vertical else kind,layer='electrical:routes',
            source_ids=(route.id,segment.id,segment.start_joint_id,segment.end_joint_id),points=(a,) if vertical else points,
            symbol='route-vertical-projection' if vertical else None,
            style=LineStyle(stroke='route',pattern='dash' if nonhorizontal else 'solid'),metadata=meta))
        if nonhorizontal:
            result.append(DrawingPrimitive(id=stable_id('drawing-primitive',f'{segment.id}:projection-note'),kind='text',
                layer='annotations:routes',source_ids=(route.id,),
                points=(a,),text=f'{segment.id}: {"Vertical location" if vertical else "XY projection only"}; Z {start.z} to {end.z} m; no OD/clearance',
                style=LineStyle(stroke='route'),metadata=meta))
    return result


def vars_point(point):
    return {axis:getattr(point,axis) for axis in ('x','y','z')}
