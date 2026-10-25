"""Pure, bounded preset resolver and first timber-wall geometry producer.

The producer consumes already reviewed canonical ``Wall`` and ``Opening``
records. It does not read plans, call AI/providers, change source wall facts,
replace an existing assembly, or make structural/code claims.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import re
from oabm.model import (
    ASSEMBLY_MAX_COMPONENTS,
    ASSEMBLY_MAX_PARAMETERS,
    ASSEMBLY_SCHEMA_VERSIONS,
    AssemblyComponent,
    Box3D,
    BuildingAssembly,
    BuildingModel,
    ContractError,
    Opening,
    PanelGeometry,
    Point3,
    Polygon3D,
    Pose,
    Provenance,
    Quaternion,
    ResolvedParameter,
    Size3,
    Vector3,
    Wall,
    WallAssemblyFrame,
    compute_assembly_digests,
    stable_assembly_component_id,
    upgrade_to_v1_6,
)

from .catalog import (
    DEFAULT_PRESET_CATALOG,
    LEGACY_PRESET_CATALOG,
    LEGACY_WALL_PARAMETER_IDS,
    WALL_GENERATOR_VERSION,
    BuildingAssemblyPresetCatalog,
    ParameterDefinition,
    WallPreset,
)

_TOLERANCE_M = 1e-9
_MAX_OPENINGS = 256
_CANONICAL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_MAX_PART_KEY_LENGTH = 2048
_MAX_PARAMETER_OVERRIDES = len(LEGACY_WALL_PARAMETER_IDS)
_OPENING_SPEC_FIELDS = (
    "header_depth_m", "header_ply_thickness_m", "header_ply_count",
    "header_spacer_thickness_m", "bearing_m", "king_count_per_side", "jack_count_per_side",
)


class WallPresetError(ContractError):
    """The requested wall preset cannot be resolved without guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterOverride:
    """One caller-authored, typed override distinct from catalog defaults."""

    parameter_id: str
    value_kind: str
    value: float | int | str | bool
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.parameter_id, str) or not self.parameter_id.strip():
            raise WallPresetError("parameter override requires a non-empty parameter_id")
        if not isinstance(self.value_kind, str) or not self.value_kind.strip():
            raise WallPresetError(f"{self.parameter_id} override requires a value_kind")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise WallPresetError(f"{self.parameter_id} override requires a canonical unit")


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningFramingSpecification:
    """Explicit selected geometry for one conventional wood opening frame.

    Header dimensions/build-up, bearing and support counts are caller-selected
    inputs, not calculated structural sizing. Spacer thickness, including zero,
    is an explicit input rather than an inferred default.
    """

    opening_id: str
    header_depth_m: float
    header_ply_thickness_m: float
    header_ply_count: int
    header_spacer_thickness_m: float
    bearing_m: float
    king_count_per_side: int
    jack_count_per_side: int

    def __post_init__(self) -> None:
        if not isinstance(self.opening_id, str) or not _CANONICAL_ID.fullmatch(self.opening_id):
            raise WallPresetError("opening framing specification requires a canonical opening_id")
        for field_name in ("header_depth_m", "header_ply_thickness_m", "bearing_m"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                    not math.isfinite(float(value)) or value <= _TOLERANCE_M:
                raise WallPresetError(
                    f"{self.opening_id}.{field_name} must be a finite length greater than "
                    f"the {_TOLERANCE_M:g} m geometric tolerance"
                )
        if isinstance(self.header_spacer_thickness_m, bool) or not isinstance(
                self.header_spacer_thickness_m, (int, float)) or \
                not math.isfinite(float(self.header_spacer_thickness_m)) or self.header_spacer_thickness_m < 0:
            raise WallPresetError(
                f"{self.opening_id}.header_spacer_thickness_m must be a finite nonnegative length"
            )
        if 0 < self.header_spacer_thickness_m <= _TOLERANCE_M:
            raise WallPresetError(
                f"{self.opening_id}.header_spacer_thickness_m must be zero or greater than "
                f"the {_TOLERANCE_M:g} m geometric tolerance"
            )
        for field_name in ("header_ply_count", "king_count_per_side", "jack_count_per_side"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 64:
                raise WallPresetError(f"{self.opening_id}.{field_name} must be an integer from 1 through 64")
        total_thickness = (self.header_ply_count * self.header_ply_thickness_m
                           + (self.header_ply_count - 1) * self.header_spacer_thickness_m)
        if not math.isfinite(total_thickness) or total_thickness <= 0:
            raise WallPresetError(f"{self.opening_id} header ply build-up must have positive finite thickness")
        if self.header_ply_count == 1 and self.header_spacer_thickness_m > 0:
            raise WallPresetError(f"{self.opening_id} cannot select a header spacer without multiple plies")


@dataclass(frozen=True, slots=True, kw_only=True)
class WallAssemblyRequest:
    """Explicit selection used by an agent/user after plan facts are reviewed."""

    assembly_id: str
    wall_id: str
    selected_by: str
    preset_id: str = "wall.wood_rect_stock"
    parameter_overrides: tuple[ParameterOverride, ...] = ()
    opening_framing_specs: tuple[OpeningFramingSpecification, ...] = ()

    def __post_init__(self) -> None:
        for label in ("assembly_id", "wall_id", "selected_by", "preset_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise WallPresetError(f"WallAssemblyRequest.{label} must be non-empty text")
        for label in ("assembly_id", "wall_id"):
            if not _CANONICAL_ID.fullmatch(getattr(self, label)):
                raise WallPresetError(f"WallAssemblyRequest.{label} is not a canonical ID")
        if len(self.selected_by) > 2048:
            raise WallPresetError("WallAssemblyRequest.selected_by exceeds the canonical text limit")
        if not isinstance(self.parameter_overrides, tuple):
            raise WallPresetError("parameter_overrides must be an immutable tuple of typed overrides")
        # Keep the historic maximum, then let the selected revision reject
        # fields that it does not define. Reject impossible lengths
        # before scanning the rows or allocating copies for uniqueness checks.
        if len(self.parameter_overrides) > _MAX_PARAMETER_OVERRIDES:
            raise WallPresetError(
                f"parameter override count exceeds the finite v1 limit {_MAX_PARAMETER_OVERRIDES}"
            )
        if any(not isinstance(row, ParameterOverride) for row in self.parameter_overrides):
            raise WallPresetError("parameter_overrides must be an immutable tuple of typed overrides")
        ids = tuple(row.parameter_id for row in self.parameter_overrides)
        if len(ids) != len(set(ids)):
            raise WallPresetError("parameter overrides must have unique parameter IDs")
        if not isinstance(self.opening_framing_specs, tuple):
            raise WallPresetError("opening_framing_specs must be an immutable tuple of typed specifications")
        if len(self.opening_framing_specs) > _MAX_OPENINGS:
            raise WallPresetError(f"opening framing specification count exceeds the {_MAX_OPENINGS}-opening producer cap")
        if any(not isinstance(row, OpeningFramingSpecification) for row in self.opening_framing_specs):
            raise WallPresetError("opening_framing_specs must be an immutable tuple of typed specifications")
        spec_ids = tuple(row.opening_id for row in self.opening_framing_specs)
        if len(spec_ids) != len(set(spec_ids)):
            raise WallPresetError("opening framing specifications must have unique opening IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class WallPresetResolution:
    catalog: BuildingAssemblyPresetCatalog
    preset: WallPreset
    selected_by: str
    parameters: tuple[ResolvedParameter, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(row, ResolvedParameter) for row in self.parameters
        ):
            raise WallPresetError("resolved parameters must be an immutable tuple")
        if tuple(row.parameter_id for row in self.parameters) != tuple(
                definition.parameter_id for definition in self.preset.parameters):
            raise WallPresetError("resolved parameter snapshot is incomplete or out of canonical order")

    def parameter_map(self) -> dict[str, ResolvedParameter]:
        return {row.parameter_id: row for row in self.parameters}


def resolve_wall_preset(
    request: WallAssemblyRequest,
    *,
    catalog: BuildingAssemblyPresetCatalog = DEFAULT_PRESET_CATALOG,
) -> WallPresetResolution:
    """Resolve the selected finite preset and typed overrides.

    All preset values are explicit ``preset_default`` assumptions or published
    dimensions used as modeling assumptions. No omitted field gets a fallback.
    """
    if not isinstance(request, WallAssemblyRequest):
        raise WallPresetError("request must be a typed WallAssemblyRequest")
    if not isinstance(catalog, BuildingAssemblyPresetCatalog):
        raise WallPresetError("catalog must be a typed immutable BuildingAssemblyPresetCatalog")
    try:
        preset = catalog.wall_preset(request.preset_id)
    except ContractError as exc:
        raise WallPresetError(str(exc)) from exc
    published_catalogs = (LEGACY_PRESET_CATALOG, DEFAULT_PRESET_CATALOG)
    builtin_source_ids = {
        definition.source_id
        for published_catalog in published_catalogs
        for published_preset in published_catalog.wall_presets
        for definition in published_preset.parameters
        if definition.source_id is not None
    }
    builtin_sources = {
        row.source_id: row for published_catalog in published_catalogs
        for row in published_catalog.sources
    }
    for source_id in {row.source_id for row in preset.parameters if row.source_id is not None} & builtin_source_ids:
        selected_source = next((row for row in catalog.sources if row.source_id == source_id), None)
        if selected_source != builtin_sources[source_id]:
            raise WallPresetError(
                f"published wall catalog source ID {source_id!r} is reserved; "
                "changed source content requires a new source_id"
            )
    for published_catalog in published_catalogs:
        if (catalog.catalog_id != published_catalog.catalog_id or
                catalog.catalog_revision != published_catalog.catalog_revision):
            continue
        published = next((row for row in published_catalog.wall_presets
                          if row.preset_id == preset.preset_id and
                          row.preset_revision == preset.preset_revision), None)
        if published is not None and preset != published:
            raise WallPresetError(
                f"published wall preset identity {catalog.catalog_id}@{catalog.catalog_revision}/"
                f"{preset.preset_id}@{preset.preset_revision} is reserved; changed content requires a new revision"
            )
    if preset.assembly_kind != "wall":
        raise WallPresetError(f"selected preset {preset.preset_id!r} is not a wall recipe")

    definitions = {row.parameter_id: row for row in preset.parameters}
    overrides = {row.parameter_id: row for row in request.parameter_overrides}
    unknown = set(overrides) - set(definitions)
    if unknown:
        raise WallPresetError(f"unsupported wall parameter overrides: {sorted(unknown)!r}")

    resolved: list[ResolvedParameter] = []
    for definition in preset.parameters:
        override = overrides.get(definition.parameter_id)
        if override is None:
            value = definition.default_value
            origin = "preset_default"
            evidence_element = _parameter_evidence_id(
                catalog.catalog_revision, preset.preset_id, preset.preset_revision,
                definition.parameter_id,
            )
            provenance = (Provenance(
                source_kind="preset_parameter",
                source_id=catalog.catalog_id,
                source_element_id=evidence_element,
                method="immutable-catalog-default-resolution",
                confidence=1.0,
                derivation="inferred",
                scope_paths=("value", "unit", "origin"),
                attributes={
                    "default_basis": definition.default_basis,
                    "source_id": definition.source_id,
                    "note": definition.note,
                },
            ),)
            source_entity_id = source_field_path = None
        else:
            if override.value_kind != definition.value_kind or override.unit != definition.unit:
                raise WallPresetError(
                    f"{definition.parameter_id} override must be {definition.value_kind} in {definition.unit}"
                )
            _validate_value(definition, override.value, f"{definition.parameter_id} override")
            value = override.value
            origin = "user_override"
            provenance = (Provenance(
                source_kind="user_override",
                source_id=request.selected_by,
                source_element_id=definition.parameter_id,
                method="explicit-wall-preset-parameter-override",
                confidence=1.0,
                derivation="user",
                scope_paths=("value", "unit", "origin"),
                attributes={"catalog_id": catalog.catalog_id,
                            "catalog_revision": catalog.catalog_revision,
                            "preset_id": preset.preset_id,
                            "preset_revision": preset.preset_revision},
            ),)
            source_entity_id = source_field_path = None
        resolved.append(ResolvedParameter(
            parameter_id=definition.parameter_id,
            value_kind=definition.value_kind,
            value=value,
            unit=definition.unit,
            origin=origin,
            provenance=provenance,
            source_entity_id=source_entity_id,
            source_field_path=source_field_path,
        ))
    return WallPresetResolution(
        catalog=catalog, preset=preset, selected_by=request.selected_by,
        parameters=tuple(resolved),
    )


def _parameter_evidence_id(catalog_revision: str, preset_id: str,
                           preset_revision: str, parameter_id: str) -> str:
    return json.dumps(
        (catalog_revision, preset_id, preset_revision, parameter_id),
        separators=(",", ":"), ensure_ascii=False,
    )


def _validate_value(definition: ParameterDefinition, value: object, label: str) -> None:
    kind = definition.value_kind
    if kind == "length":
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(float(value)) and value >= 0)
    elif kind == "count":
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    elif kind == "token":
        valid = isinstance(value, str) and bool(value.strip())
    elif kind == "boolean":
        valid = isinstance(value, bool)
    else:
        valid = False
    if not valid:
        raise WallPresetError(f"{label} has an invalid typed value")
    if definition.minimum is not None and value < definition.minimum:
        raise WallPresetError(f"{label} is below the catalog minimum {definition.minimum!r}")
    if definition.maximum is not None and value > definition.maximum:
        raise WallPresetError(f"{label} exceeds the catalog maximum {definition.maximum!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class _WallFrame:
    start: Point3
    ux: float
    uy: float
    nx: float
    ny: float
    length: float
    base_z: float

    def point(self, station: float, through: float, z: float) -> Point3:
        return Point3(
            x=self.start.x + self.ux * station + self.nx * through,
            y=self.start.y + self.uy * station + self.ny * through,
            z=z,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _OpeningLocal:
    opening: Opening
    station: float
    left: float
    right: float
    sill: float
    head: float


@dataclass(frozen=True, slots=True, kw_only=True)
class _StudSpan:
    station: float
    slot: str
    bottom_z: float
    top_z: float
    role: str
    related_openings: tuple[str, ...]
    lower_key: str
    upper_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _PanelCell:
    left: float
    right: float
    bottom_z: float
    top_z: float
    left_key: str
    right_key: str
    bottom_key: str
    top_key: str


def _frame_for_wall(wall: Wall) -> _WallFrame:
    if len(wall.centerline.points) != 2:
        raise WallPresetError("unsupported wall: only a two-point straight centerline is implemented")
    start, end = wall.centerline.points
    if abs(start.z - end.z) > _TOLERANCE_M:
        raise WallPresetError("unsupported wall: centerline endpoints must share one horizontal base elevation")
    dx, dy = end.x - start.x, end.y - start.y
    length = math.hypot(dx, dy)
    if not math.isfinite(length) or length <= _TOLERANCE_M:
        raise WallPresetError("unsupported wall: centerline must have positive horizontal length")
    ux, uy = dx / length, dy / length
    return _WallFrame(
        start=start, ux=ux, uy=uy, nx=-uy, ny=ux,
        length=length, base_z=start.z,
    )


def _rotate(q: Quaternion, v: tuple[float, float, float]) -> tuple[float, float, float]:
    # Normalize for calculation without rewriting the stored source pose, per
    # the canonical tolerance for near-unit input quaternions.
    norm = math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w)
    if not math.isfinite(norm) or norm <= 0:
        raise WallPresetError("opening pose quaternion is not finite and nonzero")
    qx, qy, qz, qw = (q.x / norm, q.y / norm, q.z / norm, q.w / norm)
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + qy * tz - qz * ty,
        vy + qw * ty + qz * tx - qx * tz,
        vz + qw * tz + qx * ty - qy * tx,
    )


def _opening_locals(model: BuildingModel, wall: Wall, frame: _WallFrame,
                    opening_clearance: float) -> tuple[_OpeningLocal, ...]:
    rows = tuple(sorted((opening for opening in model.openings if opening.host_id == wall.id),
                         key=lambda row: row.id))
    if len(rows) > _MAX_OPENINGS:
        raise WallPresetError(f"wall opening input count exceeds the {_MAX_OPENINGS}-opening producer cap")
    local: list[_OpeningLocal] = []
    for opening in rows:
        kind = opening.opening_type.casefold()
        if kind not in {"door", "window"}:
            raise WallPresetError(
                f"unsupported opening type {opening.opening_type!r} on {opening.id}; only door/window voids are framed"
            )
        if not any(record.derivation == "observed" for record in opening.provenance):
            raise WallPresetError(f"{opening.id} has no observed source provenance for its geometry")
        q = opening.pose.rotation
        actual_axes = (_rotate(q, (1.0, 0.0, 0.0)), _rotate(q, (0.0, 1.0, 0.0)), _rotate(q, (0.0, 0.0, 1.0)))
        expected_axes = ((frame.ux, frame.uy, 0.0), (frame.nx, frame.ny, 0.0), (0.0, 0.0, 1.0))
        if any(any(abs(actual[i] - expected[i]) > _TOLERANCE_M for i in range(3))
               for actual, expected in zip(actual_axes, expected_axes)):
            raise WallPresetError(f"{opening.id} orientation is not aligned with the ordered wall frame")
        dx, dy = opening.pose.position.x - frame.start.x, opening.pose.position.y - frame.start.y
        station = dx * frame.ux + dy * frame.uy
        through = dx * frame.nx + dy * frame.ny
        if abs(through) > _TOLERANCE_M:
            raise WallPresetError(f"{opening.id} center is not on the wall axis plane")
        if abs(opening.size.y - wall.thickness_m) > _TOLERANCE_M:
            raise WallPresetError(f"{opening.id}.size.y must equal the observed wall thickness")
        left, right = station - opening.size.x / 2.0, station + opening.size.x / 2.0
        sill, head = opening.pose.position.z - opening.size.z / 2.0, opening.pose.position.z + opening.size.z / 2.0
        if left < -_TOLERANCE_M or right > frame.length + _TOLERANCE_M:
            raise WallPresetError(f"{opening.id} horizontal extent exceeds the wall host")
        if sill < frame.base_z - _TOLERANCE_M or head > frame.base_z + wall.height_m + _TOLERANCE_M:
            raise WallPresetError(f"{opening.id} vertical extent exceeds the wall host")
        if kind == "door" and abs(sill - frame.base_z) > _TOLERANCE_M:
            raise WallPresetError(f"{opening.id} door sill must coincide with the wall base")
        local.append(_OpeningLocal(opening=opening, station=station, left=left, right=right, sill=sill, head=head))

    # Exact opening voids must not overlap. Treat declared opening clearance as
    # an explicit extra collision margin, never as a repair to source geometry.
    for index, first in enumerate(local):
        for second in local[index + 1:]:
            x_overlap = min(first.right + opening_clearance, second.right + opening_clearance) - max(
                first.left - opening_clearance, second.left - opening_clearance
            )
            z_overlap = min(first.head + opening_clearance, second.head + opening_clearance) - max(
                first.sill - opening_clearance, second.sill - opening_clearance
            )
            if x_overlap > _TOLERANCE_M and z_overlap > _TOLERANCE_M:
                raise WallPresetError(f"opening/frame envelopes overlap: {first.opening.id} and {second.opening.id}")
    return tuple(local)


def _component_provenance(catalog: BuildingAssemblyPresetCatalog, preset: WallPreset,
                          part_key: str,
                          framing_metadata: dict[str, str | int | float | bool] | None = None
                          ) -> tuple[Provenance, ...]:
    attributes: dict[str, str | int | float | bool | None] = {
        "geometry_basis": "specified_parameterized_geometry",
        "material_basis": "unknown",
        "structural_status": preset.structural_status,
        "generator_version": WALL_GENERATOR_VERSION,
    }
    if framing_metadata:
        attributes.update(framing_metadata)
    return (Provenance(
        source_kind="building_assembly_generator",
        source_id=catalog.catalog_id,
        source_element_id=json.dumps(
            (catalog.catalog_revision, preset.preset_id, preset.preset_revision, part_key),
            separators=(",", ":"), ensure_ascii=False,
        ),
        method=WALL_GENERATOR_VERSION,
        confidence=1.0,
        derivation="inferred",
        scope_paths=("geometry", "geometry_basis", "material_id", "material_basis"),
        attributes=attributes,
    ),)


def _make_box_component(*, assembly_id: str, level_id: str, part_key: str,
                        kind: str, frame: _WallFrame, station: float, through: float,
                        center_z: float, size_x: float, size_y: float, size_z: float,
                        catalog: BuildingAssemblyPresetCatalog, preset: WallPreset,
                        opening_id: str | None = None,
                        framing_metadata: dict[str, str | int | float | bool] | None = None
                        ) -> AssemblyComponent:
    heading = math.atan2(frame.uy, frame.ux)
    rotation = Quaternion(z=math.sin(heading / 2.0), w=math.cos(heading / 2.0))
    geometry = Box3D(
        pose=Pose(position=frame.point(station, through, center_z), rotation=rotation),
        size=Size3(x=size_x, y=size_y, z=size_z),
    )
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id,
        part_key=part_key,
        level_id=level_id,
        component_kind=kind,
        geometry=geometry,
        geometry_basis="specified_parameterized_geometry",
        material_id=None,
        material_basis="unknown",
        opening_id=opening_id,
        provenance=_component_provenance(catalog, preset, part_key, framing_metadata),
    )


def _make_panel_component(*, assembly_id: str, level_id: str, part_key: str,
                          face: str, layer: int, cell: _PanelCell,
                          center_through: float, thickness: float,
                          frame: _WallFrame, catalog: BuildingAssemblyPresetCatalog,
                          preset: WallPreset) -> AssemblyComponent:
    sign = 1.0 if face == "left" else -1.0
    vertices = (
        frame.point(cell.left, center_through, cell.bottom_z),
        frame.point(cell.right, center_through, cell.bottom_z),
        frame.point(cell.right, center_through, cell.top_z),
        frame.point(cell.left, center_through, cell.top_z),
    )
    geometry = PanelGeometry(
        boundary=Polygon3D(points=vertices),
        thickness_m=thickness,
        normal=Vector3(x=frame.nx * sign, y=frame.ny * sign, z=0.0),
    )
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id,
        part_key=part_key,
        level_id=level_id,
        component_kind="drywall_panel",
        geometry=geometry,
        geometry_basis="specified_parameterized_geometry",
        material_id=None,
        material_basis="unknown",
        provenance=_component_provenance(catalog, preset, part_key),
    )


def _wall_stations(frame: _WallFrame, start_offset: float, end_offset: float,
                   spacing: float, member_width: float) -> tuple[tuple[float, str], ...]:
    first, last = start_offset, frame.length - end_offset
    if first < member_width / 2.0 - _TOLERANCE_M or last < first - _TOLERANCE_M:
        raise WallPresetError("selected wall end offsets do not fit the explicit stock width and wall length")
    # Count before constructing the stations so a tiny spacing cannot allocate
    # an unbounded intermediate list.
    interior_count = 0
    station = first + spacing
    while station < last - _TOLERANCE_M:
        if last - station < member_width - _TOLERANCE_M:
            break  # the explicit end station occupies this layout slot
        interior_count += 1
        if interior_count + 2 > ASSEMBLY_MAX_COMPONENTS:
            raise WallPresetError(
                f"component_cap_exceeded: predicted regular wall stud stations exceed {ASSEMBLY_MAX_COMPONENTS}"
            )
        station = first + (interior_count + 1) * spacing
    if last - first <= _TOLERANCE_M:
        return ((first, "wall-start-and-end"),)
    rows: list[tuple[float, str]] = [(first, "wall-start")]
    for slot in range(1, interior_count + 1):
        rows.append((first + slot * spacing, f"station-{slot:05d}"))
    if not rows or abs(last - rows[-1][0]) > _TOLERANCE_M:
        rows.append((last, "wall-end"))
    return tuple(rows)


def _merge_intervals(intervals: list[tuple[float, float, tuple[str, ...]]]) -> list[tuple[float, float, tuple[str, ...], tuple[str, ...]]]:
    ordered = sorted(intervals, key=lambda row: (row[0], row[1], row[2]))
    merged: list[tuple[float, float, tuple[str, ...], tuple[str, ...]]] = []
    for low, high, ids in ordered:
        if not merged or low > merged[-1][1] + _TOLERANCE_M:
            merged.append((low, high, ids, ids))
        else:
            old_low, old_high, old_low_ids, old_high_ids = merged[-1]
            if high > old_high + _TOLERANCE_M:
                high_ids = ids
            elif abs(high - old_high) <= _TOLERANCE_M:
                high_ids = tuple(sorted(set(old_high_ids) | set(ids)))
            else:
                high_ids = old_high_ids
            merged[-1] = (old_low, max(old_high, high), old_low_ids, high_ids)
    return merged


def _free_cripple_spans(*, station: float, slot: str, frame: _WallFrame,
                        member_width: float, base_limit: float, top_limit: float,
                        openings: tuple[_OpeningLocal, ...],
                        header_depths: dict[str, float],
                        sill_height: float) -> tuple[_StudSpan, ...]:
    half = member_width / 2.0
    # Rough framing deliberately expands the source aperture by one member on
    # either side. A regular stud in that band is interrupted at the header or
    # sill even when it does not intersect the original void rectangle.
    associated = [row for row in openings
                  if min(station + half, row.right + member_width) -
                  max(station - half, row.left - member_width) > _TOLERANCE_M]
    blocked: list[tuple[float, float, tuple[str, ...]]] = []
    for row in associated:
        low = row.sill - (sill_height if row.opening.opening_type.casefold() == "window" else 0.0)
        high = row.head + header_depths[row.opening.id]
        low, high = max(low, base_limit), min(high, top_limit)
        if high - low > _TOLERANCE_M:
            blocked.append((low, high, (row.opening.id,)))
    merged = _merge_intervals(blocked)
    if not merged:
        return (_StudSpan(
            station=station, slot=slot, bottom_z=base_limit, top_z=top_limit,
            role="stud", related_openings=(), lower_key="bottom-plate-top", upper_key="top-plate-bottom",
        ),)
    result: list[_StudSpan] = []
    cursor = base_limit
    lower_key = "bottom-plate-top"
    for low, high, low_ids, high_ids in merged:
        if low - cursor > _TOLERANCE_M:
            upper_key = "below:" + "+".join(sorted(low_ids))
            related = tuple(sorted(set(low_ids)))
            result.append(_StudSpan(
                station=station, slot=slot, bottom_z=cursor, top_z=low,
                role="cripple", related_openings=related,
                lower_key=lower_key, upper_key=upper_key,
            ))
        cursor = max(cursor, high)
        lower_key = "above:" + "+".join(sorted(high_ids))
    if top_limit - cursor > _TOLERANCE_M:
        related = tuple(sorted({opening_id for row in associated for opening_id in (row.opening.id,)}))
        result.append(_StudSpan(
            station=station, slot=slot, bottom_z=cursor, top_z=top_limit,
            role="cripple", related_openings=related,
            lower_key=lower_key, upper_key="top-plate-bottom",
        ))
    return tuple(result)


def _opening_spec_parameters(request: WallAssemblyRequest,
                             specs: dict[str, OpeningFramingSpecification]) -> tuple[ResolvedParameter, ...]:
    rows: list[ResolvedParameter] = []
    fields = (
        ("header_depth_m", "length", "m"),
        ("header_ply_thickness_m", "length", "m"),
        ("header_ply_count", "count", "each"),
        ("header_spacer_thickness_m", "length", "m"),
        ("bearing_m", "length", "m"),
        ("king_count_per_side", "count", "each"),
        ("jack_count_per_side", "count", "each"),
    )
    for opening_id, spec in sorted(specs.items()):
        opening_key = hashlib.sha256(opening_id.encode("utf-8")).hexdigest()[:16]
        for field_name, value_kind, unit in fields:
            parameter_id = f"wall.opening.{opening_key}.{field_name}"
            rows.append(ResolvedParameter(
                parameter_id=parameter_id,
                value_kind=value_kind,
                value=getattr(spec, field_name),
                unit=unit,
                origin="user_override",
                provenance=(Provenance(
                    source_kind="user_override",
                    source_id=request.selected_by,
                    source_element_id=parameter_id,
                    method="explicit-selected-opening-framing-specification",
                    confidence=1.0,
                    derivation="user",
                    scope_paths=("value", "unit", "origin"),
                    attributes={"opening_id": opening_id, "field": field_name,
                                "sizing_status": "explicit_geometry_input_not_engineered_sizing"},
                ),),
                source_entity_id=None,
                source_field_path=None,
            ))
    return tuple(rows)


def _stud_span_part_key(span: _StudSpan) -> str:
    if span.role == "stud":
        return f"stud:station:{span.slot}"
    return f"cripple:station:{span.slot}:from:{span.lower_key}:to:{span.upper_key}"


def _rough_frame_overlap_guard(openings: tuple[_OpeningLocal, ...], *, frame: _WallFrame,
                               member_width: float,
                               specs: dict[str, OpeningFramingSpecification],
                               header_depths: dict[str, float],
                               sill_height: float, top_plate_bottom: float,
                               bottom_plate_top: float) -> None:
    envelopes: list[tuple[str, float, float, float, float]] = []
    for row in openings:
        spec = specs[row.opening.id]
        support_width = (spec.king_count_per_side + spec.jack_count_per_side) * member_width
        left, right = row.left - support_width, row.right + support_width
        # Kings and jacks continue to the bottom-plate top, independent of the
        # source rough-opening sill, so adjacent-frame collision covers their
        # full vertical envelope.
        lower = bottom_plate_top
        upper = row.head + header_depths[row.opening.id]
        if left < -_TOLERANCE_M or right > frame.length + _TOLERANCE_M:
            raise WallPresetError(f"{row.opening.id} king/jack/header frame does not fit inside the wall axis")
        if upper > top_plate_bottom + _TOLERANCE_M:
            raise WallPresetError(f"{row.opening.id} rough header overlaps the required top plate band")
        envelopes.append((row.opening.id, left, right, lower, upper))
    for index, first in enumerate(envelopes):
        for second in envelopes[index + 1:]:
            if min(first[2], second[2]) - max(first[1], second[1]) > _TOLERANCE_M and \
                    min(first[4], second[4]) - max(first[3], second[3]) > _TOLERANCE_M:
                raise WallPresetError(f"rough-frame envelopes overlap: {first[0]} and {second[0]}")


def _subtract_opening_supports(span: _StudSpan, *, member_width: float,
                                base_limit: float, top_limit: float,
                                openings: tuple[_OpeningLocal, ...],
                                specs: dict[str, OpeningFramingSpecification]) -> tuple[_StudSpan, ...]:
    """Remove only vertical intervals occupied by king/jack stock at a grid column.

    A jack occupies the stud interval from the lower plate face to its header
    underside; a king occupies that full lower-to-upper plate-face interval.
    Grid-derived framing above a jack may therefore remain as a cripple. This
    bounded recipe does not add off-grid members.
    """
    half = member_width / 2.0
    column_left, column_right = span.station - half, span.station + half
    blocked: list[tuple[float, float, tuple[str, ...]]] = []
    for row in openings:
        spec = specs[row.opening.id]
        for side, boundary, outward in (("left", row.left, -1.0), ("right", row.right, 1.0)):
            supports = (
                (boundary + outward * (member + 0.5) * member_width,
                 base_limit, row.head)
                for member in range(spec.jack_count_per_side)
            )
            kings = (
                (boundary + outward * (spec.jack_count_per_side + member + 0.5) * member_width,
                 base_limit, top_limit)
                for member in range(spec.king_count_per_side)
            )
            for center, lower, upper in (*supports, *kings):
                if min(column_right, center + half) - max(column_left, center - half) > _TOLERANCE_M:
                    lower, upper = max(lower, span.bottom_z), min(upper, span.top_z)
                    if upper - lower > _TOLERANCE_M:
                        blocked.append((lower, upper, (row.opening.id,)))
    merged = _merge_intervals(blocked)
    if not merged:
        return (span,)

    result: list[_StudSpan] = []
    cursor = span.bottom_z
    lower_key = span.lower_key
    ids = set(span.related_openings)
    for low, high, low_ids, high_ids in merged:
        if low - cursor > _TOLERANCE_M:
            related = tuple(sorted(ids | set(low_ids)))
            result.append(_StudSpan(
                station=span.station, slot=span.slot, bottom_z=cursor, top_z=low,
                role="cripple", related_openings=related,
                lower_key=lower_key, upper_key="below-support:" + "+".join(sorted(low_ids)),
            ))
        cursor = max(cursor, high)
        ids.update(low_ids)
        ids.update(high_ids)
        lower_key = "above-support:" + "+".join(sorted(high_ids))
    if span.top_z - cursor > _TOLERANCE_M:
        result.append(_StudSpan(
            station=span.station, slot=span.slot, bottom_z=cursor, top_z=span.top_z,
            role="cripple", related_openings=tuple(sorted(ids)),
            lower_key=lower_key, upper_key=span.upper_key,
        ))
    return tuple(result)


def _station_framing_spans(*, station: float, slot: str, frame: _WallFrame,
                           member_width: float, base_limit: float, top_limit: float,
                           openings: tuple[_OpeningLocal, ...],
                           header_depths: dict[str, float], sill_height: float,
                           specs: dict[str, OpeningFramingSpecification]) -> tuple[_StudSpan, ...]:
    return tuple(
        remaining
        for span in _free_cripple_spans(
            station=station, slot=slot, frame=frame, member_width=member_width,
            base_limit=base_limit, top_limit=top_limit, openings=openings,
            header_depths=header_depths, sill_height=sill_height,
        )
        for remaining in _subtract_opening_supports(
            span, member_width=member_width, base_limit=base_limit, top_limit=top_limit,
            openings=openings, specs=specs,
        )
    )


def _event_edges(events: dict[float, list[str]]) -> tuple[tuple[float, str], ...]:
    ordered = sorted(events.items())
    for (left, _), (right, _) in zip(ordered, ordered[1:]):
        if right - left <= _TOLERANCE_M:
            raise WallPresetError("opening boundary separation is below the 1e-9 m geometric tolerance")
    return tuple((coordinate, "+".join(sorted(set(labels)))) for coordinate, labels in ordered)


def _panel_edges(frame: _WallFrame, wall_height: float,
                 openings: tuple[_OpeningLocal, ...]
                 ) -> tuple[tuple[tuple[float, str], ...], tuple[tuple[float, str], ...]]:
    x_events: dict[float, list[str]] = {0.0: ["wall-start"], frame.length: ["wall-end"]}
    top_z = frame.base_z + wall_height
    z_events: dict[float, list[str]] = {
        frame.base_z: ["wall-base"], top_z: ["wall-top"],
    }
    for row in openings:
        x_events.setdefault(row.left, []).append(f"opening:{row.opening.id}:left")
        x_events.setdefault(row.right, []).append(f"opening:{row.opening.id}:right")
        z_events.setdefault(row.sill, []).append(f"opening:{row.opening.id}:sill")
        z_events.setdefault(row.head, []).append(f"opening:{row.opening.id}:head")
    return _event_edges(x_events), _event_edges(z_events)


def _panel_cell_count(x_edges: tuple[tuple[float, str], ...],
                      z_edges: tuple[tuple[float, str], ...],
                      openings: tuple[_OpeningLocal, ...]) -> int:
    x_positions = {value: index for index, (value, _) in enumerate(x_edges)}
    z_positions = {value: index for index, (value, _) in enumerate(z_edges)}
    total_cells = (len(x_edges) - 1) * (len(z_edges) - 1)
    void_cells = 0
    for row in openings:
        void_cells += (
            (x_positions[row.right] - x_positions[row.left])
            * (z_positions[row.head] - z_positions[row.sill])
        )
    physical_cells = total_cells - void_cells
    if physical_cells < 0:
        raise WallPresetError("opening partition grid is inconsistent with the source void rectangles")
    return physical_cells


def _panel_cells(x_edges: tuple[tuple[float, str], ...],
                 z_edges: tuple[tuple[float, str], ...],
                 openings: tuple[_OpeningLocal, ...]) -> tuple[_PanelCell, ...]:
    cells: list[_PanelCell] = []
    for (left, left_key), (right, right_key) in zip(x_edges, x_edges[1:]):
        for (bottom, bottom_key), (top, top_key) in zip(z_edges, z_edges[1:]):
            if right - left <= _TOLERANCE_M or top - bottom <= _TOLERANCE_M:
                raise WallPresetError("drywall partition produced a non-positive or sub-tolerance panel cell")
            cx, cz = (left + right) / 2.0, (bottom + top) / 2.0
            in_opening = any(row.left < cx < row.right and row.sill < cz < row.head for row in openings)
            if not in_opening:
                cells.append(_PanelCell(
                    left=left, right=right, bottom_z=bottom, top_z=top,
                    left_key=left_key, right_key=right_key,
                    bottom_key=bottom_key, top_key=top_key,
                ))
    return tuple(cells)


def _assignment_provenance(wall: Wall, openings: tuple[_OpeningLocal, ...],
                           request: WallAssemblyRequest,
                           host_framing_basis: str,
                           resolution: WallPresetResolution) -> tuple[Provenance, ...]:
    catalog, preset = resolution.catalog, resolution.preset
    selection_id = json.dumps(
        (catalog.catalog_id, catalog.catalog_revision, preset.preset_id, preset.preset_revision),
        separators=(",", ":"), ensure_ascii=False,
    )
    rows: list[Provenance] = [Provenance(
        source_kind="preset-selection",
        source_id=request.selected_by,
        source_element_id=selection_id,
        method="explicit-agent-or-user-preset-selection",
        confidence=1.0,
        derivation="user",
        scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by"),
        attributes={"host_framing_basis": host_framing_basis},
    )]
    for source in wall.provenance:
        if source.derivation == "observed":
            rows.append(Provenance(
                source_kind=source.source_kind, source_id=source.source_id,
                source_element_id=source.source_element_id, page=source.page,
                method=source.method, confidence=source.confidence,
                derivation="observed", scope_paths=("host_id", "input_digest"),
                attributes=dict(source.attributes),
            ))
    for opening in (row.opening for row in openings):
        for source in opening.provenance:
            if source.derivation == "observed":
                rows.append(Provenance(
                    source_kind=source.source_kind, source_id=source.source_id,
                    source_element_id=source.source_element_id, page=source.page,
                    method=source.method, confidence=source.confidence,
                    derivation="observed", scope_paths=("input_digest",),
                    attributes=dict(source.attributes),
                ))
    if not any(record.derivation == "observed" and "host_id" in (record.scope_paths or ())
               for record in rows):
        raise WallPresetError(f"{wall.id} has no observed source provenance for its geometry")
    return tuple(rows)


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


def _guard_dependencies_and_replacement(model: BuildingModel, wall: Wall,
                                        request: WallAssemblyRequest) -> None:
    if _assembly_id_exists(model, request.assembly_id):
        raise WallPresetError(f"assembly ID {request.assembly_id!r} is already used; no replacement is performed")
    if any(item.assembly_kind == "wall" and item.host_id == wall.id for item in model.building_assemblies):
        raise WallPresetError(
            f"requires_derived_rebuild: {wall.id} already has an active wall assembly; regeneration/replacement is not implemented"
        )
    if model.routes:
        raise WallPresetError(
            "requires_derived_rebuild: wall generation refuses models with existing routes; route dependency invalidation is not implemented"
        )


def _host_framing_basis(wall: Wall) -> str:
    """Reject known incompatible hosts and describe the evidence basis honestly.

    Only typed construction plus these narrowly scoped metadata fields are
    considered: ``stud_material``, ``stud_system``, and ``framing_system``.
    Fastener/tie materials and free-form unrelated prose are not framing facts.
    """
    if wall.construction is not None and wall.construction != "framed":
        raise WallPresetError(
            f"{wall.id} typed construction {wall.construction!r} is not a framed wall; "
            "the wood-stud preset cannot be applied"
        )

    metal_terms = re.compile(r"\b(?:metal|steel|cold[\s_-]*formed)\b", re.IGNORECASE)
    wood_terms = re.compile(r"\b(?:wood|timber|lumber)\b", re.IGNORECASE)
    steel_accessory_phrase = re.compile(
        r"\bwith\s+(?:steel|metal)\s+"
        r"(?:tie\s+connectors?|ties?|nail\s+plates?|nails?|fasteners?)\b",
        re.IGNORECASE,
    )
    field_names = {"stud_material", "stud_system", "framing_system"}
    wood_framing_evidence: str | None = None
    metal_stud_framing = False

    def visit(value: object, field_name: str | None = None) -> None:
        nonlocal wood_framing_evidence, metal_stud_framing
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
                next_field = normalized if normalized in field_names else field_name
                visit(child, next_field)
        elif isinstance(value, (tuple, list)):
            for child in value:
                visit(child, field_name)
        elif isinstance(value, str) and field_name in field_names:
            text = value.casefold().strip()
            wood_declared = bool(wood_terms.search(text))
            if wood_declared and wood_framing_evidence is None:
                wood_framing_evidence = (
                    "source_metadata_declares_wood_stud_material"
                    if field_name == "stud_material"
                    else "source_metadata_declares_wood_framing_system"
                )
            # Allow only an explicit accessory construction phrase attached to
            # a wood declaration. Any remaining metal/steel/cold-formed term
            # is treated as an incompatible or ambiguous structural system.
            classified_text = (
                steel_accessory_phrase.sub(" ", text) if wood_declared else text
            )
            if metal_terms.search(classified_text):
                metal_stud_framing = True

    visit(wall.attributes)
    if metal_stud_framing:
        raise WallPresetError(
            f"{wall.id} relevant source metadata declares metal/steel stud framing; "
            "the wood preset cannot be applied"
        )
    if wood_framing_evidence is not None:
        return wood_framing_evidence
    if wall.construction == "framed":
        return "source_declares_framed_construction;wood_studs_selected_by_preset"
    return "wood_framing_is_selected_preset_assumption;host_material_unknown"


def build_wall_assembly(
    model: BuildingModel,
    request: WallAssemblyRequest,
    *,
    catalog: BuildingAssemblyPresetCatalog = DEFAULT_PRESET_CATALOG,
) -> BuildingModel:
    """Return a candidate model with one generated wall assignment and parts.

    The input is immutable and untouched on any failure. This initial API only
    creates a new assembly; replacing an existing assembly or doing so in a
    model with routed systems requires a dependent rebuild and is refused.
    """
    if not isinstance(model, BuildingModel):
        raise WallPresetError("model must be a typed canonical BuildingModel")
    resolution = resolve_wall_preset(request, catalog=catalog)
    if resolution.preset.generator_kind != "wood_rect_stock_v2":
        raise WallPresetError(
            "the historical single-jamb v1 recipe is decode-only; new builds require the conventional v2 recipe"
        )
    wall = next((row for row in model.walls if row.id == request.wall_id), None)
    if wall is None:
        raise WallPresetError(f"requested wall {request.wall_id!r} is missing from the canonical model")
    host_framing_basis = _host_framing_basis(wall)
    _guard_dependencies_and_replacement(model, wall, request)
    if request.assembly_id == model.model_id:
        raise WallPresetError("assembly_id cannot collide with model_id")
    if not any(record.derivation == "observed" for record in wall.provenance):
        raise WallPresetError(f"{wall.id} has no observed source provenance for its geometry")

    params = resolution.parameter_map()
    value = {key: row.value for key, row in params.items()}
    frame = _frame_for_wall(wall)
    openings = _opening_locals(model, wall, frame, float(value["wall.opening_clearance_m"]))
    specs = {row.opening_id: row for row in request.opening_framing_specs}
    opening_ids = {row.opening.id for row in openings}
    if set(specs) != opening_ids:
        missing = sorted(opening_ids - set(specs))
        extra = sorted(set(specs) - opening_ids)
        raise WallPresetError(
            f"explicit opening framing specifications must exactly match the host door/window openings "
            f"(missing={missing!r}, extra={extra!r})"
        )
    if len(resolution.parameters) + len(specs) * len(_OPENING_SPEC_FIELDS) > ASSEMBLY_MAX_PARAMETERS:
        raise WallPresetError(
            f"opening framing specification snapshot exceeds the canonical {ASSEMBLY_MAX_PARAMETERS} resolved-parameter limit"
        )
    resolved_parameters = (*resolution.parameters, *_opening_spec_parameters(request, specs))

    axis_width = float(value["wall.stock.wall_axis_width_m"])
    framing_depth = float(value["wall.stack.framing_depth_m"])
    plate_thickness = float(value["wall.plate.thickness_m"])
    bottom_count = int(value["wall.plate.bottom_count"])
    top_count = int(value["wall.plate.top_count"])
    sill_height = float(value["wall.opening.sill_height_m"])
    drywall_thickness = float(value["wall.drywall.thickness_m"])
    left_layer = float(value["wall.stack.face_left.layer_1_m"])
    right_layer = float(value["wall.stack.face_right.layer_1_m"])
    spacing = float(value["wall.stud.spacing_m"])
    start_offset = float(value["wall.stud.start_offset_m"])
    end_offset = float(value["wall.stud.end_offset_m"])

    if abs(left_layer - drywall_thickness) > _TOLERANCE_M or abs(right_layer - drywall_thickness) > _TOLERANCE_M:
        raise WallPresetError(
            "one-layer v1 drywall thickness overrides must match both explicit wall stack face parameters"
        )
    stack_depth = math.fsum((framing_depth, left_layer, right_layer))
    if abs(stack_depth - wall.thickness_m) > _TOLERANCE_M:
        raise WallPresetError(
            f"unresolved wall thickness stack: framing + face layers = {stack_depth:.12g} m, "
            f"observed Wall.thickness_m = {wall.thickness_m:.12g} m; no source rewrite is allowed"
        )
    if spacing < axis_width - _TOLERANCE_M:
        raise WallPresetError("wall stud spacing cannot be smaller than the member axis width")
    if start_offset < axis_width / 2.0 - _TOLERANCE_M or end_offset < axis_width / 2.0 - _TOLERANCE_M:
        raise WallPresetError("stud start/end offsets must keep complete stock sections inside the wall axis")
    bottom_band = bottom_count * plate_thickness
    top_band = top_count * plate_thickness
    if bottom_band + top_band >= wall.height_m - _TOLERANCE_M:
        raise WallPresetError("selected plate stack leaves no positive stud height inside the host")
    base_limit = frame.base_z + bottom_band
    top_limit = frame.base_z + wall.height_m - top_band
    header_depths = {opening_id: float(spec.header_depth_m) for opening_id, spec in specs.items()}
    for opening in openings:
        spec = specs[opening.opening.id]
        built_up_thickness = (spec.header_ply_count * spec.header_ply_thickness_m
                              + (spec.header_ply_count - 1) * spec.header_spacer_thickness_m)
        if built_up_thickness > framing_depth + _TOLERANCE_M:
            raise WallPresetError(
                f"{opening.opening.id} header ply build-up exceeds the selected wall framing depth"
            )
        support_width = spec.jack_count_per_side * axis_width
        if abs(spec.bearing_m - support_width) > _TOLERANCE_M:
            raise WallPresetError(
                f"{opening.opening.id} header end bearing must exactly span the jack support pack "
                "so both header ends meet jacks and king studs without gaps"
            )
        if opening.head + spec.header_depth_m > top_limit + _TOLERANCE_M:
            raise WallPresetError(
                f"{opening.opening.id} selected header depth overlaps the top-plate band or wall host"
            )
        if opening.opening.opening_type.casefold() == "window" and \
                opening.sill - sill_height < base_limit - _TOLERANCE_M:
            raise WallPresetError(f"{opening.opening.id} rough sill overlaps the required bottom-plate band")
    if sill_height >= wall.height_m:
        raise WallPresetError("selected window sill section exceeds the wall height")
    _rough_frame_overlap_guard(
        openings, frame=frame, member_width=axis_width, specs=specs,
        header_depths=header_depths, sill_height=sill_height,
        top_plate_bottom=top_limit, bottom_plate_top=base_limit,
    )

    stations = _wall_stations(frame, start_offset, end_offset, spacing, axis_width)
    # Stable layout slot keys are semantic stations. Build just scalar span
    # specifications first; bound all geometry allocation to the contract cap.
    # Reserve only the vertical intervals physically occupied by kings/jacks
    # at each regular grid column. This preserves any clear grid-derived
    # cripple above a jack/header while never duplicating its support member.
    # First count spans without retaining a component-sized collection. The
    # maximum temporary per-station interval set is the capped opening count.
    framing_station_count = 0
    for station, slot in stations:
        for span in _station_framing_spans(
            station=station, slot=slot, frame=frame, member_width=axis_width,
            base_limit=base_limit, top_limit=top_limit, openings=openings,
            header_depths=header_depths, sill_height=sill_height, specs=specs,
        ):
            if len(_stud_span_part_key(span)) > _MAX_PART_KEY_LENGTH:
                raise WallPresetError(
                    "component_key_too_long: framing slot evidence exceeds the canonical part-key limit"
                )
            framing_station_count += 1
            if framing_station_count > ASSEMBLY_MAX_COMPONENTS:
                raise WallPresetError(
                    f"component_cap_exceeded: predicted wall framing components exceed {ASSEMBLY_MAX_COMPONENTS}"
                )
    x_edges, z_edges = _panel_edges(frame, wall.height_m, openings)
    panel_cell_count = _panel_cell_count(x_edges, z_edges, openings)
    opening_components_count = sum(
        2 * specs[row.opening.id].king_count_per_side
        + 2 * specs[row.opening.id].jack_count_per_side
        + specs[row.opening.id].header_ply_count
        + ((specs[row.opening.id].header_ply_count - 1)
           if specs[row.opening.id].header_spacer_thickness_m > _TOLERANCE_M else 0)
        + (1 if row.opening.opening_type.casefold() == "window" else 0)
        for row in openings
    )
    predicted = (
        framing_station_count
        + top_count
        + bottom_count * (1 + sum(row.opening.opening_type.casefold() == "door" for row in openings))
        + opening_components_count
        + 2 * panel_cell_count
    )
    if predicted > ASSEMBLY_MAX_COMPONENTS:
        raise WallPresetError(
            f"component_cap_exceeded: predicted {predicted} wall components exceeds {ASSEMBLY_MAX_COMPONENTS}"
        )
    if max((len(label) for _, label in (*x_edges, *z_edges)), default=0) * 4 + 128 > _MAX_PART_KEY_LENGTH:
        raise WallPresetError("component_key_too_long: panel grid evidence labels exceed the canonical part-key limit")

    filtered_spans = tuple(
        span
        for station, slot in stations
        for span in _station_framing_spans(
            station=station, slot=slot, frame=frame, member_width=axis_width,
            base_limit=base_limit, top_limit=top_limit, openings=openings,
            header_depths=header_depths, sill_height=sill_height, specs=specs,
        )
    )
    panel_cells = _panel_cells(x_edges, z_edges, openings)

    # The source wall frame is an aggregate spatial host only. Its thickness and
    # height remain unchanged and are part of the input digest.
    wall_frame = WallAssemblyFrame(
        origin=frame.start,
        axis=Vector3(x=frame.ux, y=frame.uy, z=0.0),
        left_normal=Vector3(x=frame.nx, y=frame.ny, z=0.0),
        base_z_m=frame.base_z,
    )

    parts: list[AssemblyComponent] = []

    def add_box(part_key: str, kind: str, *, station: float, through: float,
                center_z: float, size_x: float, size_y: float, size_z: float,
                opening_id: str | None = None,
                framing_metadata: dict[str, str | int | float | bool] | None = None) -> None:
        parts.append(_make_box_component(
            assembly_id=request.assembly_id, level_id=wall.level_id, part_key=part_key,
            kind=kind, frame=frame, station=station, through=through,
            center_z=center_z, size_x=size_x, size_y=size_y, size_z=size_z,
            catalog=resolution.catalog, preset=resolution.preset, opening_id=opening_id,
            framing_metadata=framing_metadata,
        ))

    # Bottom plates are separated only at door voids. Window sills remain above
    # the continuous base plate. The boundary points are explicit door facts.
    doors = sorted((row for row in openings if row.opening.opening_type.casefold() == "door"),
                   key=lambda row: (row.left, row.opening.id))
    plate_spans: list[tuple[float, float, str, str]] = []
    cursor = 0.0
    lower_label = "wall-start"
    for row in doors:
        if row.left - cursor > _TOLERANCE_M:
            plate_spans.append((cursor, row.left, lower_label, f"opening:{row.opening.id}:left"))
        cursor = row.right
        lower_label = f"opening:{row.opening.id}:right"
    if frame.length - cursor > _TOLERANCE_M:
        plate_spans.append((cursor, frame.length, lower_label, "wall-end"))
    if not doors:
        plate_spans = [(0.0, frame.length, "wall-start", "wall-end")]
    for layer in range(bottom_count):
        z0 = frame.base_z + layer * plate_thickness
        for left, right, left_key, right_key in plate_spans:
            if right - left <= _TOLERANCE_M:
                continue
            key = f"bottom_plate:layer-{layer+1}:span:{left_key}:to:{right_key}"
            add_box(key, "bottom_plate", station=(left + right) / 2.0, through=0.0,
                    center_z=z0 + plate_thickness / 2.0,
                    size_x=right - left, size_y=framing_depth, size_z=plate_thickness)

    # Two independently identified top plate courses are supported by this
    # recipe; the typed count parameter is bounded to 1..2 by the catalog.
    for layer in range(top_count):
        z0 = frame.base_z + wall.height_m - top_band + layer * plate_thickness
        label = "lower" if layer == 0 else "upper"
        add_box(f"top_plate:course-{label}", "top_plate", station=frame.length / 2.0,
                through=0.0, center_z=z0 + plate_thickness / 2.0,
                size_x=frame.length, size_y=framing_depth, size_z=plate_thickness)

    # Base regular studs/cripples. Every station's solid vertical interval is
    # the complement of its exact opening/rough-frame intersections.
    for span in filtered_spans:
        if span.top_z - span.bottom_z <= _TOLERANCE_M:
            continue
        role = span.role
        key = _stud_span_part_key(span)
        opening_id = (None if role == "stud" else
                      span.related_openings[0] if len(span.related_openings) == 1 else None)
        add_box(key, role, station=span.station, through=0.0,
                center_z=(span.bottom_z + span.top_z) / 2.0,
                size_x=axis_width, size_y=framing_depth,
                size_z=span.top_z - span.bottom_z, opening_id=opening_id,
                framing_metadata={"framing_role": "ordinary" if role == "stud" else "cripple"})

    # Explicitly selected conventional geometry: king studs continue from
    # bottom-plate top to top-plate underside; jack/trimmer studs meet the
    # header underside; every header ply sits on the selected jack support set.
    # This is not structural member sizing or approval.
    for row in openings:
        opening_id = row.opening.id
        spec = specs[opening_id]
        sill = row.opening.opening_type.casefold() == "window"
        jack_height = row.head - base_limit
        king_height = top_limit - base_limit
        if jack_height <= _TOLERANCE_M:
            raise WallPresetError(f"{opening_id} leaves no positive jack/trimmer height")
        if king_height <= _TOLERANCE_M:
            raise WallPresetError(f"{opening_id} leaves no positive full-height king-stud span")
        for side, boundary, outward in (("left", row.left, -1.0), ("right", row.right, 1.0)):
            for member in range(spec.jack_count_per_side):
                station = boundary + outward * (member + 0.5) * axis_width
                add_box(
                    f"stud:opening:{opening_id}:jack:{side}:member-{member+1}", "stud",
                    station=station, through=0.0, center_z=base_limit + jack_height / 2.0,
                    size_x=axis_width, size_y=framing_depth, size_z=jack_height,
                    framing_metadata={"framing_role": "jack", "opening_id": opening_id,
                                      "side": side, "member": member + 1,
                                      "support_surface": "header_underside"},
                )
            for member in range(spec.king_count_per_side):
                station = boundary + outward * (
                    spec.jack_count_per_side + member + 0.5
                ) * axis_width
                add_box(
                    f"stud:opening:{opening_id}:king:{side}:member-{member+1}", "stud",
                    station=station, through=0.0, center_z=base_limit + king_height / 2.0,
                    size_x=axis_width, size_y=framing_depth, size_z=king_height,
                    framing_metadata={"framing_role": "king", "opening_id": opening_id,
                                      "side": side, "member": member + 1,
                                      "support_span": "between_plates"},
                )
        header_stack_thickness = (spec.header_ply_count * spec.header_ply_thickness_m
                                  + (spec.header_ply_count - 1) * spec.header_spacer_thickness_m)
        first_through = -header_stack_thickness / 2.0
        header_length = row.opening.size.x + 2.0 * spec.bearing_m
        for ply in range(spec.header_ply_count):
            through = first_through + spec.header_ply_thickness_m / 2.0 + ply * (
                spec.header_ply_thickness_m + spec.header_spacer_thickness_m
            )
            add_box(
                f"header:opening:{opening_id}:ply-{ply+1}", "header",
                station=row.station, through=through,
                center_z=row.head + spec.header_depth_m / 2.0,
                size_x=header_length, size_y=spec.header_ply_thickness_m,
                size_z=spec.header_depth_m, opening_id=opening_id,
                framing_metadata={"framing_role": "header_ply", "opening_id": opening_id,
                                  "ply": ply + 1, "ply_count": spec.header_ply_count,
                                  "header_depth_m": spec.header_depth_m,
                                  "header_ply_thickness_m": spec.header_ply_thickness_m,
                                  "header_spacer_thickness_m": spec.header_spacer_thickness_m,
                                  "bearing_m": spec.bearing_m,
                                  "sizing_status": "explicit_geometry_input_not_engineered_sizing"},
            )
            if ply < spec.header_ply_count - 1 and spec.header_spacer_thickness_m > _TOLERANCE_M:
                spacer_through = through + spec.header_ply_thickness_m / 2.0 \
                    + spec.header_spacer_thickness_m / 2.0
                add_box(
                    f"header:opening:{opening_id}:spacer-{ply+1}", "header",
                    station=row.station, through=spacer_through,
                    center_z=row.head + spec.header_depth_m / 2.0,
                    size_x=header_length, size_y=spec.header_spacer_thickness_m,
                    size_z=spec.header_depth_m, opening_id=opening_id,
                    framing_metadata={"framing_role": "header_spacer", "opening_id": opening_id,
                                      "between_plies": f"{ply+1}-{ply+2}",
                                      "spacer_thickness_m": spec.header_spacer_thickness_m,
                                      "material_basis": "unknown"},
                )
        if sill:
            add_box(f"sill:opening:{opening_id}", "sill",
                    station=row.station, through=0.0,
                    center_z=row.sill - sill_height / 2.0,
                    size_x=row.opening.size.x,
                    size_y=framing_depth, size_z=sill_height,
                    opening_id=opening_id,
                    framing_metadata={"framing_role": "rough_sill", "opening_id": opening_id})
    # The station-complement pass emits clear-bay cripples above headers and
    # below window sills; support packs are excluded from ordinary stations.
    parts_by_key: dict[str, AssemblyComponent] = {}
    for part in parts:
        if part.part_key in parts_by_key:
            raise WallPresetError(f"duplicate semantic component key: {part.part_key}")
        parts_by_key[part.part_key] = part

    # Split each face into exact rectangles around opening voids. A panel cell
    # never crosses an opening boundary; board stock seams/waste are not modeled.
    for face in resolution.preset.drywall_faces:
        sign = 1.0 if face == "left" else -1.0
        thickness = left_layer if face == "left" else right_layer
        center_through = sign * (framing_depth / 2.0 + thickness / 2.0)
        for cell in panel_cells:
            key = (
                f"drywall_panel:face-{face}:layer-1:"
                f"x:{cell.left_key}:to:{cell.right_key}:"
                f"z:{cell.bottom_key}:to:{cell.top_key}"
            )
            if len(key) > _MAX_PART_KEY_LENGTH:
                raise WallPresetError("component_key_too_long: drywall panel key exceeds the canonical part-key limit")
            if key in parts_by_key:
                raise WallPresetError(f"duplicate semantic component key: {key}")
            parts_by_key[key] = _make_panel_component(
                assembly_id=request.assembly_id, level_id=wall.level_id,
                part_key=key, face=face, layer=1, cell=cell,
                center_through=center_through, thickness=thickness,
                frame=frame, catalog=resolution.catalog, preset=resolution.preset,
            )

    components = tuple(sorted(parts_by_key.values(), key=lambda row: row.id))
    if len(components) > ASSEMBLY_MAX_COMPONENTS:
        # This should be unreachable because the scalar preflight above bounds
        # the exact count, but keep the contract cap as a final invariant.
        raise WallPresetError(
            f"component_cap_exceeded: generated {len(components)} components exceeds {ASSEMBLY_MAX_COMPONENTS}"
        )

    upgraded = model if model.schema_version in ASSEMBLY_SCHEMA_VERSIONS else upgrade_to_v1_6(model)
    provenance = _assignment_provenance(wall, openings, request, host_framing_basis, resolution)
    draft = BuildingAssembly(
        id=request.assembly_id,
        assembly_kind="wall",
        level_id=wall.level_id,
        host_id=wall.id,
        host_representation="aggregate_host",
        catalog_id=resolution.catalog.catalog_id,
        catalog_revision=resolution.catalog.catalog_revision,
        preset_id=resolution.preset.preset_id,
        preset_revision=resolution.preset.preset_revision,
        selected_by=request.selected_by,
        generator_version=WALL_GENERATOR_VERSION,
        resolved_parameters=resolved_parameters,
        component_ids=tuple(component.id for component in components),
        input_digest="0" * 64,
        output_digest="0" * 64,
        wall_frame=wall_frame,
        provenance=provenance,
    )
    input_digest, output_digest = compute_assembly_digests(upgraded, draft, components)
    assignment = replace(draft, input_digest=input_digest, output_digest=output_digest)
    # Construction validates all candidate records and digests. Failure leaves
    # the original immutable model untouched.
    return replace(
        upgraded,
        building_assemblies=(*upgraded.building_assemblies, assignment),
        assembly_components=(*upgraded.assembly_components, *components),
    )


__all__ = [
    "WallPresetError", "ParameterOverride", "OpeningFramingSpecification", "WallAssemblyRequest",
    "WallPresetResolution", "resolve_wall_preset", "build_wall_assembly",
]
