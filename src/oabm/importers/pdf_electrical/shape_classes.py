"""Per-page symbol shape-class table for electrical pages.

Device recognition on real sets starts from a per-sheet table of small
closed-shape classes: symbol-sized circles, triangles, rectangles, and
other polygons, grouped by kind, size, fill state, and stroke/fill gray.
A per-set parameter look then says which class is a receptacle, a data
outlet, a floor box, and so on.  This module builds that table
deterministically from a page's vector-path observations, the way the
architecture lane's stroke-style histogram does for line styles.

Everything here is read-only observation-level evidence.  The importer
does not call any of it, imported models are unchanged, and identical
pages give identical results.  Style values the source did not carry
stay ``None`` (the lane's vector metadata gains stroke gray, fill gray,
and line width only when that metadata exists) and never split a class
by an unknown axis.  Line width rides in the same metadata but is not a
class axis: the specified row has no width field, so two shapes that
differ only in stroke width are one class.

Two CAD-export realities shape the closure and exclusion rules.  Circles
arrive as open Bézier paths whose end meets their start, so a path whose
first and last points coincide within the endpoint gap tolerance counts
as closed alongside the closed flag.  Solid fills arrive as triangle
meshes, so a closed triangle that shares an edge with another closed
triangle on the page is a mesh cell, not a symbol, and stays out of the
table; :func:`triangle_mesh_diagnostic` counts the excluded mesh
triangles per page.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .importer import PdfElectricalDocument, PdfVectorPathObservation

# Paint operators that apply a fill (PDF 8.5.3): fill, the fill-and-stroke
# pairs, and the close-variants.
_FILL_OPERATORS = frozenset({"f", "F", "f*", "B", "B*", "b", "b*"})

# A flattened bezier path counts as a circle when its bbox is square within
# the larger of an absolute and a relative tolerance.
_CIRCLE_SQUARE_ABS_PT = 0.35
_CIRCLE_SQUARE_REL = 0.08

# A path whose first and last written points coincide within this gap is
# geometrically closed even when the paint operator does not close it: CAD
# exports draw circles as open Bézier paths whose end meets their start.
_ENDPOINT_MEET_TOL_PT = 0.3

# Two triangle corners within this tolerance name the same mesh vertex.  A
# closed triangle that shares two such corners (an edge) with another closed
# triangle on the page is a tessellated-fill cell, not a symbol.
_EDGE_MATCH_TOL_PT = 0.05

# An edge is axis-aligned when its shorter component stays within this.
_AXIS_TOL_PT = 1e-6

_MAX_SAMPLE_POSITIONS = 5


def _bucket(value: float, step: float) -> float:
    """The nearest multiple of ``step`` (deterministic half-even rounding).

    The final re-round snaps the product back onto the decimal grid, so a
    bucket value never carries binary-float noise and identical inputs always
    compare equal.
    """

    return round(round(value / step) * step, 10)


def _axis_key(value: float | None) -> tuple[int, float]:
    """Ascending sort key for one class axis, ``None`` before any number."""

    return (0, 0.0) if value is None else (1, value)


def _bbox_pt(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _endpoints_meet(points: tuple[tuple[float, float], ...]) -> bool:
    """Whether a path's first and last written points coincide."""

    (x0, y0), (x1, y1) = points[0], points[-1]
    return math.hypot(x1 - x0, y1 - y0) <= _ENDPOINT_MEET_TOL_PT


def _is_closed(vector: PdfVectorPathObservation) -> bool:
    """The closed flag, or endpoints that coincide within the gap tolerance."""

    return vector.closed or _endpoints_meet(vector.points_pt)


def _distinct_vertices(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Drop a closing vertex that repeats the start within the gap tolerance."""

    if len(points) > 2 and _endpoints_meet(points):
        return points[:-1]
    return points


def _is_axis_aligned_rectangle(vertices: tuple[tuple[float, float], ...]) -> bool:
    """Whether four vertices trace an axis-aligned rectangle."""

    corners = vertices + (vertices[0],)
    for (x0, y0), (x1, y1) in zip(corners, corners[1:]):
        if abs(x1 - x0) > _AXIS_TOL_PT and abs(y1 - y0) > _AXIS_TOL_PT:
            return False
    xs = sorted({vertex[0] for vertex in vertices})
    ys = sorted({vertex[1] for vertex in vertices})
    return len(xs) == 2 and len(ys) == 2


def _straight_kind(vertices: tuple[tuple[float, float], ...]) -> str | None:
    """Class kind for one closed straight path, or ``None`` to ignore it."""

    n = len(vertices)
    if n == 3:
        return "triangle"
    if n == 4:
        return "rectangle" if _is_axis_aligned_rectangle(vertices) else "polygon4"
    if n >= 5:
        return f"polygon{n}"
    return None


def _closed_triangle_vertices(
    vector: PdfVectorPathObservation,
) -> tuple[tuple[float, float], ...] | None:
    """The three corners of one closed straight triangle, or ``None``.

    Bézier-flattened paths are never straight triangles, and the same
    closure rule as the table applies: the closed flag or endpoints that
    meet within the gap tolerance.
    """

    if not _is_closed(vector):
        return None
    if vector.metadata.get("geometry_kind") == "bezier-flattened":
        return None
    vertices = _distinct_vertices(vector.points_pt)
    return vertices if len(vertices) == 3 else None


class _VertexClusters:
    """Canonical ids for vertices that coincide within the edge tolerance.

    Vertices arrive in document order, so the ids — and everything derived
    from them — are deterministic for a given page.
    """

    def __init__(self, tolerance: float) -> None:
        self._tolerance = tolerance
        self._grid: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
        self._count = 0

    def id_for(self, x: float, y: float) -> int:
        cell_x = math.floor(x / self._tolerance)
        cell_y = math.floor(y / self._tolerance)
        for grid_x in (cell_x - 1, cell_x, cell_x + 1):
            for grid_y in (cell_y - 1, cell_y, cell_y + 1):
                for point_x, point_y, point_id in self._grid.get((grid_x, grid_y), ()):
                    if math.hypot(point_x - x, point_y - y) <= self._tolerance:
                        return point_id
        point_id = self._count
        self._count += 1
        self._grid.setdefault((cell_x, cell_y), []).append((x, y, point_id))
        return point_id


def _mesh_triangle_indices(
    triangles: list[tuple[tuple[float, float], ...]],
) -> set[int]:
    """Positions of triangles that share an edge with another triangle.

    An edge is a pair of corners; two triangles share one when both corners
    of the edge match the other triangle's corners within the edge
    tolerance.  Mesh cells of a tessellated fill always do, isolated symbol
    triangles never do.
    """

    clusters = _VertexClusters(_EDGE_MATCH_TOL_PT)
    corner_ids = [
        tuple(clusters.id_for(x, y) for x, y in triangle) for triangle in triangles
    ]
    edge_owners: dict[tuple[int, int], set[int]] = {}
    for index, ids in enumerate(corner_ids):
        for first, second in ((ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[0])):
            edge = (first, second) if first < second else (second, first)
            edge_owners.setdefault(edge, set()).add(index)
    return {
        triangle_index
        for owners in edge_owners.values()
        if len(owners) > 1
        for triangle_index in owners
    }


def _classify(
    vector: PdfVectorPathObservation,
) -> tuple[str, tuple[float, float, float, float]] | None:
    """One shape ``(kind, bbox)`` from a vector observation, or ``None``.

    Circles are closed bezier-flattened paths with a square bbox; triangles,
    rectangles, and other polygons are closed straight paths with 3, 4, or
    more distinct vertices.  A path is closed when the observation says so
    or when its first and last points coincide within the gap tolerance, so
    CAD circles drawn as open Bézier paths are still detected.  Open paths
    and anything else are ignored.
    """

    if not _is_closed(vector):
        return None
    bbox = _bbox_pt(vector.points_pt)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    larger_side = max(width, height)
    if vector.metadata.get("geometry_kind") == "bezier-flattened":
        tolerance = max(_CIRCLE_SQUARE_ABS_PT, _CIRCLE_SQUARE_REL * larger_side)
        if abs(width - height) <= tolerance:
            return "circle", bbox
        return None
    kind = _straight_kind(_distinct_vertices(vector.points_pt))
    if kind is None:
        return None
    return kind, bbox


def _style_gray(metadata: Any, key: str, gray_step: float) -> float | None:
    """The bucketed gray one metadata key carries, or ``None`` when absent."""

    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return _bucket(value, gray_step)


@dataclass(frozen=True, slots=True)
class ShapeClass:
    """One class of symbol-sized closed shapes on one page.

    ``size_pt`` is the bbox's larger side bucketed to ``size_step_pt``.
    The gray values are bucketed metadata values, or ``None`` when the
    source carried no gray.  ``sample_positions`` holds up to five bbox
    centres, deterministically the first five by ``(y, x)``.
    """

    kind: str
    size_pt: float
    filled: bool
    stroke_gray: float | None
    fill_gray: float | None
    count: int
    sample_positions: tuple[tuple[float, float], ...]

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready mapping for a compact shape-class summary."""

        return {
            "kind": self.kind,
            "size_pt": self.size_pt,
            "filled": self.filled,
            "stroke_gray": self.stroke_gray,
            "fill_gray": self.fill_gray,
            "count": self.count,
            "sample_positions": [[x, y] for x, y in self.sample_positions],
        }


def _class_key(shape_class: ShapeClass) -> tuple:
    return (
        shape_class.kind,
        shape_class.size_pt,
        shape_class.filled,
        _axis_key(shape_class.stroke_gray),
        _axis_key(shape_class.fill_gray),
    )


@dataclass(frozen=True, slots=True)
class TriangleMeshDiagnostic:
    """Why some closed triangles are missing from a page's shape-class table.

    CAD solid fills are exported as triangle meshes: every mesh cell shares
    an edge (two corners within the edge tolerance) with another closed
    triangle on the page, while an isolated symbol triangle shares none.
    The exclusion is size-independent — it applies to every closed straight
    triangle on the page, regardless of the table's size bounds — and it
    never touches bezier-flattened paths or 4+-vertex polygons.

    The counts are deterministic observation evidence, the same way the
    table itself is.
    """

    closed_triangles_on_page: int
    mesh_triangles_excluded: int
    symbol_triangles_on_page: int

    def to_dict(self) -> dict[str, int]:
        """A JSON-ready mapping for a compact shape-class summary."""

        return {
            "closed_triangles_on_page": self.closed_triangles_on_page,
            "mesh_triangles_excluded": self.mesh_triangles_excluded,
            "symbol_triangles_on_page": self.symbol_triangles_on_page,
        }


def _validate_page(document: PdfElectricalDocument, page: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int):
        raise ValueError("page must be an int")
    if page < 1:
        raise ValueError("page must be >= 1")
    if page > document.page_count:
        raise ValueError(f"page {page} out of range; document holds {document.page_count} pages")


def triangle_mesh_diagnostic(
    document: PdfElectricalDocument,
    page: int,
) -> TriangleMeshDiagnostic:
    """Count one page's closed triangles and how many are mesh cells.

    The counts document the tessellated-fill exclusion behind
    :func:`symbol_shape_classes`: ``closed_triangles_on_page`` is every
    closed straight triangle on the page, ``mesh_triangles_excluded`` the
    ones excluded for sharing an edge with another closed triangle, and
    ``symbol_triangles_on_page`` the ones that remain symbol candidates.
    """

    _validate_page(document, page)
    triangles = [
        vertices
        for vector in document.vectors
        if vector.page == page
        for vertices in (_closed_triangle_vertices(vector),)
        if vertices is not None
    ]
    mesh = _mesh_triangle_indices(triangles)
    return TriangleMeshDiagnostic(
        closed_triangles_on_page=len(triangles),
        mesh_triangles_excluded=len(mesh),
        symbol_triangles_on_page=len(triangles) - len(mesh),
    )


def symbol_shape_classes(
    document: PdfElectricalDocument,
    page: int,
    *,
    min_size_pt: float = 1.0,
    max_size_pt: float = 120.0,
    size_step_pt: float = 0.5,
    gray_step: float = 0.05,
) -> tuple[ShapeClass, ...]:
    """Group one page's vector paths into symbol shape classes.

    One :class:`ShapeClass` per ``(kind, size bucket, filled, stroke gray
    bucket, fill gray bucket)`` over the page's closed shapes: circles
    (closed bezier-flattened paths with a square bbox), triangles and
    rectangles (closed straight 3- and 4-vertex paths; only axis-aligned
    4-gons are rectangles), and other polygons (``polygon<n>``).  A path is
    closed when the observation says so or when its first and last points
    coincide within the endpoint gap tolerance, so CAD circles drawn as
    open Bézier paths are still detected.  Open paths, non-square bezier
    paths, and shapes whose raw bbox larger side falls outside
    ``[min_size_pt, max_size_pt]`` are ignored.

    Tessellated fills are excluded: a closed triangle that shares an edge
    with another closed triangle on the page is a mesh cell of a CAD solid
    fill, not a symbol, and never enters the table.  :func:`triangle_mesh_diagnostic`
    documents that exclusion with per-page counts.

    Sizes round half-even to ``size_step_pt`` and grays to ``gray_step``;
    a gray the source did not carry stays ``None`` and never splits a
    class.  Classes sort by ``count`` descending, then by the class key
    ascending with ``None`` before any number, so identical pages give an
    identical tuple.
    """

    _validate_page(document, page)
    if not math.isfinite(min_size_pt) or not math.isfinite(max_size_pt):
        raise ValueError("size bounds must be finite")
    if not 0 <= min_size_pt <= max_size_pt:
        raise ValueError("size bounds must satisfy 0 <= min_size_pt <= max_size_pt")
    if not math.isfinite(size_step_pt) or size_step_pt <= 0:
        raise ValueError("size_step_pt must be > 0")
    if not math.isfinite(gray_step) or gray_step <= 0:
        raise ValueError("gray_step must be > 0")

    page_vectors = [vector for vector in document.vectors if vector.page == page]
    triangle_entries: list[tuple[int, tuple[tuple[float, float], ...]]] = []
    for vector_index, vector in enumerate(page_vectors):
        vertices = _closed_triangle_vertices(vector)
        if vertices is not None:
            triangle_entries.append((vector_index, vertices))
    mesh_vector_indices = {
        triangle_entries[position][0]
        for position in _mesh_triangle_indices(
            [vertices for _, vertices in triangle_entries]
        )
    }

    buckets: dict[
        tuple[str, float, bool, float | None, float | None],
        list[tuple[float, float]],
    ] = {}
    for vector_index, vector in enumerate(page_vectors):
        if vector_index in mesh_vector_indices:
            continue
        classified = _classify(vector)
        if classified is None:
            continue
        kind, bbox = classified
        raw_size = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if not min_size_pt <= raw_size <= max_size_pt:
            continue
        metadata = vector.metadata
        paint_operator = metadata.get("paint_operator")
        filled = paint_operator in _FILL_OPERATORS
        centre = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
        key = (
            kind,
            _bucket(raw_size, size_step_pt),
            filled,
            _style_gray(metadata, "stroke_gray", gray_step),
            _style_gray(metadata, "fill_gray", gray_step),
        )
        buckets.setdefault(key, []).append(centre)

    return tuple(
        sorted(
            (
                ShapeClass(
                    kind=key[0],
                    size_pt=key[1],
                    filled=key[2],
                    stroke_gray=key[3],
                    fill_gray=key[4],
                    count=len(centres),
                    sample_positions=tuple(
                        sorted(centres, key=lambda centre: (centre[1], centre[0]))
                        [:_MAX_SAMPLE_POSITIONS]
                    ),
                )
                for key, centres in buckets.items()
            ),
            key=lambda shape_class: (-shape_class.count, _class_key(shape_class)),
        )
    )
