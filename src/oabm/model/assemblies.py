"""Candidate canonical building-assembly records (contract 1.6 draft).

These records describe already resolved, caller-selected assembly intent and
physical children.  They deliberately do not contain recipes, plan
recognition, or a general geometry engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any

from oabm.assembly_geometry import GeometryInputError, extrude_simple_section_m, rotate_vector_xyzw

from .common import (
    Box3D, ContractError, Point3, Polygon3D, Polyline3D, Pose, Provenance,
    Quaternion, Size3, Vector3, _finite, _positive, _validate_id,
    _validate_json_value,
)

ASSEMBLY_KINDS = frozenset({"wall", "opening_insert", "suspended_ceiling"})
HOST_REPRESENTATIONS = frozenset({"aggregate_host", "source_opening_void"})
ASSEMBLY_COMPONENT_KINDS = frozenset({
    "stud", "top_plate", "bottom_plate", "header", "sill", "cripple",
    "drywall_panel", "door_leaf", "door_frame", "window_frame",
    "window_glazing", "tbar_main_runner", "tbar_cross_runner", "tile",
    "hanger_wire", "deck_panel",
})
GEOMETRY_BASES = frozenset({
    "specified_parameterized_geometry", "manufacturer_actual_geometry",
    "caller_selected_envelope",
})
MATERIAL_BASES = frozenset({
    "unknown", "plan_specified", "manufacturer_actual", "caller_selected",
})
PARAMETER_KINDS = frozenset({"length", "angle", "count", "token", "material_ref", "boolean"})
PARAMETER_ORIGINS = frozenset({"plan_observed", "preset_default", "user_override"})
PARAMETER_UNITS = {"length": "m", "angle": "rad", "count": "each",
                   "token": "1", "material_ref": "1", "boolean": "1"}
ASSEMBLY_MAX_PARAMETERS = 128
ASSEMBLY_MAX_COMPONENTS = 4096
ASSEMBLY_MAX_PROFILE_POINTS = 64
ASSEMBLY_MAX_PANEL_VERTICES = 128
ASSEMBLY_MAX_WIRE_SEGMENTS = 32
ASSEMBLY_GEOMETRY_TOLERANCE_M = 1e-9
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, label: str, *, max_length: int = 2048) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ContractError(f"{label} must be non-empty text")


def _unique_ids(values: tuple[str, ...], label: str, *, nonempty: bool = True) -> None:
    if (not isinstance(values, tuple) or (nonempty and not values) or
            len(values) != len(set(values))):
        raise ContractError(f"{label} must contain unique canonical IDs")
    for value in values:
        _validate_id(value, label)


def _provenance(values: tuple[Provenance, ...], label: str, *, required: bool = True) -> None:
    if (not isinstance(values, tuple) or (required and not values) or
            any(not isinstance(value, Provenance) for value in values)):
        raise ContractError(f"{label} must be a tuple of typed provenance records")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedParameter:
    """One explicitly resolved preset value, with its type and field evidence."""

    parameter_id: str
    value_kind: str
    value: int | bool | float | str
    unit: str
    origin: str
    provenance: tuple[Provenance, ...]
    source_entity_id: str | None
    source_field_path: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.parameter_id, str) or not _TOKEN_RE.fullmatch(self.parameter_id):
            raise ContractError("resolved parameter_id must be a stable token")
        if not isinstance(self.value_kind, str) or self.value_kind not in PARAMETER_KINDS:
            raise ContractError(f"{self.parameter_id}.value_kind is unsupported")
        if not isinstance(self.origin, str) or self.origin not in PARAMETER_ORIGINS:
            raise ContractError(f"{self.parameter_id}.origin is unsupported")
        if self.unit != PARAMETER_UNITS[self.value_kind]:
            raise ContractError(
                f"{self.parameter_id}.unit must be {PARAMETER_UNITS[self.value_kind]!r} "
                f"for {self.value_kind}"
            )
        if self.value_kind in {"length", "angle"}:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ContractError(f"{self.parameter_id}.value must be a finite number")
            _finite(self.value, f"{self.parameter_id}.value")
        elif self.value_kind == "count":
            if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
                raise ContractError(f"{self.parameter_id}.value must be a nonnegative integer")
        elif self.value_kind in {"token", "material_ref"}:
            _text(self.value, f"{self.parameter_id}.value")
            if self.value_kind == "material_ref":
                _validate_id(self.value, f"{self.parameter_id}.value")
        elif not isinstance(self.value, bool):
            raise ContractError(f"{self.parameter_id}.value must be boolean")
        _provenance(self.provenance, f"{self.parameter_id}.provenance")
        scopes: set[str] = set()
        for record in self.provenance:
            if record.scope_paths is None:
                raise ContractError(
                    f"{self.parameter_id}.provenance must be field-qualified"
                )
            if set(record.scope_paths) - {
                    "value", "unit", "origin", "source_entity_id", "source_field_path"}:
                raise ContractError(
                    f"{self.parameter_id}.provenance has unsupported field scopes"
                )
            scopes.update(record.scope_paths)
        if not {"value", "unit", "origin"}.issubset(scopes):
            raise ContractError(
                f"{self.parameter_id}.provenance must cover value, unit and origin"
            )
        derivations = {record.derivation for record in self.provenance}
        if self.origin == "plan_observed":
            if "observed" not in derivations:
                raise ContractError("plan_observed parameters require observed source provenance")
            _validate_id(self.source_entity_id, f"{self.parameter_id}.source_entity_id")
            if (not isinstance(self.source_field_path,str) or
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*",self.source_field_path)):
                raise ContractError("plan_observed parameters require a canonical source_field_path")
            if not {"source_entity_id","source_field_path"}.issubset(scopes):
                raise ContractError("plan_observed provenance must cover its typed source reference")
        elif self.source_entity_id is not None or self.source_field_path is not None:
            raise ContractError("only plan_observed parameters may carry a source entity/field")
        if self.origin == "user_override":
            if not any(record.derivation=="user" and record.source_kind=="user_override" and
                       record.source_element_id==self.parameter_id for record in self.provenance):
                raise ContractError("user_override evidence must bind the explicit parameter identity")
        if self.origin == "preset_default":
            if not any(record.derivation=="inferred" and record.source_kind=="preset_parameter" and
                       record.source_element_id is not None for record in self.provenance):
                raise ContractError("preset_default evidence must identify an inferred preset parameter")


@dataclass(frozen=True, slots=True, kw_only=True)
class Point2D:
    x: float
    y: float

    def __post_init__(self) -> None:
        _finite(self.x, "Point2D.x")
        _finite(self.y, "Point2D.y")


@dataclass(frozen=True, slots=True, kw_only=True)
class Profile2D:
    """Named C/U/T profile dimensions; no open-ended B-rep or scripting."""

    kind: str
    overall_width_m: float
    overall_depth_m: float
    web_thickness_m: float
    flange_thickness_m: float
    return_lip_m: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"c_profile", "u_profile", "t_profile"}:
            raise ContractError("Profile2D.kind must be c_profile, u_profile or t_profile")
        for name in ("overall_width_m", "overall_depth_m", "web_thickness_m",
                     "flange_thickness_m"):
            _positive(getattr(self, name), f"Profile2D.{name}")
        _finite(self.return_lip_m, "Profile2D.return_lip_m")
        if self.return_lip_m < 0:
            raise ContractError("Profile2D.return_lip_m must be nonnegative")
        if self.web_thickness_m >= self.overall_width_m:
            raise ContractError("Profile2D.web_thickness_m must be less than width")
        if self.kind == "t_profile":
            if (self.flange_thickness_m >= self.overall_depth_m or
                    self.return_lip_m != 0):
                raise ContractError("T profile requires a thinner flange and no return lips")
        elif self.kind == "u_profile":
            if (2 * self.flange_thickness_m >= self.overall_depth_m or
                    self.return_lip_m != 0):
                raise ContractError("U profile requires two thinner flanges and no return lips")
        else:
            if (2 * self.flange_thickness_m >= self.overall_depth_m or
                    not 0 < 2*self.return_lip_m < self.overall_depth_m - 2*self.flange_thickness_m or
                    self.flange_thickness_m >= self.overall_width_m-self.web_thickness_m):
                raise ContractError(
                    "C profile requires separated, explicit return lips that fit inside its flanges"
                )
        _validate_section_outline(self.outline_xy_m(), "Profile2D")

    def outline_xy_m(self) -> tuple[tuple[float, float], ...]:
        """Canonical simple cross-section, opening toward +X for C/U."""
        w, d, web, flange, lip = (self.overall_width_m, self.overall_depth_m,
                                  self.web_thickness_m, self.flange_thickness_m,
                                  self.return_lip_m)
        if self.kind == "t_profile":
            stem_left = (w - web) / 2
            return ((0, 0), (w, 0), (w, flange),
                    (stem_left + web, flange), (stem_left + web, d),
                    (stem_left, d), (stem_left, flange), (0, flange))
        if self.kind == "c_profile":
            # Return lips turn toward the web from each flange's free edge.
            # Their clear gap is positive by the dimension check above.
            return (
                (0, 0), (w, 0), (w, flange+lip), (w-flange, flange+lip),
                (w-flange, flange), (web, flange), (web, d-flange),
                (w-flange, d-flange), (w-flange, d-flange-lip),
                (w, d-flange-lip), (w, d), (0, d),
            )
        return ((0, 0), (w, 0), (w, flange), (web, flange),
                (web, d-flange), (w, d-flange), (w, d), (0, d))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileOutline2D:
    """A bounded, source-bound manufacturer section outline (no holes)."""

    source_id: str
    points_m: tuple[Point2D, ...]
    provenance: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        _validate_id(self.source_id, "ProfileOutline2D.source_id")
        if (not isinstance(self.points_m, tuple) or len(self.points_m) < 3 or
                len(self.points_m) > ASSEMBLY_MAX_PROFILE_POINTS or
                any(not isinstance(point, Point2D) for point in self.points_m)):
            raise ContractError(
                f"ProfileOutline2D.points_m must contain 3..{ASSEMBLY_MAX_PROFILE_POINTS} points"
            )
        _validate_section_outline(tuple((p.x,p.y) for p in self.points_m), "ProfileOutline2D")
        _provenance(self.provenance, "ProfileOutline2D.provenance")
        if not any(p.source_id == self.source_id and p.derivation == "observed" and
                   p.scope_paths is not None and "points_m" in p.scope_paths
                   for p in self.provenance):
            raise ContractError(
                "manufacturer outline requires source-bound observed points_m provenance"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileExtrusion:
    kind: str = field(default="profile_extrusion", init=False)
    profile: Profile2D | ProfileOutline2D
    pose: Pose
    length_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.profile, (Profile2D, ProfileOutline2D)):
            raise ContractError("ProfileExtrusion.profile must be a supported typed Profile2D")
        if not isinstance(self.pose, Pose):
            raise ContractError("ProfileExtrusion.pose must be a typed Pose")
        _positive(self.length_m, "ProfileExtrusion.length_m")


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelGeometry:
    kind: str = field(default="panel", init=False)
    boundary: Polygon3D
    thickness_m: float
    normal: Vector3

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, Polygon3D):
            raise ContractError("PanelGeometry.boundary must be a typed simple Polygon3D")
        if len(self.boundary.points) > ASSEMBLY_MAX_PANEL_VERTICES:
            raise ContractError(f"panel exceeds {ASSEMBLY_MAX_PANEL_VERTICES} vertices")
        _positive(self.thickness_m, "PanelGeometry.thickness_m")
        if not isinstance(self.normal, Vector3):
            raise ContractError("PanelGeometry.normal must be a typed Vector3")
        if abs(self.normal.magnitude - 1.0) > 1e-9:
            raise ContractError("PanelGeometry.normal must be a unit vector")
        _validate_planar_polygon(self.boundary, self.normal)


@dataclass(frozen=True, slots=True, kw_only=True)
class WireSweep:
    kind: str = field(default="wire_sweep", init=False)
    centerline: Polyline3D
    diameter_m: float
    end_treatment: str

    def __post_init__(self) -> None:
        if not isinstance(self.centerline, Polyline3D):
            raise ContractError("WireSweep.centerline must be a typed Polyline3D")
        if len(self.centerline.points) - 1 > ASSEMBLY_MAX_WIRE_SEGMENTS:
            raise ContractError(f"wire exceeds {ASSEMBLY_MAX_WIRE_SEGMENTS} straight segments")
        _positive(self.diameter_m, "WireSweep.diameter_m")
        if self.end_treatment != "cut":
            raise ContractError("WireSweep currently supports cut ends only; hooks and loops need geometry")


AssemblyGeometry = Box3D | ProfileExtrusion | PanelGeometry | WireSweep


@dataclass(frozen=True, slots=True, kw_only=True)
class AssemblyRecord:
    """ID/provenance base intentionally outside the legacy Entity hierarchy.

    This prevents existing generic consumers from silently treating a 1.6
    assembly record as an already-supported legacy object collection. The
    canonical BuildingModel still validates these IDs and provenance itself.
    """

    id: str
    name: str | None = None
    confidence: float = 1.0
    provenance: tuple[Provenance, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id(self.id)
        confidence=_finite(self.confidence,f"{self.id}.confidence")
        if not 0.0<=confidence<=1.0:
            raise ContractError(f"{self.id}.confidence must be between 0 and 1")
        if self.name is not None and not isinstance(self.name,str):
            raise ContractError(f"{self.id}.name must be a string or null")
        if not isinstance(self.provenance,tuple) or any(not isinstance(row,Provenance) for row in self.provenance):
            raise ContractError(f"{self.id}.provenance must be a tuple of Provenance records")
        _validate_json_value(self.attributes)


@dataclass(frozen=True, slots=True, kw_only=True)
class HangerAttachment:
    top_host_id: str
    top_anchor: Point3
    bottom_runner_id: str
    bottom_anchor: Point3

    def __post_init__(self) -> None:
        _validate_id(self.top_host_id, "HangerAttachment.top_host_id")
        _validate_id(self.bottom_runner_id, "HangerAttachment.bottom_runner_id")
        if not isinstance(self.top_anchor, Point3) or not isinstance(self.bottom_anchor, Point3):
            raise ContractError("HangerAttachment anchors must be typed Point3 values")


@dataclass(frozen=True, slots=True, kw_only=True)
class AssemblyComponent(AssemblyRecord):
    assembly_id: str
    part_key: str
    level_id: str
    component_kind: str
    geometry: AssemblyGeometry
    geometry_basis: str
    material_id: str | None
    material_basis: str
    opening_id: str | None = None
    hanger_attachment: HangerAttachment | None = None

    def __post_init__(self) -> None:
        super(AssemblyComponent, self).__post_init__()
        for name in ("assembly_id", "level_id"):
            _validate_id(getattr(self, name), f"{self.id}.{name}")
        _text(self.part_key, f"{self.id}.part_key")
        if self.part_key.startswith("index:"):
            raise ContractError("part_key must be a semantic role/layout key, not a list index")
        if self.id != stable_assembly_component_id(self.assembly_id,self.part_key):
            raise ContractError("AssemblyComponent.id must be stable for its assembly_id and semantic part_key")
        if self.component_kind not in ASSEMBLY_COMPONENT_KINDS:
            raise ContractError(f"{self.id}.component_kind is unsupported")
        if not isinstance(self.geometry, (Box3D, ProfileExtrusion, PanelGeometry, WireSweep)):
            raise ContractError(f"{self.id}.geometry is outside the finite assembly geometry union")
        if self.geometry_basis not in GEOMETRY_BASES:
            raise ContractError(f"{self.id}.geometry_basis is unsupported")
        if self.material_basis not in MATERIAL_BASES:
            raise ContractError(f"{self.id}.material_basis is unsupported")
        if self.material_id is None:
            if self.material_basis != "unknown":
                raise ContractError("known material basis requires an explicit material_id")
        else:
            _validate_id(self.material_id, f"{self.id}.material_id")
            if self.material_basis == "unknown":
                raise ContractError("unknown material basis cannot carry a material_id")
        _provenance(self.provenance, f"{self.id}.provenance")
        geometry_scopes = {scope for record in self.provenance
                           for scope in (record.scope_paths or ())}
        if "geometry" not in geometry_scopes:
            raise ContractError(f"{self.id}.provenance must be field-qualified to geometry")
        if self.geometry_basis == "manufacturer_actual_geometry":
            if any(record.derivation != "observed" for record in self.provenance
                   if "geometry" in (record.scope_paths or ())):
                raise ContractError("manufacturer_actual_geometry requires observed geometry provenance")
        if self.material_id is not None:
            material_records = [record for record in self.provenance
                                if record.scope_paths is not None and
                                "material_id" in record.scope_paths]
            required_derivation = {
                "plan_specified": "observed",
                "manufacturer_actual": "observed",
                "caller_selected": "user",
            }[self.material_basis]
            if not material_records:
                raise ContractError(
                    f"{self.material_basis} material requires field-scoped material_id provenance"
                )
            if any(record.derivation != required_derivation for record in material_records):
                raise ContractError(
                    f"{self.material_basis} material requires {required_derivation} material_id provenance"
                )
        if self.opening_id is not None:
            _validate_id(self.opening_id, f"{self.id}.opening_id")
        if self.component_kind == "hanger_wire":
            if not isinstance(self.geometry, WireSweep) or not isinstance(self.hanger_attachment, HangerAttachment):
                raise ContractError("hanger_wire requires a straight wire geometry and one typed attachment")
        elif self.hanger_attachment is not None:
            raise ContractError("only hanger_wire may carry a HangerAttachment")


@dataclass(frozen=True, slots=True, kw_only=True)
class WallAssemblyFrame:
    origin: Point3
    axis: Vector3
    left_normal: Vector3
    base_z_m: float

    def __post_init__(self) -> None:
        if not all(isinstance(v, cls) for v, cls in (
                (self.origin, Point3), (self.axis, Vector3), (self.left_normal, Vector3))):
            raise ContractError("WallAssemblyFrame requires typed origin, axis and normal")
        _finite(self.base_z_m, "WallAssemblyFrame.base_z_m")
        if abs(self.base_z_m - self.origin.z) > ASSEMBLY_GEOMETRY_TOLERANCE_M:
            raise ContractError("WallAssemblyFrame base z must equal its origin z")
        if abs(self.axis.magnitude - 1) > 1e-9 or abs(self.left_normal.magnitude - 1) > 1e-9:
            raise ContractError("WallAssemblyFrame axis and left_normal must be unit vectors")
        if abs(self.axis.z) > 1e-9 or abs(self.left_normal.z) > 1e-9:
            raise ContractError("wall assembly frame must be horizontal")
        dot = self.axis.x*self.left_normal.x + self.axis.y*self.left_normal.y
        expected = (-self.axis.y, self.axis.x)
        if (abs(dot) > 1e-9 or abs(self.left_normal.x-expected[0]) > 1e-9 or
                abs(self.left_normal.y-expected[1]) > 1e-9):
            raise ContractError("WallAssemblyFrame left_normal must be the ordered-axis left normal")


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildingAssembly(AssemblyRecord):
    assembly_kind: str
    level_id: str
    host_id: str
    host_representation: str
    catalog_id: str
    catalog_revision: str
    preset_id: str
    preset_revision: str
    selected_by: str
    generator_version: str
    resolved_parameters: tuple[ResolvedParameter, ...]
    component_ids: tuple[str, ...]
    input_digest: str
    output_digest: str
    wall_frame: WallAssemblyFrame | None = None
    deck_id: str | None = None

    def __post_init__(self) -> None:
        super(BuildingAssembly, self).__post_init__()
        if self.assembly_kind not in ASSEMBLY_KINDS:
            raise ContractError("assembly_kind must be wall, opening_insert or suspended_ceiling")
        _validate_id(self.level_id, f"{self.id}.level_id")
        _validate_id(self.host_id, f"{self.id}.host_id")
        expected_host = "source_opening_void" if self.assembly_kind == "opening_insert" else "aggregate_host"
        if self.host_representation != expected_host:
            raise ContractError(f"{self.id}.host_representation must be {expected_host!r}")
        for name in ("catalog_id", "catalog_revision", "preset_id", "preset_revision", "selected_by", "generator_version"):
            _text(getattr(self, name), f"{self.id}.{name}")
        if (not isinstance(self.resolved_parameters, tuple) or
                len(self.resolved_parameters) > ASSEMBLY_MAX_PARAMETERS or
                any(not isinstance(value, ResolvedParameter) for value in self.resolved_parameters)):
            raise ContractError(f"{self.id}.resolved_parameters must contain at most {ASSEMBLY_MAX_PARAMETERS} typed records")
        parameter_ids = [value.parameter_id for value in self.resolved_parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ContractError(f"{self.id} has duplicate resolved parameter IDs")
        _unique_ids(self.component_ids, f"{self.id}.component_ids")
        if len(self.component_ids) > ASSEMBLY_MAX_COMPONENTS:
            raise ContractError(f"{self.id} exceeds {ASSEMBLY_MAX_COMPONENTS} components")
        for name in ("input_digest", "output_digest"):
            if not isinstance(getattr(self, name), str) or not _SHA256_RE.fullmatch(getattr(self, name)):
                raise ContractError(f"{self.id}.{name} must be a lowercase SHA-256 digest")
        if self.assembly_kind == "wall":
            if not isinstance(self.wall_frame, WallAssemblyFrame):
                raise ContractError("wall assembly requires an explicit typed wall axis/base/normal frame")
            if self.deck_id is not None:
                raise ContractError("wall assembly cannot claim a suspended-ceiling deck")
        elif self.wall_frame is not None:
            raise ContractError("only a wall assembly may carry WallAssemblyFrame")
        if self.assembly_kind == "suspended_ceiling":
            if self.deck_id is None:
                raise ContractError("suspended ceiling requires exactly one existing or generated deck identity")
            _validate_id(self.deck_id, f"{self.id}.deck_id")
        elif self.assembly_kind != "wall" and self.deck_id is not None:
            raise ContractError("only a suspended ceiling may claim a deck identity")
        _provenance(self.provenance, f"{self.id}.provenance")
        selection_scopes = {scope for record in self.provenance
                           for scope in (record.scope_paths or ())}
        if not {"catalog_id", "catalog_revision", "preset_id", "preset_revision",
                "selected_by", "host_id", "input_digest"}.issubset(selection_scopes):
            raise ContractError(
                f"{self.id}.provenance must field-qualify selection and the source-bound input digest"
            )


def stable_assembly_component_id(assembly_id: str, part_key: str) -> str:
    """Stable component identity based on assembly ID plus semantic layout key."""
    from .common import stable_id
    _validate_id(assembly_id, "assembly_id")
    _text(part_key, "part_key")
    if part_key.startswith("index:"):
        raise ContractError("component identity cannot use a mutable list index")
    return stable_id("assembly-component", f"{assembly_id}:{part_key}")


def _validate_section_outline(points: tuple[tuple[float,float],...], label: str) -> None:
    try:
        # This dependency-neutral kernel owns robust translated-area math and
        # the strict non-touching/simple-outline checks used by actual meshes.
        extrude_simple_section_m(points,1.0,((0.0,0.0,0.0),(0.0,0.0,0.0,1.0)))
    except GeometryInputError as exc:
        raise ContractError(f"{label} outline is not a bounded simple section: {exc}") from exc


def _validate_planar_polygon(polygon: Polygon3D, normal: Vector3) -> None:
    points = polygon.points
    origin = points[0]
    for point in points[1:]:
        offset = (point.x-origin.x, point.y-origin.y, point.z-origin.z)
        if abs(offset[0]*normal.x + offset[1]*normal.y + offset[2]*normal.z) > 1e-9:
            raise ContractError("panel polygon must be planar and match its explicit normal")
    # Drop the dominant normal axis for a stable simple-polygon check.
    axis = max(range(3), key=lambda i: abs((normal.x, normal.y, normal.z)[i]))
    coords = tuple((p.y,p.z) if axis == 0 else (p.x,p.z) if axis == 1 else (p.x,p.y) for p in points)
    _validate_section_outline(coords,"PanelGeometry.boundary")


def rotate_vector(vector: tuple[float,float,float], q: Quaternion) -> tuple[float,float,float]:
    try:
        return rotate_vector_xyzw(vector,q)
    except GeometryInputError as exc:
        raise ContractError(f"invalid rotation for assembly geometry: {exc}") from exc


def _point_on_segment_2d(point, start, end, tolerance: float = 1e-9) -> bool:
    dx,dy=end[0]-start[0],end[1]-start[1]
    squared=dx*dx+dy*dy
    if squared == 0: return math.hypot(point[0]-start[0],point[1]-start[1]) <= tolerance
    fraction=max(0.0,min(1.0,((point[0]-start[0])*dx+(point[1]-start[1])*dy)/squared))
    return math.hypot(point[0]-start[0]-fraction*dx,point[1]-start[1]-fraction*dy) <= tolerance


def _point_in_polygon_2d(point, polygon) -> bool:
    if any(_point_on_segment_2d(point,a,b) for a,b in zip(polygon,polygon[1:]+polygon[:1])):
        return True
    inside=False; x,y=point
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        if (a[1]>y)!=(b[1]>y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            inside=not inside
    return inside


def point_on_component_surface(component: AssemblyComponent, point: Point3,
                               tolerance_m: float = ASSEMBLY_GEOMETRY_TOLERANCE_M) -> bool:
    """Exact supported-surface test for explicit hanger attachment anchors."""
    geometry=component.geometry
    from .common import Box3D
    if isinstance(geometry, Box3D):
        pose=geometry.pose; q=pose.rotation
        # Inverse-rotate the world-relative point.
        conjugate=Quaternion(x=-q.x,y=-q.y,z=-q.z,w=q.w)
        local=rotate_vector((point.x-pose.position.x,point.y-pose.position.y,point.z-pose.position.z),conjugate)
        half=(geometry.size.x/2,geometry.size.y/2,geometry.size.z/2)
        return any(abs(abs(local[i])-half[i])<=tolerance_m and
                   all(abs(local[j])<=half[j]+tolerance_m for j in range(3) if j!=i) for i in range(3))
    if isinstance(geometry, PanelGeometry):
        coords=(point.x,point.y,point.z); normal=(geometry.normal.x,geometry.normal.y,geometry.normal.z)
        polygon=tuple((p.x,p.y,p.z) for p in geometry.boundary.points)
        for sign in (-1,1):
            offset=tuple(coords[i]-sign*normal[i]*geometry.thickness_m/2 for i in range(3))
            if abs(sum((offset[i]-polygon[0][i])*normal[i] for i in range(3))) <= tolerance_m:
                axis=max(range(3),key=lambda i:abs(normal[i]))
                project=lambda p: (p[1],p[2]) if axis==0 else (p[0],p[2]) if axis==1 else (p[0],p[1])
                if _point_in_polygon_2d(project(offset),tuple(project(p) for p in polygon)): return True
        return False
    if isinstance(geometry, ProfileExtrusion):
        pose=geometry.pose; q=pose.rotation
        local=rotate_vector((point.x-pose.position.x,point.y-pose.position.y,point.z-pose.position.z),
                            Quaternion(x=-q.x,y=-q.y,z=-q.z,w=q.w))
        profile=geometry.profile.outline_xy_m() if isinstance(geometry.profile,Profile2D) else tuple((p.x,p.y) for p in geometry.profile.points_m)
        if min(abs(local[2]),abs(local[2]-geometry.length_m)) <= tolerance_m and _point_in_polygon_2d((local[0],local[1]),profile):
            return True
        return (-tolerance_m <= local[2] <= geometry.length_m+tolerance_m and
                any(_point_on_segment_2d((local[0],local[1]),a,b,tolerance_m)
                    for a,b in zip(profile,profile[1:]+profile[:1])))
    if isinstance(geometry, WireSweep):
        # The wire sweep has no material attachment surface: it may not attach to itself.
        return False
    return False


def point_on_slab_surface(slab: Any, point: Point3,
                          tolerance_m: float = ASSEMBLY_GEOMETRY_TOLERANCE_M) -> bool:
    """Check the canonical footprint top face or its downward offset face."""
    points=slab.footprint.points
    elevations=tuple(p.z for p in points)
    if max(elevations)-min(elevations)>tolerance_m:
        return False
    top_z=elevations[0]
    plan=tuple((p.x,p.y) for p in points)
    try:
        extrude_simple_section_m(plan,1.0,((0.0,0.0,0.0),(0.0,0.0,0.0,1.0)))
    except GeometryInputError:
        return False
    if _point_in_polygon_2d((point.x,point.y),plan):
        if abs(point.z-top_z)<=tolerance_m:
            return True
        if abs(point.z-(top_z-slab.thickness_m))<=tolerance_m:
            return True
    return False

