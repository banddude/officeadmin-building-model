"""Analytic canonical physical-route surface preview, never a clearance proof."""
from __future__ import annotations
import math
import struct
from oabm.model import PhysicalRoute, physical_segment_facts
from oabm.model.physical_routes import point_ticks

NANO = 1_000_000_000
MAX_VERTICES = 500_000
CHORD_TARGET_M = 0.00025
MAX_QUANTIZATION_ERROR_M = 0.0001


def _add(a, b): return tuple(x+y for x,y in zip(a,b))
def _sub(a, b): return tuple(x-y for x,y in zip(a,b))
def _scale(a, n): return tuple(x*n for x in a)
def _dot(a, b): return sum(x*y for x,y in zip(a,b))
def _cross(a,b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _rotate(v, axis, cosine, sine):
    return _add(_add(_scale(v,cosine),_scale(_cross(axis,v),sine)),_scale(axis,_dot(axis,v)*(1-cosine)))
def _float32(v): return tuple(struct.unpack('<f',struct.pack('<f',x))[0] for x in v)


def physical_route_mesh(route: PhysicalRoute, *, vertex_budget: int = MAX_VERTICES):
    """Return one indexed local-origin mesh with exact tangent seams.

    Circumferential and longitudinal chord deviations are disclosed separately.
    They are not a claimed total surface Hausdorff bound or clearance tolerance.
    """
    route.__post_init__()
    facts = physical_segment_facts(route)
    origin_ticks = point_ticks(route.joints[0].position)
    point = lambda ticks: tuple((v-o)/NANO for v,o in zip(ticks,origin_ticks))
    joint_points = {j.id: point(point_ticks(j.position)) for j in route.joints}
    radius = route.requirements.outside_diameter_m/2
    sides = max(32, math.ceil(math.pi/math.acos(max(-1,1-CHORD_TARGET_M/radius))))
    sides = 4*math.ceil(sides/4)
    arc_steps=[]
    for segment,fact in zip(route.segments,facts):
        if segment.kind=='line':arc_steps.append(1)
        else:
            outer=fact['radius_ticks']/NANO+radius
            arc_steps.append(max(16,math.ceil(math.pi/(4*math.acos(max(-1,1-CHORD_TARGET_M/outer))))))
    if sides>256 or max(arc_steps)>256:
        raise ValueError('physical route preview tessellation profile exceeded')
    ring_count=1+sum(arc_steps)
    vertex_count=ring_count*sides+2*(sides+1)
    if type(vertex_budget) is not int or vertex_budget<0 or vertex_count>min(vertex_budget,MAX_VERTICES):
        raise ValueError('physical route preview vertex budget exceeded')
    tangent=tuple(facts[0]['start_tangent'])
    normal=next(tuple(1 if i==axis else 0 for i in range(3)) for axis in range(3) if tangent[axis]==0)
    binormal=_cross(tangent,normal)
    ring_trig=tuple((math.cos(2*math.pi*j/sides),math.sin(2*math.pi*j/sides)) for j in range(sides))
    vertices=[];normals=[];indices=[]
    def ring(center,n,b):
        start=len(vertices)
        for c,s in ring_trig:
            outward=_add(_scale(n,c),_scale(b,s))
            vertices.append(_add(center,_scale(outward,radius)));normals.append(outward)
        if start:
            previous=start-sides
            for j in range(sides):
                k=(j+1)%sides
                indices.extend((previous+j,previous+k,start+k,previous+j,start+k,start+j))
    ring(joint_points[route.joints[0].id],normal,binormal)
    max_arc_chord=0.0
    for segment,fact,steps in zip(route.segments,facts,arc_steps):
        end=joint_points[segment.end_joint_id]
        if segment.kind=='line':
            ring(end,normal,binormal)
        else:
            sign=1 if segment.turn=='positive_quarter' else -1
            axis=tuple(sign*getattr(segment.plane_normal,a) for a in 'xyz')
            center=point(fact['centre_ticks'])
            radial=_sub(joint_points[segment.start_joint_id],center)
            start_normal,start_binormal=normal,binormal
            for step in range(1,steps+1):
                if step==steps:
                    cosine,sine=0.0,1.0;position=end
                else:
                    theta=step*math.pi/(2*steps)
                    cosine,sine=math.cos(theta),math.sin(theta)
                    position=_add(center,_rotate(radial,axis,cosine,sine))
                ring(position,_rotate(start_normal,axis,cosine,sine),_rotate(start_binormal,axis,cosine,sine))
            normal=_rotate(start_normal,axis,0,1)
            binormal=_rotate(start_binormal,axis,0,1)
            max_arc_chord=max(max_arc_chord,(fact['radius_ticks']/NANO+radius)*(1-math.cos(math.pi/(4*steps))))
        tangent=tuple(fact['end_tangent'])
    last_side_ring=len(vertices)-sides
    for start_ring,center,t,at_start in ((0,joint_points[route.joints[0].id],tuple(facts[0]['start_tangent']),True),
                                        (last_side_ring,joint_points[route.joints[-1].id],tangent,False)):
        cap_normal=_scale(t,-1 if at_start else 1)
        center_index=len(vertices);vertices.append(center);normals.append(cap_normal)
        cap_ring=len(vertices)
        for j in range(sides):vertices.append(vertices[start_ring+j]);normals.append(cap_normal)
        for j in range(sides):
            k=(j+1)%sides
            indices.extend((center_index,cap_ring+k,cap_ring+j) if at_start else (center_index,cap_ring+j,cap_ring+k))
    quantized=[_float32(v) for v in vertices]
    max_error=max(math.dist(a,b) for a,b in zip(vertices,quantized))
    if max_error>min(MAX_QUANTIZATION_ERROR_M,radius*.01):
        raise ValueError('physical route preview float32 position error exceeds profile')
    for i in range(0,len(indices),3):
        a,b,c=indices[i:i+3]
        face=_cross(_sub(quantized[b],quantized[a]),_sub(quantized[c],quantized[a]))
        outward=_add(_add(normals[a],normals[b]),normals[c])
        if _dot(face,face)==0 or _dot(face,outward)<=0:
            raise ValueError('physical route preview float32 mesh collapsed or inverted')
    straight=sum(f['length_ticks'] for segment,f in zip(route.segments,facts) if segment.kind=='line')/NANO
    arcs=[f['radius_ticks']/NANO for segment,f in zip(route.segments,facts) if segment.kind=='quarter_arc']
    return {'vertices':vertices,'normals':normals,'indices':indices,'origin':route.joints[0].position,
            'extras':{'geometry_kind':'line_arc_v1','outside_diameter_m':route.requirements.outside_diameter_m,
                'centerline_radius_m':route.requirements.centerline_radius_m,'clearance_m':route.requirements.clearance_m,
                'segment_count':len(route.segments),'bend_count':len(arcs),
                'length_m':math.fsum((straight,*(r*math.pi/2 for r in arcs))),
                'geometry_disclosure':'Tessellated canonical line/arc normal-disc surface; flat terminal caps; export does not verify clearance or code compliance.',
                'clearance_verified_by_export':False,
                'tessellation':{'circumferential_sides':sides,'arc_steps':[n for s,n in zip(route.segments,arc_steps) if s.kind=='quarter_arc'],
                    'radial_chord_deviation_m':radius*(1-math.cos(math.pi/sides)),
                    'maximum_longitudinal_chord_deviation_m':max_arc_chord,
                    'measured_maximum_float32_vertex_error_m':max_error,
                    'combined_surface_error_bound_claimed':False}}}
