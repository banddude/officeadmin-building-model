"""Semantic validation helpers for the isolated canonical assembly draft."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
import math
import types
from typing import get_args, get_origin, get_type_hints
from typing import Any

from oabm.assembly_geometry import GeometryInputError, extrude_simple_section_m, straight_round_wire_m

from .assemblies import (
    ASSEMBLY_GEOMETRY_TOLERANCE_M, AssemblyComponent, BuildingAssembly,
    PanelGeometry, Profile2D, ProfileExtrusion, ResolvedParameter, WireSweep,
    point_on_component_surface, point_on_slab_surface, rotate_vector,
    stable_assembly_component_id, _point_in_polygon_2d,
)
from .common import Box3D, ContractError, Point3, Quaternion, Vector3, _distance
from .entities import Ceiling, Opening, Slab, Wall


def _portable(value: Any, *, numeric_hint: str | None = None) -> Any:
    if numeric_hint == "float" and isinstance(value, int) and not isinstance(value, bool):
        value=float(value)
    if is_dataclass(value) and not isinstance(value, type):
        hints=get_type_hints(type(value))
        output={}
        for item in fields(value):
            child=getattr(value,item.name)
            hint=hints.get(item.name)
            origin=get_origin(hint)
            args=get_args(hint)
            expected_float=(hint is float or
                            (origin in (types.UnionType,) and float in args and int not in args))
            if type(value).__name__=="ResolvedParameter" and item.name=="value":
                expected_float=value.value_kind in {"length","angle"}
            output[item.name]=_portable(child,numeric_hint="float" if expected_float else None)
        return output
    if isinstance(value, tuple):
        return [_portable(item) for item in value]
    if isinstance(value, list):
        return [_portable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in value.items()}
    return value


def _digest(domain: str, value: Any) -> str:
    payload=json.dumps(_portable(value),sort_keys=True,separators=(",",":"),allow_nan=False)
    return hashlib.sha256((domain+"\0"+payload).encode("utf-8")).hexdigest()


def compute_assembly_input_digest(model: Any, assembly: BuildingAssembly) -> str:
    """Digest exact current source records, including provenance, plus frozen choices."""
    walls={item.id:item for item in model.walls}
    ceilings={item.id:item for item in model.ceilings}
    openings={item.id:item for item in model.openings}
    levels={item.id:item for item in model.levels}
    slabs={item.id:item for item in model.slabs}
    source_objects: dict[str, Any] = {}
    if assembly.assembly_kind == "wall":
        host=walls[assembly.host_id]
        source_objects[host.id]=host
        source_objects[host.level_id]=levels[host.level_id]
        for opening in sorted((row for row in model.openings if row.host_id==host.id),key=lambda row:row.id):
            source_objects[opening.id]=opening
    elif assembly.assembly_kind == "opening_insert":
        opening=openings[assembly.host_id]
        host=walls.get(opening.host_id) or ceilings.get(opening.host_id)
        if host is None: raise ContractError("opening insert has no supported source host")
        source_objects[opening.id]=opening
        source_objects[host.id]=host
        source_objects[host.level_id]=levels[host.level_id]
    else:
        host=ceilings[assembly.host_id]
        source_objects[host.id]=host
        source_objects[host.level_id]=levels[host.level_id]
        if assembly.deck_id in slabs:
            source_objects[assembly.deck_id]=slabs[assembly.deck_id]
    snapshot={
        "assembly_id": assembly.id,
        "assembly_kind": assembly.assembly_kind,
        "host_id": assembly.host_id,
        "level_id": assembly.level_id,
        "host_representation": assembly.host_representation,
        "catalog_id": assembly.catalog_id,
        "catalog_revision": assembly.catalog_revision,
        "preset_id": assembly.preset_id,
        "preset_revision": assembly.preset_revision,
        "selected_by": assembly.selected_by,
        "generator_version": assembly.generator_version,
        "resolved_parameters": tuple(sorted(assembly.resolved_parameters,key=lambda row:row.parameter_id)),
        "provenance": assembly.provenance,
        "wall_frame": assembly.wall_frame,
        "deck_id": assembly.deck_id,
        "source_objects": {key:source_objects[key] for key in sorted(source_objects)},
    }
    return _digest("oabm-building-assembly-input-v1.6",snapshot)


def compute_assembly_output_digest(components: tuple[AssemblyComponent, ...]) -> str:
    """Digest complete generated physical children in canonical ID order."""
    ordered=tuple(sorted(components,key=lambda row:row.id))
    return _digest("oabm-building-assembly-output-v1.6",ordered)


def compute_assembly_digests(model: Any, assembly: BuildingAssembly,
                             components: tuple[AssemblyComponent, ...]) -> tuple[str,str]:
    return compute_assembly_input_digest(model,assembly),compute_assembly_output_digest(components)


def assembly_routing_context_receipt(model: Any) -> dict[str, Any] | None:
    """Return a finite source-bound summary for routes that check 1.6 parts.

    This deliberately is not a generic model-dependency graph. It records only
    the assembly assignments and physical identities the router treats as hard
    geometry, plus the host/deck identities that replace aggregate preferences.
    ``validate_building_assemblies`` is responsible for validating the assembly
    digests and ownership before this receipt is used.
    """
    assemblies = tuple(getattr(model, "building_assemblies", ()))
    components = tuple(getattr(model, "assembly_components", ()))
    if not assemblies and not components:
        return None
    if not assemblies or not components:
        # The canonical validator will give the detailed invalid-model error;
        # retain a clear deterministic result if this helper is called alone.
        raise ContractError("assembly routing context requires assignments and owned components")

    component_by_id = {row.id: row for row in components}
    slab_ids = {row.id for row in getattr(model, "slabs", ())}
    assignment_digests = []
    checked_deck_ids: set[str] = set()
    checked_deck_kinds: dict[str, str] = {}
    assigned_wall_ids: set[str] = set()
    assigned_ceiling_ids: set[str] = set()
    for row in sorted(assemblies, key=lambda item: item.id):
        assignment_digests.append({
            "assembly_id": row.id,
            "assembly_kind": row.assembly_kind,
            "host_id": row.host_id,
            "input_digest": row.input_digest,
            "output_digest": row.output_digest,
        })
        if row.assembly_kind == "wall":
            assigned_wall_ids.add(row.host_id)
        elif row.assembly_kind == "suspended_ceiling":
            assigned_ceiling_ids.add(row.host_id)
            if row.deck_id:
                checked_deck_ids.add(row.deck_id)
                if row.deck_id in slab_ids:
                    checked_deck_kinds[row.deck_id] = "existing_slab"
                elif row.deck_id in component_by_id:
                    checked_deck_kinds[row.deck_id] = "owned_deck_panel"
                else:
                    checked_deck_kinds[row.deck_id] = "unresolved"

    body: dict[str, Any] = {
        "version": "assembly-routing-context-v1",
        "assignment_digests": assignment_digests,
        "checked_component_ids": sorted(row.id for row in components),
        "checked_deck_ids": sorted(checked_deck_ids),
        "checked_deck_kinds": [
            {"deck_id": identity, "kind": checked_deck_kinds[identity]}
            for identity in sorted(checked_deck_kinds)
        ],
        "assigned_wall_ids": sorted(assigned_wall_ids),
        "assigned_ceiling_ids": sorted(assigned_ceiling_ids),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return {**body, "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def validate_assembly_route_contexts(model: Any) -> None:
    """Reject stale explicit assembly-aware router receipts on model reload/update.

    Manual routes and older unbounded routes are not promoted to verified
    assembly routes. A generated assembly-routing claim must retain paired
    receipts on the primary router provenance and route geometry-check record,
    matching current canonical assembly identities and digests. Supplemental
    router provenance does not carry a second bounded receipt.
    """
    current = assembly_routing_context_receipt(model)
    current_assembly_ids = set()
    if current is not None:
        current_assembly_ids.update(current.get("checked_component_ids", ()))
        current_assembly_ids.update(current.get("checked_deck_ids", ()))
    for route in getattr(model, "routes", ()):
        route_attributes = getattr(route, "attributes", {})
        route_attributes = route_attributes if isinstance(route_attributes, dict) else {}
        geometry_check = route_attributes.get("geometry_check")
        geometry_check = geometry_check if isinstance(geometry_check, dict) else {}
        router_provenance = tuple(
            item for item in getattr(route, "provenance", ())
            if getattr(item, "source_kind", None) == "router"
        )
        engine_method = route_attributes.get("routing_engine")
        primary_records = tuple(
            item for item in router_provenance
            if isinstance(engine_method, str) and getattr(item, "method", None) == engine_method
        )
        provenance_attributes = tuple(
            item.attributes if isinstance(getattr(item, "attributes", None), dict) else {}
            for item in router_provenance
        )

        # A receipt in either half is an assembly-routing claim. Keep the
        # claim paired and source-bound even if the primary bounded receipt is
        # removed or malformed. Supplemental router provenance (for example,
        # bundle-source-alignment) is deliberately not a receipt owner.
        checked_ids = geometry_check.get("checked_blocker_ids", ())
        checked_classes = geometry_check.get("checked_blocker_classes", ())
        claims_assembly_check = (
            "assembly_routing_context" in geometry_check
            or (
                isinstance(checked_ids, (list, tuple))
                and any(
                    isinstance(identity, str) and identity in current_assembly_ids
                    for identity in checked_ids
                )
            )
            or (
                isinstance(checked_classes, (list, tuple))
                and any(
                    isinstance(name, str) and name.startswith("AssemblyComponent")
                    for name in checked_classes
                )
            )
            or any(
                "assembly_routing_context" in attributes
                or (
                    isinstance(attributes.get("bounded_routing_context"), dict)
                    and "assembly_routing_context" in attributes["bounded_routing_context"]
                )
                for attributes in provenance_attributes
            )
        )
        if not claims_assembly_check:
            # Preserve older bounded/unbounded route semantics, but retain the
            # established fail-closed check when a bounded primary route is
            # evaluated against a populated assembly model.
            if current is not None:
                for item, attributes in zip(router_provenance, provenance_attributes):
                    if not any(item is primary for primary in primary_records):
                        continue
                    bounded = attributes.get("bounded_routing_context")
                    if isinstance(bounded, dict) and "assembly_routing_context" not in bounded:
                        raise ContractError(
                            f"{route.id} is a bounded router route without an assembly-context receipt; "
                            "regenerate it against the current assembly model"
                        )
            continue

        if len(primary_records) != 1:
            raise ContractError(
                f"{route.id} claims assembly-aware routing but does not have exactly one primary "
                "router provenance record; regenerate the route"
            )
        primary = primary_records[0]
        primary_attributes = (
            primary.attributes if isinstance(getattr(primary, "attributes", None), dict) else {}
        )
        bounded = primary_attributes.get("bounded_routing_context")

        # Do not silently accept duplicate or malformed receipt fields attached
        # to supplemental provenance; the primary router record owns the one
        # source-bound bounded context for this route.
        for item in router_provenance:
            if item is primary:
                continue
            attributes = item.attributes if isinstance(getattr(item, "attributes", None), dict) else {}
            if "bounded_routing_context" in attributes or "assembly_routing_context" in attributes:
                raise ContractError(
                    f"{route.id} stores an assembly routing receipt outside its primary router "
                    "provenance record"
                )

        if not isinstance(bounded, dict):
            raise ContractError(
                f"{route.id} claims assembly-aware routing without its paired bounded "
                "assembly-context receipt; regenerate the route"
            )
        if (
            current is None
            or bounded.get("version") != "bounded-routing-context-v1"
            or "assembly_routing_context" not in bounded
            or bounded.get("assembly_routing_context") != current
            or "assembly_routing_context" not in geometry_check
            or geometry_check.get("assembly_routing_context") != current
        ):
            raise ContractError(
                f"{route.id} has stale assembly routing context; regenerate the route"
            )


def _distance2(a, b) -> float:
    return math.hypot(a[0]-b[0],a[1]-b[1])


def _wall_frame_points(wall: Wall):
    points=wall.centerline.points
    if len(points)!=2:
        raise ContractError("assembly wall proof supports exactly two centerline points")
    first,last=points
    if abs(first.z-last.z)>ASSEMBLY_GEOMETRY_TOLERANCE_M:
        raise ContractError("assembly wall axis must be horizontal with a shared base z")
    dx,dy=last.x-first.x,last.y-first.y
    length=math.hypot(dx,dy)
    if length<=ASSEMBLY_GEOMETRY_TOLERANCE_M:
        raise ContractError("assembly wall axis must have nonzero horizontal length")
    return first,last,(dx/length,dy/length,0.0),(-dy/length,dx/length,0.0),length


def _validate_wall_frame(assembly: BuildingAssembly, wall: Wall) -> tuple[float,float]:
    first,last,axis,left_normal,length=_wall_frame_points(wall)
    frame=assembly.wall_frame
    if frame is None:
        raise ContractError("wall assignment is missing its explicit axis/base/normal frame")
    comparisons=(
        (frame.origin.x,first.x),(frame.origin.y,first.y),(frame.origin.z,first.z),
        (frame.axis.x,axis[0]),(frame.axis.y,axis[1]),(frame.axis.z,axis[2]),
        (frame.left_normal.x,left_normal[0]),(frame.left_normal.y,left_normal[1]),
        (frame.left_normal.z,left_normal[2]),(frame.base_z_m,first.z),
    )
    if any(abs(actual-expected)>ASSEMBLY_GEOMETRY_TOLERANCE_M for actual,expected in comparisons):
        raise ContractError("wall assembly frame contradicts the ordered source wall axis/base/normal")
    return length,first.z


def _opening_clearance(assembly: BuildingAssembly) -> float:
    values=[row.value for row in assembly.resolved_parameters
            if row.parameter_id=="wall.opening_clearance_m"]
    if len(values)>1: raise ContractError("duplicate wall opening clearance")
    if not values: return 0.0
    row=next(row for row in assembly.resolved_parameters if row.parameter_id=="wall.opening_clearance_m")
    if row.value_kind!="length" or row.unit!="m" or not isinstance(values[0],(float,int)) or isinstance(values[0],bool) or values[0]<0:
        raise ContractError("wall.opening_clearance_m must be a nonnegative resolved length")
    return float(values[0])


def _validate_wall_openings(assembly: BuildingAssembly, wall: Wall,
                            openings: tuple[Opening,...], length: float, base_z: float) -> None:
    axis=(-wall.centerline.points[0].x+wall.centerline.points[1].x,
          -wall.centerline.points[0].y+wall.centerline.points[1].y)
    axis_len=math.hypot(*axis); axis=(axis[0]/axis_len,axis[1]/axis_len)
    normal=(-axis[1],axis[0]); clearance=_opening_clearance(assembly)
    rectangles=[]
    for opening in openings:
        q=opening.pose.rotation
        local_x=rotate_vector((1.0,0.0,0.0),q)
        local_y=rotate_vector((0.0,1.0,0.0),q)
        local_z=rotate_vector((0.0,0.0,1.0),q)
        expected=((axis[0],axis[1],0.0),(normal[0],normal[1],0.0),(0.0,0.0,1.0))
        for actual,wanted in zip((local_x,local_y,local_z),expected):
            if any(abs(actual[i]-wanted[i])>1e-9 for i in range(3)):
                raise ContractError(f"{opening.id} orientation is not aligned to the ordered wall frame")
        p=opening.pose.position; start=wall.centerline.points[0]
        dx,dy=p.x-start.x,p.y-start.y
        station=dx*axis[0]+dy*axis[1]
        normal_offset=dx*normal[0]+dy*normal[1]
        if abs(normal_offset)>1e-9:
            raise ContractError(f"{opening.id} center is not on the wall axis plane")
        if abs(opening.size.y-wall.thickness_m)>1e-9:
            raise ContractError(f"{opening.id}.size.y must match observed wall thickness")
        lo,hi=station-opening.size.x/2,station+opening.size.x/2
        sill=p.z-opening.size.z/2; head=sill+opening.size.z
        if lo < -1e-9 or hi > length+1e-9 or sill < base_z-1e-9 or head > base_z+wall.height_m+1e-9:
            raise ContractError(f"{opening.id} projected width/height/sill does not fit its wall host")
        if opening.opening_type.casefold()=="door" and abs(sill-base_z)>1e-9:
            raise ContractError(f"{opening.id} door sill must coincide with the wall base")
        rectangles.append((opening,lo-clearance,hi+clearance,sill-clearance,head+clearance))
    for index,(first,*a) in enumerate(rectangles):
        for second,*b in rectangles[index+1:]:
            if min(a[1],b[1])-max(a[0],b[0])>1e-9 and min(a[3],b[3])-max(a[2],b[2])>1e-9:
                raise ContractError(f"opening/frame envelopes overlap: {first.id} and {second.id}")


def _wall_stack_depth(assembly: BuildingAssembly, wall: Wall) -> None:
    params={row.parameter_id:row for row in assembly.resolved_parameters}
    required=params.get("wall.stack.framing_depth_m")
    if required is None or required.value_kind!="length" or required.unit!="m":
        raise ContractError("wall assembly requires resolved wall.stack.framing_depth_m")
    prefixes=("wall.stack.face_left.","wall.stack.face_right.","wall.stack.cavity_gap.")
    values=[required.value]
    for row in assembly.resolved_parameters:
        if row.parameter_id.startswith(prefixes):
            if row.value_kind!="length" or row.unit!="m":
                raise ContractError(f"{row.parameter_id} must be a resolved length in metres")
            values.append(row.value)
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or value<0 for value in values):
        raise ContractError("wall stack depths must be nonnegative resolved metre values")
    if abs(math.fsum(values)-wall.thickness_m)>ASSEMBLY_GEOMETRY_TOLERANCE_M:
        raise ContractError("wall framing/layer/gap stack must equal observed thickness; no source rewrite")


def _transform_local(point, pose) -> tuple[float,float,float]:
    rotated=rotate_vector(point,pose.rotation)
    return (pose.position.x+rotated[0],pose.position.y+rotated[1],pose.position.z+rotated[2])


def component_world_points(component: AssemblyComponent) -> tuple[tuple[float,float,float],...]:
    """Finite vertex/extremum set used for bounded host checks and conservative overlap."""
    geometry=component.geometry
    if isinstance(geometry,Box3D):
        sx,sy,sz=geometry.size.x/2,geometry.size.y/2,geometry.size.z/2
        return tuple(_transform_local((x,y,z),geometry.pose)
                     for x in (-sx,sx) for y in (-sy,sy) for z in (-sz,sz))
    if isinstance(geometry,ProfileExtrusion):
        outline=(geometry.profile.outline_xy_m() if isinstance(geometry.profile,Profile2D)
                 else tuple((p.x,p.y) for p in geometry.profile.points_m))
        return tuple(_transform_local((x,y,z),geometry.pose)
                     for x,y in outline for z in (0.0,geometry.length_m))
    if isinstance(geometry,PanelGeometry):
        n=(geometry.normal.x,geometry.normal.y,geometry.normal.z); half=geometry.thickness_m/2
        return tuple((p.x+sign*n[0]*half,p.y+sign*n[1]*half,p.z+sign*n[2]*half)
                     for p in geometry.boundary.points for sign in (-1.0,1.0))
    if isinstance(geometry,WireSweep):
        radius=geometry.diameter_m/2
        return tuple((p.x+dx*radius,p.y+dy*radius,p.z+dz*radius)
                     for p in geometry.centerline.points
                     for dx in (-1.0,1.0) for dy in (-1.0,1.0) for dz in (-1.0,1.0))
    raise ContractError("unsupported assembly component geometry")


def _validate_wall_components(assembly: BuildingAssembly, wall: Wall,
                              components: tuple[AssemblyComponent,...], length: float, base_z: float,
                              openings: tuple[Opening,...]) -> None:
    start=wall.centerline.points[0]
    axis=(wall.centerline.points[1].x-start.x,wall.centerline.points[1].y-start.y)
    norm=math.hypot(*axis); ux,uy=axis[0]/norm,axis[1]/norm; nx,ny=-uy,ux
    for component in components:
        local_points=[]
        for x,y,z in component_world_points(component):
            dx,dy=x-start.x,y-start.y
            station=dx*ux+dy*uy; through=dx*nx+dy*ny
            local_points.append((station,through,z))
            if (station < -1e-9 or station > length+1e-9 or
                    abs(through)>wall.thickness_m/2+1e-9 or
                    z < base_z-1e-9 or z > base_z+wall.height_m+1e-9):
                raise ContractError(f"{component.id} geometry exceeds its explicit wall host bounds")
        part_min=(min(row[0] for row in local_points),min(row[1] for row in local_points),min(row[2] for row in local_points))
        part_max=(max(row[0] for row in local_points),max(row[1] for row in local_points),max(row[2] for row in local_points))
        for opening in openings:
            station=(opening.pose.position.x-start.x)*ux+(opening.pose.position.y-start.y)*uy
            open_min=(station-opening.size.x/2,-wall.thickness_m/2,
                      opening.pose.position.z-opening.size.z/2)
            open_max=(station+opening.size.x/2,wall.thickness_m/2,
                      opening.pose.position.z+opening.size.z/2)
            if all(min(part_max[i],open_max[i])-max(part_min[i],open_min[i])>1e-9
                   for i in range(3)):
                raise ContractError(f"{component.id} conservatively overlaps opening void {opening.id}")


def _validate_opening_insert_components(assembly: BuildingAssembly, opening: Opening,
                                        wall: Wall, components: tuple[AssemblyComponent,...]) -> None:
    first,last,axis,left_normal,length=_wall_frame_points(wall)
    _validate_wall_openings(assembly,wall,(opening,),length,first.z)
    station=(opening.pose.position.x-first.x)*axis[0]+(opening.pose.position.y-first.y)*axis[1]
    sill=opening.pose.position.z-opening.size.z/2
    for component in components:
        for x,y,z in component_world_points(component):
            dx,dy=x-first.x,y-first.y
            local_station=dx*axis[0]+dy*axis[1]
            through=dx*left_normal[0]+dy*left_normal[1]
            if (local_station<station-opening.size.x/2-1e-9 or
                    local_station>station+opening.size.x/2+1e-9 or
                    abs(through)>wall.thickness_m/2+1e-9 or
                    z<sill-1e-9 or z>sill+opening.size.z+1e-9):
                raise ContractError(f"{component.id} insert geometry exceeds its exact opening void")


def _aabb(points):
    return tuple(min(point[i] for point in points) for i in range(3))+tuple(max(point[i] for point in points) for i in range(3))


def _bounds_overlap(first, second, tol=1e-9) -> bool:
    return all(first[i] < second[i+3]-tol and second[i] < first[i+3]-tol for i in range(3))


def _horizontal_host_polygon(owner: Any, label: str) -> tuple[float,tuple[tuple[float,float],...]]:
    points=owner.footprint.points
    elevations=tuple(point.z for point in points)
    if max(elevations)-min(elevations)>ASSEMBLY_GEOMETRY_TOLERANCE_M:
        raise ContractError(f"{label} footprint must be horizontal for the bounded ceiling proof")
    polygon=tuple((point.x,point.y) for point in points)
    try:
        extrude_simple_section_m(polygon,1.0,((0.0,0.0,0.0),(0.0,0.0,0.0,1.0)))
    except GeometryInputError as exc:
        raise ContractError(f"{label} footprint must be a strict simple polygon: {exc}") from exc
    _require_convex_footprint(polygon,label)
    return elevations[0],polygon


def _require_convex_footprint(polygon: tuple[tuple[float,float],...], label: str) -> None:
    """Validate convexity using dimensionless turns and local differences."""
    turns=[]
    for first,second,third in zip(polygon,polygon[1:]+polygon[:1],polygon[2:]+polygon[:2]):
        ux,uy=second[0]-first[0],second[1]-first[1]
        vx,vy=third[0]-second[0],third[1]-second[1]
        first_length,second_length=math.hypot(ux,uy),math.hypot(vx,vy)
        if (not math.isfinite(first_length) or not math.isfinite(second_length) or
                first_length==0.0 or second_length==0.0):
            raise ContractError(f"{label} footprint has a zero-length edge")
        normalized_turn=(ux/first_length)*(vy/second_length)-(uy/first_length)*(vx/second_length)
        if abs(normalized_turn)>1e-12:
            turns.append(math.copysign(1.0,normalized_turn))
    if not turns or min(turns)!=max(turns):
        raise ContractError(f"{label} footprint must be convex for conservative child bounds")


def _component_world_bounds(component: AssemblyComponent) -> tuple[float,float,float,float,float,float]:
    if isinstance(component.geometry,WireSweep):
        bounds=[]
        points=component.geometry.centerline.points
        for start,end in zip(points,points[1:]):
            mesh=straight_round_wire_m(start,end,component.geometry.diameter_m/2)
            bounds.append(mesh.world_bounds_m)
        return tuple(min(row[index] for row in bounds) if index<3 else
                     max(row[index] for row in bounds) for index in range(6))
    return _aabb(component_world_points(component))


def _component_xy_vertices_within(component: AssemblyComponent, polygon) -> bool:
    """Contain supported component vertices in a convex host footprint.

    These are actual vertices/extrema for boxes, panels and profile extrusions,
    and a conservative endpoint-radius envelope for wires. A convex footprint
    contains the component's convex hull when it contains every such point,
    avoiding the false geometry of substituting a world-axis AABB for a yawed
    child.
    """
    return all(_point_in_polygon_2d((point[0],point[1]),polygon)
               for point in component_world_points(component))


def _generated_deck_footprint(component: AssemblyComponent):
    """Return actual horizontal deck footprint plus bottom/top z faces.

    Generated Box3D decks may use any yaw but must keep their local z axis
    vertical. PanelGeometry decks must have a horizontal boundary and vertical
    normal. Sloped/tilted generated decks fail closed rather than being treated
    as the larger axis-aligned bounding box.
    """
    geometry=component.geometry
    points=component_world_points(component)
    if isinstance(geometry,Box3D):
        local_z=rotate_vector((0.0,0.0,1.0),geometry.pose.rotation)
        if (abs(local_z[0])>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                abs(local_z[1])>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                abs(abs(local_z[2])-1.0)>ASSEMBLY_GEOMETRY_TOLERANCE_M):
            raise ContractError(
                "generated deck_panel Box3D must be horizontal; tilted/sloped decks are unsupported"
            )
        center=geometry.pose.position
        hx,hy=geometry.size.x/2,geometry.size.y/2
        local_corners=((-hx,-hy,0.0),(hx,-hy,0.0),(hx,hy,0.0),(-hx,hy,0.0))
        polygon_points=[]
        for corner in local_corners:
            rotated=rotate_vector(corner,geometry.pose.rotation)
            polygon_points.append((center.x+rotated[0],center.y+rotated[1]))
        polygon=tuple(polygon_points)
    elif isinstance(geometry,PanelGeometry):
        normal=(geometry.normal.x,geometry.normal.y,geometry.normal.z)
        boundary_z=tuple(point.z for point in geometry.boundary.points)
        if (abs(normal[0])>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                abs(normal[1])>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                abs(abs(normal[2])-1.0)>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                max(boundary_z)-min(boundary_z)>ASSEMBLY_GEOMETRY_TOLERANCE_M):
            raise ContractError(
                "generated deck_panel PanelGeometry must be horizontal; tilted/sloped decks are unsupported"
            )
        polygon=tuple((point.x,point.y) for point in geometry.boundary.points)
    else:
        raise ContractError("generated deck_panel supports only horizontal Box3D or PanelGeometry")
    try:
        extrude_simple_section_m(polygon,1.0,((0.0,0.0,0.0),(0.0,0.0,0.0,1.0)))
    except GeometryInputError as exc:
        raise ContractError(f"generated deck footprint is invalid: {exc}") from exc
    _require_convex_footprint(polygon,"generated deck")
    return min(point[2] for point in points),max(point[2] for point in points),polygon


def _validate_ceiling_child_bounds(ceiling: Any, deck: Slab | None,
                                   owned: tuple[AssemblyComponent,...],
                                   deck_component: AssemblyComponent | None) -> None:
    ceiling_z,ceiling_polygon=_horizontal_host_polygon(ceiling,"Ceiling")
    lower_bound=ceiling_z-(ceiling.thickness_m or 0.0)
    deck_top,deck_polygon=(
        _horizontal_host_polygon(deck,"Slab deck") if deck is not None else (None,None)
    )
    if deck is not None:
        deck_bottom=deck_top-deck.thickness_m
    elif deck_component is not None:
        deck_bottom,deck_top,deck_polygon=_generated_deck_footprint(deck_component)
    else:
        raise ContractError("suspended ceiling requires a bounded existing or generated deck")
    if deck_bottom<=ceiling_z+ASSEMBLY_GEOMETRY_TOLERANCE_M:
        raise ContractError("selected deck underside must be above the ceiling host datum")
    for component in owned:
        bounds=_component_world_bounds(component)
        if not _component_xy_vertices_within(component,ceiling_polygon):
            raise ContractError(f"{component.id} geometry exceeds its Ceiling host footprint")
        if component.component_kind=="deck_panel":
            if deck is not None:
                raise ContractError("existing Slab deck cannot be duplicated by a deck_panel")
            if component.id!=deck_component.id or bounds[5]<=bounds[2]:
                raise ContractError("generated deck_panel must be the positive-volume selected deck identity")
            continue
        if not _component_xy_vertices_within(component,deck_polygon):
            label="selected Slab deck" if deck is not None else "generated deck"
            raise ContractError(f"{component.id} geometry exceeds its {label} footprint")
        if bounds[2]<lower_bound-ASSEMBLY_GEOMETRY_TOLERANCE_M or \
                bounds[5]>deck_bottom+ASSEMBLY_GEOMETRY_TOLERANCE_M:
            raise ContractError(f"{component.id} geometry exceeds the ceiling-host/deck vertical bounds")


def _preset_parameter_evidence_id(assembly: BuildingAssembly, parameter: ResolvedParameter) -> str:
    return json.dumps((assembly.catalog_revision,assembly.preset_id,
                       assembly.preset_revision,parameter.parameter_id),
                      separators=(",",":"),ensure_ascii=False)


def _selection_evidence_id(assembly: BuildingAssembly) -> str:
    return json.dumps((assembly.catalog_id,assembly.catalog_revision,
                       assembly.preset_id,assembly.preset_revision),
                      separators=(",",":"),ensure_ascii=False)


def _resolve_field(source: Any, path: str) -> Any:
    current=source
    for segment in path.split("."):
        if is_dataclass(current) and not isinstance(current,type):
            if segment not in {item.name for item in fields(current)}:
                raise ContractError(f"observed source path {path!r} does not exist")
            current=getattr(current,segment)
        elif isinstance(current,dict) and segment in current:
            current=current[segment]
        else:
            raise ContractError(f"observed source path {path!r} does not exist")
    return current


def _same_observation_source(first: Any, second: Any) -> bool:
    return (first.source_kind==second.source_kind and first.source_id==second.source_id and
            first.source_element_id==second.source_element_id and first.page==second.page and
            first.method==second.method and first.derivation=="observed" and
            second.derivation=="observed")


def _geometry_source_fields(source: Any) -> tuple[str,...]:
    """Canonical fields that define the source's geometry and placement."""
    if isinstance(source,Wall):
        return ("level_id","centerline","thickness_m","height_m")
    if isinstance(source,Opening):
        return ("host_id","opening_type","pose","size")
    if isinstance(source,Ceiling):
        fields=("level_id","footprint")
        return fields+("thickness_m",) if source.thickness_m is not None else fields
    if isinstance(source,Slab):
        return ("level_id","footprint","thickness_m")
    raise ContractError(f"unsupported source type for assembly geometry evidence: {type(source).__name__}")


def _validate_source_geometry_evidence(assembly: BuildingAssembly, source: Any,
                                       assignment_scopes: set[str], label: str) -> None:
    """Bind matching observed evidence and require it to cover source geometry.

    Scope-less provenance is the canonical entity-wide form. Otherwise, the
    union of matching, field-qualified source records must cover every source
    field used to establish this host/deck geometry. An unrelated scope such as
    ``name`` cannot certify a physical centerline or footprint merely because
    the assembly copied its source ID.
    """
    assignment_records=[record for record in assembly.provenance
                        if record.derivation=="observed" and
                        assignment_scopes.issubset(record.scope_paths or ())]
    matched=[]
    for source_record in source.provenance:
        if source_record.derivation!="observed":
            continue
        if any(_same_observation_source(source_record,record)
               for record in assignment_records):
            matched.append(source_record)
    if not matched:
        raise ContractError(
            f"{assembly.id} requires observed {label} provenance from its source"
        )
    if any(record.scope_paths is None for record in matched):
        return
    covered={field_path for record in matched for field_path in (record.scope_paths or ())}
    required=set(_geometry_source_fields(source))
    missing=required-covered
    if missing:
        raise ContractError(
            f"{assembly.id} observed {label} provenance does not cover geometry field(s): "
            f"{', '.join(sorted(missing))}"
        )


def _validate_assignment_evidence(model: Any, assembly: BuildingAssembly, host: Any,
                                  deck: Slab | None, parent_host: Any | None = None) -> None:
    selection_scopes={"catalog_id","catalog_revision","preset_id","preset_revision","selected_by"}
    selection_records=[record for record in assembly.provenance
                       if selection_scopes.issubset(record.scope_paths or ())]
    if not any(record.derivation=="user" and record.source_id==assembly.selected_by and
               record.source_element_id==_selection_evidence_id(assembly)
               for record in selection_records):
        raise ContractError(f"{assembly.id} selection provenance does not bind the selected immutable preset reference")

    _validate_source_geometry_evidence(
        assembly,host,{"host_id","input_digest"},"host-geometry"
    )

    if assembly.assembly_kind=="wall":
        for opening in (row for row in model.openings if row.host_id==assembly.host_id):
            _validate_source_geometry_evidence(
                assembly,opening,{"input_digest"},f"opening geometry ({opening.id})"
            )

    if parent_host is not None:
        _validate_source_geometry_evidence(
            assembly,parent_host,{"input_digest"},"parent-host geometry"
        )

    if deck is not None:
        _validate_source_geometry_evidence(
            assembly,deck,{"deck_id","input_digest"},"deck-geometry"
        )

    all_sources={row.id:row for collection in (
        model.levels,model.spaces,model.walls,model.slabs,model.ceilings,model.openings,
        model.electrical_devices,model.electrical_equipment,model.obstacles,
    ) for row in collection}
    if assembly.assembly_kind=="wall":
        allowed={assembly.host_id}|{row.id for row in model.openings if row.host_id==assembly.host_id}
    elif assembly.assembly_kind=="opening_insert":
        allowed={assembly.host_id,host.host_id}
    else:
        allowed={assembly.host_id}
        if deck is not None: allowed.add(deck.id)
    for parameter in assembly.resolved_parameters:
        if parameter.origin=="preset_default":
            expected_element=_preset_parameter_evidence_id(assembly,parameter)
            if not any(record.source_kind=="preset_parameter" and
                       record.source_id==assembly.catalog_id and
                       record.source_element_id==expected_element and
                       record.derivation=="inferred"
                       for record in parameter.provenance):
                raise ContractError(
                    f"{assembly.id}.{parameter.parameter_id} preset-default evidence does not bind "
                    "the selected catalog/preset revision and parameter"
                )
        elif parameter.origin=="plan_observed":
            if parameter.source_entity_id not in allowed:
                raise ContractError(f"{assembly.id}.{parameter.parameter_id} observed source must be its host/deck fact")
            source_entity=all_sources.get(parameter.source_entity_id)
            if source_entity is None:
                raise ContractError(f"{assembly.id}.{parameter.parameter_id} references a missing observed source entity")
            source_value=_resolve_field(source_entity,parameter.source_field_path)
            if isinstance(source_value,(tuple,list,dict)) or source_value!=parameter.value:
                raise ContractError(
                    f"{assembly.id}.{parameter.parameter_id} observed value does not equal its typed source field"
                )
            observed_rows=[record for record in source_entity.provenance if record.derivation=="observed"]
            if not any(source.source_kind==record.source_kind and
                       source.source_id==record.source_id and
                       source.source_element_id==record.source_element_id and
                       source.page==record.page and
                       (source.scope_paths is None or parameter.source_field_path in source.scope_paths)
                       for source in observed_rows for record in parameter.provenance
                       if record.derivation=="observed"):
                raise ContractError(
                    f"{assembly.id}.{parameter.parameter_id} provenance does not match its observed source field"
                )


def _validate_component_roles(assembly: BuildingAssembly,
                              components: tuple[AssemblyComponent,...]) -> None:
    allowed={
        "wall":{"stud","top_plate","bottom_plate","header","sill","cripple","drywall_panel"},
        "opening_insert":{"door_leaf","door_frame","window_frame","window_glazing"},
        "suspended_ceiling":{"tbar_main_runner","tbar_cross_runner","tile","hanger_wire","deck_panel"},
    }[assembly.assembly_kind]
    geometry_roles={
        "stud":(Box3D,ProfileExtrusion),"top_plate":(Box3D,ProfileExtrusion),
        "bottom_plate":(Box3D,ProfileExtrusion),"header":(Box3D,ProfileExtrusion),
        "sill":(Box3D,ProfileExtrusion),"cripple":(Box3D,ProfileExtrusion),
        "drywall_panel":(Box3D,PanelGeometry),
        "door_leaf":(Box3D,PanelGeometry),"door_frame":(Box3D,ProfileExtrusion),
        "window_frame":(Box3D,ProfileExtrusion),"window_glazing":(Box3D,PanelGeometry),
        "tbar_main_runner":(Box3D,ProfileExtrusion),
        "tbar_cross_runner":(Box3D,ProfileExtrusion),"tile":(Box3D,PanelGeometry),
        "hanger_wire":(WireSweep,),"deck_panel":(Box3D,PanelGeometry),
    }
    part_keys=[component.part_key for component in components]
    if len(part_keys)!=len(set(part_keys)):
        raise ContractError(f"{assembly.id} component part_key values must be unique")
    for component in components:
        if component.component_kind not in allowed:
            raise ContractError(
                f"{component.id} role {component.component_kind!r} is incompatible with "
                f"{assembly.assembly_kind} assembly"
            )
        if not isinstance(component.geometry,geometry_roles[component.component_kind]):
            raise ContractError(
                f"{component.id} geometry is incompatible with its {component.component_kind} role"
            )
        if component.id!=stable_assembly_component_id(assembly.id,component.part_key):
            raise ContractError(f"{component.id} is not the stable ID for its semantic part_key")
        if assembly.assembly_kind=="wall" and component.opening_id is not None and \
                component.component_kind not in {"header","sill","cripple"}:
            raise ContractError("only explicit opening-framing roles may name opening_id on a wall assignment")
        if assembly.assembly_kind=="suspended_ceiling" and component.opening_id is not None:
            raise ContractError("suspended-ceiling components cannot claim an opening_id")


def validate_building_assemblies(model: Any) -> None:
    assemblies=tuple(model.building_assemblies); components=tuple(model.assembly_components)
    if not assemblies and not components: return
    levels={row.id:row for row in model.levels}
    walls={row.id:row for row in model.walls}; ceilings={row.id:row for row in model.ceilings}
    openings={row.id:row for row in model.openings}; slabs={row.id:row for row in model.slabs}
    assembly_by_id={row.id:row for row in assemblies}; component_by_id={row.id:row for row in components}
    host_claims=set(); claimed_components=set()
    for assembly in assemblies:
        selected_deck = None
        if assembly.assembly_kind=="wall":
            host=walls.get(assembly.host_id)
            if host is None: raise ContractError(f"{assembly.id}.host_id must reference a Wall")
            key=("aggregate",assembly.host_id)
            level_id=host.level_id
        elif assembly.assembly_kind=="suspended_ceiling":
            host=ceilings.get(assembly.host_id)
            if host is None: raise ContractError(f"{assembly.id}.host_id must reference a Ceiling")
            key=("aggregate",assembly.host_id)
            level_id=host.level_id
        else:
            host=openings.get(assembly.host_id)
            if host is None: raise ContractError(f"{assembly.id}.host_id must reference an Opening")
            key=("opening_insert",assembly.host_id)
            parent=walls.get(host.host_id) or ceilings.get(host.host_id)
            if parent is None: raise ContractError("opening insert host must be on a wall or ceiling")
            level_id=parent.level_id
        if key in host_claims: raise ContractError("one active aggregate/insert assignment is allowed per host")
        host_claims.add(key)
        if assembly.level_id not in levels or assembly.level_id!=level_id:
            raise ContractError(f"{assembly.id}.level_id contradicts its source host level")
        if assembly.assembly_kind=="suspended_ceiling":
            selected_deck=slabs.get(assembly.deck_id)
        _validate_assignment_evidence(model,assembly,host,selected_deck,
            parent if assembly.assembly_kind=="opening_insert" else None)
        if len(assembly.component_ids)!=len(set(assembly.component_ids)):
            raise ContractError(f"{assembly.id} has duplicate component_ids")
        owned=[]
        for component_id in assembly.component_ids:
            component=component_by_id.get(component_id)
            if component is None: raise ContractError(f"{assembly.id} references missing component {component_id!r}")
            if component.assembly_id!=assembly.id or component.level_id!=assembly.level_id:
                raise ContractError("assembly component ownership or level contradicts its assignment")
            if component_id in claimed_components: raise ContractError("component must be owned by exactly one assembly")
            claimed_components.add(component_id); owned.append(component)
        if not owned: raise ContractError(f"{assembly.id} requires at least one physical component")
        _validate_component_roles(assembly,tuple(owned))
        for component in owned:
            if assembly.assembly_kind=="opening_insert":
                if component.opening_id!=assembly.host_id:
                    raise ContractError("opening insert component must name its single assigned opening")
                if (host.opening_type.casefold()=="door" and
                        component.component_kind not in {"door_leaf","door_frame"}):
                    raise ContractError("door opening insert owns only explicit door leaf/frame components")
                if (host.opening_type.casefold()=="window" and
                        component.component_kind not in {"window_frame","window_glazing"}):
                    raise ContractError("window opening insert owns only explicit window frame/glazing components")
                if host.opening_type.casefold() not in {"door","window"}:
                    raise ContractError("opening insert supports only explicit door or window opening types")
            elif component.opening_id is not None:
                target=openings.get(component.opening_id)
                if target is None or target.host_id!=assembly.host_id:
                    raise ContractError("wall assembly component opening_id must be hosted by its wall")
            if component.component_kind=="hanger_wire" and assembly.assembly_kind!="suspended_ceiling":
                raise ContractError("hanger attachments are gated to a suspended-ceiling assembly")
        if assembly.assembly_kind=="wall":
            length,base_z=_validate_wall_frame(assembly,host)
            _wall_stack_depth(assembly,host)
            host_openings=tuple(sorted((row for row in model.openings if row.host_id==host.id),key=lambda row:row.id))
            _validate_wall_openings(assembly,host,host_openings,length,base_z)
            _validate_wall_components(assembly,host,tuple(owned),length,base_z,host_openings)
        elif assembly.assembly_kind=="opening_insert":
            if not isinstance(parent,Wall):
                raise ContractError("opening insert geometry currently supports only wall-hosted rectangular apertures")
            _validate_opening_insert_components(assembly,host,parent,tuple(owned))
        elif assembly.assembly_kind=="suspended_ceiling":
            deck=slabs.get(assembly.deck_id)
            deck_component=component_by_id.get(assembly.deck_id)
            if deck is not None:
                if deck.level_id!=assembly.level_id or any(row.component_kind=="deck_panel" for row in owned):
                    raise ContractError("existing Slab deck must be reused exactly once, never duplicated as a component")
            elif deck_component is None or deck_component.assembly_id!=assembly.id or deck_component.component_kind!="deck_panel":
                raise ContractError("suspended ceiling deck_id must reference one existing Slab or owned deck_panel")
            elif sum(row.component_kind=="deck_panel" for row in owned)!=1:
                raise ContractError("generated suspended-ceiling deck must have one physical identity")
            _validate_ceiling_child_bounds(host,deck,tuple(owned),deck_component)
        expected_input=compute_assembly_input_digest(model,assembly)
        expected_output=compute_assembly_output_digest(tuple(owned))
        if assembly.input_digest!=expected_input:
            raise ContractError(f"{assembly.id} input_digest is stale for host facts/preset parameters")
        if assembly.output_digest!=expected_output:
            raise ContractError(f"{assembly.id} output_digest is stale for owned component geometry")
    if claimed_components!=set(component_by_id):
        raise ContractError("every assembly component must have exactly one owning assignment")
    # The 1.4 structural-member representation remains a separate physical identity.
    # Conservative AABB overlap is unresolved; do not infer equivalence or delete either.
    for component in components:
        component_bounds=_aabb(component_world_points(component))
        for member in model.structural_members:
            if _bounds_overlap(component_bounds,member.axis_aligned_bounds()):
                raise ContractError(f"member_overlap_unresolved: {component.id} overlaps unowned {member.id}")
    _validate_hanger_attachments(model,assembly_by_id,component_by_id,slabs)


def _validate_hanger_attachments(model: Any, assemblies: dict[str,BuildingAssembly],
                                 components: dict[str,AssemblyComponent], slabs: dict[str,Slab]) -> None:
    for component in components.values():
        if component.component_kind!="hanger_wire": continue
        attachment=component.hanger_attachment
        assembly=assemblies[component.assembly_id]
        if not isinstance(component.geometry,WireSweep):
            raise ContractError(f"{component.id} hanger wire requires straight-wire geometry")
        line=component.geometry.centerline.points
        if (_distance(line[0],attachment.top_anchor)>ASSEMBLY_GEOMETRY_TOLERANCE_M or
                _distance(line[-1],attachment.bottom_anchor)>ASSEMBLY_GEOMETRY_TOLERANCE_M):
            raise ContractError(f"{component.id} wire endpoints must match its typed top/bottom anchors")
        top_component=components.get(attachment.top_host_id)
        top_slab=slabs.get(attachment.top_host_id)
        if top_component is None and top_slab is None:
            raise ContractError(f"{component.id} hanger top_host_id must reference a physical component or Slab")
        if attachment.top_host_id!=assembly.deck_id:
            raise ContractError(f"{component.id} hanger must attach to its assembly's selected deck identity")
        if top_component is not None:
            if (top_component.assembly_id!=assembly.id or top_component.component_kind!="deck_panel" or
                    top_component.level_id!=assembly.level_id or
                    not point_on_component_surface(top_component,attachment.top_anchor)):
                raise ContractError(f"{component.id} top anchor is not on its named physical surface")
        else:
            if top_slab.level_id!=assembly.level_id or not point_on_slab_surface(top_slab,attachment.top_anchor):
                raise ContractError(f"{component.id} top anchor is not on its named Slab surface")
        bottom=components.get(attachment.bottom_runner_id)
        if (bottom is None or bottom.assembly_id!=assembly.id or
                bottom.component_kind not in {"tbar_main_runner","tbar_cross_runner"} or
                bottom.level_id!=assembly.level_id or
                not point_on_component_surface(bottom,attachment.bottom_anchor)):
            raise ContractError(f"{component.id} bottom anchor is not on its named physical runner surface")

