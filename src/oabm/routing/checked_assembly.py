"""Caller-bounded assembly routing candidates followed by bend preflight.

Joins existing geometry and QA APIs. No radius, size, clearance or installation
policy is inferred, and curved swept clearance is not asserted.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, replace
import math
from oabm.model import BuildingModel, Route, RouteFitting, validate_model
from oabm.qa.route_bends import BendReport, check_route_bends
from .router import (RoutingError, RoutingOptions, derive_suspended_ceiling_route_domain,
                     derive_wall_cavity_route_domain, route_between_ports)


@dataclass(frozen=True)
class AssemblyRouteCandidate:
    id: str
    options: RoutingOptions


@dataclass(frozen=True)
class CheckedAssemblyRoute:
    model: BuildingModel
    route: Route
    fittings: tuple[RouteFitting, ...]
    bend_report: BendReport
    selected_candidate_id: str
    attempts: tuple[dict, ...]


class NoCheckedAssemblyRoute(RoutingError):
    def __init__(self, attempts):
        self.attempts=tuple(attempts)
        super().__init__('No supplied routing candidate passed both assembly geometry and bend preflight; no model was changed')


def route_checked_assembly(model, start_port_id, end_port_id, route_type, *, assembly_id,
                           domain_kind, nominal_diameter_m, centerline_radius_m, candidates):
    """Return the first caller-ordered candidate passing both existing checks.

    Re-derive each candidate's domain for its actual clearance. Append only the
    chosen route/fittings. Bend admission is scoped to that new route; the caller
    still connects circuit intent and validates whole-job QA.
    """
    validate_model(model)
    for label,value in (('nominal diameter',nominal_diameter_m),('centerline radius',centerline_radius_m)):
        if type(value) not in (float,int) or not math.isfinite(value) or value<=0:
            raise ValueError(f'Explicit finite positive {label} required')
    if domain_kind not in ('wall-cavity','ceiling-plenum'):
        raise ValueError('Explicit wall-cavity or ceiling-plenum domain kind required')
    if not isinstance(candidates,(tuple,list)) or not 1<=len(candidates)<=8:
        raise ValueError('Supply one to eight ordered routing candidates')
    ids=set()
    for candidate in candidates:
        if (not isinstance(candidate,AssemblyRouteCandidate) or not isinstance(candidate.id,str)
            or not candidate.id.strip() or candidate.id in ids or not isinstance(candidate.options,RoutingOptions)):
            raise ValueError('Each routing candidate requires a unique nonempty id and RoutingOptions')
        for key,value in asdict(candidate.options).items():
            if key=='max_bends' and value is None:continue
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('Routing options must be finite numeric values, not booleans')
        ids.add(candidate.id)
    ports={p.id for p in model.ports}
    if start_port_id not in ports or end_port_id not in ports or start_port_id==end_port_id:
        raise ValueError('Two distinct canonical port IDs required')
    if not isinstance(route_type,str) or not route_type.strip():
        raise ValueError('Explicit route type required')
    if assembly_id not in {a.id for a in model.building_assemblies}:
        raise ValueError('Unknown canonical assembly ID')
    derive=derive_wall_cavity_route_domain if domain_kind=='wall-cavity' else derive_suspended_ceiling_route_domain
    attempts=[]
    for candidate in candidates:
        receipt={'candidate_id':candidate.id,'routing_options':asdict(candidate.options),
                 'centerline_radius_m':float(centerline_radius_m),'nominal_diameter_m':float(nominal_diameter_m),
                 'assembly_id':assembly_id,'domain_kind':domain_kind}
        try:
            domain=derive(model,assembly_id,nominal_diameter_m=nominal_diameter_m,options=candidate.options)
            receipt['domain_id']=domain.id
            route,fittings=route_between_ports(model,start_port_id,end_port_id,route_type,
                nominal_diameter_m=nominal_diameter_m,options=candidate.options,allowed_domain=domain)
        except RoutingError as exc:
            receipt.update(status='geometry_unresolved',reason=str(exc),reason_code=getattr(exc,'reason',None))
            attempts.append(receipt)
            continue
        if route.id in {r.id for r in model.routes}:
            raise ValueError('Generated route already exists; use an explicit revision workflow')
        proposed=replace(model,routes=(*model.routes,route),route_fittings=(*model.route_fittings,*fittings))
        full=check_route_bends(proposed,radii_m={route.id:centerline_radius_m})
        report=replace(full,bends=tuple(b for b in full.bends if b.route_id==route.id),
                       diagnostics=tuple(d for d in full.diagnostics if d.route_id==route.id))
        receipt.update(route_id=route.id,status='selected' if report.ready else 'bend_preflight_failed',
                       bend_diagnostics=[asdict(d) for d in report.diagnostics])
        attempts.append(receipt)
        if report.ready:
            return CheckedAssemblyRoute(proposed,route,fittings,report,candidate.id,tuple(attempts))
    raise NoCheckedAssemblyRoute(attempts)
