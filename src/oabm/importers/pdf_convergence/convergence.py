from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from oabm.model import (
    BuildingModel,
    Ceiling,
    ElectricalDevice,
    ElectricalEquipment,
    Level,
    Point3,
    Polygon3D,
    Port,
    Pose,
    Provenance,
    Slab,
    Space,
    Wall,
    stable_id,
)


class PdfConvergenceError(ValueError):
    """Raised when PDF lane outputs cannot be combined without inventing geometry."""


@dataclass(frozen=True, slots=True)
class PdfConvergenceOptions:
    level_elevation_tolerance_m: float = 0.25
    max_wall_host_distance_m: float = 0.50
    wall_host_ambiguity_tolerance_m: float = 0.05
    vertical_conflict_tolerance_m: float = 0.15
    boundary_tolerance_m: float = 1e-6

    def __post_init__(self) -> None:
        values = (
            self.level_elevation_tolerance_m,
            self.max_wall_host_distance_m,
            self.wall_host_ambiguity_tolerance_m,
            self.vertical_conflict_tolerance_m,
            self.boundary_tolerance_m,
        )
        if not all(math.isfinite(value) for value in values):
            raise PdfConvergenceError("convergence tolerances must be finite")
        if self.level_elevation_tolerance_m < 0:
            raise PdfConvergenceError("level_elevation_tolerance_m must be >= 0")
        if self.max_wall_host_distance_m <= 0:
            raise PdfConvergenceError("max_wall_host_distance_m must be > 0")
        if self.wall_host_ambiguity_tolerance_m < 0:
            raise PdfConvergenceError("wall_host_ambiguity_tolerance_m must be >= 0")
        if self.vertical_conflict_tolerance_m < 0:
            raise PdfConvergenceError("vertical_conflict_tolerance_m must be >= 0")
        if self.boundary_tolerance_m < 0:
            raise PdfConvergenceError("boundary_tolerance_m must be >= 0")


ElectricalEntity = ElectricalEquipment | ElectricalDevice
PhysicalHost = Wall | Slab | Ceiling


def _point_segment_distance_xy(
    point: Point3,
    start: Point3,
    end: Point3,
) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return math.hypot(point.x - start.x, point.y - start.y)
    t = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    px = start.x + t * dx
    py = start.y + t * dy
    return math.hypot(point.x - px, point.y - py)


def _polyline_distance_xy(point: Point3, wall: Wall) -> float:
    return min(
        _point_segment_distance_xy(point, start, end)
        for start, end in zip(wall.centerline.points, wall.centerline.points[1:])
    )


def _point_in_polygon_xy(
    point: Point3,
    polygon: Polygon3D,
    *,
    tolerance: float,
) -> bool:
    points = polygon.points
    for start, end in zip(points, (*points[1:], points[0])):
        if _point_segment_distance_xy(point, start, end) <= tolerance:
            return True

    inside = False
    for start, end in zip(points, (*points[1:], points[0])):
        if (start.y > point.y) == (end.y > point.y):
            continue
        intersection_x = (
            (end.x - start.x) * (point.y - start.y) / (end.y - start.y) + start.x
        )
        if point.x < intersection_x:
            inside = not inside
    return inside


def _polygon_plane_z(polygon: Polygon3D) -> float:
    return sum(point.z for point in polygon.points) / len(polygon.points)


def _ambiguity(
    rows: list[dict[str, Any]],
    *,
    entity: ElectricalEntity,
    code: str,
    detail: str,
    **evidence: Any,
) -> None:
    row: dict[str, Any] = {
        "entity_id": entity.id,
        "entity_name": entity.name,
        "code": code,
        "detail": detail,
    }
    page = entity.attributes.get("pdf_electrical", {}).get("source_page")
    if isinstance(page, int):
        row["page"] = page
    row.update(evidence)
    rows.append(row)


def _resolve_level(
    entity: ElectricalEntity,
    architecture: BuildingModel,
    *,
    options: PdfConvergenceOptions,
    ambiguities: list[dict[str, Any]],
) -> tuple[Level | None, str | None]:
    levels_by_id = {level.id: level for level in architecture.levels}
    if not levels_by_id:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="level_unresolved",
            detail="architectural model contains no level that can host the electrical object",
        )
        return None, None

    if len(levels_by_id) == 1:
        return next(iter(levels_by_id.values())), "single_architectural_level"

    point = entity.pose.position
    z_matches = sorted(
        (
            level
            for level in architecture.levels
            if abs(point.z - level.elevation_m) <= options.level_elevation_tolerance_m
        ),
        key=lambda item: item.id,
    )
    xy_level_ids = sorted(
        {
            space.level_id
            for space in architecture.spaces
            if _point_in_polygon_xy(
                point,
                space.footprint,
                tolerance=options.boundary_tolerance_m,
            )
        }
    )

    if len(z_matches) == 1 and len(xy_level_ids) == 1 and z_matches[0].id != xy_level_ids[0]:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="level_evidence_conflict",
            detail="registered Z and architectural plan containment point to different levels",
            z_level_id=z_matches[0].id,
            plan_level_id=xy_level_ids[0],
        )
        return None, None

    if len(z_matches) == 1:
        return z_matches[0], "registered_z_matches_level_elevation"
    if len(z_matches) > 1:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="level_ambiguous",
            detail="registered Z matches more than one architectural level",
            candidate_level_ids=[item.id for item in z_matches],
        )
        return None, None

    if len(xy_level_ids) == 1:
        return levels_by_id[xy_level_ids[0]], "unique_plan_containment_level"
    if len(xy_level_ids) > 1:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="level_ambiguous",
            detail="registered plan position lies within spaces on more than one level",
            candidate_level_ids=xy_level_ids,
        )
        return None, None

    _ambiguity(
        ambiguities,
        entity=entity,
        code="level_unresolved",
        detail="registered position does not resolve to a unique architectural level",
    )
    return None, None


def _resolve_space(
    entity: ElectricalEntity,
    architecture: BuildingModel,
    level: Level | None,
    *,
    options: PdfConvergenceOptions,
    ambiguities: list[dict[str, Any]],
) -> tuple[Space | None, str | None]:
    if level is None:
        return None, None

    matches = sorted(
        (
            space
            for space in architecture.spaces
            if space.level_id == level.id
            and _point_in_polygon_xy(
                entity.pose.position,
                space.footprint,
                tolerance=options.boundary_tolerance_m,
            )
        ),
        key=lambda item: item.id,
    )
    if len(matches) == 1:
        return matches[0], "unique_space_plan_containment"
    if len(matches) > 1:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="space_ambiguous",
            detail="registered plan position lies within more than one architectural space",
            candidate_space_ids=[item.id for item in matches],
        )
        return None, None

    _ambiguity(
        ambiguities,
        entity=entity,
        code="space_unresolved",
        detail="registered plan position is not contained by an architectural space on the resolved level",
    )
    return None, None


def _resolve_wall_host(
    entity: ElectricalEntity,
    architecture: BuildingModel,
    level: Level,
    *,
    options: PdfConvergenceOptions,
    ambiguities: list[dict[str, Any]],
) -> tuple[Wall | None, float | None]:
    candidates = sorted(
        (
            (_polyline_distance_xy(entity.pose.position, wall), wall)
            for wall in architecture.walls
            if wall.level_id == level.id
        ),
        key=lambda item: (item[0], item[1].id),
    )
    candidates = [
        item for item in candidates if item[0] <= options.max_wall_host_distance_m
    ]
    if not candidates:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="wall_host_unresolved",
            detail="wall-mounted electrical object is not close enough to a wall on the resolved level",
            max_distance_m=options.max_wall_host_distance_m,
        )
        return None, None

    if (
        len(candidates) > 1
        and candidates[1][0] - candidates[0][0]
        <= options.wall_host_ambiguity_tolerance_m
    ):
        _ambiguity(
            ambiguities,
            entity=entity,
            code="wall_host_ambiguous",
            detail="more than one architectural wall is equally plausible for the wall-mounted object",
            candidate_hosts=[
                {"host_id": wall.id, "distance_m": distance}
                for distance, wall in candidates
                if distance - candidates[0][0]
                <= options.wall_host_ambiguity_tolerance_m
            ],
        )
        return None, None

    return candidates[0][1], candidates[0][0]


def _resolve_polygon_host(
    entity: ElectricalEntity,
    candidates: tuple[Slab, ...] | tuple[Ceiling, ...],
    *,
    host_kind: str,
    options: PdfConvergenceOptions,
    ambiguities: list[dict[str, Any]],
) -> PhysicalHost | None:
    matches = sorted(
        (
            host
            for host in candidates
            if _point_in_polygon_xy(
                entity.pose.position,
                host.footprint,
                tolerance=options.boundary_tolerance_m,
            )
        ),
        key=lambda item: item.id,
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _ambiguity(
            ambiguities,
            entity=entity,
            code=f"{host_kind}_host_ambiguous",
            detail=f"registered plan position lies within more than one architectural {host_kind} host",
            candidate_host_ids=[item.id for item in matches],
        )
        return None
    _ambiguity(
        ambiguities,
        entity=entity,
        code=f"{host_kind}_host_unresolved",
        detail=f"registered plan position is not contained by an architectural {host_kind} on the resolved level",
    )
    return None


def _resolve_entity(
    entity: ElectricalEntity,
    architecture: BuildingModel,
    *,
    options: PdfConvergenceOptions,
    convergence_source_id: str,
    ambiguities: list[dict[str, Any]],
) -> ElectricalEntity:
    before_ambiguity_count = len(ambiguities)
    lane = entity.attributes.get("pdf_electrical", {})
    if not isinstance(lane, Mapping):
        raise PdfConvergenceError(
            f"{entity.id} has invalid pdf_electrical attributes"
        )

    level, level_method = _resolve_level(
        entity,
        architecture,
        options=options,
        ambiguities=ambiguities,
    )
    space, space_method = _resolve_space(
        entity,
        architecture,
        level,
        options=options,
        ambiguities=ambiguities,
    )

    position = entity.pose.position
    new_position = Point3(x=position.x, y=position.y, z=position.z)
    vertical_status = "source_page_plane_unresolved"
    mounting_height = lane.get("mounting_height_m")
    mounting_candidates = lane.get("mounting_height_candidates_m")
    mounting_applied = False

    if isinstance(mounting_candidates, list) and len(mounting_candidates) > 1:
        _ambiguity(
            ambiguities,
            entity=entity,
            code="mounting_height_ambiguous",
            detail="electrical source contains more than one mounting-height candidate",
            candidates_m=sorted(float(value) for value in mounting_candidates),
        )
    elif isinstance(mounting_height, (int, float)) and not isinstance(mounting_height, bool):
        if level is None:
            _ambiguity(
                ambiguities,
                entity=entity,
                code="mounting_height_without_level",
                detail="mounting height cannot be applied until a unique architectural level is resolved",
                mounting_height_m=float(mounting_height),
            )
        else:
            new_position = Point3(
                x=position.x,
                y=position.y,
                z=level.elevation_m + float(mounting_height),
            )
            vertical_status = "mounting_height_applied"
            mounting_applied = True

    host: PhysicalHost | None = None
    host_distance_m: float | None = None
    host_hint = lane.get("host_hint")
    host_candidates = lane.get("host_candidates")
    host_status = lane.get("host_status")

    if host_status == "ambiguous" or (
        isinstance(host_candidates, list) and len(host_candidates) > 1
    ):
        _ambiguity(
            ambiguities,
            entity=entity,
            code="host_hint_ambiguous",
            detail="electrical source contains more than one physical-host hint",
            host_candidates=sorted(str(value) for value in (host_candidates or [])),
        )
    elif isinstance(host_hint, str):
        if level is None:
            _ambiguity(
                ambiguities,
                entity=entity,
                code="host_without_level",
                detail="physical host cannot be resolved until a unique architectural level is resolved",
                host_hint=host_hint,
            )
        elif host_hint == "wall":
            host, host_distance_m = _resolve_wall_host(
                entity,
                architecture,
                level,
                options=options,
                ambiguities=ambiguities,
            )
        elif host_hint == "ceiling":
            host = _resolve_polygon_host(
                entity,
                tuple(
                    item for item in architecture.ceilings if item.level_id == level.id
                ),
                host_kind="ceiling",
                options=options,
                ambiguities=ambiguities,
            )
        elif host_hint == "floor":
            host = _resolve_polygon_host(
                entity,
                tuple(item for item in architecture.slabs if item.level_id == level.id),
                host_kind="floor",
                options=options,
                ambiguities=ambiguities,
            )
        else:
            _ambiguity(
                ambiguities,
                entity=entity,
                code="unsupported_host_hint",
                detail="electrical host hint has no corresponding architectural host type in this gate",
                host_hint=host_hint,
            )

    if isinstance(host, (Ceiling, Slab)):
        host_z = _polygon_plane_z(host.footprint)
        if mounting_applied:
            if abs(new_position.z - host_z) > options.vertical_conflict_tolerance_m:
                _ambiguity(
                    ambiguities,
                    entity=entity,
                    code="host_vertical_conflict",
                    detail="explicit mounting height conflicts with the resolved architectural host plane",
                    mounting_z_m=new_position.z,
                    host_plane_z_m=host_z,
                    tolerance_m=options.vertical_conflict_tolerance_m,
                )
                host = None
            else:
                new_position = Point3(
                    x=new_position.x,
                    y=new_position.y,
                    z=host_z,
                )
                vertical_status = "mounting_height_consistent_with_host_plane"
        else:
            new_position = Point3(
                x=new_position.x,
                y=new_position.y,
                z=host_z,
            )
            vertical_status = "architectural_host_plane_applied"

    target_confidences = [
        item.confidence for item in (level, space, host) if item is not None
    ]
    spatial_confidence = min(target_confidences) if target_confidences else None
    confidence = (
        min(entity.confidence, spatial_confidence)
        if spatial_confidence is not None
        else entity.confidence
    )

    entity_ambiguities = ambiguities[before_ambiguity_count:]
    status = "hosted" if host is not None else ("located" if level is not None else "unresolved")
    convergence_attributes: dict[str, Any] = {
        "status": status,
        "vertical_status": vertical_status,
        "source_pose_m": {
            "x": position.x,
            "y": position.y,
            "z": position.z,
        },
    }
    if level_method is not None:
        convergence_attributes["level_resolution_method"] = level_method
    if space_method is not None:
        convergence_attributes["space_resolution_method"] = space_method
    if mounting_applied:
        convergence_attributes["mounting_height_applied_m"] = float(mounting_height)
    if host_distance_m is not None:
        convergence_attributes["host_distance_m"] = host_distance_m
    if spatial_confidence is not None:
        convergence_attributes["spatial_confidence"] = spatial_confidence
    if entity_ambiguities:
        convergence_attributes["ambiguity_codes"] = sorted(
            {str(item["code"]) for item in entity_ambiguities}
        )

    attributes = copy.deepcopy(entity.attributes)
    if "pdf_convergence" in attributes:
        raise PdfConvergenceError(
            f"{entity.id} already contains pdf_convergence attributes"
        )
    attributes["pdf_convergence"] = convergence_attributes

    provenance = entity.provenance
    if level is not None or space is not None or host is not None or new_position != position:
        provenance = provenance + (
            Provenance(
                source_kind="pdf-convergence",
                source_id=convergence_source_id,
                source_element_id=entity.id,
                page=(
                    int(lane["source_page"])
                    if isinstance(lane.get("source_page"), int)
                    else None
                ),
                method="registered electrical plan position resolved against architectural geometry",
                confidence=spatial_confidence if spatial_confidence is not None else entity.confidence,
                attributes={
                    "level_id": level.id if level is not None else None,
                    "space_id": space.id if space is not None else None,
                    "host_id": host.id if host is not None else None,
                },
            ),
        )

    return replace(
        entity,
        pose=Pose(position=new_position, rotation=entity.pose.rotation),
        level_id=level.id if level is not None else None,
        space_id=space.id if space is not None else None,
        host_id=host.id if host is not None else None,
        confidence=confidence,
        provenance=provenance,
        attributes=attributes,
    )


def _translate_port(
    port: Port,
    *,
    before_owner: ElectricalEntity,
    after_owner: ElectricalEntity,
    convergence_source_id: str,
) -> Port:
    before = before_owner.pose.position
    after = after_owner.pose.position
    dx = after.x - before.x
    dy = after.y - before.y
    dz = after.z - before.z
    translated = Point3(
        x=port.pose.position.x + dx,
        y=port.pose.position.y + dy,
        z=port.pose.position.z + dz,
    )

    owner_lane = after_owner.attributes["pdf_convergence"]
    attributes = copy.deepcopy(port.attributes)
    if "pdf_convergence" in attributes:
        raise PdfConvergenceError(
            f"{port.id} already contains pdf_convergence attributes"
        )
    attributes["pdf_convergence"] = {
        "owner_spatial_status": owner_lane["status"],
        "translation_m": {"x": dx, "y": dy, "z": dz},
    }

    provenance = port.provenance + (
        Provenance(
            source_kind="pdf-convergence",
            source_id=convergence_source_id,
            source_element_id=port.id,
            page=(
                int(after_owner.attributes["pdf_electrical"]["source_page"])
                if isinstance(
                    after_owner.attributes["pdf_electrical"].get("source_page"),
                    int,
                )
                else None
            ),
            method="port translated with spatially resolved electrical owner",
            confidence=after_owner.confidence,
            attributes={"owner_id": port.owner_id},
        ),
    )
    return replace(
        port,
        pose=Pose(position=translated, rotation=port.pose.rotation),
        confidence=min(port.confidence, after_owner.confidence),
        provenance=provenance,
        attributes=attributes,
    )


def _validate_lane_models(
    architecture: BuildingModel,
    electrical: BuildingModel,
) -> None:
    if "pdf_architecture" not in architecture.attributes:
        raise PdfConvergenceError(
            "architecture input must be the canonical output of the PDF architecture lane"
        )
    if "pdf_electrical" not in electrical.attributes:
        raise PdfConvergenceError(
            "electrical input must be the canonical output of the PDF electrical lane"
        )

    if any(
        (
            architecture.electrical_equipment,
            architecture.electrical_devices,
            architecture.ports,
            architecture.circuits,
            architecture.conductors,
            architecture.routes,
            architecture.route_fittings,
        )
    ):
        raise PdfConvergenceError(
            "architecture input contains downstream/electrical entities; Gate D expects the pure PDF architecture lane output"
        )
    if any(
        (
            electrical.levels,
            electrical.spaces,
            electrical.walls,
            electrical.slabs,
            electrical.ceilings,
            electrical.openings,
            electrical.obstacles,
            electrical.route_constraints,
            electrical.routes,
            electrical.route_fittings,
        )
    ):
        raise PdfConvergenceError(
            "electrical input contains building/downstream entities; Gate D expects the pure PDF electrical lane output"
        )

    lane = electrical.attributes["pdf_electrical"]
    if not isinstance(lane, Mapping):
        raise PdfConvergenceError("electrical pdf lane attributes must be an object")
    if lane.get("registration_pending") is not False:
        raise PdfConvergenceError(
            "electrical PDF geometry must be explicitly registered to the architectural canonical frame before convergence"
        )
    if lane.get("spatial_status") != "registered-to-canonical-frame":
        raise PdfConvergenceError(
            "electrical PDF model is not marked as registered to a canonical frame"
        )
    if electrical.coordinate_system.frame_id != architecture.coordinate_system.frame_id:
        raise PdfConvergenceError(
            "architectural and electrical PDF models use different canonical coordinate frames"
        )
    if lane.get("registered_frame_id") != architecture.coordinate_system.frame_id:
        raise PdfConvergenceError(
            "electrical page transforms do not target the architectural canonical frame"
        )

    electrical_owner_ids = {
        item.id for item in (*electrical.electrical_equipment, *electrical.electrical_devices)
    }
    foreign_ports = sorted(
        port.id for port in electrical.ports if port.owner_id not in electrical_owner_ids
    )
    if foreign_ports:
        raise PdfConvergenceError(
            "electrical PDF lane contains ports not owned by recognized electrical objects: "
            + ", ".join(foreign_ports)
        )


def _merged_attributes(
    architecture: BuildingModel,
    electrical: BuildingModel,
    *,
    convergence: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(architecture.attributes)
    for key, value in electrical.attributes.items():
        if key in result and result[key] != value:
            raise PdfConvergenceError(
                f"lane attribute collision at {key!r}; refusing to choose one source silently"
            )
        result[key] = copy.deepcopy(value)
    if "pdf_convergence" in result:
        raise PdfConvergenceError("input model already contains pdf_convergence attributes")
    result["pdf_convergence"] = convergence
    return result


def converge_pdf_models(
    architecture: BuildingModel,
    electrical: BuildingModel,
    *,
    options: PdfConvergenceOptions | None = None,
) -> BuildingModel:
    """Join the two PDF lane outputs into one canonical semantic model.

    Both inputs remain canonical v1 BuildingModel values.  This function does
    not re-run PDF recognition or create a parallel representation.  The
    electrical lane must already be explicitly registered into the same frame
    as the architectural lane.
    """

    _validate_lane_models(architecture, electrical)
    options = options or PdfConvergenceOptions()
    convergence_source_id = (
        f"{architecture.model_id}|{electrical.model_id}"
    )
    ambiguities: list[dict[str, Any]] = []

    before_owners: dict[str, ElectricalEntity] = {
        item.id: item
        for item in (*electrical.electrical_equipment, *electrical.electrical_devices)
    }
    after_owners: dict[str, ElectricalEntity] = {
        item.id: _resolve_entity(
            item,
            architecture,
            options=options,
            convergence_source_id=convergence_source_id,
            ambiguities=ambiguities,
        )
        for item in sorted(before_owners.values(), key=lambda value: value.id)
    }

    equipment = tuple(
        sorted(
            (
                item
                for item in after_owners.values()
                if isinstance(item, ElectricalEquipment)
            ),
            key=lambda item: item.id,
        )
    )
    devices = tuple(
        sorted(
            (
                item
                for item in after_owners.values()
                if isinstance(item, ElectricalDevice)
            ),
            key=lambda item: item.id,
        )
    )
    ports = tuple(
        sorted(
            (
                _translate_port(
                    port,
                    before_owner=before_owners[port.owner_id],
                    after_owner=after_owners[port.owner_id],
                    convergence_source_id=convergence_source_id,
                )
                for port in electrical.ports
            ),
            key=lambda item: item.id,
        )
    )

    ambiguities = sorted(
        ambiguities,
        key=lambda item: (
            str(item.get("entity_id", "")),
            str(item.get("code", "")),
            str(item.get("detail", "")),
        ),
    )
    attachment_rows = [
        {
            "entity_id": item.id,
            "entity_kind": (
                "equipment" if isinstance(item, ElectricalEquipment) else "device"
            ),
            "status": item.attributes["pdf_convergence"]["status"],
            "ambiguity_codes": item.attributes["pdf_convergence"].get(
                "ambiguity_codes", []
            ),
        }
        for item in sorted(after_owners.values(), key=lambda value: value.id)
    ]
    convergence_attributes = {
        "architecture_model_id": architecture.model_id,
        "electrical_model_id": electrical.model_id,
        "frame_id": architecture.coordinate_system.frame_id,
        "attachments": attachment_rows,
        "ambiguities": ambiguities,
        "options": {
            "level_elevation_tolerance_m": options.level_elevation_tolerance_m,
            "max_wall_host_distance_m": options.max_wall_host_distance_m,
            "wall_host_ambiguity_tolerance_m": options.wall_host_ambiguity_tolerance_m,
            "vertical_conflict_tolerance_m": options.vertical_conflict_tolerance_m,
            "boundary_tolerance_m": options.boundary_tolerance_m,
        },
    }

    model_confidence_candidates = [
        *(item.confidence for item in equipment),
        *(item.confidence for item in devices),
    ]
    convergence_confidence = (
        min(model_confidence_candidates) if model_confidence_candidates else 1.0
    )
    convergence_provenance = Provenance(
        source_kind="pdf-convergence",
        source_id=convergence_source_id,
        method="canonical architectural/electrical PDF spatial convergence",
        confidence=convergence_confidence,
        attributes={
            "architecture_model_id": architecture.model_id,
            "electrical_model_id": electrical.model_id,
        },
    )

    return BuildingModel(
        model_id=stable_id(
            "model",
            f"pdf-convergence:{architecture.model_id}:{electrical.model_id}",
        ),
        name=f"PDF convergence: {architecture.name or architecture.model_id} + {electrical.name or electrical.model_id}",
        coordinate_system=architecture.coordinate_system,
        levels=tuple(sorted(architecture.levels, key=lambda item: item.id)),
        spaces=tuple(sorted(architecture.spaces, key=lambda item: item.id)),
        walls=tuple(sorted(architecture.walls, key=lambda item: item.id)),
        slabs=tuple(sorted(architecture.slabs, key=lambda item: item.id)),
        ceilings=tuple(sorted(architecture.ceilings, key=lambda item: item.id)),
        openings=tuple(sorted(architecture.openings, key=lambda item: item.id)),
        electrical_equipment=equipment,
        electrical_devices=devices,
        ports=ports,
        obstacles=tuple(sorted(architecture.obstacles, key=lambda item: item.id)),
        route_constraints=tuple(
            sorted(architecture.route_constraints, key=lambda item: item.id)
        ),
        circuits=tuple(sorted(electrical.circuits, key=lambda item: item.id)),
        conductors=tuple(sorted(electrical.conductors, key=lambda item: item.id)),
        provenance=(
            *architecture.provenance,
            *electrical.provenance,
            convergence_provenance,
        ),
        attributes=_merged_attributes(
            architecture,
            electrical,
            convergence=convergence_attributes,
        ),
    )
