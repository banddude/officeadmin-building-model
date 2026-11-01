"""Exact bounded XY centerline presentation, never swept clearance geometry."""
import math
from .model import DrawingPrimitive, LineStyle, Point2
from oabm.model import ContractError, PhysicalRoute, Point3, validate_model, physical_segment_facts


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
    for route in physical:
        if len({joint.position.z for joint in route.joints}) != 1:
            raise ContractError(f'{route.id}: XY DrawingView supports horizontal routes only; vertical/oblique projections require a dedicated adapter')


def quarter_arc_geometry(points):
    """Start, centre, end; finite quarter-circle only, in the supplied frame."""
    if len(points) != 3:
        raise ValueError('quarter_arc requires start, centre, end')
    a,c,b = points
    ax,ay,bx,by = a.x-c.x,a.y-c.y,b.x-c.x,b.y-c.y
    radius,other = math.hypot(ax,ay),math.hypot(bx,by)
    if radius <= 0 or abs(radius-other) > 1e-8*max(1,radius) or abs(ax*bx+ay*by) > 1e-8*max(1,radius*radius):
        raise ValueError('quarter_arc requires equal positive radii and perpendicular radial endpoints')
    return radius, 1 if ax*by-ay*bx > 0 else -1


def physical_plan_primitives(route, frame, bounds):
    joints = {joint.id:joint.position for joint in route.joints}
    result=[]
    for segment,fact in zip(route.segments,physical_segment_facts(route)):
        a = frame.project(joints[segment.start_joint_id])[0]
        b = frame.project(joints[segment.end_joint_id])[0]
        points=(a,b)
        kind='line'
        if segment.kind == 'quarter_arc':
            center=Point3(**dict(zip(('x','y','z'),(float(value/1_000_000_000) for value in fact['centre_ticks']))))
            points=(a,frame.project(center)[0],b)
            kind='quarter_arc'
        # Exact quarter-circle extrema in XY are endpoints. The centre is
        # inside their bounding rectangle. Partial crop needs analytic trimming;
        # refuse instead of returning a chord or dropping part of the curve.
        if any(not (bounds.min_x <= p.x <= bounds.max_x and bounds.min_y <= p.y <= bounds.max_y) for p in points):
            raise ContractError(f'{segment.id}: explicit plan bounds crop physical geometry; enlarge bounds')
        result.append(DrawingPrimitive(id=f'{segment.id}:xy',kind=kind,layer='electrical:routes',
            source_ids=(route.id,segment.id,segment.start_joint_id,segment.end_joint_id),points=points,
            style=LineStyle(stroke='route'),metadata=(('geometry_scope','horizontal analytic centerline; OD/clearance not drawn'),)))
    return result
