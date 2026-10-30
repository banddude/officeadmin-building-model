"""Neutral deterministic world-space meshes for canonical assembly components.

Shared by physical consumers. Derived triangles preserve canonical dimensions
and poses; they are not an alternative model or a claim of manufactured fit.
Wire sweeps use straight round segments, not curved manufactured bends.
"""
from __future__ import annotations
from typing import Any
from oabm.model import (AssemblyComponent, Box3D, PanelGeometry, Profile2D,
                        ProfileExtrusion, ProfileOutline2D, WireSweep)
from oabm.geometry_primitives import _plan_ear_triangles
from oabm.assembly_geometry import (GeometryInputError, extrude_simple_section_m,
                                    rotate_vector_xyzw, straight_round_wire_m)

def assembly_component_mesh(
    component: AssemblyComponent,
) -> tuple[list[tuple[float, float, float]], tuple[int, ...]]:
    """Return one indexed world-space mesh for one canonical assembly part.

    Section extrusions and every round-wire segment use the strict shared
    assembly geometry kernel. Panels use the shared deterministic planar ear
    clipper after the canonical contract has already established a simple,
    planar polygon; failure here is explicit and never falls back to a fan.
    """

    geometry = component.geometry
    if isinstance(geometry, Box3D):
        soup = _local_box_vertices(geometry.size.x, geometry.size.y, geometry.size.z)
        world = [
            _pose_world_point(vertex, geometry.pose.position, geometry.pose.rotation)
            for vertex in soup
        ]
        return _index_triangle_soup(world)

    if isinstance(geometry, ProfileExtrusion):
        if isinstance(geometry.profile, Profile2D):
            profile_xy = geometry.profile.outline_xy_m()
        elif isinstance(geometry.profile, ProfileOutline2D):
            profile_xy = tuple((point.x, point.y) for point in geometry.profile.points_m)
        else:  # defensive: the v1.6 discriminated union must fail closed
            raise ValueError(f"{component.id} has an unsupported profile section")
        try:
            derived = extrude_simple_section_m(profile_xy, geometry.length_m, geometry.pose)
        except GeometryInputError as error:
            raise ValueError(f"{component.id} cannot be meshed: {error}") from error
        world = [
            _pose_world_point(vertex, derived.pose.position_m, derived.pose.rotation_xyzw)
            for vertex in derived.vertices_m
        ]
        return world, tuple(index for tri in derived.triangles for index in tri)

    if isinstance(geometry, PanelGeometry):
        points = geometry.boundary.points
        normal = (geometry.normal.x, geometry.normal.y, geometry.normal.z)
        axis = max(range(3), key=lambda index: abs(normal[index]))
        plan = [
            (point.y, point.z) if axis == 0 else
            (point.x, point.z) if axis == 1 else
            (point.x, point.y)
            for point in points
        ]
        # The shared ear clipper operates on local planar extents. Subtracting
        # a stable anchor avoids cancellation in its signed-area predicates at
        # large world origins without changing the canonical points or output
        # vertex positions.
        anchor = plan[0]
        local_plan = [(u - anchor[0], v - anchor[1]) for u, v in plan]
        triangles = _plan_ear_triangles(local_plan)
        if triangles is None:
            raise ValueError(f"{component.id} panel boundary is not triangulable")
        half = geometry.thickness_m / 2.0
        lower = [
            (point.x - normal[0] * half,
             point.y - normal[1] * half,
             point.z - normal[2] * half)
            for point in points
        ]
        upper = [
            (point.x + normal[0] * half,
             point.y + normal[1] * half,
             point.z + normal[2] * half)
            for point in points
        ]
        count = len(points)
        vertices = lower + upper
        # Ear clipping returns consistent planar triangles; orient the caps
        # against the canonical explicit normal, independent of ring winding.
        first = triangles[0]
        a, b, c = (points[index] for index in first)
        ab = (b.x - a.x, b.y - a.y, b.z - a.z)
        ac = (c.x - a.x, c.y - a.y, c.z - a.z)
        cross = (ab[1] * ac[2] - ab[2] * ac[1],
                 ab[2] * ac[0] - ab[0] * ac[2],
                 ab[0] * ac[1] - ab[1] * ac[0])
        aligned = sum(cross[index] * normal[index] for index in range(3)) > 0.0
        indices: list[int] = []
        for first, second, third in triangles:
            top = (count + first, count + second, count + third)
            bottom = (first, second, third)
            if not aligned:
                top = (top[0], top[2], top[1])
                bottom = (bottom[0], bottom[2], bottom[1])
            indices.extend(top)
            indices.extend((bottom[0], bottom[2], bottom[1]))
        for index in range(count):
            nxt = (index + 1) % count
            first_pair = ((index, nxt, count + nxt), (index, count + nxt, count + index))
            if aligned:
                indices.extend(first_pair[0])
                indices.extend(first_pair[1])
            else:
                indices.extend((first_pair[0][0], first_pair[0][2], first_pair[0][1]))
                indices.extend((first_pair[1][0], first_pair[1][2], first_pair[1][1]))
        return vertices, tuple(indices)

    if isinstance(geometry, WireSweep):
        vertices: list[tuple[float, float, float]] = []
        indices: list[int] = []
        for start, end in zip(geometry.centerline.points, geometry.centerline.points[1:]):
            try:
                derived = straight_round_wire_m(start, end, geometry.diameter_m / 2.0)
            except (GeometryInputError, ValueError) as error:
                raise ValueError(f"{component.id} wire segment cannot be meshed: {error}") from error
            base = len(vertices)
            vertices.extend(
                _pose_world_point(vertex, derived.pose.position_m, derived.pose.rotation_xyzw)
                for vertex in derived.vertices_m
            )
            indices.extend(base + index for tri in derived.triangles for index in tri)
        if not indices:
            raise ValueError(f"{component.id} wire sweep produced no triangles")
        return vertices, tuple(indices)

    raise ValueError(f"unsupported assembly component geometry: {type(geometry).__name__}")


def _pose_world_point(vertex: tuple[float, float, float], position: Any,
                      rotation: Any) -> tuple[float, float, float]:
    rotated = rotate_vector_xyzw(vertex, rotation)
    if isinstance(position, tuple):
        origin = position
    else:
        origin = (position.x, position.y, position.z)
    return tuple(origin[index] + rotated[index] for index in range(3))


def _index_triangle_soup(
    vertices: list[tuple[float, float, float]],
) -> tuple[list[tuple[float, float, float]], tuple[int, ...]]:
    """Deduplicate exact box corners while retaining its deterministic winding."""

    indexed: list[tuple[float, float, float]] = []
    lookup: dict[tuple[float, float, float], int] = {}
    indices: list[int] = []
    for vertex in vertices:
        index = lookup.get(vertex)
        if index is None:
            index = len(indexed)
            lookup[vertex] = index
            indexed.append(vertex)
        indices.append(index)
    if not indexed or len(indices) % 3:
        raise ValueError("assembly box did not produce a complete triangle mesh")
    return indexed, tuple(indices)


def _local_box_vertices(
    width: float, depth: float, height: float
) -> list[tuple[float, float, float]]:
    """Axis-aligned box centred on the local origin (base at -height/2)."""

    hx, hy, hz = width / 2.0, depth / 2.0, height / 2.0
    corners = [
        (-hx, -hy, -hz),
        (hx, -hy, -hz),
        (hx, hy, -hz),
        (-hx, hy, -hz),
    ]
    top = [(x, y, hz) for x, y, _ in corners]
    return [
        corners[0], corners[2], corners[1],
        corners[0], corners[3], corners[2],
        top[0], top[1], top[2],
        top[0], top[2], top[3],
        corners[0], corners[1], top[1],
        corners[0], top[1], top[0],
        corners[1], corners[2], top[2],
        corners[1], top[2], top[1],
        corners[2], corners[3], top[3],
        corners[2], top[3], top[2],
        corners[3], corners[0], top[0],
        corners[3], top[0], top[3],
    ]
