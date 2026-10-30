"""Explicit physical model view requests for the shared production coordinator."""
from __future__ import annotations
from .generator import PlanSpec,SectionSpec,VisibilityPolicy,generate_plan,generate_section
from .model import Bounds2
from .view_body import render_scaled_view_body


def build_model_view_body(model, sheet, request):
    """Read a strict cut-view request and return body plus identity/scale coverage.

    This first production adapter supports physical architecture cuts, with
    labels/dimensions/electrical presentation explicitly disabled by the request.
    Existing specialized tool bodies remain responsible for their annotations.
    """
    from oabm.model import Point3,Vector3
    if not isinstance(request,dict) or set(request)!={'sheet_id','type','options','origin_mm','assembly_geometry','annotations'}:
        raise ValueError('model view: exact sheet_id/type/options/origin_mm/assembly_geometry/annotations keys required')
    if request['sheet_id']!=sheet.id:raise ValueError('model view: sheet identity differs')
    if request['assembly_geometry']!='cut' or request['annotations']!='none':
        raise ValueError("model view: explicit assembly_geometry='cut' and annotations='none' required")
    options=request['options']
    if not isinstance(options,dict):raise ValueError('model view: options must be an object')
    options=dict(options)
    if request['type']=='plan':
        allowed={'level_id','cut_height_m','view_depth_below_m','view_depth_above_m','bounds','scale'}
        factory,generate=PlanSpec,generate_plan
    elif request['type']=='section':
        allowed={'origin','direction','depth_m','back_depth_m','bounds','scale','level_ids'}
        factory,generate=SectionSpec,generate_section
        for key,cls in [('origin',Point3),('direction',Vector3)]:
            if key in options:options[key]=cls(**options[key])
        if 'level_ids' in options:
            if not isinstance(options['level_ids'],list) or any(not isinstance(x,str) for x in options['level_ids']):raise ValueError('model view: level_ids must be an explicit list')
            if set(options['level_ids'])-{level.id for level in model.levels}:raise ValueError('model view: unknown canonical level')
            options['level_ids']=tuple(options['level_ids'])
    else:raise ValueError('model view: supported cut types are plan and section; elevation is unsupported')
    if set(options)-allowed or 'scale' not in options:raise ValueError('model view: unsupported options or missing explicit scale')
    if options.get('bounds') is not None:options['bounds']=Bounds2(**options['bounds'])
    visibility=VisibilityPolicy(architecture=True,electrical=False,routes=False,spaces=False,labels=False,dimensions=False)
    spec=factory(id=sheet.id,visibility=visibility,**options)
    if sheet.fields.get('scale')!=f'1:{spec.scale:g}':raise ValueError('model view: shared titleblock scale must match explicit view denominator')
    view=generate(model,spec,assembly_geometry='cut',symbol_provider=lambda *_:None)
    result=render_scaled_view_body(view,drawing_bounds_mm=sheet.drawing_bounds_mm,origin_mm=request['origin_mm'])
    return result,view
