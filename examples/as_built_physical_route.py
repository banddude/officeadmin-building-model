"""Independent public three-story field reroute; no installed-product claim."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile

from oabm.model import (AllowedPhysicalDomain,Box3D,BuildingModel,ElectricalDevice,
    ElectricalEquipment,Level,LineSegment3D,PhysicalRoute,PhysicalRouteJoint,
    PhysicalRouteRequirements,PhysicalRoutingContext,PhysicalRoutingExecutionLimits,
    PhysicalRoutingObjective,Point3,Port,Pose,Provenance,QuarterArc3D,Size3,Vector3,
    compute_physical_source_sha256,compute_physical_request_sha256,validate_model)
from oabm.pipeline.as_built import VERSION,start_history,replay_history,record_sha256,snapshot,_sha
from oabm.pipeline.as_built_delivery import create_as_built_delivery
from as_built_quick_edit import _retain_sheet_images


def supplied(*paths):
    return (Provenance(source_kind='synthetic',source_id='source:physical-field-example',
        derivation='user',scope_paths=tuple(paths) or None),)


def physical_field_inputs():
    """Author exact canonical records, then hash the retained typed parent.

    Both routes have the same ports and endpoint joints. The field route raises
    at X=.75, crosses at Z=3.5 and rises again at X=2. These are supplied example
    centerlines, never a clearance-verified route or fabricated installed item.
    """
    start,end=Point3(x=0.,y=0.,z=0.),Point3(x=2.,y=0.,z=7.)
    source=BuildingModel(model_id='model:synthetic-physical-field-reroute',schema_version='1.7.0',
        name='Independent synthetic physical field reroute; products unresolved',
        levels=tuple(Level(id=f'level:physical:{i}',name=f'Physical example story {i+1}',
            elevation_m=float(i*3),height_m=3.) for i in range(3)),
        electrical_equipment=(ElectricalEquipment(id='panel:physical-example',equipment_type='panelboard',
            name='Independent supplied panel; ratings unknown',pose=Pose(position=Point3(x=-1.,y=1.,z=1.5)),level_id='level:physical:0'),),
        electrical_devices=tuple(ElectricalDevice(id=f'device:physical:{key}',device_type='geometry-unspecified-endpoint',
            name=f'Physical route endpoint {key}',pose=Pose(position=point),level_id=f'level:physical:{level}')
            for key,point,level in (('a',start,0),('b',end,2))),
        ports=(Port(id='port:physical:a',owner_id='device:physical:a',domain='power',role='source',
            pose=Pose(position=start),direction=Vector3(x=1.,y=0.,z=0.)),
            Port(id='port:physical:b',owner_id='device:physical:b',domain='power',role='sink',
            pose=Pose(position=end),direction=Vector3(x=0.,y=0.,z=-1.))))
    source=BuildingModel.from_json(source.to_json())
    requirements=PhysicalRouteRequirements(id='requirements:physical-example',route_id='route:physical-example',
        outside_diameter_m=.02,centerline_radius_m=.25,clearance_m=.01,
        start_straight_min_m=.1,end_straight_min_m=.1,between_bends_straight_min_m=.1,
        provenance=supplied('outside_diameter_m','centerline_radius_m','clearance_m',
            'start_straight_min_m','end_straight_min_m','between_bends_straight_min_m'))
    context=PhysicalRoutingContext(request_id='request:physical-example',route_type='emt',
        source_model_sha256=compute_physical_source_sha256(source),source_model_schema_version=source.schema_version,
        coordinate_frame_id=source.coordinate_system.frame_id,start_port_id='port:physical:a',end_port_id='port:physical:b',
        allowed_domain=AllowedPhysicalDomain(id='domain:physical-example',coordinate_frame_id=source.coordinate_system.frame_id,
            volumes=(Box3D(pose=Pose(position=Point3(x=1.,y=0.,z=3.5)),size=Size3(x=6.,y=4.,z=10.)),),
            provenance=supplied('volumes')),coverage=(),objective=PhysicalRoutingObjective(bend_penalty_m=0.),
        execution_limits=PhysicalRoutingExecutionLimits(candidate_routes=8,candidate_grid_nodes=200000,
            pushed_search_states=500000,interval_refinement_depth=64,total_refinement_cells=100000),request_sha256='0'*64)
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))

    def route(points,turns):
        joints=tuple(PhysicalRouteJoint(id=f'joint:physical:{"start" if i==0 else "end" if i==len(points)-1 else i}',
            route_id=requirements.route_id,position=Point3(x=x,y=0.,z=z),provenance=supplied('position'))
            for i,(x,z) in enumerate(points))
        segments=[]
        for i,(a,b) in enumerate(zip(joints,joints[1:])):
            args=dict(id=f'segment:physical:{i}',route_id=requirements.route_id,
                start_joint_id=a.id,end_joint_id=b.id)
            if i in turns:
                segments.append(QuarterArc3D(**args,plane_normal=Vector3(x=0.,y=1.,z=0.),turn=turns[i],
                    provenance=supplied('start_joint_id','end_joint_id','plane_normal','turn')))
            else:
                segments.append(LineSegment3D(**args,provenance=supplied('start_joint_id','end_joint_id')))
        return PhysicalRoute(id=requirements.route_id,route_type='emt',start_port_id=context.start_port_id,
            end_port_id=context.end_port_id,joints=joints,segments=tuple(segments),requirements=requirements,
            context=context,provenance=supplied('joints','segments'))

    baseline=route(((0.,0.),(1.75,0.),(2.,.25),(2.,7.)),{1:'negative_quarter'})
    changed=route(((0.,0.),(.5,0.),(.75,.25),(.75,3.25),(1.,3.5),(1.75,3.5),(2.,3.75),(2.,7.)),
        {1:'negative_quarter',3:'positive_quarter',5:'negative_quarter'})
    validate_model(replace(source,routes=(changed,)))
    history=start_history(replace(source,routes=(baseline,)));parent=replay_history(history)
    replacement=replace(parent,routes=(changed,)).to_dict()['routes'][0]
    old=parent.to_dict()['routes'][0]
    statement='Synthetic supplied field reroute with fixed endpoints and unchanged source/request bindings. Products and clearance remain unconfirmed.'
    request={'version':VERSION,'transaction_id':'field:physical-example','actor':'synthetic-field-example','reason':statement,
        'design_sha256':history['design_sha256'],'parent_sha256':_sha(snapshot(parent)),
        'evidence':[{'id':'evidence:physical-example','kind':'quick_edit','reference':'synthetic-inline-field-note',
            'sha256':_sha(statement),'statement':statement}],
        'edits':[{'collection':'routes','id':old['id'],'expected_sha256':record_sha256(old),
            'record':replacement,'evidence_ids':['evidence:physical-example']}],
        'method_reconciliations':[],'installed_assertions':[]}
    return history,request


def create_physical_field_delivery(output_directory,*,preview_images=False):
    history,request=physical_field_inputs()
    with tempfile.TemporaryDirectory(prefix='physical-field-inputs-') as temporary:
        temporary=Path(temporary)
        (temporary/'history.json').write_text(json.dumps(history))
        (temporary/'transaction.json').write_text(json.dumps(request))
        manifest=create_as_built_delivery(temporary/'history.json',temporary/'transaction.json',output_directory=output_directory)
    if preview_images:
        _retain_sheet_images(output_directory,manifest)
    return manifest


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python examples/as_built_physical_route.py FRESH_OUTPUT_DIRECTORY')
    print(json.dumps(create_physical_field_delivery(sys.argv[1]),sort_keys=True))
