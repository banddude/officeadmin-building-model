"""Deterministic, explicitly inferred source-equipment placement proposals."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from oabm.model import (
    DERIVATION_INFERRED,
    DERIVATION_USER,
    Box3D,
    BuildingModel,
    ElectricalEquipment,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Vector3,
    stable_id,
)

from .router import route_between_ports


class PlacementError(ValueError):
    """The available canonical evidence cannot support a safe proposal."""


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    position: Point3
    wall_id: str
    space_id: str
    direction: Vector3
    score: float
    confidence: float
    reasons: tuple[str, ...]
    valid: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "position": {"x": self.position.x, "y": self.position.y, "z": self.position.z},
            "wall_id": self.wall_id,
            "space_id": self.space_id,
            "direction": {"x": self.direction.x, "y": self.direction.y, "z": self.direction.z},
            "score": self.score,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class EquipmentPlacementProposal:
    identity_key: str
    equipment_type: str
    name: str | None
    level_id: str
    selected: PlacementCandidate
    alternatives: tuple[PlacementCandidate, ...]


_SEMANTIC_WEIGHTS = (
    ("electrical", 100.0), ("utility", 85.0), ("service", 75.0),
    ("telecom", 70.0), ("it", 70.0), ("mechanical", 60.0),
    ("storage", 35.0), ("back of house", 30.0),
)


def propose_equipment_placement(
    model: BuildingModel,
    *,
    identity_key: str,
    equipment_type: str,
    name: str | None,
    level_id: str,
    served_device_ids: tuple[str, ...] = (),
    mounting_height_m: float = 1.5,
    working_depth_m: float = 0.9,
    working_width_m: float = 0.8,
) -> EquipmentPlacementProposal:
    """Propose a wall position; never claim it came from a drawing.

    The caller supplies equipment identity/type, not circuit membership. Space
    semantics dominate the score; load distance is a small tie-breaker. A room
    footprint and a host wall are required to check front working clearance.
    """

    if not identity_key.strip() or not equipment_type.strip():
        raise PlacementError("stable identity_key and equipment_type are required")
    if level_id not in {level.id for level in model.levels}:
        raise PlacementError("level_id must identify a canonical level")
    if any(not math.isfinite(value) or value <= 0 for value in (
        mounting_height_m, working_depth_m, working_width_m,
    )):
        raise PlacementError("mounting height and working clearance must be positive")
    devices = {device.id: device for device in model.electrical_devices}
    if len(served_device_ids) != len(set(served_device_ids)):
        raise PlacementError("served_device_ids contains duplicates")
    if any(device_id not in devices for device_id in served_device_ids):
        raise PlacementError("served_device_ids contains an unknown device")
    loads = tuple(devices[device_id].pose.position for device_id in sorted(served_device_ids))
    level = next(level for level in model.levels if level.id == level_id)
    spaces = tuple(sorted((space for space in model.spaces if space.level_id == level_id), key=lambda item: item.id))
    walls = tuple(sorted((wall for wall in model.walls if wall.level_id == level_id), key=lambda item: item.id))
    if not spaces or not walls:
        raise PlacementError("a level needs registered space footprints and host walls")

    candidates: list[PlacementCandidate] = []
    for wall in walls:
        for start, end in zip(wall.centerline.points, wall.centerline.points[1:]):
            length = math.hypot(end.x - start.x, end.y - start.y)
            if length < working_width_m + 0.2:
                continue
            tangent = ((end.x - start.x) / length, (end.y - start.y) / length)
            for fraction in (0.25, 0.5, 0.75):
                x = round(start.x + fraction * (end.x - start.x), 6)
                y = round(start.y + fraction * (end.y - start.y), 6)
                position = Point3(x=x, y=y, z=round(level.elevation_m + mounting_height_m, 6))
                for side in (-1, 1):
                    nx, ny = -side * tangent[1], side * tangent[0]
                    for space in spaces:
                        if not _inside_xy(x + nx * 0.15, y + ny * 0.15, space.footprint):
                            continue
                        reasons: list[str] = []
                        semantic = _semantic_score(space.usage or space.name or "")
                        reasons.append(f"space_semantics:{semantic:g}")
                        if loads:
                            mean_distance = math.fsum(
                                abs(x - point.x) + abs(y - point.y) + abs(position.z - point.z)
                                for point in loads
                            ) / len(loads)
                        else:
                            mean_distance = 0.0
                        reasons.append(f"mean_manhattan_load_distance_m:{mean_distance:.3f}")
                        conflicts = _clearance_conflicts(
                            model, wall.id, space.footprint, position, tangent,
                            (nx, ny), working_depth_m, working_width_m,
                        )
                        reasons.extend(conflicts)
                        score = round(semantic - min(mean_distance, 100.0) * 0.1, 6)
                        confidence = round(min(0.8, 0.25 + semantic / 200.0), 3)
                        candidates.append(PlacementCandidate(
                            position=position, wall_id=wall.id, space_id=space.id,
                            direction=Vector3(x=nx, y=ny, z=0.0), score=score,
                            confidence=confidence, reasons=tuple(reasons), valid=not conflicts,
                        ))
    candidates.sort(key=lambda candidate: (
        not candidate.valid, -candidate.score, candidate.wall_id,
        candidate.space_id, candidate.position.x, candidate.position.y,
        candidate.direction.x, candidate.direction.y,
    ))
    if not candidates or not candidates[0].valid:
        raise PlacementError("no wall candidate satisfies known working-clearance constraints")
    return EquipmentPlacementProposal(
        identity_key=identity_key, equipment_type=equipment_type, name=name,
        level_id=level_id, selected=candidates[0], alternatives=tuple(candidates[1:]),
    )


def apply_equipment_proposal(
    model: BuildingModel, proposal: EquipmentPlacementProposal,
) -> BuildingModel:
    """Add canonical inferred equipment and a source port, with no circuit facts."""

    equipment_id = stable_id("equipment", f"{model.model_id}:proposal:{proposal.identity_key}")
    port_id = stable_id("port", f"{equipment_id}:source")
    if any(item.id == equipment_id or (proposal.name and item.name == proposal.name)
           for item in model.electrical_equipment):
        raise PlacementError("equipment identity or name already exists; resolve ambiguity first")
    provenance = (Provenance(
        source_kind="placement-engine", source_id=model.model_id,
        source_element_id=proposal.identity_key, method="canonical-space-wall-score-v1",
        confidence=proposal.selected.confidence, derivation=DERIVATION_INFERRED,
        attributes={"source_location_observed": False},
    ),)
    equipment = ElectricalEquipment(
        id=equipment_id, name=proposal.name, equipment_type=proposal.equipment_type,
        pose=Pose(position=proposal.selected.position), level_id=proposal.level_id,
        space_id=proposal.selected.space_id, host_id=proposal.selected.wall_id,
        confidence=proposal.selected.confidence, provenance=provenance,
        attributes={"placement": {
            "status": "inferred", "identity_key": proposal.identity_key,
            "selected": proposal.selected.to_dict(),
            "alternatives": [item.to_dict() for item in proposal.alternatives],
        }},
    )
    port = Port(
        id=port_id, owner_id=equipment_id, domain="electrical", role="source",
        pose=equipment.pose, direction=proposal.selected.direction,
        confidence=equipment.confidence, provenance=provenance,
    )
    return replace(model, electrical_equipment=tuple(sorted(
        (*model.electrical_equipment, equipment), key=lambda item: item.id,
    )), ports=tuple(sorted((*model.ports, port), key=lambda item: item.id)))


def set_user_equipment_placement(
    model: BuildingModel, *, equipment_id: str, position: Point3,
    user_input_id: str, wall_id: str, space_id: str,
    direction: Vector3,
) -> BuildingModel:
    """Accept or move a proposal and recalculate its direct dependent routes."""

    if not user_input_id.strip():
        raise PlacementError("user_input_id is required for an authoritative placement")
    equipment = next((item for item in model.electrical_equipment if item.id == equipment_id), None)
    if equipment is None or "placement" not in equipment.attributes:
        raise PlacementError("equipment_id must identify a proposed equipment placement")
    prior = equipment.attributes["placement"]
    if any(item.get("user_input_id") == user_input_id for item in prior.get("history", [])):
        raise PlacementError("historical user_input_id cannot be reused for a new placement")
    if prior.get("user_input_id") == user_input_id:
        directions = tuple(port.direction for port in model.ports if port.owner_id == equipment_id)
        if (position == equipment.pose.position and wall_id == equipment.host_id
                and space_id == equipment.space_id
                and directions and all(item == direction for item in directions)):
            return model
        raise PlacementError("one user_input_id cannot describe conflicting placements")
    if any(item.id != equipment_id and (
        item.attributes.get("placement", {}).get("user_input_id") == user_input_id
        or any(row.get("user_input_id") == user_input_id
               for row in item.attributes.get("placement", {}).get("history", []))
    ) for item in model.electrical_equipment):
        raise PlacementError("user_input_id already belongs to another equipment placement")
    wall = next((item for item in model.walls if item.id == wall_id), None)
    if wall is None or wall.level_id != equipment.level_id:
        raise PlacementError("wall_id must identify a canonical wall")
    space = next((item for item in model.spaces if item.id == space_id), None)
    if (space is None or space.level_id != equipment.level_id
            or not _inside_xy(position.x, position.y, space.footprint, tolerance=0.15)):
        raise PlacementError("user placement must be in the selected canonical space")
    direction_length = math.hypot(direction.x, direction.y)
    if direction_length < 1e-9:
        raise PlacementError("placement direction must have a horizontal working side")
    if min(_point_segment_distance(position.x, position.y, a.x, a.y, b.x, b.y)
           for a, b in zip(wall.centerline.points, wall.centerline.points[1:])) > 0.15:
        raise PlacementError("user placement must remain on its stated host wall")
    normal = (direction.x / direction_length, direction.y / direction_length)
    conflicts = _clearance_conflicts(
        model, wall_id, space.footprint, position, (-normal[1], normal[0]),
        normal, 0.9, 0.8,
    )
    if conflicts:
        raise PlacementError(f"user placement has known clearance conflicts: {', '.join(conflicts)}")
    if any(
        item.id != equipment_id and item.name and item.name == equipment.name
        for item in model.electrical_equipment
    ):
        raise PlacementError("duplicate equipment name remains ambiguous")
    status = "user-confirmed" if position == equipment.pose.position else "user-moved"
    provenance = (Provenance(
        source_kind="user-placement", source_id=model.model_id,
        source_element_id=user_input_id, method=status,
        confidence=1.0, derivation=DERIVATION_USER,
        attributes={"source_location_observed": False},
    ),)
    updated = replace(
        equipment, pose=Pose(position=position), space_id=space_id, host_id=wall_id,
        confidence=1.0, provenance=provenance,
        attributes={**equipment.attributes, "placement": {
            **prior, "status": status, "prior_proposal": prior["selected"],
            "history": [*prior.get("history", []), {
                "status": prior["status"],
                "position": {"x": equipment.pose.position.x,
                             "y": equipment.pose.position.y,
                             "z": equipment.pose.position.z},
                "user_input_id": prior.get("user_input_id"),
            }],
            "user_input_id": user_input_id,
        }},
    )
    owned_ports = tuple(port for port in model.ports if port.owner_id == equipment_id)
    updated_ports = tuple(
        replace(port, pose=Pose(position=position), direction=direction,
                provenance=provenance, confidence=1.0)
        if port.owner_id == equipment_id else port
        for port in model.ports
    )
    changed_port_ids = {port.id for port in owned_ports}
    designed_circuit_route_ids = {
        route_id for circuit in model.circuits
        if circuit.source_port_id in changed_port_ids
        for route_id in circuit.route_ids
    }
    dependent = tuple(route for route in model.routes if (
        route.start_port_id in changed_port_ids
        or route.end_port_id in changed_port_ids
        or route.id in designed_circuit_route_ids
    ))
    dependent_ids = {route.id for route in dependent}
    base = replace(
        model,
        electrical_equipment=tuple(updated if item.id == equipment_id else item
                                   for item in model.electrical_equipment),
        ports=updated_ports,
        routes=tuple(route for route in model.routes if route.id not in dependent_ids),
        route_fittings=tuple(item for item in model.route_fittings
                             if item.route_id not in dependent_ids),
        circuits=tuple(replace(circuit, route_ids=tuple(
            route_id for route_id in circuit.route_ids if route_id not in dependent_ids
        )) for circuit in model.circuits),
        conductors=tuple(replace(conductor, route_ids=tuple(
            route_id for route_id in conductor.route_ids if route_id not in dependent_ids
        )) for conductor in model.conductors),
    )
    replacements = []
    fittings = []
    for old in sorted(dependent, key=lambda item: item.id):
        route, new_fittings = route_between_ports(
            base, old.start_port_id, old.end_port_id, old.route_type,
            nominal_diameter_m=old.nominal_diameter_m,
        )
        design_provenance = tuple(item for item in old.provenance if item.source_kind not in {
            "router", "placement-engine", "user-placement",
        })
        route = replace(
            route,
            provenance=tuple((*route.provenance, *updated.provenance, *design_provenance)),
            attributes={**old.attributes, **route.attributes},
        )
        replacements.append(route)
        fittings.extend(replace(
            fitting, provenance=route.provenance,
            attributes={**fitting.attributes, "design_status": "designed",
                        "equipment_id": equipment_id},
        ) for fitting in new_fittings)
    return replace(
        base,
        routes=tuple(sorted((*base.routes, *replacements), key=lambda item: item.id)),
        route_fittings=tuple(sorted((*base.route_fittings, *fittings), key=lambda item: item.id)),
        circuits=model.circuits, conductors=model.conductors,
    )


def _semantic_score(value: str) -> float:
    normalized = value.lower().replace("_", " ").replace("-", " ")
    tokens = set(normalized.split())
    for token, score in _SEMANTIC_WEIGHTS:
        if (token in normalized if " " in token else token in tokens):
            return score
    return 15.0


def _inside_xy(x: float, y: float, polygon: Polygon3D, *, tolerance: float = 0.0) -> bool:
    points = polygon.points
    if tolerance and any(_point_segment_distance(x, y, a.x, a.y, b.x, b.y) <= tolerance
                         for a, b in zip(points, (*points[1:], points[0]))):
        return True
    inside = False
    for a, b in zip(points, (*points[1:], points[0])):
        if (a.y > y) != (b.y > y) and x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x:
            inside = not inside
    return inside


def _point_segment_distance(x: float, y: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy))


def _clearance_conflicts(
    model: BuildingModel, wall_id: str, space: Polygon3D, position: Point3,
    tangent: tuple[float, float], normal: tuple[float, float], depth: float, width: float,
) -> list[str]:
    def clearance_point(distance: float, lateral: float) -> tuple[float, float]:
        return (
            position.x + normal[0] * distance + tangent[0] * lateral,
            position.y + normal[1] * distance + tangent[1] * lateral,
        )

    corners = (
        clearance_point(0.1, -width / 2), clearance_point(0.1, width / 2),
        clearance_point(depth, width / 2), clearance_point(depth, -width / 2),
    )
    boundary = tuple((point.x, point.y) for point in space.points)
    conflicts: list[str] = []
    if (any(not _inside_xy(x, y, space) for x, y in corners)
            or _polygons_edges_intersect(corners, boundary)):
        conflicts.append("working_clearance_outside_space")
    for opening in model.openings:
        if opening.host_id != wall_id:
            continue
        radius = width / 2 + max(opening.size.x, opening.size.y) / 2 + 0.1
        if math.hypot(position.x - opening.pose.position.x,
                      position.y - opening.pose.position.y) < radius:
            conflicts.append(f"opening:{opening.id}")
    for obstacle in model.obstacles:
        if obstacle.obstacle_type.lower() == "soft":
            continue
        if _geometry_intersects_clearance(obstacle.geometry, corners,
                                          obstacle.clearance_m):
            conflicts.append(f"obstacle:{obstacle.id}")
    for constraint in model.route_constraints:
        if constraint.hard and constraint.constraint_type.lower().replace("_", "-") in {
            "keep-out", "keepout", "no-go", "nogo", "forbidden",
        } and _geometry_intersects_clearance(constraint.geometry, corners,
                                              constraint.clearance_m):
            conflicts.append(f"keep_out:{constraint.id}")
    return conflicts


def _geometry_intersects_clearance(
    geometry: Box3D | Polygon3D | Polyline3D,
    corners: tuple[tuple[float, float], ...], clearance: float,
) -> bool:
    if isinstance(geometry, Box3D):
        # This disk encloses even a rotated box, so known conflicts cannot be
        # missed because a corner falls between a finite set of sample points.
        radius = math.hypot(geometry.size.x, geometry.size.y) / 2 + clearance
        x, y = geometry.pose.position.x, geometry.pose.position.y
        return (_inside_polygon_xy(x, y, corners)
                or any(_point_segment_distance(x, y, *a, *b) <= radius
                       for a, b in zip(corners, (*corners[1:], corners[0]))))
    if isinstance(geometry, Polygon3D):
        outline = tuple((point.x, point.y) for point in geometry.points)
        return (any(_inside_polygon_xy(x, y, corners) for x, y in outline)
                or any(_inside_xy(x, y, geometry, tolerance=clearance) for x, y in corners)
                or _polygons_edges_intersect(corners, outline))
    outline = tuple((point.x, point.y) for point in geometry.points)
    return (any(_inside_polygon_xy(x, y, corners) for x, y in outline)
            or _polygons_edges_intersect(corners, outline, closed_second=False)
            or any(_point_segment_distance(x, y, *a, *b) <= clearance
                   for x, y in corners for a, b in zip(outline, outline[1:])))


def _inside_polygon_xy(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    for (ax, ay), (bx, by) in zip(points, (*points[1:], points[0])):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _polygons_edges_intersect(
    first: tuple[tuple[float, float], ...],
    second: tuple[tuple[float, float], ...], *, closed_second: bool = True,
) -> bool:
    first_edges = tuple(zip(first, (*first[1:], first[0])))
    second_edges = tuple(zip(second, (*second[1:], second[0]))) if closed_second else tuple(zip(second, second[1:]))
    return any(_segments_intersect(a, b, c, d)
               for a, b in first_edges for c, d in second_edges)


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float],
    c: tuple[float, float], d: tuple[float, float],
) -> bool:
    def cross(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    ab_c, ab_d = cross(a, b, c), cross(a, b, d)
    cd_a, cd_b = cross(c, d, a), cross(c, d, b)
    return ((ab_c > 1e-9 and ab_d < -1e-9 or ab_c < -1e-9 and ab_d > 1e-9)
            and (cd_a > 1e-9 and cd_b < -1e-9 or cd_a < -1e-9 and cd_b > 1e-9))
