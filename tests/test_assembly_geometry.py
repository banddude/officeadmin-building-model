"""Known-answer tests for the bounded math-only assembly mesh helpers."""

from __future__ import annotations

import math

import pytest

from oabm.assembly_geometry import (
    MAX_SECTION_VERTICES,
    MAX_WIRE_SIDES,
    GeometryInputError,
    extrude_simple_section_m,
    straight_round_wire_m,
)
from oabm.model import Point3, Pose, Quaternion


def _pose(x: float = 0.0, y: float = 0.0, z: float = 0.0,
          rotation: Quaternion | None = None) -> Pose:
    return Pose(position=Point3(x=x, y=y, z=z), rotation=rotation or Quaternion())


def _triangle_area_xy(mesh, triangle: tuple[int, int, int]) -> float:
    a, b, c = (mesh.vertices_m[index] for index in triangle)
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2


def _point_in_triangle_xy(point: tuple[float, float], a, b, c) -> bool:
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    signs = (cross(a, b, point), cross(b, c, point), cross(c, a, point))
    return min(signs) >= -1e-12 or max(signs) <= 1e-12


def _assert_closed_two_manifold(mesh) -> None:
    edge_count: dict[tuple[int, int], int] = {}
    for triangle in mesh.triangles:
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            edge = (min(a, b), max(a, b))
            edge_count[edge] = edge_count.get(edge, 0) + 1
    assert edge_count
    assert set(edge_count.values()) == {2}


def _posed_point(mesh, vertex: tuple[float, float, float]):
    x, y, z = vertex
    qx, qy, qz, qw = mesh.pose.rotation_xyzw
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        mesh.pose.position_m[0] + x + qw * tx + qy * tz - qz * ty,
        mesh.pose.position_m[1] + y + qw * ty + qz * tx - qx * tz,
        mesh.pose.position_m[2] + z + qw * tz + qx * ty - qy * tx,
    )


def _signed_mesh_volume(mesh) -> float:
    volume_terms = []
    for triangle in mesh.triangles:
        a, b, c = (mesh.vertices_m[index] for index in triangle)
        volume_terms.append((
            a[0] * (b[1] * c[2] - b[2] * c[1])
            + a[1] * (b[2] * c[0] - b[0] * c[2])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0)
    return math.fsum(volume_terms)


def test_rectangle_has_known_mesh_area_volume_bounds_and_closed_surface():
    mesh = extrude_simple_section_m(
        ((0, 0), (.2, 0), (.2, .1), (0, .1)), 2.0, _pose()
    )

    assert mesh.cross_section_area_m2 == pytest.approx(.02)
    assert mesh.volume_m3 == pytest.approx(.04)
    assert mesh.local_bounds_m == pytest.approx((0, 0, 0, .2, .1, 2))
    assert mesh.world_bounds_m == pytest.approx(mesh.local_bounds_m)
    assert len(mesh.vertices_m) == 8
    assert len(mesh.triangles) == 12
    assert sum(_triangle_area_xy(mesh, tri) for tri in mesh.triangles
               if all(mesh.vertices_m[index][2] == 2.0 for index in tri)) == pytest.approx(.02)
    _assert_closed_two_manifold(mesh)


def test_redundant_collinear_outline_points_are_preserved_as_input_not_degenerate_mesh_edges():
    mesh = extrude_simple_section_m(
        ((0, 0), (.1, 0), (.2, 0), (.2, .1), (0, .1)), 1.0, _pose()
    )

    assert mesh.input_profile_xy_m == ((0, 0), (.1, 0), (.2, 0), (.2, .1), (0, .1))
    assert len(mesh.vertices_m) == 8
    assert mesh.cross_section_area_m2 == pytest.approx(.02)
    _assert_closed_two_manifold(mesh)


@pytest.mark.parametrize(
    ("profile", "expected_area"),
    [
        # C channel opens to +X; it is outside the outline and must remain empty.
        (((0, 0), (.10, 0), (.10, .02), (.02, .02), (.02, .08),
          (.10, .08), (.10, .10), (0, .10)), .0052),
        # T stem plus a top flange: concave but still one simple outline.
        (((.04, 0), (.08, 0), (.08, .08), (.12, .08),
          (.12, .10), (0, .10), (0, .08), (.04, .08)), .0056),
    ],
)
def test_concave_c_and_t_sections_triangulate_without_filling_open_notches(profile, expected_area):
    mesh = extrude_simple_section_m(profile, .4, _pose())
    top_triangles = [
        tri for tri in mesh.triangles
        if all(mesh.vertices_m[index][2] == .4 for index in tri)
    ]

    assert mesh.cross_section_area_m2 == pytest.approx(expected_area)
    assert mesh.volume_m3 == pytest.approx(expected_area * .4)
    assert sum(_triangle_area_xy(mesh, tri) for tri in top_triangles) == pytest.approx(expected_area)
    _assert_closed_two_manifold(mesh)
    if expected_area < .0055:
        # The open C channel at x=.06, y=.05 is not accidentally capped in.
        sample = (.06, .05)
        assert not any(
            _point_in_triangle_xy(sample, *(mesh.vertices_m[index] for index in tri))
            for tri in top_triangles
        )
    else:
        # The two lower corners beside the T stem are likewise outside material.
        for sample in ((.02, .05), (.10, .05)):
            assert not any(
                _point_in_triangle_xy(sample, *(mesh.vertices_m[index] for index in tri))
                for tri in top_triangles
            )


def test_profile_mesh_is_canonical_for_winding_and_cyclic_shift_but_keeps_input_order():
    profile = ((0, 0), (.1, 0), (.1, .02), (.02, .02), (.02, .08),
               (.1, .08), (.1, .1), (0, .1))
    shifted = profile[3:] + profile[:3]
    reversed_profile = tuple(reversed(profile))
    first = extrude_simple_section_m(profile, .75, _pose())
    second = extrude_simple_section_m(shifted, .75, _pose())
    third = extrude_simple_section_m(reversed_profile, .75, _pose())

    assert first.vertices_m == second.vertices_m == third.vertices_m
    assert first.triangles == second.triangles == third.triangles
    assert first.input_profile_xy_m == profile
    assert second.input_profile_xy_m == shifted
    assert third.input_profile_xy_m == reversed_profile


def test_translated_unit_profile_keeps_analytic_area_and_triangulates_relative_to_anchor():
    offset = 1e8
    mesh = extrude_simple_section_m(
        ((offset, offset), (offset + 1, offset),
         (offset + 1, offset + 1), (offset, offset + 1)),
        .5, _pose(),
    )

    assert mesh.cross_section_area_m2 == pytest.approx(1.0)
    assert mesh.volume_m3 == pytest.approx(.5)
    assert mesh.local_bounds_m == pytest.approx((offset, offset, 0, offset + 1, offset + 1, .5))


def test_pose_rotates_local_extrusion_and_reports_world_bounds():
    quarter_turn_y = Quaternion(x=0, y=math.sqrt(.5), z=0, w=math.sqrt(.5))
    mesh = extrude_simple_section_m(
        ((0, 0), (.2, 0), (.2, .1), (0, .1)), 2.0,
        _pose(1, 2, 4, quarter_turn_y),
    )

    assert mesh.local_bounds_m == pytest.approx((0, 0, 0, .2, .1, 2))
    assert mesh.world_bounds_m == pytest.approx((1, 2, 3.8, 3, 2.1, 4))
    assert mesh.pose.position_m == (1, 2, 4)


def test_tuple_pose_input_is_dependency_neutral():
    mesh = extrude_simple_section_m(
        ((0, 0), (.1, 0), (.1, .1), (0, .1)), 1.0,
        ((1, 2, 3), (0, 0, 0, 1)),
    )
    assert mesh.pose.position_m == (1, 2, 3)


def test_accepted_near_unit_pose_quaternion_is_normalized_before_transform_and_storage():
    norm = 1.000009
    q = math.sqrt(.5) * norm
    mesh = extrude_simple_section_m(
        ((0, 0), (.2, 0), (.2, .1), (0, .1)), 2.0,
        ((1, 2, 4), (0, q, 0, q)),
    )
    expected = (1, 2, 3.8, 3, 2.1, 4)

    assert math.hypot(*mesh.pose.rotation_xyzw) == pytest.approx(1.0)
    assert mesh.world_bounds_m == pytest.approx(expected)
    assert mesh.volume_m3 == pytest.approx(.04)


def test_straight_round_wire_has_indexed_closed_mesh_and_analytic_circle_quantity():
    start = Point3(x=1, y=2, z=3)
    end = Point3(x=1, y=2, z=5)
    first = straight_round_wire_m(start, end, .01)
    second = straight_round_wire_m(start, end, .01)

    assert first == second
    assert first.cross_section_area_m2 == pytest.approx(math.pi * .01**2)
    assert first.volume_m3 == pytest.approx(math.pi * .01**2 * 2)
    assert first.pose.position_m == (1, 2, 3)
    assert len(first.vertices_m) == 20
    assert len(first.triangles) == 36
    assert first.world_bounds_m[0] == pytest.approx(1 - .01)
    assert first.world_bounds_m[3] == pytest.approx(1 + .01)
    assert first.world_bounds_m == pytest.approx((
        1 - .01, 2 - .01, 3,
        1 + .01, 2 + .01, 5,
    ))
    _assert_closed_two_manifold(first)
    assert _signed_mesh_volume(first) > 0.0
    for triangle in first.triangles:
        a, b, c = (first.vertices_m[index] for index in triangle)
        ab = tuple(b[index] - a[index] for index in range(3))
        ac = tuple(c[index] - a[index] for index in range(3))
        normal = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        assert math.hypot(*normal) > 0.0
        if all(first.vertices_m[index][2] == 0.0 for index in triangle):
            assert normal[2] < 0.0
        elif all(first.vertices_m[index][2] == 2.0 for index in triangle):
            assert normal[2] > 0.0
        else:
            centroid = ((a[0] + b[0] + c[0]) / 3,
                        (a[1] + b[1] + c[1]) / 3)
            assert normal[0] * centroid[0] + normal[1] * centroid[1] > 0.0


def test_low_facet_rotated_wire_bounds_cover_the_full_circle_not_just_mesh_vertices():
    radius = .05
    mesh = straight_round_wire_m(
        Point3(x=0, y=0, z=0), Point3(x=2, y=2, z=0), radius, sides=6
    )
    radial_xy = radius / math.sqrt(2)
    expected = (-radial_xy, -radial_xy, -radius,
                2 + radial_xy, 2 + radial_xy, radius)

    assert mesh.local_bounds_m == pytest.approx((
        -radius, -radius, 0, radius, radius, 2 * math.sqrt(2),
    ))
    assert mesh.world_bounds_m == pytest.approx(expected)
    transformed = [_posed_point(mesh, vertex) for vertex in mesh.vertices_m]
    assert mesh.world_bounds_m[0] < min(point[0] for point in transformed)
    assert mesh.world_bounds_m[3] > max(point[0] for point in transformed)


def test_near_antiparallel_wire_pose_maps_endpoint_without_dropping_small_xy_component():
    end = Point3(x=1, y=0, z=-1e6)
    mesh = straight_round_wire_m(Point3(x=0, y=0, z=0), end, .01)
    local_endpoint = (0.0, 0.0, math.hypot(1, -1e6))
    posed_endpoint = _posed_point(mesh, local_endpoint)

    assert abs(mesh.pose.rotation_xyzw[3]) > 0.0
    assert posed_endpoint == pytest.approx((end.x, end.y, end.z), abs=1e-9)


def test_wire_pose_follows_diagonal_endpoints():
    mesh = straight_round_wire_m(
        Point3(x=1, y=2, z=3), Point3(x=3, y=2, z=3), .01
    )
    assert mesh.world_bounds_m[0] == pytest.approx(1)
    assert mesh.world_bounds_m[3] == pytest.approx(3)
    assert mesh.world_bounds_m[1] < 2 < mesh.world_bounds_m[4]
    assert mesh.world_bounds_m[2] < 3 < mesh.world_bounds_m[5]


@pytest.mark.parametrize(
    ("profile", "length"),
    [
        (((0, 0), (1, 1)), 1),
        (((0, 0), (1, 0), (0, 1)), 0),
        (((0, 0), (1, 0), (0, 1)), -1),
        (((0, 0), (1, 0), (0, 1)), math.inf),
        (((0, 0), (1, 0), (math.nan, 1)), 1),
        (((0, 0), (1, 0), (0, 1), (0, 0)), 1),
        (((0, 0), (1e308, 0), (1e308, 2), (0, 2)), 1),
        (((0, 0), (2, 2), (0, 2), (2, 0)), 1),
        (((0, 0), (1, 0), (2, 0)), 1),
        # Adjacent collinear edges that overlap by backtracking are not simple.
        (((0, 0), (2, 0), (1, 0), (1, 1), (0, 1)), 1),
    ],
)
def test_invalid_or_unsupported_profiles_refuse_without_fan_fallback(profile, length):
    with pytest.raises(GeometryInputError):
        extrude_simple_section_m(profile, length, _pose())


def test_resource_limits_refuse_large_profiles_and_wire_meshes():
    too_many = tuple(
        (math.cos(2 * math.pi * index / (MAX_SECTION_VERTICES + 1)),
         math.sin(2 * math.pi * index / (MAX_SECTION_VERTICES + 1)))
        for index in range(MAX_SECTION_VERTICES + 1)
    )
    with pytest.raises(GeometryInputError, match="3..128"):
        extrude_simple_section_m(too_many, 1, _pose())
    with pytest.raises(GeometryInputError, match="sides"):
        straight_round_wire_m(Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1),
                              .01, sides=MAX_WIRE_SIDES + 1)


def test_underflowed_or_overflowed_derived_quantities_refuse():
    tiny_radius = 1e-200
    with pytest.raises(GeometryInputError, match="underflowed"):
        straight_round_wire_m(Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1), tiny_radius)
    with pytest.raises(GeometryInputError, match="volume_m3 underflowed"):
        straight_round_wire_m(
            Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=2e-12), 1e-162
        )
    with pytest.raises(GeometryInputError, match="underflowed"):
        extrude_simple_section_m(((0, 0), (1, 0), (1, .1), (0, .1)), 5e-324, _pose())
    with pytest.raises(GeometryInputError):
        straight_round_wire_m(Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1), 1e200)
    with pytest.raises(GeometryInputError):
        extrude_simple_section_m(
            ((0, 0), (2000, 0), (2000, 2000), (0, 2000)), 1e308, _pose()
        )


@pytest.mark.parametrize(
    ("start", "end", "radius", "sides"),
    [
        (Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=0), .01, 10),
        (Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1), 0, 10),
        (Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1), .01, 3),
        (Point3(x=0, y=0, z=0), Point3(x=0, y=0, z=1), .01, True),
    ],
)
def test_invalid_wire_inputs_are_refused(start, end, radius, sides):
    with pytest.raises(GeometryInputError):
        straight_round_wire_m(start, end, radius, sides=sides)
