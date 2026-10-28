"""Synthetic framing/ceiling -> checked plenum route -> plans, quantities and GLB.

Run with a fresh output root. Endpoints and dimensions below are explicit demo
design inputs. No site interpretation, drilling or engineering decision is made.
"""
from dataclasses import replace
import argparse
import json
from pathlib import Path

from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.exports.model_delivery import deliver_model
from oabm.model import ElectricalEquipment, Point3, Port, Pose, Vector3
from oabm.quantities import extract_quantities
from oabm.routing import RoutingOptions
from oabm.routing.checked_assembly import AssemblyRouteCandidate, NoCheckedAssemblyRoute, route_checked_assembly


def build_selection():
    model=synthetic_wall_inserts_ceiling_assembly()
    level=model.levels[0].id
    # Place the direct chord through a generated hanger, so the router must
    # actually use component geometry rather than drawing the straight chord.
    hanger=next(c for c in sorted(model.assembly_components,key=lambda c:c.id)
                if c.component_kind=='hanger_wire' and .2<c.geometry.centerline.points[0].x<4.6)
    y=hanger.geometry.centerline.points[0].y
    points=(Point3(x=.25,y=y,z=2.6),Point3(x=4.55,y=y,z=2.6))
    equipment=tuple(ElectricalEquipment(id=f'equipment:demo-{i}',equipment_type='junction',
        pose=Pose(position=p),level_id=level,name=f'Synthetic plenum endpoint {i}') for i,p in enumerate(points))
    ports=tuple(Port(id=f'port:demo-{i}',owner_id=e.id,domain='electrical',role='source' if i==0 else 'load',
        pose=e.pose,direction=Vector3(x=1.0 if i==0 else -1.0,y=0.0,z=0.0),nominal_diameter_m=.02)
        for i,e in enumerate(equipment))
    model=replace(model,electrical_equipment=equipment,ports=ports)
    return route_checked_assembly(model,ports[0].id,ports[1].id,'emt',
        assembly_id='assembly:ceiling-proof',domain_kind='ceiling-plenum',
        nominal_diameter_m=.02,centerline_radius_m=.08,candidates=(
            AssemblyRouteCandidate('tight-envelope',RoutingOptions(port_stub_m=.05,clearance_m=.005)),
            AssemblyRouteCandidate('wide-envelope',RoutingOptions(port_stub_m=.10,clearance_m=.17)),
        ))


def build_model():
    return build_selection().model


def run(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    try:
        selection=build_selection()
    except NoCheckedAssemblyRoute as exc:
        (root/'routing-failure.json').write_text(json.dumps({'status':'no_checked_route','attempts':exc.attempts},indent=2)+'\n')
        raise
    model=selection.model
    (root/'routing-selection.json').write_text(json.dumps({'selected_candidate_id':selection.selected_candidate_id,
        'attempts':selection.attempts,'qualification':'Passed bounded geometry and tangent-space checks only; curved swept clearance and installation approval remain unverified.'},sort_keys=True,indent=2)+'\n')
    source=root/'model.json';source.write_text(model.to_json())
    config={'version':'model-delivery-v2','assembly_geometry':'cut','bend_policy':'require',
        'radii_m':{model.routes[0].id:.08},'preview_glb':True,
        'plans':[{'id':'framing-cut','level_id':model.levels[0].id,'cut_height_m':1.2,
                  'view_depth_below_m':0.,'view_depth_above_m':0.,'scale':20},
                 {'id':'plenum-route','level_id':model.levels[0].id,'cut_height_m':2.6,
                  'view_depth_below_m':0.,'view_depth_above_m':.1,'scale':20}]}
    options=root/'delivery-options.json';options.write_text(json.dumps(config,indent=2)+'\n')
    output=deliver_model(source,options,output_directory=root/'attempt')
    quantities=extract_quantities(model)
    (root/'quantities.json').write_text(quantities.to_json()+'\n')
    report={'model_id':model.model_id,'assemblies':len(model.building_assemblies),
        'components':len(model.assembly_components),'route_ids':[r.id for r in model.routes],
        'route_points':[p.to_dict() if hasattr(p,'to_dict') else {'x':p.x,'y':p.y,'z':p.z}
                        for p in model.routes[0].centerline.points],
        'geometry_check':dict(model.routes[0].attributes['geometry_check']),
        'quantity_items':len(quantities.items),
        'limitations':['Synthetic one-wall and ceiling-region proof, not a complete building.',
                      'The route remains within the plenum; no wall/plate penetrations are created.',
                      'Nominal route geometry is not manufacturer fitting geometry or installation approval.',
                      'Read the retained bend report; routing does not prove swept elbow clearance.']}
    (root/'proof.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output')
    print(run(parser.parse_args().output))
