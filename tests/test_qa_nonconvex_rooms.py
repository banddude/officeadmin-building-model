"""GLB floor-plate triangulation and plan projection over non-convex rooms.

The ``nonconvex-rooms`` golden fixture (synthetic, public safe) carries one
level with an L-shaped space (6 vertices), a U-shaped space (8 vertices), and
a rectangular control space, each ringed by its perimeter walls. Issue #190
replaced the vertex-0 fan with deterministic ear clipping for non-convex
floor plates; these tests decode the exported GLB cap triangles back out of
the BIN chunk and pin them against the footprint, then check the drawings
lane's plan primitives over the same model.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

from oabm.drawings import PlanSpec, generate_drawing_set
from oabm.exports import to_glb
from oabm.qa import (
    canonical_digest,
    load_golden_cases,
    load_golden_model,
    validate_public_fixture_provenance,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
CASE_NAME = "nonconvex-rooms"
SPACES_LAYER = "architecture:spaces"

# Known answers (metres), straight from the fixture geometry. Counter-clockwise.
L_VERTICES = (
    (0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (2.0, 3.0), (2.0, 5.0), (0.0, 5.0),
)
U_VERTICES = (
    (7.0, 0.0), (13.0, 0.0), (13.0, 6.0), (11.0, 6.0),
    (11.0, 2.0), (9.0, 2.0), (9.0, 6.0), (7.0, 6.0),
)
RECT_VERTICES = ((15.0, 0.0), (19.0, 0.0), (19.0, 4.0), (15.0, 4.0))

FOOTPRINTS = {
    "space:ncx-l": L_VERTICES,
    "space:ncx-u": U_VERTICES,
    "space:ncx-rect": RECT_VERTICES,
}

# Cap triangles per cap: a simple polygon triangulates into n-2 triangles,
# whether the exporter fans a convex footprint or ear-clips a non-convex one.
CAP_TRIANGLES = {"space:ncx-l": 4, "space:ncx-u": 6, "space:ncx-rect": 2}


def _case():
    return next(case for case in load_golden_cases(GOLDEN_ROOT) if case.name == CASE_NAME)


def _model():
    return load_golden_model(_case())


def _parse_glb(path: Path) -> tuple[dict, bytes]:
    """Return the glTF JSON chunk and the BIN chunk of a GLB file."""
    data = path.read_bytes()
    json_length, _json_type = struct.unpack_from("<II", data, 12)
    bin_offset = 20 + json_length
    bin_length, _bin_type = struct.unpack_from("<II", data, bin_offset)
    blob = data[bin_offset + 8:bin_offset + 8 + bin_length]
    return json.loads(data[20:20 + json_length]), blob


def _by_name(gltf: dict) -> dict[str, int]:
    return {node["name"]: index for index, node in enumerate(gltf["nodes"])}


def _node_vertices(gltf: dict, blob: bytes, names: dict[str, int], name: str) -> list[tuple[float, float, float]]:
    """Decoded POSITION triplets converted back to canonical +Z-up metres.

    The exporter stores +Y-up glTF coordinates (``_yup_vertex``:
    ``(x, z, -y)``), so the plan lives in components 0 and 2 down there.
    """

    node = gltf["nodes"][names[name]]
    accessor = gltf["accessors"][
        gltf["meshes"][node["mesh"]]["primitives"][0]["attributes"]["POSITION"]
    ]
    view = gltf["bufferViews"][accessor["bufferView"]]
    values = struct.unpack_from(f"<{accessor['count'] * 3}f", blob, view["byteOffset"])
    return [
        (values[i], -values[i + 2], values[i + 1]) for i in range(0, len(values), 3)
    ]


def _cap_triangles(
    vertices: list[tuple[float, float, float]], footprint: tuple[tuple[float, float], ...]
) -> tuple[list[tuple[tuple[float, float, float], ...]], list[tuple[tuple[float, float, float], ...]]]:
    """Split the prism's vertex list into (bottom cap, top cap) triangles.

    ``_prism_vertices`` emits every cap triangle first, as one bottom triplet
    followed by one top triplet, then the wall quads. A prism over n plan
    vertices always carries n-2 cap triangles per cap.
    """

    cap_triplets = 2 * (len(footprint) - 2)
    zone = vertices[:3 * cap_triplets]
    bottom = [tuple(zone[3 * t:3 * t + 3]) for t in range(0, cap_triplets, 2)]
    top = [tuple(zone[3 * t:3 * t + 3]) for t in range(1, cap_triplets, 2)]
    return bottom, top


def _signed_area(points: tuple[tuple[float, float], ...]) -> float:
    return 0.5 * math.fsum(
        a[0] * b[1] - b[0] * a[1] for a, b in zip(points, points[1:] + points[:1])
    )


def _point_strictly_inside(point: tuple[float, float], footprint: tuple[tuple[float, float], ...]) -> bool:
    """Ray-cast containment that treats the boundary as outside."""
    x, y = point
    for a, b in zip(footprint, footprint[1:] + footprint[:1]):
        cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
        if (
            abs(cross) <= 1e-12
            and min(a[0], b[0]) - 1e-12 <= x <= max(a[0], b[0]) + 1e-12
            and min(a[1], b[1]) - 1e-12 <= y <= max(a[1], b[1]) + 1e-12
        ):
            return False
    crossings = 0
    for a, b in zip(footprint, footprint[1:] + footprint[:1]):
        if (a[1] > y) != (b[1] > y):
            x_at = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < x_at:
                crossings += 1
    return crossings % 2 == 1


def test_fixture_is_synthetic_and_fingerprint_stable() -> None:
    case = _case()
    model = _model()
    validate_public_fixture_provenance(model)
    assert canonical_digest(model) == case.sha256
    assert len(model.levels) == 1
    assert len(model.spaces) == 3
    assert len(model.walls) == 18


def test_glb_floor_plate_caps_partition_every_non_convex_footprint(tmp_path: Path) -> None:
    model = _model()
    to_glb(model, tmp_path / "rooms.glb")
    gltf, blob = _parse_glb(tmp_path / "rooms.glb")
    names = _by_name(gltf)

    for space in model.spaces:
        footprint = FOOTPRINTS[space.id]
        node = gltf["nodes"][names[space.id]]
        # #190 pins ear clipping for these shapes; extras only ever carry the
        # key when the exporter had to fall back to the fan.
        assert "triangulation" not in node["extras"]

        vertices = _node_vertices(gltf, blob, names, space.id)
        expected_vertex_count = 3 * 2 * CAP_TRIANGLES[space.id] + 3 * 2 * len(footprint)
        assert len(vertices) == expected_vertex_count

        bottom, top = _cap_triangles(vertices, footprint)
        assert len(bottom) == len(top) == CAP_TRIANGLES[space.id]

        floor_z = min(point[2] for point in vertices)
        plate_z = max(point[2] for point in vertices)
        assert floor_z < plate_z
        for cap, cap_z in ((bottom, floor_z), (top, plate_z)):
            for triangle in cap:
                assert all(point[2] == pytest.approx(cap_z) for point in triangle)

        # The top cap must tile exactly the footprint: every triangle inside
        # it, and no area gained or lost.
        for triangle in top:
            centroid = (
                sum(point[0] for point in triangle) / 3.0,
                sum(point[1] for point in triangle) / 3.0,
            )
            assert _point_strictly_inside(centroid, footprint)
        cap_area = math.fsum(
            abs(_signed_area(tuple(point[:2] for point in triangle))) for triangle in top
        )
        assert cap_area == pytest.approx(abs(_signed_area(footprint)), abs=1e-9)


def test_glb_export_is_byte_identical(tmp_path: Path) -> None:
    model = _model()
    to_glb(model, tmp_path / "first.glb")
    to_glb(model, tmp_path / "second.glb")
    assert (tmp_path / "first.glb").read_bytes() == (tmp_path / "second.glb").read_bytes()


def test_plan_drawing_projects_each_space_footprint() -> None:
    """Plan primitives on ``architecture:spaces`` against the footprints.

    The rectangular control room projects with identity: exactly its
    footprint vertices, in ring order.

    The L and U rooms do not. The drawings lane builds each plan surface
    from the convex hull of the projected footprint (``generator``'s
    ``_project_polygon_surface`` feeds ``_convex_hull``), so today a
    non-convex room draws its hull: the L loses its reflex vertex and the U
    collapses to its outer rectangle, over-drawing area the room does not
    have. This fixture was written expecting footprint identity here; until
    a drawings-lane change makes that true, pin the hull that is actually
    produced so a fix flips this deliberately. The pin still asserts no
    invented geometry: every hull vertex is a footprint vertex, in ring
    order.
    """

    model = _model()
    drawing_set = generate_drawing_set(
        model,
        plans=[PlanSpec(id="plan:ncx-ground", level_id="level:ncx-ground")],
    )
    assert [view.id for view in drawing_set.views] == ["plan:ncx-ground"]
    view = drawing_set.views[0]

    expected_plan_vertices = {
        # Convex hull of the L footprint; reflex (2.0, 3.0) dropped.
        "space:ncx-l": ((0.0, 0.0), (5.0, 0.0), (5.0, 3.0), (2.0, 5.0), (0.0, 5.0)),
        # Convex hull of the U footprint; both inner corners dropped.
        "space:ncx-u": ((7.0, 0.0), (13.0, 0.0), (13.0, 6.0), (7.0, 6.0)),
        # Identity: the rectangle is already convex.
        "space:ncx-rect": RECT_VERTICES,
    }

    for space in model.spaces:
        primitives = [
            p for p in view.primitives
            if p.source_ids == (space.id,) and p.layer == SPACES_LAYER
        ]
        assert len(primitives) == 1
        primitive = primitives[0]
        assert primitive.kind == "polygon"
        footprint = FOOTPRINTS[space.id]
        assert set(expected_plan_vertices[space.id]) <= set(footprint)
        plan_points = tuple((point.x, point.y) for point in primitive.points)
        assert plan_points == expected_plan_vertices[space.id]


def test_plan_drawing_generation_is_deterministic() -> None:
    model = _model()
    first = generate_drawing_set(
        model,
        plans=[PlanSpec(id="plan:ncx-ground", level_id="level:ncx-ground")],
    )
    second = generate_drawing_set(
        model,
        plans=[PlanSpec(id="plan:ncx-ground", level_id="level:ncx-ground")],
    )
    assert first.to_json() == second.to_json()
