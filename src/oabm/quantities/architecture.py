"""Conservative architectural claims. No material assemblies or geometry repair.

The 1e-9 metre predicate tolerance is absolute; it never scales with world
coordinates and never rewrites canonical geometry. Ambiguous solids are declined.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from oabm.model import BuildingModel, Entity, Polygon3D

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
    for wall in sorted(model.walls, key=lambda e: e.id):
        points = wall.centerline.points
        length = math.fsum(math.dist((a.x,a.y,a.z), (b.x,b.y,b.z)) for a,b in zip(points,points[1:]))
        variant = (("construction", wall.construction), ("height_m", wall.height_m), ("thickness_m", wall.thickness_m))
        yield Claim(wall, "wall_length", "wall", length, "m", variant, ("centerline",))
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
        yield Claim(opening, warning="opening_deduction_undefined", message="Opening counted; host-face axes, clipping and overlap-union semantics are unspecified, so gross host areas have no opening deductions.")
    for entity in sorted((e for collection in (model.walls, model.slabs, model.ceilings, model.openings) for e in collection), key=lambda e: e.id):
        yield Claim(entity, warning="assemblies_unresolved", message="Geometric building quantities do not specify studs, plates, layers, grid, hangers or fasteners. Assembly specifications and material rules are still required; no complete material takeoff is asserted.")
