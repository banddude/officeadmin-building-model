"""Bounded deterministic producer for a rectangular horizontal T-bar ceiling.

This module consumes an already reviewed canonical ``Ceiling`` and an existing
canonical ``Slab``.  It does not read drawings, create a deck, infer joists,
select anchors/hardware, perform structural or code design, or regenerate
dependent routes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import re
from typing import Any

from oabm.assembly_geometry import GeometryInputError, extrude_simple_section_m
from oabm.model import (
    ASSEMBLY_MAX_COMPONENTS,
    ASSEMBLY_SCHEMA_VERSIONS,
    AssemblyComponent,
    BuildingAssembly,
    BuildingModel,
    Ceiling,
    ContractError,
    HangerAttachment,
    PanelGeometry,
    Point3,
    Polygon3D,
    Polyline3D,
    Pose,
    Profile2D,
    ProfileExtrusion,
    Provenance,
    Quaternion,
    ResolvedParameter,
    Slab,
    Vector3,
    WireSweep,
    compute_assembly_digests,
    point_on_component_surface,
    point_on_slab_surface,
    stable_assembly_component_id,
    upgrade_to_v1_6,
)

from .catalog import ParameterDefinition
from .suspended_ceiling_catalog import (
    DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
    SUSPENDED_CEILING_GENERATOR_VERSION,
    SUSPENDED_CEILING_PARAMETER_IDS,
    SUSPENDED_CEILING_PRESET_ID,
    CEILING_DATUM_OFFSET_KEY,
    SuspendedCeilingPreset,
    SuspendedCeilingPresetCatalog,
    _require_reserved_builtin_identities,
)


_TOLERANCE_M = 1e-9
_ANGLE_TOLERANCE = 1e-9
_CANONICAL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_MAX_PART_KEY_LENGTH = 2048
_MAX_PARAMETER_OVERRIDES = len(SUSPENDED_CEILING_PARAMETER_IDS)
_GLB_ASSEMBLY_VERTEX_LIMIT = 250_000  # Must match exports.gltf._MAX_ASSEMBLY_PREVIEW_VERTICES.
_GLB_CYLINDER_SIDES = 10  # Must match exports.gltf._CYLINDER_SIDES.
_PROFILE_OUTLINE_VERTICES = 8  # Profile2D(kind="t_profile") has eight outline points.
_TILE_BOUNDARY_VERTICES = 4

_LAYOUT_PARAMETER_IDS = (
    "ceiling.grid_origin_x_m",
    "ceiling.grid_origin_y_m",
    "ceiling.grid_axis_angle_rad",
    "ceiling.edge_policy",
)
_SOURCE_THICKNESS_PARAMETER_ID = "ceiling.source_thickness_m"
_TILE_TOP_OFFSET_PARAMETER_ID = CEILING_DATUM_OFFSET_KEY
_SUPPORTED_EDGE_POLICY = "cut_tiles_without_perimeter_angle_v1"


class SuspendedCeilingPresetError(ContractError):
    """The finite selected ceiling preset cannot resolve without guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CeilingParameterOverride:
    """One explicit caller-authored value, distinct from preset assumptions."""

    parameter_id: str
    value_kind: str
    value: float | int | str | bool
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.parameter_id, str) or not self.parameter_id.strip():
            raise SuspendedCeilingPresetError("ceiling parameter override requires a parameter_id")
        if not isinstance(self.value_kind, str) or not self.value_kind.strip():
            raise SuspendedCeilingPresetError(f"{self.parameter_id} override requires value_kind")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise SuspendedCeilingPresetError(f"{self.parameter_id} override requires unit")


@dataclass(frozen=True, slots=True, kw_only=True)
class SuspendedCeilingAssemblyRequest:
    """Explicit agent-selected host, deck, grid origin/axis and edge policy."""

    assembly_id: str
    ceiling_id: str
    deck_id: str
    selected_by: str
    grid_origin_x_m: float
    grid_origin_y_m: float
    grid_axis_angle_rad: float
    edge_policy: str
    preset_id: str = SUSPENDED_CEILING_PRESET_ID
    parameter_overrides: tuple[CeilingParameterOverride, ...] = ()

    def __post_init__(self) -> None:
        for label in ("assembly_id", "ceiling_id", "deck_id", "selected_by", "preset_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not _CANONICAL_ID.fullmatch(value):
                raise SuspendedCeilingPresetError(
                    f"SuspendedCeilingAssemblyRequest.{label} must be a canonical ID"
                )
        for label in ("grid_origin_x_m", "grid_origin_y_m", "grid_axis_angle_rad"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SuspendedCeilingPresetError(f"{label} must be finite numeric input")
        if abs(self.grid_axis_angle_rad) > 2.0 * math.pi:
            raise SuspendedCeilingPresetError("grid_axis_angle_rad must be within one turn (±2π rad)")
        if self.edge_policy != _SUPPORTED_EDGE_POLICY:
            raise SuspendedCeilingPresetError(
                f"edge_policy must explicitly be {_SUPPORTED_EDGE_POLICY!r}; other boundary policies are unsupported"
            )
        if not isinstance(self.parameter_overrides, tuple):
            raise SuspendedCeilingPresetError("parameter_overrides must be an immutable tuple")
        # The finite catalog has nine recipe fields plus the special placement
        # offset. Reject oversized input before scanning or materializing IDs.
        if len(self.parameter_overrides) > _MAX_PARAMETER_OVERRIDES:
            raise SuspendedCeilingPresetError(
                f"parameter override count exceeds the finite v1 limit {_MAX_PARAMETER_OVERRIDES}"
            )
        if any(not isinstance(row, CeilingParameterOverride) for row in self.parameter_overrides):
            raise SuspendedCeilingPresetError("parameter_overrides must contain typed ceiling overrides")
        ids = tuple(row.parameter_id for row in self.parameter_overrides)
        if len(ids) != len(set(ids)):
            raise SuspendedCeilingPresetError("ceiling parameter overrides must have unique IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class SuspendedCeilingPresetResolution:
    catalog: SuspendedCeilingPresetCatalog
    preset: SuspendedCeilingPreset
    selected_by: str
    parameters: tuple[ResolvedParameter, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, tuple) or any(
                not isinstance(row, ResolvedParameter) for row in self.parameters):
            raise SuspendedCeilingPresetError("resolved parameters must be an immutable tuple")
        expected = ((_SOURCE_THICKNESS_PARAMETER_ID,) if any(
            row.parameter_id == _SOURCE_THICKNESS_PARAMETER_ID for row in self.parameters
        ) else ()) + _LAYOUT_PARAMETER_IDS + SUSPENDED_CEILING_PARAMETER_IDS
        if tuple(row.parameter_id for row in self.parameters) != expected:
            raise SuspendedCeilingPresetError("ceiling parameter snapshot is incomplete or out of canonical order")

    def parameter_map(self) -> dict[str, ResolvedParameter]:
        return {row.parameter_id: row for row in self.parameters}


@dataclass(frozen=True, slots=True)
class _RectangleFrame:
    """A strict horizontal rectangle represented in an orthonormal local frame."""

    origin: Point3
    axis_u: tuple[float, float]
    axis_v: tuple[float, float]
    width_m: float
    height_m: float
    z_m: float
    polygon_xy: tuple[tuple[float, float], ...]

    def to_local(self, x: float, y: float, *, origin_x: float, origin_y: float,
                 grid_u: tuple[float, float], grid_v: tuple[float, float]) -> tuple[float, float]:
        dx, dy = x - origin_x, y - origin_y
        return dx * grid_u[0] + dy * grid_u[1], dx * grid_v[0] + dy * grid_v[1]


@dataclass(frozen=True, slots=True)
class _GridBounds:
    x_low: float
    x_high: float
    y_low: float
    y_high: float
    x_index_low: int
    x_index_high: int
    y_index_low: int
    y_index_high: int

    @property
    def main_count(self) -> int:
        return self.x_index_high - self.x_index_low + 1

    @property
    def cross_row_count(self) -> int:
        return self.y_index_high - self.y_index_low + 1


def _parameter_evidence_id(catalog: SuspendedCeilingPresetCatalog,
                           preset: SuspendedCeilingPreset,
                           parameter_id: str) -> str:
    return json.dumps(
        (catalog.catalog_revision, preset.preset_id, preset.preset_revision, parameter_id),
        separators=(",", ":"), ensure_ascii=False,
    )


def _user_parameter(parameter_id: str, value_kind: str, value: Any, unit: str,
                    selected_by: str, *, note: str | None = None) -> ResolvedParameter:
    attributes = {} if note is None else {"note": note}
    return ResolvedParameter(
        parameter_id=parameter_id,
        value_kind=value_kind,
        value=value,
        unit=unit,
        origin="user_override",
        provenance=(Provenance(
            source_kind="user_override",
            source_id=selected_by,
            source_element_id=parameter_id,
            method="explicit-suspended-ceiling-layout-input",
            confidence=1.0,
            derivation="user",
            scope_paths=("value", "unit", "origin"),
            attributes=attributes,
        ),),
        source_entity_id=None,
        source_field_path=None,
    )


def _preset_parameter(catalog: SuspendedCeilingPresetCatalog,
                      preset: SuspendedCeilingPreset, definition: ParameterDefinition,
                      value: float, *, case: str | None = None,
                      formula: str | None = None,
                      source_entity_id: str | None = None,
                      source_parameter_id: str | None = None) -> ResolvedParameter:
    attributes: dict[str, Any] = {
        "default_basis": definition.default_basis,
        "source_id": definition.source_id,
        "note": definition.note,
    }
    if case is not None:
        attributes["placement_case"] = case
    if formula is not None:
        attributes["default_formula"] = formula
    if source_entity_id is not None:
        attributes["source_entity_id"] = source_entity_id
        attributes["source_field_path"] = "thickness_m"
    if source_parameter_id is not None:
        attributes["source_parameter_id"] = source_parameter_id
    return ResolvedParameter(
        parameter_id=definition.parameter_id,
        value_kind=definition.value_kind,
        value=value,
        unit=definition.unit,
        origin="preset_default",
        provenance=(Provenance(
            source_kind="preset_parameter",
            source_id=catalog.catalog_id,
            source_element_id=_parameter_evidence_id(catalog, preset, definition.parameter_id),
            method="immutable-ceiling-catalog-default-resolution",
            confidence=1.0,
            derivation="inferred",
            scope_paths=("value", "unit", "origin"),
            attributes=attributes,
        ),),
        source_entity_id=None,
        source_field_path=None,
    )


def _observed_source_parameter(source: Ceiling, source_record: Provenance) -> ResolvedParameter:
    return ResolvedParameter(
        parameter_id=_SOURCE_THICKNESS_PARAMETER_ID,
        value_kind="length",
        value=float(source.thickness_m),
        unit="m",
        origin="plan_observed",
        provenance=(Provenance(
            source_kind=source_record.source_kind,
            source_id=source_record.source_id,
            source_element_id=source_record.source_element_id,
            page=source_record.page,
            method=source_record.method,
            confidence=source_record.confidence,
            derivation="observed",
            scope_paths=("value", "unit", "origin", "source_entity_id", "source_field_path"),
            attributes={**source_record.attributes, "canonical_source_field": "thickness_m"},
        ),),
        source_entity_id=source.id,
        source_field_path="thickness_m",
    )


def _validate_override_value(definition: ParameterDefinition, value: object,
                             label: str, *, signed: bool = False,
                             signed_minimum: float = -10.0,
                             signed_maximum: float = 10.0) -> None:
    if definition.value_kind == "length":
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(float(value)) and (signed or value >= 0))
    elif definition.value_kind == "count":
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    elif definition.value_kind == "token":
        valid = isinstance(value, str) and bool(value.strip())
    elif definition.value_kind == "boolean":
        valid = isinstance(value, bool)
    else:
        valid = False
    if not valid:
        qualifier = "finite signed length" if signed else "finite nonnegative length"
        raise SuspendedCeilingPresetError(f"{label} must be a {qualifier}")
    if not signed:
        if definition.minimum is not None and value < definition.minimum:
            raise SuspendedCeilingPresetError(
                f"{label} is below the catalog minimum {definition.minimum!r}"
            )
        if definition.maximum is not None and value > definition.maximum:
            raise SuspendedCeilingPresetError(
                f"{label} exceeds the catalog maximum {definition.maximum!r}"
            )
    elif float(value) < signed_minimum or float(value) > signed_maximum:
        raise SuspendedCeilingPresetError(
            f"{label} is outside the bounded placement-offset range "
            f"[{signed_minimum!r}, {signed_maximum!r}] m"
        )


def resolve_suspended_ceiling_preset(
    request: SuspendedCeilingAssemblyRequest,
    ceiling: Ceiling,
    *,
    catalog: SuspendedCeilingPresetCatalog = DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
) -> SuspendedCeilingPresetResolution:
    """Resolve one selected preset, preserving observed/default/user values."""
    if not isinstance(request, SuspendedCeilingAssemblyRequest):
        raise SuspendedCeilingPresetError("request must be a typed SuspendedCeilingAssemblyRequest")
    if not isinstance(ceiling, Ceiling):
        raise SuspendedCeilingPresetError("ceiling must be a typed canonical Ceiling")
    if not isinstance(catalog, SuspendedCeilingPresetCatalog):
        raise SuspendedCeilingPresetError("catalog must be a typed immutable ceiling preset catalog")
    try:
        _require_reserved_builtin_identities(catalog)
    except ContractError as exc:
        raise SuspendedCeilingPresetError(str(exc)) from exc
    try:
        preset = catalog.suspended_ceiling_preset(request.preset_id)
    except ContractError as exc:
        raise SuspendedCeilingPresetError(str(exc)) from exc
    definitions = {row.parameter_id: row for row in preset.parameters}
    definitions[_TILE_TOP_OFFSET_PARAMETER_ID] = ParameterDefinition(
        parameter_id=preset.tile_top_offset_parameter_id,
        value_kind="length",
        default_value=0.0,
        unit=preset.tile_top_offset_unit,
        default_basis="illustrative_modeling_assumption",
        source_id=None,
        note=preset.tile_top_offset_note,
        minimum=0.0,
        maximum=preset.tile_top_offset_maximum_m,
    )
    overrides = {row.parameter_id: row for row in request.parameter_overrides}
    unknown = set(overrides) - set(definitions)
    if unknown:
        raise SuspendedCeilingPresetError(
            f"unsupported suspended-ceiling parameter overrides: {sorted(unknown)!r}"
        )

    resolved: dict[str, ResolvedParameter] = {}
    for parameter_id in SUSPENDED_CEILING_PARAMETER_IDS:
        definition = definitions[parameter_id]
        override = overrides.get(parameter_id)
        if override is not None:
            if (override.value_kind != definition.value_kind or override.unit != definition.unit):
                raise SuspendedCeilingPresetError(
                    f"{parameter_id} override must be {definition.value_kind} in {definition.unit}"
                )
            _validate_override_value(
                definition, override.value, f"{parameter_id} override",
                signed=(parameter_id == _TILE_TOP_OFFSET_PARAMETER_ID),
                signed_minimum=preset.tile_top_offset_minimum_m,
                signed_maximum=preset.tile_top_offset_maximum_m,
            )
            parameter = _user_parameter(
                parameter_id, definition.value_kind, override.value, definition.unit,
                request.selected_by,
                note=("Explicit tile-top offset from Ceiling top face."
                      if parameter_id == _TILE_TOP_OFFSET_PARAMETER_ID else None),
            )
        else:
            if parameter_id == _TILE_TOP_OFFSET_PARAMETER_ID:
                if ceiling.thickness_m is not None:
                    default_value = float(preset.tile_top_offset_with_observed_thickness_m)
                    default_case = "observed_ceiling_thickness_align_tile_top_to_source_top"
                    default_formula = "preset.tile_top_offset_with_observed_thickness_m = 0 m"
                else:
                    # The immutable preset declares the formula. Resolve it
                    # from the already-resolved values so any caller override
                    # to tile/flange thickness remains internally coherent.
                    default_value = float(resolved["ceiling.tile.thickness_m"].value +
                                          resolved["ceiling.runner.flange_thickness_m"].value)
                    default_case = "missing_ceiling_thickness_place_flange_bottom_on_source_datum"
                    default_formula = preset.tile_top_offset_without_thickness_formula
                parameter = _preset_parameter(
                    catalog, preset, definition, default_value, case=default_case,
                    formula=default_formula, source_entity_id=ceiling.id,
                    source_parameter_id=(
                        _SOURCE_THICKNESS_PARAMETER_ID if ceiling.thickness_m is not None else None
                    ),
                )
            else:
                parameter = _preset_parameter(
                    catalog, preset, definition, float(definition.default_value),
                )
        resolved[parameter_id] = parameter

    # Input geometry must be observed before deriving a physical recipe. A
    # scope-limited name observation cannot authorize the ceiling/deck bounds.
    _require_source_geometry_evidence(ceiling, "Ceiling")

    layout_parameters = (
        _user_parameter("ceiling.grid_origin_x_m", "length", float(request.grid_origin_x_m),
                        "m", request.selected_by),
        _user_parameter("ceiling.grid_origin_y_m", "length", float(request.grid_origin_y_m),
                        "m", request.selected_by),
        _user_parameter("ceiling.grid_axis_angle_rad", "angle",
                        float(request.grid_axis_angle_rad), "rad", request.selected_by,
                        note="Caller-specified horizontal grid axis; it must align to a source rectangle axis."),
        _user_parameter("ceiling.edge_policy", "token", request.edge_policy,
                        "1", request.selected_by,
                        note="Cut perimeter tiles at the host boundary; no perimeter angle/support is modeled."),
    )

    source_parameters: tuple[ResolvedParameter, ...] = ()
    if ceiling.thickness_m is not None:
        source_record = next((row for row in ceiling.provenance
                              if row.derivation == "observed" and
                              (row.scope_paths is None or "thickness_m" in row.scope_paths)), None)
        if source_record is None:
            raise SuspendedCeilingPresetError(
                f"{ceiling.id}.thickness_m has no field-qualified observed source evidence"
            )
        source_parameters = (_observed_source_parameter(ceiling, source_record),)

    parameters = (*source_parameters, *layout_parameters,
                  *(resolved[row] for row in SUSPENDED_CEILING_PARAMETER_IDS))
    if len(parameters) > 128:
        raise SuspendedCeilingPresetError("resolved ceiling parameters exceed the canonical 128-record cap")
    return SuspendedCeilingPresetResolution(
        catalog=catalog, preset=preset, selected_by=request.selected_by,
        parameters=tuple(parameters),
    )


def _require_source_geometry_evidence(source: Ceiling | Slab, label: str) -> None:
    if isinstance(source, Ceiling):
        required = {"level_id", "footprint"}
        if source.thickness_m is not None:
            required.add("thickness_m")
    else:
        required = {"level_id", "footprint", "thickness_m"}
    observed = tuple(row for row in source.provenance if row.derivation == "observed")
    if not observed:
        raise SuspendedCeilingPresetError(f"{source.id} has no observed source provenance for {label} geometry")
    if any(row.scope_paths is None for row in observed):
        return
    covered = {path for row in observed for path in (row.scope_paths or ())}
    missing = required - covered
    if missing:
        raise SuspendedCeilingPresetError(
            f"{source.id} observed {label} provenance does not cover geometry field(s): {', '.join(sorted(missing))}"
        )


def _rectangle_frame(source: Ceiling | Slab, label: str) -> _RectangleFrame:
    points = source.footprint.points
    if len(points) != 4:
        raise SuspendedCeilingPresetError(
            f"unsupported {label}: only a four-corner rectangular horizontal footprint is supported"
        )
    z_values = tuple(point.z for point in points)
    if max(z_values) - min(z_values) > _TOLERANCE_M:
        raise SuspendedCeilingPresetError(f"unsupported {label}: sloped footprints are not supported")
    edges = tuple((points[(index + 1) % 4].x - points[index].x,
                   points[(index + 1) % 4].y - points[index].y)
                  for index in range(4))
    lengths = tuple(math.hypot(x, y) for x, y in edges)
    if any(not math.isfinite(length) or length <= _TOLERANCE_M for length in lengths):
        raise SuspendedCeilingPresetError(f"unsupported {label}: rectangle edges must have positive finite lengths")
    if any(abs(lengths[index] - lengths[index + 2]) > _TOLERANCE_M
           for index in range(2)):
        raise SuspendedCeilingPresetError(f"unsupported {label}: opposite rectangular edges must match")
    ux, uy = edges[0][0] / lengths[0], edges[0][1] / lengths[0]
    vx, vy = -uy, ux
    dot = ux * edges[1][0] / lengths[1] + uy * edges[1][1] / lengths[1]
    if abs(dot) > 1e-9:
        raise SuspendedCeilingPresetError(f"unsupported {label}: footprint must be a rectangle, not a skew polygon")
    if (edges[2][0] / lengths[2] + ux)**2 + (edges[2][1] / lengths[2] + uy)**2 > 1e-18:
        raise SuspendedCeilingPresetError(f"unsupported {label}: opposite rectangular edges must be parallel")
    e1x, e1y = edges[1][0] / lengths[1], edges[1][1] / lengths[1]
    if ((edges[3][0] / lengths[3] + e1x)**2 +
            (edges[3][1] / lengths[3] + e1y)**2 > 1e-18):
        raise SuspendedCeilingPresetError(f"unsupported {label}: opposite rectangular edges must be parallel")
    polygon = tuple((point.x, point.y) for point in points)
    try:
        extrude_simple_section_m(polygon, 1.0, ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
    except GeometryInputError as exc:
        raise SuspendedCeilingPresetError(f"unsupported {label}: invalid footprint polygon: {exc}") from exc
    # Validate the remaining pair of edges against the selected local frame,
    # including the clockwise-ring case, without altering source coordinates.
    local = tuple((point.x - points[0].x, point.y - points[0].y) for point in points)
    local_x = tuple(row[0] * ux + row[1] * uy for row in local)
    local_y = tuple(row[0] * vx + row[1] * vy for row in local)
    x_span = max(local_x) - min(local_x)
    y_span = max(local_y) - min(local_y)
    if (not math.isfinite(x_span) or not math.isfinite(y_span) or
            x_span <= _TOLERANCE_M or y_span <= _TOLERANCE_M):
        raise SuspendedCeilingPresetError(f"unsupported {label}: rectangle spans must be finite and positive")
    expected = {(0, 0), (1, 0), (1, 1), (0, 1)}
    normalized = {(round((x - min(local_x)) / x_span), round((y - min(local_y)) / y_span))
                  for x, y in zip(local_x, local_y)}
    if normalized != expected:
        raise SuspendedCeilingPresetError(f"unsupported {label}: footprint is not a strict rectangle")
    return _RectangleFrame(
        origin=points[0], axis_u=(ux, uy), axis_v=(vx, vy),
        width_m=x_span, height_m=y_span, z_m=z_values[0], polygon_xy=polygon,
    )


def _point_in_polygon(point: tuple[float, float], polygon: tuple[tuple[float, float], ...]) -> bool:
    x, y = point
    inside = False
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length2 = dx * dx + dy * dy
        if length2 > 0:
            fraction = max(0.0, min(1.0, ((x - first[0]) * dx + (y - first[1]) * dy) / length2))
            if math.hypot(x - first[0] - fraction * dx, y - first[1] - fraction * dy) <= _TOLERANCE_M:
                return True
        if (first[1] > y) != (second[1] > y):
            cross_x = first[0] + (second[0] - first[0]) * (y - first[1]) / (second[1] - first[1])
            if x < cross_x:
                inside = not inside
    return inside


def _require_host_inside_deck(ceiling: _RectangleFrame, deck: _RectangleFrame, slab: Slab) -> None:
    for point in ceiling.polygon_xy:
        if not _point_in_polygon(point, deck.polygon_xy):
            raise SuspendedCeilingPresetError(
                f"Ceiling host {point!r} lies outside selected deck Slab {slab.id}"
            )


def _lattice_indices(low: float, high: float, origin: float, spacing: float,
                     half_width: float, label: str) -> tuple[int, int, int]:
    first = math.ceil((low + half_width - origin - _TOLERANCE_M) / spacing)
    last = math.floor((high - half_width - origin + _TOLERANCE_M) / spacing)
    count = last - first + 1
    if count < 2:
        raise SuspendedCeilingPresetError(
            f"unsupported grid: at least two full {label} T runners must fit inside the source Ceiling boundary"
        )
    # Guard gigantic coordinate ranges before constructing a range, tuple, or
    # any component-sized collection.
    if count > ASSEMBLY_MAX_COMPONENTS:
        raise SuspendedCeilingPresetError(
            f"component_cap_exceeded: predicted {label} runner lines alone exceed {ASSEMBLY_MAX_COMPONENTS}"
        )
    return first, last, count


def _validate_profile_parameters(values: dict[str, float]) -> Profile2D:
    width = values["ceiling.runner.profile_width_m"]
    depth = values["ceiling.runner.profile_depth_m"]
    web = values["ceiling.runner.web_thickness_m"]
    flange = values["ceiling.runner.flange_thickness_m"]
    spacing = values["ceiling.module_spacing_m"]
    tile_thickness = values["ceiling.tile.thickness_m"]
    tile_edge_clearance = values["ceiling.tile.edge_clearance_m"]
    wire_diameter = values["ceiling.hanger.wire_diameter_m"]
    if spacing <= width + _TOLERANCE_M:
        raise SuspendedCeilingPresetError("grid module spacing must exceed the complete tee flange width")
    if web >= width or flange >= depth or tile_thickness <= _TOLERANCE_M or wire_diameter <= _TOLERANCE_M:
        raise SuspendedCeilingPresetError("selected T profile, tile and wire dimensions are inconsistent")
    if tile_edge_clearance < 0 or tile_edge_clearance >= width / 2.0 - _TOLERANCE_M:
        raise SuspendedCeilingPresetError(
            "tile edge clearance must be smaller than half the T flange width to retain positive bearing overlap"
        )
    try:
        return Profile2D(
            kind="t_profile", overall_width_m=width, overall_depth_m=depth,
            web_thickness_m=web, flange_thickness_m=flange,
        )
    except ContractError as exc:
        raise SuspendedCeilingPresetError(f"selected simple T profile is invalid: {exc}") from exc


def _quat_for_runner(angle: float) -> Quaternion:
    """Map local (profile-width, profile-height, extrusion) to (-/+ across, up, run)."""
    cosine, sine = math.cos(angle / 2.0), math.sin(angle / 2.0)
    # qz(angle) * (0.5, 0.5, 0.5, 0.5); the latter maps local z to +X,
    # local x to +Y, and local y to +Z.
    return Quaternion(
        x=0.5 * (cosine - sine),
        y=0.5 * (cosine + sine),
        z=0.5 * (cosine + sine),
        w=0.5 * (cosine - sine),
    )


def _rotate(q: Quaternion, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    vx, vy, vz = vector
    tx = 2.0 * (q.y * vz - q.z * vy)
    ty = 2.0 * (q.z * vx - q.x * vz)
    tz = 2.0 * (q.x * vy - q.y * vx)
    return (
        vx + q.w * tx + q.y * tz - q.z * ty,
        vy + q.w * ty + q.z * tx - q.x * tz,
        vz + q.w * tz + q.x * ty - q.y * tx,
    )


def _world_grid_point(x: float, y: float, z: float, origin_x: float, origin_y: float,
                      axis_u: tuple[float, float], axis_v: tuple[float, float]) -> Point3:
    return Point3(
        x=origin_x + axis_u[0] * x + axis_v[0] * y,
        y=origin_y + axis_u[1] * x + axis_v[1] * y,
        z=z,
    )


def _profile_pose_start(*, x: float, y: float, z: float, run_angle: float,
                        origin_x: float, origin_y: float,
                        axis_u: tuple[float, float], axis_v: tuple[float, float],
                        profile_width_m: float) -> Pose:
    # The local T outline spans x=[0,width]. Translate its section origin by
    # half a face width so the runner centerline lies on the requested grid.
    center = _world_grid_point(x, y, z, origin_x, origin_y, axis_u, axis_v)
    across = (-math.sin(run_angle), math.cos(run_angle))
    return Pose(
        position=Point3(
            x=center.x - across[0] * profile_width_m / 2.0,
            y=center.y - across[1] * profile_width_m / 2.0,
            z=z,
        ),
        rotation=_quat_for_runner(run_angle),
    )


def _component_provenance(catalog: SuspendedCeilingPresetCatalog,
                          preset: SuspendedCeilingPreset, part_key: str) -> tuple[Provenance, ...]:
    return (Provenance(
        source_kind="building_assembly_generator",
        source_id=catalog.catalog_id,
        source_element_id=json.dumps(
            (catalog.catalog_revision, preset.preset_id, preset.preset_revision, part_key),
            separators=(",", ":"), ensure_ascii=False,
        ),
        method=SUSPENDED_CEILING_GENERATOR_VERSION,
        confidence=1.0,
        derivation="inferred",
        scope_paths=("geometry", "geometry_basis", "material_id", "material_basis"),
        attributes={
            "geometry_basis": "specified_parameterized_geometry",
            "material_basis": "unknown",
            "preset_id": preset.preset_id,
            "preset_revision": preset.preset_revision,
            "profile_geometry_basis": preset.profile_geometry_basis,
            "tile_geometry_basis": preset.tile_geometry_basis,
            "fire_listing_structural_capacity_or_code_approval": "not evaluated",
        },
    ),)


def _make_component(*, assembly_id: str, level_id: str, part_key: str,
                    component_kind: str, geometry: Any,
                    catalog: SuspendedCeilingPresetCatalog,
                    preset: SuspendedCeilingPreset,
                    hanger_attachment: HangerAttachment | None = None) -> AssemblyComponent:
    if len(part_key) > _MAX_PART_KEY_LENGTH:
        raise SuspendedCeilingPresetError("component_key_too_long: ceiling part key exceeds canonical text limit")
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id,
        part_key=part_key,
        level_id=level_id,
        component_kind=component_kind,
        geometry=geometry,
        geometry_basis="specified_parameterized_geometry",
        material_id=None,
        material_basis="unknown",
        hanger_attachment=hanger_attachment,
        provenance=_component_provenance(catalog, preset, part_key),
    )


def _existing_component_vertex_count(component: AssemblyComponent) -> int:
    geometry = component.geometry
    if isinstance(geometry, ProfileExtrusion):
        if isinstance(geometry.profile, Profile2D):
            point_count = len(geometry.profile.outline_xy_m())
        else:
            point_count = len(geometry.profile.points_m)
        return point_count * 2
    if isinstance(geometry, PanelGeometry):
        return len(geometry.boundary.points) * 2
    if isinstance(geometry, WireSweep):
        return (len(geometry.centerline.points) - 1) * 2 * _GLB_CYLINDER_SIDES
    # Suspended assembly roles also admit boxes; keep the global estimate in
    # lockstep with the canonical GLB component-mesh preflight.
    from oabm.model import Box3D
    if isinstance(geometry, Box3D):
        return 8
    raise SuspendedCeilingPresetError(
        f"unsupported existing assembly geometry for GLB preflight: {type(geometry).__name__}"
    )


def _grid_bounds(frame: _RectangleFrame, request: SuspendedCeilingAssemblyRequest,
                 spacing: float, profile_width: float) -> tuple[_GridBounds, tuple[float, float], tuple[float, float]]:
    grid_u = (math.cos(request.grid_axis_angle_rad), math.sin(request.grid_axis_angle_rad))
    grid_v = (-grid_u[1], grid_u[0])
    alignment_u = abs(grid_u[0] * frame.axis_u[0] + grid_u[1] * frame.axis_u[1])
    alignment_v = abs(grid_u[0] * frame.axis_v[0] + grid_u[1] * frame.axis_v[1])
    if max(alignment_u, alignment_v) < 1.0 - _ANGLE_TOLERANCE:
        raise SuspendedCeilingPresetError(
            "unsupported grid axis: horizontal grid angle must be parallel to one source Ceiling rectangle axis"
        )
    origin_x, origin_y = float(request.grid_origin_x_m), float(request.grid_origin_y_m)
    local_corners = tuple(
        ((x - origin_x) * grid_u[0] + (y - origin_y) * grid_u[1],
         (x - origin_x) * grid_v[0] + (y - origin_y) * grid_v[1])
        for x, y in frame.polygon_xy
    )
    x_low = min(row[0] for row in local_corners)
    x_high = max(row[0] for row in local_corners)
    y_low = min(row[1] for row in local_corners)
    y_high = max(row[1] for row in local_corners)
    if not all(math.isfinite(value) for value in (x_low, x_high, y_low, y_high)):
        raise SuspendedCeilingPresetError("grid-local Ceiling bounds are not finite")
    if not _point_in_polygon((origin_x, origin_y), frame.polygon_xy):
        raise SuspendedCeilingPresetError("explicit grid origin must lie inside the source Ceiling footprint")
    ix_low, ix_high, main_count = _lattice_indices(
        x_low, x_high, origin=0.0, spacing=spacing, half_width=profile_width / 2.0,
        label="main-runner",
    )
    iy_low, iy_high, cross_count = _lattice_indices(
        y_low, y_high, origin=0.0, spacing=spacing, half_width=profile_width / 2.0,
        label="cross-runner",
    )
    return (_GridBounds(x_low, x_high, y_low, y_high,
                        ix_low, ix_high, iy_low, iy_high), grid_u, grid_v)


def _cross_runner_span_count(bounds: _GridBounds, spacing: float, half_width: float) -> int:
    first_center = bounds.x_index_low * spacing
    last_center = bounds.x_index_high * spacing
    left_span = first_center - half_width - bounds.x_low > _TOLERANCE_M
    right_span = bounds.x_high - (last_center + half_width) > _TOLERANCE_M
    return bounds.main_count - 1 + int(left_span) + int(right_span)


def _cross_runner_spans(bounds: _GridBounds, spacing: float,
                        half_width: float) -> tuple[tuple[float, float, str], ...]:
    """Return cross-tee intervals, including cut perimeter bays.

    Each interior end stops exactly at a main-runner flange side face. A
    perimeter bay receives a cross tee to the host boundary so its interior
    tile seams still have a real flange face; no wall-edge support is implied
    at that free cut end.
    """
    spans: list[tuple[float, float, str]] = []
    first_center = bounds.x_index_low * spacing
    last_center = bounds.x_index_high * spacing
    left_end = first_center - half_width
    if left_end - bounds.x_low > _TOLERANCE_M:
        spans.append((bounds.x_low, left_end, "perimeter-low"))
    for main_index in range(bounds.x_index_low, bounds.x_index_high):
        start = main_index * spacing + half_width
        end = (main_index + 1) * spacing - half_width
        if end - start <= _TOLERANCE_M:
            raise SuspendedCeilingPresetError("cross-tee span is non-positive after main-runner face clearance")
        spans.append((start, end, f"main-bay:{main_index}"))
    right_start = last_center + half_width
    if bounds.x_high - right_start > _TOLERANCE_M:
        spans.append((right_start, bounds.x_high, "perimeter-high"))
    if not spans:
        raise SuspendedCeilingPresetError("selected grid leaves no positive cross-runner intervals")
    return tuple(spans)


def _station_count(start: float, end: float, spacing: float) -> int:
    available = end - start
    if not math.isfinite(available) or available < -_TOLERANCE_M:
        raise SuspendedCeilingPresetError("hanger end offsets do not fit the source Ceiling runner length")
    if available < -_TOLERANCE_M:
        return 0
    count = math.floor((available + _TOLERANCE_M) / spacing) + 1
    return max(0, count)


def _model_has_assignment(model: BuildingModel, ceiling_id: str) -> bool:
    return any(row.assembly_kind == "suspended_ceiling" and row.host_id == ceiling_id
               for row in model.building_assemblies)


def _assembly_id_exists(model: BuildingModel, assembly_id: str) -> bool:
    collections = (
        model.levels, model.spaces, model.walls, model.slabs, model.ceilings,
        model.openings, model.electrical_equipment, model.electrical_devices,
        model.ports, model.obstacles, model.route_constraints, model.routes,
        model.route_fittings, model.circuits, model.conductors,
        model.electrical_boxes, model.box_selection_outcomes, model.raceway_selections,
        model.raceway_selection_outcomes, model.support_assemblies,
        model.support_components, model.support_interfaces, model.support_selection_outcomes,
        model.structural_members, model.fitting_selections, model.fitting_interfaces,
        model.fitting_selection_outcomes, model.building_assemblies,
        model.assembly_components,
    )
    return any(row.id == assembly_id for collection in collections for row in collection)


def _assignment_provenance(ceiling: Ceiling, deck: Slab,
                           request: SuspendedCeilingAssemblyRequest,
                           resolution: SuspendedCeilingPresetResolution) -> tuple[Provenance, ...]:
    selection_id = json.dumps(
        (resolution.catalog.catalog_id, resolution.catalog.catalog_revision,
         resolution.preset.preset_id, resolution.preset.preset_revision),
        separators=(",", ":"), ensure_ascii=False,
    )
    rows = [Provenance(
        source_kind="preset-selection",
        source_id=request.selected_by,
        source_element_id=selection_id,
        method="explicit-agent-or-user-preset-selection",
        confidence=1.0,
        derivation="user",
        scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by"),
    )]
    for source, scope in ((ceiling, ("host_id", "input_digest")),
                          (deck, ("deck_id", "input_digest"))):
        for observation in source.provenance:
            if observation.derivation != "observed":
                continue
            rows.append(Provenance(
                source_kind=observation.source_kind,
                source_id=observation.source_id,
                source_element_id=observation.source_element_id,
                page=observation.page,
                method=observation.method,
                confidence=observation.confidence,
                derivation="observed",
                scope_paths=scope,
                attributes=dict(observation.attributes),
            ))
    if not any(row.derivation == "observed" and "host_id" in (row.scope_paths or ()) for row in rows):
        raise SuspendedCeilingPresetError(f"{ceiling.id} has no observed source provenance for its geometry")
    if not any(row.derivation == "observed" and "deck_id" in (row.scope_paths or ()) for row in rows):
        raise SuspendedCeilingPresetError(f"{deck.id} has no observed source provenance for its geometry")
    return tuple(rows)


def _preflight_component_counts(model: BuildingModel, *, main_count: int,
                                cross_count: int, cross_spans_per_row: int,
                                hanger_count_per_main: int) -> tuple[int, int]:
    cross_components = cross_spans_per_row * cross_count
    tile_components = (main_count + 1) * (cross_count + 1)
    wire_components = main_count * hanger_count_per_main
    predicted_components = main_count + cross_components + tile_components + wire_components
    if predicted_components > ASSEMBLY_MAX_COMPONENTS:
        raise SuspendedCeilingPresetError(
            f"component_cap_exceeded: predicted {predicted_components} ceiling components exceed {ASSEMBLY_MAX_COMPONENTS}"
        )
    predicted_vertices = (
        (main_count + cross_components) * _PROFILE_OUTLINE_VERTICES * 2
        + tile_components * _TILE_BOUNDARY_VERTICES * 2
        + wire_components * 2 * _GLB_CYLINDER_SIDES
    )
    existing_vertices = sum(_existing_component_vertex_count(component)
                            for component in model.assembly_components)
    if existing_vertices + predicted_vertices > _GLB_ASSEMBLY_VERTEX_LIMIT:
        raise SuspendedCeilingPresetError(
            "glb_vertex_cap_exceeded: existing plus predicted suspended-ceiling meshes "
            f"would require {existing_vertices + predicted_vertices} vertices, over "
            f"the model-wide GLB assembly limit {_GLB_ASSEMBLY_VERTEX_LIMIT}"
        )
    return predicted_components, predicted_vertices


def build_suspended_ceiling_assembly(
    model: BuildingModel,
    request: SuspendedCeilingAssemblyRequest,
    *,
    catalog: SuspendedCeilingPresetCatalog = DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
) -> BuildingModel:
    """Build one atomic suspended-ceiling assignment below an existing Slab.

    The source BuildingModel is immutable. Existing source facts are preserved;
    a source Ceiling thickness is the canonical downward extent from its top
    footprint plane, not a guessed center plane. A source thickness that cannot
    contain the selected tile/tee placement is refused rather than relabelled.
    """
    if not isinstance(model, BuildingModel):
        raise SuspendedCeilingPresetError("model must be a typed canonical BuildingModel")
    if not isinstance(request, SuspendedCeilingAssemblyRequest):
        raise SuspendedCeilingPresetError("request must be a typed SuspendedCeilingAssemblyRequest")
    resolution_host = next((row for row in model.ceilings if row.id == request.ceiling_id), None)
    if resolution_host is None:
        raise SuspendedCeilingPresetError(
            f"requested Ceiling {request.ceiling_id!r} is missing from the canonical model"
        )
    resolution = resolve_suspended_ceiling_preset(request, resolution_host, catalog=catalog)
    deck = next((row for row in model.slabs if row.id == request.deck_id), None)
    if deck is None:
        raise SuspendedCeilingPresetError(
            f"requested existing deck Slab {request.deck_id!r} is missing; generated/replacement decks are unsupported"
        )
    if model.routes:
        raise SuspendedCeilingPresetError(
            "requires_derived_rebuild: suspended-ceiling generation refuses models with existing routes; route dependency invalidation is not implemented"
        )
    if _model_has_assignment(model, resolution_host.id):
        raise SuspendedCeilingPresetError(
            f"requires_derived_rebuild: {resolution_host.id} already has an active suspended-ceiling assembly; replacement is not implemented"
        )
    if _assembly_id_exists(model, request.assembly_id):
        raise SuspendedCeilingPresetError(
            f"assembly ID {request.assembly_id!r} is already used; no replacement is performed"
        )
    if request.assembly_id == model.model_id:
        raise SuspendedCeilingPresetError("assembly_id cannot collide with model_id")
    if deck.level_id != resolution_host.level_id:
        raise SuspendedCeilingPresetError("selected deck Slab must be on the same canonical level as the Ceiling host")
    _require_source_geometry_evidence(deck, "Slab deck")

    frame = _rectangle_frame(resolution_host, "Ceiling")
    deck_frame = _rectangle_frame(deck, "Slab deck")
    _require_host_inside_deck(frame, deck_frame, deck)
    values = {parameter.parameter_id: parameter.value for parameter in resolution.parameters}
    numeric = {key: float(value) for key, value in values.items()
               if key in SUSPENDED_CEILING_PARAMETER_IDS or key == _SOURCE_THICKNESS_PARAMETER_ID}
    profile = _validate_profile_parameters(numeric)

    module = numeric["ceiling.module_spacing_m"]
    profile_width = numeric["ceiling.runner.profile_width_m"]
    tile_edge_clearance = numeric["ceiling.tile.edge_clearance_m"]
    tile_thickness = numeric["ceiling.tile.thickness_m"]
    flange_thickness = numeric["ceiling.runner.flange_thickness_m"]
    profile_depth = numeric["ceiling.runner.profile_depth_m"]
    hanger_spacing = numeric["ceiling.hanger.spacing_m"]
    hanger_end_offset = numeric["ceiling.hanger.end_offset_m"]
    wire_diameter = numeric["ceiling.hanger.wire_diameter_m"]
    tile_top_offset = numeric[_TILE_TOP_OFFSET_PARAMETER_ID]
    tile_top_z = frame.z_m + tile_top_offset
    tile_bottom_z = tile_top_z - tile_thickness
    runner_bottom_z = tile_bottom_z - flange_thickness
    runner_top_z = runner_bottom_z + profile_depth
    deck_top_z = deck_frame.z_m
    deck_underside_z = deck_top_z - deck.thickness_m
    ceiling_lower_z = frame.z_m - (resolution_host.thickness_m or 0.0)
    if runner_bottom_z < ceiling_lower_z - _TOLERANCE_M:
        available = frame.z_m - ceiling_lower_z
        needed = tile_thickness + flange_thickness - tile_top_offset
        raise SuspendedCeilingPresetError(
            "unresolved Ceiling thickness/datum: selected tile + T flange stack extends "
            f"{needed:.9g} m below the source top face, but Ceiling.thickness_m bounds "
            f"only {available:.9g} m downward; adjust the explicit tile-top offset or select a compatible preset"
        )
    if deck_underside_z <= frame.z_m + _TOLERANCE_M:
        raise SuspendedCeilingPresetError("selected deck underside must be above the source Ceiling top-face datum")
    if runner_top_z >= deck_underside_z - _TOLERANCE_M:
        raise SuspendedCeilingPresetError("selected T-bar profile leaves no positive deck-to-runner hanger length")
    if tile_top_z > deck_underside_z - _TOLERANCE_M:
        raise SuspendedCeilingPresetError("selected tile top face exceeds the deck underside bounds")

    origin_x, origin_y = float(request.grid_origin_x_m), float(request.grid_origin_y_m)
    grid_bounds, grid_u, grid_v = _grid_bounds(frame, request, module, profile_width)
    main_count = grid_bounds.main_count
    cross_count = grid_bounds.cross_row_count
    if grid_bounds.y_high - grid_bounds.y_low <= 2.0 * hanger_end_offset + _TOLERANCE_M:
        raise SuspendedCeilingPresetError("hanger end offsets leave no valid station interval on the main runners")
    hanger_count = _station_count(
        grid_bounds.y_low + hanger_end_offset,
        grid_bounds.y_high - hanger_end_offset,
        hanger_spacing,
    )
    if hanger_count < 1:
        raise SuspendedCeilingPresetError("no hanger wire station fits along the selected main runner length")

    predicted_components, _ = _preflight_component_counts(
        model, main_count=main_count, cross_count=cross_count,
        cross_spans_per_row=_cross_runner_span_count(grid_bounds, module, profile_width / 2.0),
        hanger_count_per_main=hanger_count,
    )
    if predicted_components > len(SUSPENDED_CEILING_PARAMETER_IDS) * ASSEMBLY_MAX_COMPONENTS:
        # Secondary integer sanity guard for unexpectedly enormous arithmetic.
        raise SuspendedCeilingPresetError("ceiling component prediction exceeds the bounded producer integer range")

    # Do not construct a line/span list until its full predicted component and
    # model-wide GLB vertex cost is known to fit the strict caps above.
    cross_spans = _cross_runner_spans(grid_bounds, module, profile_width / 2.0)

    # All expensive geometry lists/records are created only after the complete
    # component and model-wide GLB vertex preflights above have succeeded.
    parts: list[AssemblyComponent] = []
    main_by_index: dict[int, AssemblyComponent] = {}
    run_angle = float(request.grid_axis_angle_rad)
    main_angle = run_angle + math.pi / 2.0
    main_quaternion = _quat_for_runner(main_angle)
    tee_profile = profile
    for grid_index in range(grid_bounds.x_index_low, grid_bounds.x_index_high + 1):
        grid_x = grid_index * module
        pose = _profile_pose_start(
            x=grid_x, y=grid_bounds.y_low, z=runner_bottom_z,
            run_angle=main_angle, origin_x=origin_x, origin_y=origin_y,
            axis_u=grid_u, axis_v=grid_v, profile_width_m=profile_width,
        )
        key = f"tbar_main_runner:grid-column:{grid_index}"
        geometry = ProfileExtrusion(profile=tee_profile, pose=pose,
                                    length_m=grid_bounds.y_high - grid_bounds.y_low)
        component = _make_component(
            assembly_id=request.assembly_id, level_id=resolution_host.level_id,
            part_key=key, component_kind="tbar_main_runner", geometry=geometry,
            catalog=resolution.catalog, preset=resolution.preset,
        )
        parts.append(component)
        main_by_index[grid_index] = component

    # Cross tees terminate flush at main-tee flange side faces; the flange
    # side contact has positive area without positive-volume runner overlap.
    # The upper web-side gap, connection tabs/clips and splice hardware are not
    # modeled; no complete mechanical joint is implied.
    half_width = profile_width / 2.0
    for row_index in range(grid_bounds.y_index_low, grid_bounds.y_index_high + 1):
        grid_y = row_index * module
        for start_x, end_x, span_key in cross_spans:
            pose = _profile_pose_start(
                x=start_x, y=grid_y, z=runner_bottom_z,
                run_angle=run_angle, origin_x=origin_x, origin_y=origin_y,
                axis_u=grid_u, axis_v=grid_v, profile_width_m=profile_width,
            )
            key = f"tbar_cross_runner:grid-row:{row_index}:span:{span_key}"
            parts.append(_make_component(
                assembly_id=request.assembly_id, level_id=resolution_host.level_id,
                part_key=key, component_kind="tbar_cross_runner",
                geometry=ProfileExtrusion(
                    profile=tee_profile, pose=pose, length_m=end_x - start_x,
                ),
                catalog=resolution.catalog, preset=resolution.preset,
            ))

    # The caller-selected small positive tile edge clearance leaves a physical
    # plan overlap onto each T flange. Only edges on the actual Ceiling host
    # perimeter lack a modeled runner or perimeter angle.
    x_edges: list[tuple[float, str, bool]] = [(grid_bounds.x_low, "edge-low", False)]
    x_edges.extend((index * module, f"main:{index}", True)
                   for index in range(grid_bounds.x_index_low, grid_bounds.x_index_high + 1))
    x_edges.append((grid_bounds.x_high, "edge-high", False))
    y_edges: list[tuple[float, str, bool]] = [(grid_bounds.y_low, "edge-low", False)]
    y_edges.extend((index * module, f"cross:{index}", True)
                   for index in range(grid_bounds.y_index_low, grid_bounds.y_index_high + 1))
    y_edges.append((grid_bounds.y_high, "edge-high", False))
    tile_count = 0
    for (x0, x0_key, x0_is_runner), (x1, x1_key, x1_is_runner) in zip(x_edges, x_edges[1:]):
        left = x0 + (tile_edge_clearance if x0_is_runner else 0.0)
        right = x1 - (tile_edge_clearance if x1_is_runner else 0.0)
        if right - left <= _TOLERANCE_M:
            raise SuspendedCeilingPresetError("cut tile has non-positive width after T-bar bearing allowance")
        for (y0, y0_key, y0_is_runner), (y1, y1_key, y1_is_runner) in zip(y_edges, y_edges[1:]):
            bottom = y0 + (tile_edge_clearance if y0_is_runner else 0.0)
            top = y1 - (tile_edge_clearance if y1_is_runner else 0.0)
            if top - bottom <= _TOLERANCE_M:
                raise SuspendedCeilingPresetError("cut tile has non-positive length after T-bar bearing allowance")
            if max(right - left, top - bottom) > module + _TOLERANCE_M:
                raise SuspendedCeilingPresetError("cut tile exceeds the selected nominal module bounds")
            part_key = (f"tile:bay-x:{x0_key}:to:{x1_key}:bay-y:{y0_key}:to:{y1_key}")
            mid_z = (tile_bottom_z + tile_top_z) / 2.0
            polygon = Polygon3D(points=(
                _world_grid_point(left, bottom, mid_z, origin_x, origin_y, grid_u, grid_v),
                _world_grid_point(right, bottom, mid_z, origin_x, origin_y, grid_u, grid_v),
                _world_grid_point(right, top, mid_z, origin_x, origin_y, grid_u, grid_v),
                _world_grid_point(left, top, mid_z, origin_x, origin_y, grid_u, grid_v),
            ))
            parts.append(_make_component(
                assembly_id=request.assembly_id, level_id=resolution_host.level_id,
                part_key=part_key, component_kind="tile",
                geometry=PanelGeometry(
                    boundary=polygon, thickness_m=tile_thickness,
                    normal=Vector3(x=0.0, y=0.0, z=1.0),
                ),
                catalog=resolution.catalog, preset=resolution.preset,
            ))
            tile_count += 1

    # Straight vertical wires attach to the actual underside of the existing
    # Slab and the top edge of one named main-runner web. No hardware or capacity
    # is inferred from these endpoint references.
    for grid_index, runner in main_by_index.items():
        for station_index in range(hanger_count):
            station = grid_bounds.y_low + hanger_end_offset + station_index * hanger_spacing
            if station > grid_bounds.y_high - hanger_end_offset + _TOLERANCE_M:
                raise SuspendedCeilingPresetError("hanger station preflight disagrees with generated station range")
            local_anchor = (profile_width / 2.0, profile_depth, station_index * hanger_spacing + hanger_end_offset)
            # Use exact pose transform at the physical top-web boundary; the
            # station is local to the full main-runner extrusion.
            rotated = _rotate(main_quaternion, local_anchor)
            bottom_anchor = Point3(
                x=runner.geometry.pose.position.x + rotated[0],
                y=runner.geometry.pose.position.y + rotated[1],
                z=runner.geometry.pose.position.z + rotated[2],
            )
            top_anchor = Point3(x=bottom_anchor.x, y=bottom_anchor.y, z=deck_underside_z)
            wire_length = math.dist(
                (bottom_anchor.x, bottom_anchor.y, bottom_anchor.z),
                (top_anchor.x, top_anchor.y, top_anchor.z),
            )
            if wire_length <= _TOLERANCE_M:
                raise SuspendedCeilingPresetError("hanger wire must have positive exact endpoint distance")
            key = f"hanger:main:{grid_index}:station:{station_index}"
            wire = _make_component(
                assembly_id=request.assembly_id, level_id=resolution_host.level_id,
                part_key=key, component_kind="hanger_wire",
                geometry=WireSweep(
                    centerline=Polyline3D(points=(top_anchor, bottom_anchor)),
                    diameter_m=wire_diameter, end_treatment="cut",
                ),
                catalog=resolution.catalog, preset=resolution.preset,
                hanger_attachment=HangerAttachment(
                    top_host_id=deck.id,
                    top_anchor=top_anchor,
                    bottom_runner_id=runner.id,
                    bottom_anchor=bottom_anchor,
                ),
            )
            if not point_on_slab_surface(deck, top_anchor):
                raise SuspendedCeilingPresetError("generated hanger top anchor is not on the selected Slab underside")
            if not point_on_component_surface(runner, bottom_anchor):
                raise SuspendedCeilingPresetError("generated hanger bottom anchor is not on the named runner web surface")
            parts.append(wire)

    if tile_count != (main_count + 1) * (cross_count + 1):
        raise SuspendedCeilingPresetError("generated tile count disagrees with preflight bounds")
    components = tuple(sorted(parts, key=lambda row: row.id))
    if len(components) != predicted_components or len(components) > ASSEMBLY_MAX_COMPONENTS:
        raise SuspendedCeilingPresetError("generated component count disagrees with preflight bounds")

    upgraded = (model if model.schema_version in ASSEMBLY_SCHEMA_VERSIONS
                else upgrade_to_v1_6(model))
    placement = next(parameter for parameter in resolution.parameters
                     if parameter.parameter_id == _TILE_TOP_OFFSET_PARAMETER_ID)
    placement_attributes = placement.provenance[0].attributes
    assignment = BuildingAssembly(
        id=request.assembly_id,
        assembly_kind="suspended_ceiling",
        level_id=resolution_host.level_id,
        host_id=resolution_host.id,
        host_representation="aggregate_host",
        catalog_id=resolution.catalog.catalog_id,
        catalog_revision=resolution.catalog.catalog_revision,
        preset_id=resolution.preset.preset_id,
        preset_revision=resolution.preset.preset_revision,
        selected_by=request.selected_by,
        generator_version=SUSPENDED_CEILING_GENERATOR_VERSION,
        resolved_parameters=resolution.parameters,
        component_ids=tuple(component.id for component in components),
        input_digest="0" * 64,
        output_digest="0" * 64,
        deck_id=deck.id,
        provenance=_assignment_provenance(resolution_host, deck, request, resolution),
        attributes={
            "modeling_scope": "horizontal_rectangular_tbar_grid_v1",
            "boundary_policy": _SUPPORTED_EDGE_POLICY,
            "perimeter_support": (
                "the only intentionally unsupported tile edges lie on the actual host footprint boundary "
                "(grid-local u-min, u-max, v-min, v-max); no perimeter angle or wall-edge support is modeled"
            ),
            "unsupported_tile_edge_boundaries": [
                "host_grid_u_min", "host_grid_u_max", "host_grid_v_min", "host_grid_v_max",
            ],
            "tile_runner_bearing": (
                "every interior tile edge has positive plan overlap with a named simple-T flange; "
                f"resolved overlap width is {half_width - tile_edge_clearance:.9g} m and tile underside "
                "is coplanar with flange top"
            ),
            "runner_joints": (
                "cross-tee flange end faces have positive-area contact with main-runner flange side faces "
                "without positive-volume overlap; upper web-side separation, connector clips/tabs, splices "
                "and complete mechanical joint geometry are not modeled"
            ),
            "perimeter_cross_runner_ends": (
                "cross tees extend to the host boundary in perimeter bays to bear interior tile seams; "
                "their free ends have no modeled wall support or connector"
            ),
            "hanger_semantics": "wire centerlines attach at the named Slab underside and main-runner web top; no anchor hardware, capacity or substrate design is modeled",
            "deck_semantics": "reuse exactly one observed canonical Slab identity; no deck_panel is generated",
            "profile_disclosure": resolution.preset.profile_geometry_basis,
            "tile_disclosure": resolution.preset.tile_geometry_basis,
            "tile_edge_clearance_m": numeric["ceiling.tile.edge_clearance_m"],
            "tile_top_offset_m": float(placement.value),
            "tile_top_offset_case": placement_attributes.get("placement_case", "caller_user_override"),
            "tile_top_offset_formula": placement_attributes.get("default_formula", "caller_user_override"),
            "source_ceiling_thickness_m": resolution_host.thickness_m,
            "source_ceiling_thickness_semantics": "Ceiling footprint is the top-face datum; optional thickness extends downward and is not rewritten as the assembly envelope",
            "structural_capacity_fire_listing_and_code_approval": "not evaluated",
        },
    )
    input_digest, output_digest = compute_assembly_digests(upgraded, assignment, components)
    assignment = replace(assignment, input_digest=input_digest, output_digest=output_digest)
    # BuildingModel construction validates ownership, digest freshness, exact
    # host/deck bounds and every typed hanger endpoint atomically.
    return replace(
        upgraded,
        building_assemblies=(*upgraded.building_assemblies, assignment),
        assembly_components=(*upgraded.assembly_components, *components),
    )


__all__ = [
    "CeilingParameterOverride", "SuspendedCeilingAssemblyRequest",
    "SuspendedCeilingPresetError", "SuspendedCeilingPresetResolution",
    "resolve_suspended_ceiling_preset", "build_suspended_ceiling_assembly",
]
