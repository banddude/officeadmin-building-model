"""Synthetic room + legacy + physical revision evidence, without engineering claims."""
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

from oabm.building_assemblies import regenerate_building_assemblies
from oabm.building_assemblies.room_fixtures import (
    synthetic_enclosed_room_assembly, synthetic_room_edit_requests,
)
from oabm.model import (
    AllowedPhysicalDomain, Box3D, ElectricalDevice, PhysicalGeometryCoverage,
    PhysicalRoute, PhysicalRouteRequirements, PhysicalRoutingContext,
    PhysicalRoutingExecutionLimits, PhysicalRoutingObjective, Point3, Polyline3D,
    Port, Pose, Provenance, Route, Size3, Vector3, compute_physical_request_sha256,
    compute_physical_source_sha256, physical_geometry_inventory,
    upgrade_to_v1_7, validate_physical_context_source,
)
from oabm.qa.revision_diff import RevisionDiff, build_revision_diff
from oabm.quantities import extract_quantities
from oabm.reports.physical_route_revisions import (
    generate_physical_route_comparison, validate_physical_route_comparison,
)
from oabm.routing.physical_generation_adapter import canonical_candidate
from oabm.routing.physical_generation_math import realize


def supplied(*paths):
    return (Provenance(source_kind="synthetic", source_id="source:mixed-revision",
                       derivation="user", scope_paths=tuple(paths) or None),)


def mixed_snapshot(*, endpoint_x=4., radius_m=.15, edit_room=False):
    """Fresh supplied path/request bound to each exact pre-route room snapshot.

    Room and legacy geometry are explicitly outside this fixture's clearance
    proof scope. A canonical candidate is not a generated verification receipt.
    """
    room = upgrade_to_v1_7(synthetic_enclosed_room_assembly())
    if edit_room:
        room = regenerate_building_assemblies(room, synthetic_room_edit_requests())
    a, b = Point3(x=1., y=1., z=1.), Point3(x=endpoint_x, y=1., z=1.)
    devices = tuple(ElectricalDevice(id=f"device:{key}", device_type="geometry-unspecified-endpoint",
        pose=Pose(position=point), level_id="level:proof", provenance=supplied())
        for key, point in (("a", a), ("b", b)))
    ports = tuple(Port(id=f"port:{key}", owner_id=f"device:{key}", domain="power", role=role,
        pose=Pose(position=point), direction=Vector3(x=sign, y=0, z=0))
        for key, point, role, sign in (("a", a, "source", 1), ("b", b, "sink", -1)))
    legacy = Route(id="route:legacy", route_type="emt",
        start_port_id="port:a", end_port_id="port:b", nominal_diameter_m=.02,
        centerline=Polyline3D(points=(a, b)), provenance=supplied("centerline"))
    source = replace(room, electrical_devices=devices, ports=ports, routes=(legacy,))
    requirements = PhysicalRouteRequirements(id="requirements:physical", route_id="route:physical",
        outside_diameter_m=.02, centerline_radius_m=radius_m, clearance_m=.01,
        start_straight_min_m=.1, end_straight_min_m=.1, between_bends_straight_min_m=.1,
        provenance=supplied("outside_diameter_m", "centerline_radius_m", "clearance_m",
                           "start_straight_min_m", "end_straight_min_m", "between_bends_straight_min_m"))
    coverage = tuple(PhysicalGeometryCoverage(source_entity_id=identity,
        decision="outside_proof_scope", blocker_entity_ids=(),
        rationale="Supplied room/legacy context for revision inspection; no clearance proof.",
        provenance=supplied("source_entity_id", "decision", "blocker_entity_ids", "rationale"))
        for identity in sorted(physical_geometry_inventory(source, "emt")))
    context = PhysicalRoutingContext(request_id="request:physical", route_type="emt",
        source_model_sha256=compute_physical_source_sha256(source), source_model_schema_version=source.schema_version,
        coordinate_frame_id=source.coordinate_system.frame_id, start_port_id="port:a", end_port_id="port:b",
        allowed_domain=AllowedPhysicalDomain(id="domain:physical", coordinate_frame_id=source.coordinate_system.frame_id,
            volumes=(Box3D(pose=Pose(position=Point3(x=2.5,y=1.5,z=1)),size=Size3(x=6,y=4,z=3)),),
            provenance=supplied("volumes")), coverage=coverage,
        objective=PhysicalRoutingObjective(bend_penalty_m=0),
        execution_limits=PhysicalRoutingExecutionLimits(candidate_routes=8,candidate_grid_nodes=200000,
            pushed_search_states=500000,interval_refinement_depth=64,total_refinement_cells=100000),
        request_sha256="0"*64)
    context = replace(context, request_sha256=compute_physical_request_sha256(context, requirements))
    points = ((1,1,1),(1.75,1,1),(1.75,2,1),(3.25,2,1),(3.25,1,1),(endpoint_x,1,1))
    ticks = lambda value: int(Decimal(str(value))*1_000_000_000)
    shape = realize(tuple(tuple(ticks(value) for value in point) for point in points),
                    ticks(radius_m), (1,0,0), (1,0,0))
    route = canonical_candidate(source, requirements, context, shape)
    validate_physical_context_source(source, route)
    return replace(source, routes=(*source.routes, route))


def mixed_snapshots():
    return mixed_snapshot(), mixed_snapshot(endpoint_x=4.5, radius_m=.2, edit_room=True)


def create_example(output):
    before, after = mixed_snapshots()
    frozen = tuple(model.to_json(indent=None) for model in (before, after))
    report = build_revision_diff(before, after)
    physical = generate_physical_route_comparison(before, after)
    physical.validate_against_models(before, after)
    assert validate_physical_route_comparison(physical.to_json()).to_json() == physical.to_json()
    assert RevisionDiff.from_dict(report.to_dict()).to_json() == report.to_json()
    assert not build_revision_diff(before, before).changes
    contents = {"revision.json": report.to_json(), "physical-comparison.json": physical.to_json()}
    for label, model, text in zip(("before", "after"), (before, after), frozen):
        source = replace(model, routes=tuple(route for route in model.routes if not isinstance(route, PhysicalRoute)))
        route = next(route for route in model.routes if isinstance(route, PhysicalRoute))
        validate_physical_context_source(source, route)
        contents[f"{label}.json"] = text
        contents[f"{label}-source.json"] = source.to_json(indent=None)
        contents[f"{label}-quantities.json"] = extract_quantities(model).to_json()
    assert tuple(model.to_json(indent=None) for model in (before, after)) == frozen
    summary = {"canonical_changes": {key: getattr(report,key) for key in ("added_count","removed_count","changed_count")},
        "physical_exact_delta": next(row["delta"] for row in physical.to_dict()["routes"] if row["route_id"] == "route:physical"),
        "quantity_deltas": [row.to_dict() for row in report.quantity_deltas],
        "immutable_sources": True, "no_pricing_or_clearance_approval": True,
        "artifact_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name,text in contents.items()}}
    contents["SUMMARY.json"] = json.dumps(summary,sort_keys=True,indent=2)+"\n"
    contents["README.txt"] = (
        "SYNTHETIC ONLY. Actual enclosed room assembly edits plus endpoint X=4 -> 4.5 m and radius .15 -> .2 m. "
        "Stable IDs, explicit pre-route sources and request hashes are retained. Canonical supplied paths have "
        "four quarter bends; exact physical net length is .1 + .1*pi m, and the legacy run adds .5 m. "
        "Room/legacy geometry is explicitly outside clearance proof scope. No live route verification, "
        "product selection, pricing, automatic changed-source reconciliation or engineering approval. "
        "revision.json is the unchanged canonical diff schema; physical-comparison.json retains the existing "
        "source-bound exact tick/pi measurements. No physical quantity is added twice.\n")
    root = Path(output); root.mkdir(exist_ok=False)
    for name,text in contents.items(): (root/name).write_text(text)
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python examples/canonical_physical_revisions.py FRESH_OUTPUT_DIRECTORY")
    create_example(sys.argv[1])
