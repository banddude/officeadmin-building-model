"""Pure explicit-preset producer for bounded wall-hosted door/window inserts.

The producer consumes already interpreted canonical wall/opening facts.  It
never edits an opening, infers a preset, or claims construction/manufacturer or
code status.  Its simple box bars and flat panels are intentionally honest
modeling geometry, not detailed product CAD.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import re

from oabm.assembly_geometry import GeometryInputError, rotate_vector_xyzw
from oabm.model import (
    ASSEMBLY_MAX_COMPONENTS,
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
    compute_assembly_digests,
    stable_assembly_component_id,
    upgrade_to_v1_6,
)

from .opening_insert_presets import (
    DEFAULT_OPENING_INSERT_CATALOG,
    OPENING_INSERT_GENERATOR_VERSION,
    InsertParameterDefinition,
    OpeningInsertPreset,
    OpeningInsertPresetCatalog,
    OpeningInsertPresetError,
)

_TOLERANCE_M = 1e-9
_CANONICAL_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_MAX_PART_KEY_LENGTH = 512
_MAX_PARAMETER_OVERRIDES = 5
_MAX_ASSIGNMENT_PROVENANCE_RECORDS = 256
_DETAIL_NOTE = (
    "simple parameterized illustrative geometry; not manufacturer CAD, "
    "material identification, or a construction/code approval"
)


class OpeningInsertError(ContractError):
    """The selected opening-insert recipe cannot be resolved without guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningInsertParameterOverride:
    """Caller-authored typed length override; never mistaken for plan evidence."""

    parameter_id: str
    value_kind: str
    value: float
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.parameter_id, str) or not self.parameter_id.strip():
            raise OpeningInsertError("opening-insert override requires a non-empty parameter_id")
        if self.value_kind != "length" or self.unit != "m":
            raise OpeningInsertError(f"{self.parameter_id} override must be a length in canonical metres")
        if (isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or
                not math.isfinite(float(self.value))):
            raise OpeningInsertError(f"{self.parameter_id} override must be a finite metre value")


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningInsertAssemblyRequest:
    """An explicit immutable preset selection for one existing aperture."""

    assembly_id: str
    opening_id: str
    selected_by: str
    preset_id: str
    parameter_overrides: tuple[OpeningInsertParameterOverride, ...] = ()

    def __post_init__(self) -> None:
        for label in ("assembly_id", "opening_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not _CANONICAL_ID.fullmatch(value):
                raise OpeningInsertError(f"OpeningInsertAssemblyRequest.{label} is not a canonical ID")
        for label in ("selected_by", "preset_id"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise OpeningInsertError(f"OpeningInsertAssemblyRequest.{label} must be non-empty bounded text")
        if not isinstance(self.parameter_overrides, tuple):
            raise OpeningInsertError("parameter_overrides must be an immutable tuple")
        # Cap before reading any caller-supplied rows or constructing a map.
        if len(self.parameter_overrides) > _MAX_PARAMETER_OVERRIDES:
            raise OpeningInsertError(
                f"parameter_override_cap_exceeded: at most {_MAX_PARAMETER_OVERRIDES} typed overrides are supported"
            )
        if any(not isinstance(row, OpeningInsertParameterOverride) for row in self.parameter_overrides):
            raise OpeningInsertError("parameter_overrides must contain typed OpeningInsertParameterOverride records")
        ids = tuple(row.parameter_id for row in self.parameter_overrides)
        if len(ids) != len(set(ids)):
            raise OpeningInsertError("parameter overrides must have unique parameter IDs")


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningInsertPresetResolution:
    catalog: OpeningInsertPresetCatalog
    preset: OpeningInsertPreset
    selected_by: str
    parameters: tuple[ResolvedParameter, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.parameters, tuple) or
                any(not isinstance(row, ResolvedParameter) for row in self.parameters)):
            raise OpeningInsertError("resolved opening-insert parameters must be an immutable typed tuple")
        if tuple(row.parameter_id for row in self.parameters) != tuple(
            row.parameter_id for row in self.preset.parameters
        ):
            raise OpeningInsertError("resolved parameter snapshot is incomplete or out of preset order")

    def parameter_map(self) -> dict[str, ResolvedParameter]:
        return {row.parameter_id: row for row in self.parameters}


def _parameter_evidence_id(catalog_revision: str, preset: OpeningInsertPreset,
                           parameter_id: str) -> str:
    return json.dumps(
        (catalog_revision, preset.preset_id, preset.preset_revision, parameter_id),
        separators=(",", ":"), ensure_ascii=False,
    )


def resolve_opening_insert_preset(
    request: OpeningInsertAssemblyRequest,
    *,
    catalog: OpeningInsertPresetCatalog = DEFAULT_OPENING_INSERT_CATALOG,
) -> OpeningInsertPresetResolution:
    """Resolve one explicit recipe and typed default/override snapshot."""
    if not isinstance(request, OpeningInsertAssemblyRequest):
        raise OpeningInsertError("request must be a typed OpeningInsertAssemblyRequest")
    if not isinstance(catalog, OpeningInsertPresetCatalog):
        raise OpeningInsertError("catalog must be an immutable OpeningInsertPresetCatalog")
    try:
        preset = catalog.opening_insert_preset(request.preset_id)
    except ContractError as exc:
        raise OpeningInsertError(str(exc)) from exc
    if (catalog.catalog_id == DEFAULT_OPENING_INSERT_CATALOG.catalog_id and
            catalog.catalog_revision == DEFAULT_OPENING_INSERT_CATALOG.catalog_revision):
        published = next((row for row in DEFAULT_OPENING_INSERT_CATALOG.presets
                          if row.preset_id == preset.preset_id and
                          row.preset_revision == preset.preset_revision), None)
        if published is not None and preset != published:
            raise OpeningInsertError(
                f"published opening-insert identity {catalog.catalog_id}@{catalog.catalog_revision}/"
                f"{preset.preset_id}@{preset.preset_revision} is reserved; changed content requires a new revision"
            )

    definitions = {row.parameter_id: row for row in preset.parameters}
    overrides = {row.parameter_id: row for row in request.parameter_overrides}
    unknown = set(overrides) - set(definitions)
    if unknown:
        raise OpeningInsertError(f"unsupported opening-insert overrides: {sorted(unknown)!r}")

    resolved: list[ResolvedParameter] = []
    for definition in preset.parameters:
        override = overrides.get(definition.parameter_id)
        if override is None:
            value = definition.default_value
            origin = "preset_default"
            evidence = Provenance(
                source_kind="preset_parameter",
                source_id=catalog.catalog_id,
                source_element_id=_parameter_evidence_id(
                    catalog.catalog_revision, preset, definition.parameter_id,
                ),
                method="immutable-catalog-default-resolution",
                confidence=1.0,
                derivation="inferred",
                scope_paths=("value", "unit", "origin"),
                attributes={
                    "default_basis": "illustrative_modeling_assumption",
                    "note": definition.note,
                    "not_manufacturer_cad": True,
                },
            )
        else:
            if override.value_kind != "length" or override.unit != "m":
                raise OpeningInsertError(
                    f"{definition.parameter_id} override must be a length in canonical metres"
                )
            _validate_override(definition, override.value)
            value = override.value
            origin = "user_override"
            evidence = Provenance(
                source_kind="user_override",
                source_id=request.selected_by,
                source_element_id=definition.parameter_id,
                method="explicit-opening-insert-parameter-override",
                confidence=1.0,
                derivation="user",
                scope_paths=("value", "unit", "origin"),
                attributes={
                    "catalog_id": catalog.catalog_id,
                    "catalog_revision": catalog.catalog_revision,
                    "preset_id": preset.preset_id,
                    "preset_revision": preset.preset_revision,
                },
            )
        resolved.append(ResolvedParameter(
            parameter_id=definition.parameter_id,
            value_kind="length",
            value=value,
            unit="m",
            origin=origin,
            provenance=(evidence,),
            source_entity_id=None,
            source_field_path=None,
        ))
    return OpeningInsertPresetResolution(
        catalog=catalog, preset=preset, selected_by=request.selected_by,
        parameters=tuple(resolved),
    )


def _validate_override(definition: InsertParameterDefinition, value: object) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(float(value))):
        raise OpeningInsertError(f"{definition.parameter_id} override must be a finite length")
    if not definition.minimum <= value <= definition.maximum:
        raise OpeningInsertError(
            f"{definition.parameter_id} override must be in {definition.minimum}..{definition.maximum} m"
        )


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
    frame: _WallFrame
    station: float
    left: float
    right: float
    sill: float
    head: float


def _wall_frame(wall: Wall) -> _WallFrame:
    if len(wall.centerline.points) != 2:
        raise OpeningInsertError("unsupported wall orientation: insert producer requires a two-point straight wall")
    start, end = wall.centerline.points
    if abs(start.z - end.z) > _TOLERANCE_M:
        raise OpeningInsertError("unsupported wall orientation: centerline must be horizontal")
    dx, dy = end.x - start.x, end.y - start.y
    length = math.hypot(dx, dy)
    if not math.isfinite(length) or length <= _TOLERANCE_M:
        raise OpeningInsertError("unsupported wall orientation: centerline has no positive horizontal length")
    ux, uy = dx / length, dy / length
    return _WallFrame(start=start, ux=ux, uy=uy, nx=-uy, ny=ux,
                      length=length, base_z=start.z)


def _opening_local(opening: Opening, wall: Wall, frame: _WallFrame) -> _OpeningLocal:
    if opening.opening_type.casefold() not in {"door", "window"}:
        raise OpeningInsertError(
            f"unsupported opening type {opening.opening_type!r}; only explicit door/window apertures are supported"
        )
    _require_geometry_evidence(
        opening, {"host_id", "opening_type", "pose", "size"},
        f"{opening.id} aperture",
    )
    _require_geometry_evidence(
        wall, {"level_id", "centerline", "thickness_m", "height_m"},
        f"{wall.id} parent wall",
    )

    try:
        rotation = opening.pose.rotation
        actual_axes = tuple(rotate_vector_xyzw(axis, rotation) for axis in (
            (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
        ))
    except GeometryInputError as exc:
        raise OpeningInsertError(f"{opening.id} quaternion is unsupported for an opening insert") from exc
    expected_axes = ((frame.ux, frame.uy, 0.0), (frame.nx, frame.ny, 0.0), (0.0, 0.0, 1.0))
    if any(any(abs(actual[axis] - expected[axis]) > _TOLERANCE_M for axis in range(3))
           for actual, expected in zip(actual_axes, expected_axes)):
        raise OpeningInsertError(f"{opening.id} orientation is not aligned to the ordered wall frame")

    dx = opening.pose.position.x - frame.start.x
    dy = opening.pose.position.y - frame.start.y
    station = dx * frame.ux + dy * frame.uy
    through = dx * frame.nx + dy * frame.ny
    if abs(through) > _TOLERANCE_M:
        raise OpeningInsertError(f"{opening.id} center is not on the parent wall axis plane")
    if abs(opening.size.y - wall.thickness_m) > _TOLERANCE_M:
        raise OpeningInsertError(f"{opening.id}.size.y must equal the unchanged observed wall thickness")
    left, right = station - opening.size.x / 2.0, station + opening.size.x / 2.0
    sill = opening.pose.position.z - opening.size.z / 2.0
    head = opening.pose.position.z + opening.size.z / 2.0
    if left < -_TOLERANCE_M or right > frame.length + _TOLERANCE_M:
        raise OpeningInsertError(f"{opening.id} horizontal extent exceeds its parent wall")
    if sill < frame.base_z - _TOLERANCE_M or head > frame.base_z + wall.height_m + _TOLERANCE_M:
        raise OpeningInsertError(f"{opening.id} vertical extent exceeds its parent wall")
    if opening.opening_type.casefold() == "door" and abs(sill - frame.base_z) > _TOLERANCE_M:
        raise OpeningInsertError(f"{opening.id} door sill must coincide with the parent wall base")
    return _OpeningLocal(opening=opening, frame=frame, station=station,
                         left=left, right=right, sill=sill, head=head)


def _require_geometry_evidence(source: Opening | Wall, required: set[str], label: str) -> None:
    """Match the canonical host-evidence rule before allocating any child geometry."""
    observed = tuple(row for row in source.provenance if row.derivation == "observed")
    if not observed:
        raise OpeningInsertError(f"{label} has no observed source provenance for geometry")
    if any(row.scope_paths is None for row in observed):
        return
    covered = {field for row in observed for field in (row.scope_paths or ())}
    missing = required - covered
    if missing:
        raise OpeningInsertError(
            f"{label} observed provenance does not cover geometry field(s): {', '.join(sorted(missing))}"
        )


def _assignment_provenance(opening: Opening, wall: Wall,
                           request: OpeningInsertAssemblyRequest,
                           resolution: OpeningInsertPresetResolution) -> tuple[Provenance, ...]:
    catalog, preset = resolution.catalog, resolution.preset
    selection_id = json.dumps(
        (catalog.catalog_id, catalog.catalog_revision, preset.preset_id, preset.preset_revision),
        separators=(",", ":"), ensure_ascii=False,
    )
    rows: list[Provenance] = [Provenance(
        source_kind="preset-selection",
        source_id=request.selected_by,
        source_element_id=selection_id,
        method="explicit-agent-or-user-opening-insert-preset-selection",
        confidence=1.0,
        derivation="user",
        scope_paths=("catalog_id", "catalog_revision", "preset_id", "preset_revision",
                     "selected_by", "host_id", "input_digest"),
    )]
    for source in opening.provenance:
        if source.derivation != "observed":
            continue
        rows.append(Provenance(
            source_kind=source.source_kind, source_id=source.source_id,
            source_element_id=source.source_element_id, page=source.page,
            method=source.method, confidence=source.confidence,
            derivation="observed", scope_paths=("host_id", "input_digest"),
            attributes=dict(source.attributes),
        ))
    for source in wall.provenance:
        if source.derivation != "observed":
            continue
        rows.append(Provenance(
            source_kind=source.source_kind, source_id=source.source_id,
            source_element_id=source.source_element_id, page=source.page,
            method=source.method, confidence=source.confidence,
            derivation="observed", scope_paths=("input_digest",),
            attributes=dict(source.attributes),
        ))
    if not any(row.derivation == "observed" and "host_id" in (row.scope_paths or ())
               for row in rows):
        raise OpeningInsertError(f"{opening.id} has no observed source evidence binding its host and digest")
    if not any(row.derivation == "observed" and "input_digest" in (row.scope_paths or ())
               for row in rows if any(
                   row.source_kind == source.source_kind and row.source_id == source.source_id and
                   row.source_element_id == source.source_element_id
                   for source in wall.provenance if source.derivation == "observed"
               )):
        raise OpeningInsertError(f"{wall.id} has no observed parent-host evidence for the insert digest")
    return tuple(rows)


def _component_provenance(resolution: OpeningInsertPresetResolution,
                          part_key: str) -> tuple[Provenance, ...]:
    catalog, preset = resolution.catalog, resolution.preset
    return (Provenance(
        source_kind="building_assembly_generator",
        source_id=catalog.catalog_id,
        source_element_id=json.dumps(
            (catalog.catalog_revision, preset.preset_id, preset.preset_revision, part_key),
            separators=(",", ":"), ensure_ascii=False,
        ),
        method=OPENING_INSERT_GENERATOR_VERSION,
        confidence=1.0,
        derivation="inferred",
        scope_paths=("geometry", "geometry_basis", "material_id", "material_basis"),
        attributes={
            "detail_level": "simple_illustrative_parameterized_geometry",
            "disclosure": _DETAIL_NOTE,
            "material_basis": "unknown",
            "preset_id": preset.preset_id,
            "preset_revision": preset.preset_revision,
            "generator_version": OPENING_INSERT_GENERATOR_VERSION,
        },
    ),)


def _geometry_rotation(frame: _WallFrame) -> Quaternion:
    heading = math.atan2(frame.uy, frame.ux)
    return Quaternion(z=math.sin(heading / 2.0), w=math.cos(heading / 2.0))


def _make_box(*, assembly_id: str, level_id: str, part_key: str,
              component_kind: str, opening_id: str, local: _OpeningLocal,
              left: float, right: float, bottom: float, top: float,
              depth: float, resolution: OpeningInsertPresetResolution) -> AssemblyComponent:
    if len(part_key) > _MAX_PART_KEY_LENGTH:
        raise OpeningInsertError("component_key_too_long: insert semantic component key is over the limit")
    if (right - left <= _TOLERANCE_M or top - bottom <= _TOLERANCE_M or
            depth <= _TOLERANCE_M):
        raise OpeningInsertError(f"{part_key} has a non-positive or sub-tolerance solid dimension")
    geometry = Box3D(
        pose=Pose(
            position=local.frame.point((left + right) / 2.0, 0.0, (bottom + top) / 2.0),
            rotation=_geometry_rotation(local.frame),
        ),
        size=Size3(x=right - left, y=depth, z=top - bottom),
    )
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
        opening_id=opening_id,
        provenance=_component_provenance(resolution, part_key),
    )


def _make_glazing(*, assembly_id: str, level_id: str, opening_id: str,
                  local: _OpeningLocal, left: float, right: float,
                  sill: float, head: float, thickness: float,
                  resolution: OpeningInsertPresetResolution) -> AssemblyComponent:
    part_key = "window:glazing:single-flat-panel"
    if len(part_key) > _MAX_PART_KEY_LENGTH or right - left <= _TOLERANCE_M or head - sill <= _TOLERANCE_M:
        raise OpeningInsertError("window glazing panel must have positive bounded in-aperture dimensions")
    points = (
        local.frame.point(left, 0.0, sill),
        local.frame.point(right, 0.0, sill),
        local.frame.point(right, 0.0, head),
        local.frame.point(left, 0.0, head),
    )
    return AssemblyComponent(
        id=stable_assembly_component_id(assembly_id, part_key),
        assembly_id=assembly_id,
        part_key=part_key,
        level_id=level_id,
        component_kind="window_glazing",
        geometry=PanelGeometry(
            boundary=Polygon3D(points=points),
            thickness_m=thickness,
            normal=Vector3(x=local.frame.nx, y=local.frame.ny, z=0.0),
        ),
        geometry_basis="specified_parameterized_geometry",
        material_id=None,
        material_basis="unknown",
        opening_id=opening_id,
        provenance=_component_provenance(resolution, part_key),
    )


def _door_components(*, request: OpeningInsertAssemblyRequest,
                     local: _OpeningLocal, wall: Wall,
                     resolution: OpeningInsertPresetResolution) -> tuple[AssemblyComponent, ...]:
    values = {key: float(row.value) for key, row in resolution.parameter_map().items()}
    opening, left, right, sill, head = local.opening, local.left, local.right, local.sill, local.head
    face = values["door.frame.face_width_m"]
    leaf_thickness = values["door.leaf.thickness_m"]
    side_gap = values["door.leaf.side_gap_m"]
    head_gap = values["door.leaf.head_gap_m"]
    bottom_gap = values["door.leaf.bottom_gap_m"]
    leaf_left, leaf_right = left + face + side_gap, right - face - side_gap
    leaf_bottom, leaf_top = sill + bottom_gap, head - face - head_gap
    if (face <= _TOLERANCE_M or face >= opening.size.x / 2.0 or
            head - sill <= face + _TOLERANCE_M or leaf_right - leaf_left <= _TOLERANCE_M or
            leaf_top - leaf_bottom <= _TOLERANCE_M):
        raise OpeningInsertError("selected door frame/gap assumptions do not fit the exact observed aperture")
    if leaf_thickness <= _TOLERANCE_M or leaf_thickness > opening.size.y + _TOLERANCE_M:
        raise OpeningInsertError("selected door leaf thickness must be positive and fit the aperture depth")
    # The jamb pair spans the full aperture height.  The head occupies only
    # the inner width, so the two bars butt at a face without double volume or
    # leaving empty outer corner squares.
    parts = (
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="door:frame:jamb:left", component_kind="door_frame", opening_id=opening.id,
            local=local, left=left, right=left + face, bottom=sill, top=head,
            depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="door:frame:jamb:right", component_kind="door_frame", opening_id=opening.id,
            local=local, left=right - face, right=right, bottom=sill, top=head,
            depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="door:frame:head", component_kind="door_frame", opening_id=opening.id,
            local=local, left=left + face, right=right - face, bottom=head - face, top=head,
            depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="door:leaf:closed-flat-panel", component_kind="door_leaf", opening_id=opening.id,
            local=local, left=leaf_left, right=leaf_right,
            bottom=leaf_bottom, top=leaf_top, depth=leaf_thickness, resolution=resolution),
    )
    return parts


def _window_components(*, request: OpeningInsertAssemblyRequest,
                       local: _OpeningLocal, wall: Wall,
                       resolution: OpeningInsertPresetResolution) -> tuple[AssemblyComponent, ...]:
    values = {key: float(row.value) for key, row in resolution.parameter_map().items()}
    opening, left, right, sill, head = local.opening, local.left, local.right, local.sill, local.head
    face = values["window.frame.face_width_m"]
    glazing_thickness = values["window.glazing.thickness_m"]
    edge_gap = values["window.glazing.edge_gap_m"]
    glass_left, glass_right = left + face + edge_gap, right - face - edge_gap
    glass_sill, glass_head = sill + face + edge_gap, head - face - edge_gap
    inner_width, inner_height = opening.size.x - 2.0 * face, opening.size.z - 2.0 * face
    if (face <= _TOLERANCE_M or inner_width <= _TOLERANCE_M or inner_height <= _TOLERANCE_M or
            glass_right - glass_left <= _TOLERANCE_M or glass_head - glass_sill <= _TOLERANCE_M):
        raise OpeningInsertError("selected window frame/gap assumptions do not fit the exact observed aperture")
    if (glazing_thickness <= _TOLERANCE_M or
            glazing_thickness > opening.size.y + _TOLERANCE_M):
        raise OpeningInsertError("selected glazing thickness must be positive and fit the aperture depth")
    # The jamb pair spans the full aperture height; sill and head rails fit
    # between it.  This partitions the perimeter into non-overlapping solids.
    parts = (
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="window:frame:jamb:left", component_kind="window_frame", opening_id=opening.id,
            local=local, left=left, right=left + face, bottom=sill,
            top=head, depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="window:frame:jamb:right", component_kind="window_frame", opening_id=opening.id,
            local=local, left=right - face, right=right, bottom=sill,
            top=head, depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="window:frame:sill", component_kind="window_frame", opening_id=opening.id,
            local=local, left=left + face, right=right - face, bottom=sill,
            top=sill + face, depth=opening.size.y, resolution=resolution),
        _make_box(assembly_id=request.assembly_id, level_id=wall.level_id,
            part_key="window:frame:head", component_kind="window_frame", opening_id=opening.id,
            local=local, left=left + face, right=right - face, bottom=head - face,
            top=head, depth=opening.size.y, resolution=resolution),
        _make_glazing(assembly_id=request.assembly_id, level_id=wall.level_id,
            opening_id=opening.id, local=local, left=glass_left, right=glass_right,
            sill=glass_sill, head=glass_head, thickness=glazing_thickness,
            resolution=resolution),
    )
    return parts


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
    return assembly_id == model.model_id or any(
        row.id == assembly_id for collection in collections for row in collection
    )


def _guard_assignment(model: BuildingModel, opening: Opening,
                      request: OpeningInsertAssemblyRequest) -> None:
    if _assembly_id_exists(model, request.assembly_id):
        raise OpeningInsertError(f"assembly ID {request.assembly_id!r} is already used; no replacement is performed")
    if any(row.assembly_kind == "opening_insert" and row.host_id == opening.id
           for row in model.building_assemblies):
        raise OpeningInsertError(
            f"requires_derived_rebuild: {opening.id} already has an active insert assignment; "
            "regeneration is unsupported"
        )
    if model.routes:
        raise OpeningInsertError(
            "requires_derived_rebuild: opening-insert generation refuses models with routes; "
            "route dependency invalidation is not implemented"
        )


def build_opening_insert_assembly(
    model: BuildingModel,
    request: OpeningInsertAssemblyRequest,
    *,
    catalog: OpeningInsertPresetCatalog = DEFAULT_OPENING_INSERT_CATALOG,
) -> BuildingModel:
    """Append one exact in-aperture insert as a new canonical opening assignment.

    Only a simple closed single-leaf door recipe and a fixed single-glazing
    window recipe are executable.  Both require an aggregate wall assembly so
    GLB keeps the opaque source wall from masking the inserted geometry.  The
    source Opening and Wall objects are never rewritten.  Existing insert
    assignments or routes are refused rather than implicitly regenerated.
    """
    if not isinstance(model, BuildingModel):
        raise OpeningInsertError("model must be a typed canonical BuildingModel")
    if not isinstance(request, OpeningInsertAssemblyRequest):
        raise OpeningInsertError("request must be a typed OpeningInsertAssemblyRequest")
    resolution = resolve_opening_insert_preset(request, catalog=catalog)
    opening = next((row for row in model.openings if row.id == request.opening_id), None)
    if opening is None:
        raise OpeningInsertError(f"requested opening {request.opening_id!r} is missing")
    wall = next((row for row in model.walls if row.id == opening.host_id), None)
    if wall is None:
        raise OpeningInsertError(f"{opening.id} must reference an existing Wall host")
    if 1 + len(opening.provenance) + len(wall.provenance) > _MAX_ASSIGNMENT_PROVENANCE_RECORDS:
        raise OpeningInsertError(
            "provenance_cap_exceeded: selected opening and parent wall evidence would exceed "
            f"the {_MAX_ASSIGNMENT_PROVENANCE_RECORDS}-record assignment limit"
        )
    _guard_assignment(model, opening, request)
    if not any(assignment.assembly_kind == "wall" and assignment.host_id == wall.id and
               assignment.host_representation == "aggregate_host"
               for assignment in model.building_assemblies):
        raise OpeningInsertError(
            f"{opening.id} insert requires an aggregate wall assembly so its source opaque wall is suppressed in GLB"
        )
    if resolution.preset.opening_type != opening.opening_type.casefold():
        raise OpeningInsertError(
            f"preset {resolution.preset.preset_id!r} does not match explicit opening type {opening.opening_type!r}"
        )
    local = _opening_local(opening, wall, _wall_frame(wall))

    # Closed recipes have finite, known child counts.  Check these and the
    # canonical per-assignment ceiling before allocating any component list.
    predicted_count = 4 if resolution.preset.generator_kind == "door_closed_single_leaf_v1" else 5
    if predicted_count > ASSEMBLY_MAX_COMPONENTS:
        raise OpeningInsertError(
            f"component_cap_exceeded: predicted {predicted_count} insert components exceed {ASSEMBLY_MAX_COMPONENTS}"
        )

    if resolution.preset.generator_kind == "door_closed_single_leaf_v1":
        components = _door_components(
            request=request, local=local, wall=wall, resolution=resolution,
        )
    elif resolution.preset.generator_kind == "window_fixed_single_glazing_v1":
        components = _window_components(
            request=request, local=local, wall=wall, resolution=resolution,
        )
    else:  # The catalog type already refuses this, retained as a fail-closed interpreter guard.
        raise OpeningInsertError(f"unsupported finite generator token {resolution.preset.generator_kind!r}")
    components = tuple(sorted(components, key=lambda row: row.id))
    if len(components) != predicted_count or len(components) > ASSEMBLY_MAX_COMPONENTS:
        raise OpeningInsertError(
            "component_cap_exceeded: generated insert count contradicts its finite recipe preflight"
        )
    existing_component_ids = {row.id for row in model.assembly_components}
    if existing_component_ids.intersection(row.id for row in components):
        raise OpeningInsertError("generated stable component ID already exists; no replacement is performed")

    upgraded = model if model.schema_version in ASSEMBLY_SCHEMA_VERSIONS else upgrade_to_v1_6(model)
    provenance = _assignment_provenance(opening, wall, request, resolution)
    draft = BuildingAssembly(
        id=request.assembly_id,
        assembly_kind="opening_insert",
        level_id=wall.level_id,
        host_id=opening.id,
        host_representation="source_opening_void",
        catalog_id=resolution.catalog.catalog_id,
        catalog_revision=resolution.catalog.catalog_revision,
        preset_id=resolution.preset.preset_id,
        preset_revision=resolution.preset.preset_revision,
        selected_by=request.selected_by,
        generator_version=OPENING_INSERT_GENERATOR_VERSION,
        resolved_parameters=resolution.parameters,
        component_ids=tuple(row.id for row in components),
        input_digest="0" * 64,
        output_digest="0" * 64,
        provenance=provenance,
    )
    input_digest, output_digest = compute_assembly_digests(upgraded, draft, components)
    assignment = replace(draft, input_digest=input_digest, output_digest=output_digest)
    return replace(
        upgraded,
        building_assemblies=(*upgraded.building_assemblies, assignment),
        assembly_components=(*upgraded.assembly_components, *components),
    )


__all__ = [
    "OpeningInsertError", "OpeningInsertParameterOverride",
    "OpeningInsertAssemblyRequest", "OpeningInsertPresetResolution",
    "resolve_opening_insert_preset", "build_opening_insert_assembly",
]
