"""Opt-in exact plane cuts of physical assembly child meshes.

No hidden-line elevation, projection beyond the cut, hatching or structural
approval is implied. The finite neutral mesh contract is shared with GLB/IFC.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import replace
from oabm.assembly_mesh import assembly_component_mesh
from oabm.model import Point3, Vector3, validate_model
from .model import Bounds2, DrawingPrimitive, LineStyle, Point2
from .projection import ProjectionFrame, frame_from_view_direction, clip_polyline

EPS=1e-8


def _key(a,b):
    return tuple(sorted(((round(a[0],9),round(a[1],9)),(round(b[0],9),round(b[1],9)))))


def mesh_plane_segments(vertices, indices, frame, cut_depth=0.):
    """Intersect triangle surfaces; remove coplanar internal triangulation edges."""
    projected=[frame.project(Point3(x=x,y=y,z=z)) for x,y,z in vertices]
    coplanar=Counter();segments=set()
    for offset in range(0,len(indices),3):
        tri=[projected[indices[offset+j]] for j in range(3)]
        points=[((p.x,p.y),depth-cut_depth) for p,depth in tri]
        if all(abs(d)<=EPS for p,d in points):
            for i in range(3):coplanar[_key(points[i][0],points[(i+1)%3][0])]+=1
            continue
        hits=[]
        for i in range(3):
            a,da=points[i];b,db=points[(i+1)%3]
            if abs(da)<=EPS:hits.append(a)
            if (da>EPS and db<-EPS) or (da<-EPS and db>EPS):
                t=da/(da-db);hits.append((a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])))
        unique=list(dict.fromkeys((round(x,9),round(y,9)) for x,y in hits))
        if len(unique)==2 and unique[0]!=unique[1]:segments.add(_key(*unique))
    segments.update(edge for edge,count in coplanar.items() if count==1)
    # Merge adjacent collinear triangle fragments, preserving corners and gaps.
    lines=sorted(segments)
    changed=True
    while changed:
        changed=False
        for i,(a,b) in enumerate(lines):
            for j in range(i+1,len(lines)):
                c,d=lines[j];shared=set((a,b))&set((c,d))
                if len(shared)!=1:continue
                common=next(iter(shared));u=next(p for p in (a,b) if p!=common);v=next(p for p in (c,d) if p!=common)
                dx,dy=u[0]-common[0],u[1]-common[1];ex,ey=v[0]-common[0],v[1]-common[1]
                if abs(dx*ey-dy*ex)<=EPS*max(1,abs(dx)+abs(dy)+abs(ex)+abs(ey)) and dx*ex+dy*ey<0:
                    lines[i]=_key(u,v);lines.pop(j);changed=True;break
            if changed:break
    return tuple(sorted(lines))


def physical_cut_view(model,spec,base_generator,kwargs):
    """Combine existing ordinary view content with explicit child cut outlines."""
    from .generator import PlanSpec
    validate_model(model)
    if isinstance(spec,PlanSpec):
        level=next((x for x in model.levels if x.id==spec.level_id),None)
        if level is None:raise ValueError('unknown plan level')
        frame=ProjectionFrame(origin=Point3(x=0,y=0,z=level.elevation_m),
            right=Vector3(x=1,y=0,z=0),up=Vector3(x=0,y=1,z=0),normal=Vector3(x=0,y=0,z=1))
        cut=spec.cut_height_m;levels={spec.level_id}
    else:
        frame=frame_from_view_direction(origin=spec.origin,direction=spec.direction)
        cut=0.;levels=set(spec.level_ids)
    # Project the original validated model. Removing assemblies would invalidate
    # the checked routes whose freshness receipts bind those exact components.
    base=base_generator(model,spec,**kwargs)
    if not spec.visibility.architecture:return replace(base,metadata=base.metadata+(("assembly_mode","cut-plane-only; architecture hidden"),))
    components=[c for c in sorted(model.assembly_components,key=lambda c:c.id) if not levels or c.level_id in levels]
    outlines=[]
    for component in components:
        vertices,indices=assembly_component_mesh(component)
        outlines.extend((component,segment) for segment in mesh_plane_segments(vertices,indices,frame,cut))
    bounds=base.bounds
    if spec.bounds is None and outlines:
        pts=[p for c,segment in outlines for p in segment]
        bounds=Bounds2(min(bounds.min_x,min(p[0] for p in pts)-.1),min(bounds.min_y,min(p[1] for p in pts)-.1),
            max(bounds.max_x,max(p[0] for p in pts)+.1),max(bounds.max_y,max(p[1] for p in pts)+.1))
    assembly_ids={c.assembly_id for c in components}
    replaced_hosts={a.host_id for a in model.building_assemblies if a.id in assembly_ids and a.host_representation=='aggregate_host'}
    primitives=[p for p in base.primitives if not (set(p.source_ids)&replaced_hosts)]
    for n,(component,segment) in enumerate(outlines):
        for clipped in clip_polyline(tuple(Point2(x=x,y=y) for x,y in segment),bounds):
            primitives.append(DrawingPrimitive(id=f'{spec.id}:{component.id}:cut:{n}',kind='line',
                layer='architecture:assembly-cuts',source_ids=(component.id,component.assembly_id),
                points=clipped,style=LineStyle(stroke='cut',weight='heavy'),
                metadata=(("geometry_basis",component.geometry_basis),("assembly_id",component.assembly_id))))
    return replace(base,bounds=bounds,primitives=tuple(sorted(primitives,key=lambda p:p.id)),
        dimensions=tuple(d for d in base.dimensions if not set(d.source_ids)&replaced_hosts),
        metadata=base.metadata+(("assembly_mode","cut-plane-only"),("assembly_component_count",str(len(components))),
            ("assembly_cut_component_count",str(len({c.id for c,s in outlines}))),
            ("assembly_projection_limitation","no assembly beyond-cut projection or hidden-line elevations")))
