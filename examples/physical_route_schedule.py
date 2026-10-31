"""Generate a synthetic physical route, GLB and geometric schedule together."""
from dataclasses import replace
import json
from pathlib import Path
import struct
import sys

from oabm.model import (
    BuildingModel, ElectricalDevice, Port, Obstacle, Point3, Vector3, Pose, Size3,
    Box3D, Provenance, PhysicalRouteRequirements, PhysicalRoutingContext,
    PhysicalRoutingExecutionLimits, PhysicalRoutingObjective, PhysicalGeometryCoverage,
    AllowedPhysicalDomain, compute_physical_source_sha256, compute_physical_request_sha256,
)
from oabm.routing.physical_generate import generate_physical_route
from oabm.exports.gltf import build_glb_bytes
from oabm.reports.physical_routes import generate_physical_route_schedule


def supplied(*fields):
    return (Provenance(source_kind='synthetic', source_id='source:route-schedule-demo',
                       derivation='user', scope_paths=tuple(fields) or None),)


def create_example(output):
    a,b=Point3(x=0,y=0,z=1),Point3(x=4,y=0,z=1)
    obstacle=Obstacle(id='obstacle:demo',name='Synthetic supplied obstruction',
        geometry=Box3D(pose=Pose(position=Point3(x=2,y=0,z=1)),size=Size3(x=.7,y=.7,z=.7)),
        provenance=supplied())
    source=BuildingModel(model_id='model:route-schedule-demo',schema_version='1.7.0',
        electrical_devices=tuple(ElectricalDevice(id=f'device:{key}',device_type='geometry-unspecified-endpoint',
            pose=Pose(position=p),provenance=supplied()) for key,p in (('a',a),('b',b))),
        ports=(Port(id='port:a',owner_id='device:a',domain='power',role='source',pose=Pose(position=a),direction=Vector3(x=1,y=0,z=0)),
               Port(id='port:b',owner_id='device:b',domain='power',role='sink',pose=Pose(position=b),direction=Vector3(x=-1,y=0,z=0))),
        obstacles=(obstacle,))
    source=BuildingModel.from_json(source.to_json(indent=None))
    requirements=PhysicalRouteRequirements(id='requirements:demo',route_id='route:demo',
        outside_diameter_m=.025,centerline_radius_m=.15,clearance_m=.01,
        start_straight_min_m=.2,end_straight_min_m=.2,between_bends_straight_min_m=.1,
        provenance=supplied('outside_diameter_m','centerline_radius_m','clearance_m',
                            'start_straight_min_m','end_straight_min_m','between_bends_straight_min_m'))
    context=PhysicalRoutingContext(request_id='request:demo',route_type='emt',
        source_model_sha256=compute_physical_source_sha256(source),source_model_schema_version='1.7.0',
        coordinate_frame_id='model',start_port_id='port:a',end_port_id='port:b',
        allowed_domain=AllowedPhysicalDomain(id='domain:demo',coordinate_frame_id='model',
            volumes=(Box3D(pose=Pose(position=Point3(x=2,y=0,z=1)),size=Size3(x=6,y=4,z=3)),),provenance=supplied('volumes')),
        coverage=(PhysicalGeometryCoverage(source_entity_id=obstacle.id,decision='direct_blocker',
            blocker_entity_ids=(obstacle.id,),rationale='Explicit synthetic box.',
            provenance=supplied('source_entity_id','decision','blocker_entity_ids','rationale')),),
        objective=PhysicalRoutingObjective(bend_penalty_m=0),
        execution_limits=PhysicalRoutingExecutionLimits(candidate_routes=8,candidate_grid_nodes=200000,
            pushed_search_states=500000,interval_refinement_depth=64,total_refinement_cells=100000),
        request_sha256='0'*64)
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    generated=generate_physical_route(source,context,requirements)
    if generated.status!='verified_route':
        raise ValueError('Synthetic routing did not produce a verified incumbent: '+generated.to_json())
    model=BuildingModel.from_json(replace(source,routes=(generated.route,)).to_json(indent=None))
    report=generate_physical_route_schedule(model)
    glb=build_glb_bytes(model,physical_routes=True,include_obstacles=True,emphasized_ids=(generated.route.id,))
    json_size=struct.unpack_from('<I',glb,12)[0]
    exported=json.loads(glb[20:20+json_size])
    if exported['asset']['extras']['canonical_model_sha256']!=report.to_dict()['source']['sha256']:
        raise ValueError('Model, GLB and measurement report must share one canonical source identity')
    prepared={name:body.encode('utf-8') for name,body in {
        'source.json':source.to_json(indent=None),'model.json':report.source_model_json,
        'generation-result.json':generated.to_json(),'report.json':report.to_json(),
        'routes.csv':report.routes_csv(),'segments.csv':report.segments_csv(),
    }.items()}
    prepared['model.glb']=glb
    output=Path(output);output.mkdir(parents=False,exist_ok=False)
    for name,data in prepared.items():(output/name).write_bytes(data)
    return report


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python examples/physical_route_schedule.py FRESH_OUTPUT_DIRECTORY')
    result=create_example(sys.argv[1])
    print(json.dumps(result.to_dict()['totals'],sort_keys=True))
