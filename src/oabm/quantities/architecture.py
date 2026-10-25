"""Conservative architectural claims. No material assemblies or geometry repair.

The 1e-9 metre predicate tolerance is absolute; it never scales with world
coordinates and never rewrites canonical geometry. Ambiguous solids are declined.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from oabm.model import (
    ASSEMBLY_SCHEMA_VERSIONS,
    AssemblyComponent,
    Box3D,
    BuildingModel,
    Entity,
    Opening,
    PanelGeometry,
    Polygon3D,
    Profile2D,
    ProfileExtrusion,
    ProfileOutline2D,
    WireSweep,
)
from oabm.assembly_geometry import GeometryInputError, extrude_simple_section_m

PREDICATE_TOLERANCE_M = 1e-9


@dataclass(frozen=True)
class Claim:
    entity: Entity
    category: str = ""
    item_type: str = ""
    quantity: float = 0
    unit: str = ""
    variant: tuple[tuple[str, object], ...] = ()
    consumed_paths: tuple[str, ...] = ()
    warning: str | None = None
    message: str = ""


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _dot(a, b):
    return math.fsum(x*y for x, y in zip(a, b))


def polygon_area(polygon: Polygon3D) -> float:
    """Actual planar surface area, refusing crossings, touches and degeneracy."""
    p0 = polygon.points[0]
    points = [(p.x-p0.x, p.y-p0.y, p.z-p0.z) for p in polygon.points]
    normal = max((_cross(a, b) for a, b in zip(points[1:], points[2:])),
                 key=lambda n: math.hypot(*n))
    norm = math.hypot(*normal)
    eps = PREDICATE_TOLERANCE_M
    if not math.isfinite(norm) or norm <= eps*eps:
        raise ValueError("degenerate polygon")
    normal = tuple(value/norm for value in normal)
    if any(abs(_dot(p, normal)) > eps for p in points):
        raise ValueError("nonplanar polygon")
    # Orthonormal projection onto the polygon's own plane, not the XY floor.
    axis = max(points, key=lambda p: math.hypot(*p))
    length = math.hypot(*axis)
    axis = tuple(value/length for value in axis)
    second = _cross(normal, axis)
    planar = [(_dot(p, axis), _dot(p, second)) for p in points]
    count = len(planar)
    edges = [(planar[i], planar[(i+1) % count]) for i in range(count)]
    def orientation(a, b, p):
        length = math.dist(a, b)
        if length <= eps:
            raise ValueError("zero-length polygon edge")
        return ((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))/length
    def on_segment(a, b, p):
        return abs(orientation(a, b, p)) <= eps and all(min(a[k], b[k])-eps <= p[k] <= max(a[k], b[k])+eps for k in (0, 1))
    for i, (a, b) in enumerate(edges):
        orientation(a, b, a)
        # Reject backtracking adjacent edges, even when the ring's net area is nonzero.
        c = planar[(i+2) % count]
        if abs(orientation(a, b, c)) <= eps and _dot((b[0]-a[0], b[1]-a[1]), (c[0]-b[0], c[1]-b[1])) < 0:
            raise ValueError("backtracking polygon edge")
        for j in range(i+1, count):
            if j == i+1 or (i == 0 and j == count-1):
                continue
            c, d = edges[j]
            ab_c, ab_d = orientation(a, b, c), orientation(a, b, d)
            cd_a, cd_b = orientation(c, d, a), orientation(c, d, b)
            crosses = ((ab_c > eps and ab_d < -eps) or (ab_c < -eps and ab_d > eps)) and ((cd_a > eps and cd_b < -eps) or (cd_a < -eps and cd_b > eps))
            if crosses or any((on_segment(a, b, c), on_segment(a, b, d), on_segment(c, d, a), on_segment(c, d, b))):
                raise ValueError("self-intersecting or touching polygon")
    area = abs(math.fsum(a[0]*b[1]-b[0]*a[1] for a, b in edges))/2
    if not math.isfinite(area) or area <= eps*eps:
        raise ValueError("degenerate polygon area")
    return area


def architectural_claims(model: BuildingModel) -> Iterator[Claim]:
    assemblies = (model.building_assemblies
                  if model.schema_version in ASSEMBLY_SCHEMA_VERSIONS else ())
    aggregate_wall_ids = {
        row.host_id for row in assemblies
        if row.assembly_kind == "wall" and row.host_representation == "aggregate_host"
    }
    aggregate_ceiling_ids = {
        row.host_id for row in assemblies
        if row.assembly_kind == "suspended_ceiling" and row.host_representation == "aggregate_host"
    }
    opening_insert_ids = {
        row.host_id for row in assemblies if row.assembly_kind == "opening_insert"
    }
    for wall in sorted(model.walls, key=lambda e: e.id):
        points = wall.centerline.points
        length = math.fsum(math.dist((a.x,a.y,a.z), (b.x,b.y,b.z)) for a,b in zip(points,points[1:]))
        variant = (("construction", wall.construction), ("height_m", wall.height_m), ("thickness_m", wall.thickness_m))
        yield Claim(wall, "wall_length", "wall", length, "m", variant, ("centerline",))
        if wall.id in aggregate_wall_ids:
            # The host stays a measurable spatial axis, not a second physical
            # shell over its owned framing/layer components.
            continue
        # Multiple segments do not specify corner joins/offset faces. Do not
        # manufacture material area or overlap-corrected volume at those joins.
        if len(points) != 2 or abs(points[0].z-points[1].z) > PREDICATE_TOLERANCE_M:
            yield Claim(wall, warning="wall_solid_undefined", message="Wall length measured; face area and volume need a straight horizontal base or explicit solid/join semantics.")
            continue
        for side in ("left", "right"):
            yield Claim(wall, "wall_face_area", "wall", length*wall.height_m, "m2", variant+(("side", side), ("deductions", "none")), ("centerline", "height_m"))
        yield Claim(wall, "wall_volume", "wall", length*wall.height_m*wall.thickness_m, "m3", variant, ("centerline", "height_m", "thickness_m"))
    for collection, kind in ((model.slabs, "slab"), (model.ceilings, "ceiling"), (model.spaces, "space")):
        for entity in sorted(collection, key=lambda e: e.id):
            if kind == "ceiling" and entity.id in aggregate_ceiling_ids:
                continue
            try:
                area = polygon_area(entity.footprint)
            except ValueError as error:
                yield Claim(entity, warning="unmeasurable_polygon", message=f"{kind}: {error}; no area or volume inferred.")
                continue
            field = "height_m" if kind == "space" else "thickness_m"
            depth = getattr(entity, field)
            variant = ((field, depth),)
            yield Claim(entity, "space_floor_area" if kind == "space" else f"{kind}_area", kind, area, "m2", variant, ("footprint",))
            if depth is None:
                yield Claim(entity, warning="missing_volume_dimension", message=f"{kind} has no {field}; volume was not inferred.")
            elif max(p.z for p in entity.footprint.points)-min(p.z for p in entity.footprint.points) > PREDICATE_TOLERANCE_M:
                yield Claim(entity, warning="volume_direction_undefined", message=f"{kind} surface area measured; tilted-footprint extrusion direction is not specified, so volume was declined.")
            else:
                yield Claim(entity, f"{kind}_volume", kind, area*depth, "m3", variant, ("footprint", field))
    for opening in sorted(model.openings, key=lambda e: e.id):
        yield Claim(opening, "opening_count", opening.opening_type, 1, "ea", (("size_x_m", opening.size.x), ("size_y_m", opening.size.y), ("size_z_m", opening.size.z)), ("opening_type",))
        if opening.host_id not in aggregate_wall_ids:
            yield Claim(opening, warning="opening_deduction_undefined", message="Opening counted; host-face axes, clipping and overlap-union semantics are unspecified, so gross host areas have no opening deductions.")
    for entity in sorted((e for collection in (model.walls, model.slabs, model.ceilings, model.openings) for e in collection), key=lambda e: e.id):
        if (entity.id in aggregate_wall_ids or entity.id in aggregate_ceiling_ids or
                entity.id in opening_insert_ids or
                (isinstance(entity, Opening) and entity.host_id in aggregate_wall_ids)):
            continue
        yield Claim(entity, warning="assemblies_unresolved", message="Geometric building quantities do not specify studs, plates, layers, grid, hangers or fasteners. Assembly specifications and material rules are still required; no complete material takeoff is asserted.")


def assembly_component_claims(model: BuildingModel) -> Iterator[Claim]:
    """Deterministic count and bounded geometry claims for canonical 1.6 parts.

    A component is measured from its canonical record only. Parameterized or
    manufacturer-actual solid geometry may yield a geometry volume; an
    envelope yields only a separately labelled envelope volume. A bent
    multi-segment wire keeps its length/count but declines volume because a
    union/join kernel is not part of this bounded slice.
    """

    if model.schema_version not in ASSEMBLY_SCHEMA_VERSIONS:
        return
    assemblies = {row.id: row for row in model.building_assemblies}
    for component in sorted(model.assembly_components, key=lambda row: row.id):
        owner = assemblies.get(component.assembly_id)
        if owner is None:
            yield Claim(component, warning="assembly_owner_missing",
                        message="Component has no canonical owning assembly; no quantity was produced.")
            continue
        base_variant = (
            ("assembly_kind", owner.assembly_kind),
            ("catalog_id", owner.catalog_id),
            ("catalog_revision", owner.catalog_revision),
            ("preset_id", owner.preset_id),
            ("preset_revision", owner.preset_revision),
            ("component_kind", component.component_kind),
            ("geometry_type", _component_geometry_type(component.geometry)),
            ("geometry_basis", component.geometry_basis),
            ("material_id", component.material_id),
            ("material_basis", component.material_basis),
        )
        yield Claim(component, "assembly_component_count", component.component_kind,
                    1.0, "ea", base_variant, ("geometry",))

        try:
            geometry = component.geometry
            if isinstance(geometry, Box3D):
                x, y, z = geometry.size.x, geometry.size.y, geometry.size.z
                volume = x * y * z
                surface_area = 2.0 * (x*y + x*z + y*z)
                face_area = math.prod(sorted((x, y, z), reverse=True)[:2])
                dimensions = (("size_x_m", x), ("size_y_m", y), ("size_z_m", z))
                if component.component_kind == "drywall_panel":
                    yield Claim(component,
                        "drywall_envelope_area" if component.geometry_basis == "caller_selected_envelope" else "drywall_modeled_area",
                        component.component_kind, face_area, "m2", base_variant + dimensions,
                        ("geometry",))
                elif component.geometry_basis == "caller_selected_envelope":
                    yield Claim(component, "assembly_component_envelope_area",
                        component.component_kind, surface_area, "m2", base_variant + dimensions,
                        ("geometry",))
                else:
                    yield Claim(component, "assembly_component_surface_area",
                        component.component_kind, surface_area, "m2", base_variant + dimensions,
                        ("geometry",))
                volume_variant = base_variant + dimensions
            elif isinstance(geometry, ProfileExtrusion):
                if isinstance(geometry.profile, Profile2D):
                    outline = geometry.profile.outline_xy_m()
                    profile_variant = (
                        ("profile_kind", geometry.profile.kind),
                        ("overall_width_m", geometry.profile.overall_width_m),
                        ("overall_depth_m", geometry.profile.overall_depth_m),
                        ("web_thickness_m", geometry.profile.web_thickness_m),
                        ("flange_thickness_m", geometry.profile.flange_thickness_m),
                        ("return_lip_m", geometry.profile.return_lip_m),
                    )
                elif isinstance(geometry.profile, ProfileOutline2D):
                    outline = tuple((point.x, point.y) for point in geometry.profile.points_m)
                    profile_variant = (("profile_outline_source_id", geometry.profile.source_id),
                                       ("profile_outline_xy_m", tuple(outline)))
                else:
                    yield Claim(component, warning="assembly_geometry_unsupported",
                                message="Unsupported profile section; no dimensional quantity was produced.")
                    continue
                derived = extrude_simple_section_m(outline, geometry.length_m, geometry.pose)
                perimeter = math.fsum(math.dist(outline[index], outline[(index + 1) % len(outline)])
                                      for index in range(len(outline)))
                volume = derived.volume_m3
                surface_area = 2.0*derived.cross_section_area_m2 + perimeter*geometry.length_m
                length_variant = base_variant + profile_variant
                yield Claim(component,
                            "assembly_component_envelope_length" if component.geometry_basis == "caller_selected_envelope" else "assembly_component_length",
                            component.component_kind,
                            geometry.length_m, "m", length_variant,
                            ("geometry",))
                if component.geometry_basis == "caller_selected_envelope":
                    yield Claim(component, "assembly_component_envelope_area", component.component_kind,
                                surface_area, "m2", length_variant + (("length_m", geometry.length_m),),
                                ("geometry",))
                else:
                    yield Claim(component, "assembly_component_cross_section_area", component.component_kind,
                                derived.cross_section_area_m2, "m2", length_variant,
                                ("geometry",))
                    yield Claim(component, "assembly_component_surface_area", component.component_kind,
                                surface_area, "m2", length_variant + (("length_m", geometry.length_m),),
                                ("geometry",))
                volume_variant = length_variant + (("length_m", geometry.length_m),)
            elif isinstance(geometry, PanelGeometry):
                area = polygon_area(geometry.boundary)
                perimeter = math.fsum(
                    math.dist((a.x,a.y,a.z),(b.x,b.y,b.z))
                    for a,b in zip(geometry.boundary.points,
                                   geometry.boundary.points[1:] + geometry.boundary.points[:1])
                )
                volume = area * geometry.thickness_m
                surface_area = 2.0 * area + perimeter * geometry.thickness_m
                xs = tuple(point.x for point in geometry.boundary.points)
                ys = tuple(point.y for point in geometry.boundary.points)
                zs = tuple(point.z for point in geometry.boundary.points)
                dimensions = (("bounds_x_m", max(xs)-min(xs)),
                              ("bounds_y_m", max(ys)-min(ys)),
                              ("bounds_z_m", max(zs)-min(zs)),
                              ("thickness_m", geometry.thickness_m))
                panel_variant = base_variant + dimensions
                area_category = (
                    "drywall_envelope_area" if component.component_kind == "drywall_panel" and component.geometry_basis == "caller_selected_envelope" else
                    "drywall_modeled_area" if component.component_kind == "drywall_panel" else
                    "assembly_component_envelope_area" if component.geometry_basis == "caller_selected_envelope" else
                    "assembly_component_surface_area"
                )
                yield Claim(component, area_category, component.component_kind,
                    area if component.component_kind == "drywall_panel" or component.geometry_basis == "caller_selected_envelope" else surface_area,
                    "m2", panel_variant, ("geometry",))
                volume_variant = panel_variant
            elif isinstance(geometry, WireSweep):
                length = math.fsum(
                    math.dist((a.x,a.y,a.z),(b.x,b.y,b.z))
                    for a,b in zip(geometry.centerline.points, geometry.centerline.points[1:])
                )
                wire_variant = base_variant + (("diameter_m", geometry.diameter_m),
                                                ("end_treatment", geometry.end_treatment))
                yield Claim(component,
                            "assembly_component_envelope_length" if component.geometry_basis == "caller_selected_envelope" else "assembly_component_length",
                            component.component_kind,
                            length, "m", wire_variant, ("geometry",))
                if len(geometry.centerline.points) != 2:
                    yield Claim(component, warning="wire_volume_join_undefined",
                                message="Straight segment lengths are measurable, but joined multi-segment wire volume requires an explicit union/join solid kernel.")
                    continue
                radius = geometry.diameter_m / 2.0
                volume = math.pi * radius * radius * length
                surface_area = math.pi * geometry.diameter_m * length + 2.0 * math.pi * radius * radius
                yield Claim(component,
                            "assembly_component_envelope_area" if component.geometry_basis == "caller_selected_envelope" else "assembly_component_surface_area",
                            component.component_kind, surface_area, "m2", wire_variant + (("length_m", length),),
                            ("geometry",))
                volume_variant = wire_variant + (("length_m", length),)
            else:
                yield Claim(component, warning="assembly_geometry_unsupported",
                            message="Unsupported component geometry; no dimensional quantity was produced.")
                continue
        except (GeometryInputError, ValueError, OverflowError) as error:
            yield Claim(component, warning="assembly_geometry_unmeasurable",
                        message=f"Canonical component geometry could not be measured: {error}")
            continue

        if component.geometry_basis == "caller_selected_envelope":
            yield Claim(component, "assembly_component_envelope_volume", component.component_kind,
                        volume, "m3", volume_variant + (("volume_semantics", "envelope_only_not_material_volume"),),
                        ("geometry",))
        else:
            volume_category = (
                "assembly_component_parametric_material_volume"
                if component.geometry_basis == "specified_parameterized_geometry"
                and component.material_id is not None and component.material_basis != "unknown"
                else "assembly_component_geometry_volume"
            )
            volume_semantics = (
                "parameterized_geometry_with_explicit_material"
                if volume_category == "assembly_component_parametric_material_volume"
                else "represented_geometry_volume_not_material_purchase"
            )
            yield Claim(component, volume_category, component.component_kind,
                        volume, "m3", volume_variant + (("volume_semantics", volume_semantics),),
                        ("geometry",))


def _component_geometry_type(geometry: object) -> str:
    if isinstance(geometry, Box3D):
        return "box3d"
    if isinstance(geometry, ProfileExtrusion):
        return "profile_extrusion"
    if isinstance(geometry, PanelGeometry):
        return "panel"
    if isinstance(geometry, WireSweep):
        return "wire_sweep"
    return "unsupported"
