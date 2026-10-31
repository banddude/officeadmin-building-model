"""Synthetic source and a structurally valid route. No physical verification claim."""
from dataclasses import replace
from oabm.model import (
    AllowedPhysicalDomain, Box3D, BuildingModel, ElectricalDevice, LineSegment3D,
    PhysicalRoute, PhysicalRouteJoint, PhysicalRouteRequirements, PhysicalRoutingContext,
    PhysicalRoutingExecutionLimits, PhysicalRoutingObjective, Point3, Port, Pose,
    Provenance, Size3, Vector3, compute_physical_source_sha256,
    compute_physical_request_sha256,
)


def supplied(*scopes):
    return (Provenance(source_kind="synthetic", source_id="source:physical-fixture",
                       derivation="user", scope_paths=tuple(scopes) or None),)


def make_physical_source():
    a, b = Point3(x=0.0,y=0.0,z=0.0), Point3(x=2.0,y=0.0,z=0.0)
    return BuildingModel(
        model_id="model:physical-fixture", schema_version="1.7.0",
        electrical_devices=(
            ElectricalDevice(id="device:a",device_type="geometry-unspecified-endpoint",pose=Pose(position=a)),
            ElectricalDevice(id="device:b",device_type="geometry-unspecified-endpoint",pose=Pose(position=b)),
        ),
        ports=(
            Port(id="port:a",owner_id="device:a",domain="power",role="source",pose=Pose(position=a),direction=Vector3(x=1,y=0,z=0)),
            Port(id="port:b",owner_id="device:b",domain="power",role="sink",pose=Pose(position=b),direction=Vector3(x=-1,y=0,z=0)),
        ),
    )


def make_physical_route(source=None):
    source = source or make_physical_source()
    requirements = PhysicalRouteRequirements(
        id="requirements:physical",route_id="route:physical",
        outside_diameter_m=0.02,centerline_radius_m=0.3,clearance_m=0.01,
        start_straight_min_m=0.1,end_straight_min_m=0.1,between_bends_straight_min_m=0.1,
        provenance=supplied("outside_diameter_m","centerline_radius_m","clearance_m",
                            "start_straight_min_m","end_straight_min_m","between_bends_straight_min_m"),
    )
    context = PhysicalRoutingContext(
        request_id="request:physical",route_type="emt",source_model_sha256=compute_physical_source_sha256(source),
        source_model_schema_version=source.schema_version,coordinate_frame_id="model",
        start_port_id="port:a",end_port_id="port:b",
        allowed_domain=AllowedPhysicalDomain(id="domain:physical",coordinate_frame_id="model",
            volumes=(Box3D(pose=Pose(position=Point3(x=1,y=0,z=0)),size=Size3(x=4,y=4,z=4)),),
            provenance=supplied("volumes")),
        coverage=(),objective=PhysicalRoutingObjective(bend_penalty_m=0.0),
        execution_limits=PhysicalRoutingExecutionLimits(candidate_routes=128,candidate_grid_nodes=200000,
            pushed_search_states=500000,interval_refinement_depth=64,total_refinement_cells=1000000),
        request_sha256="0"*64,
    )
    context=replace(context,request_sha256=compute_physical_request_sha256(context,requirements))
    return PhysicalRoute(
        id="route:physical",route_type="emt",start_port_id="port:a",end_port_id="port:b",
        joints=tuple(PhysicalRouteJoint(id="joint:"+key,route_id="route:physical",
                                       position=source.ports[index].pose.position,provenance=supplied("position"))
                     for index,key in enumerate(("a","b"))),
        segments=(LineSegment3D(id="segment:line",route_id="route:physical",start_joint_id="joint:a",
                                end_joint_id="joint:b",provenance=supplied("start_joint_id","end_joint_id")),),
        requirements=requirements,context=context,provenance=supplied("joints","segments"),
    )


def make_physical_model():
    source=make_physical_source()
    return replace(source,routes=(make_physical_route(source),))
