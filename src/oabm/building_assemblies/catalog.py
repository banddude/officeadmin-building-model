"""Strict immutable wall-preset catalog for the bounded v1 wall producer.

This catalog is a finite recipe library, separate from ``BuildingModel``. It
contains no Python callbacks or arbitrary rule language: recipe tokens are a
closed set interpreted by the pure producer in :mod:`oabm.building_assemblies.wall`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping

from oabm.model import ContractError

CATALOG_ID = "catalog:oabm-building-assemblies"
CATALOG_REVISION = "1.0.0"
WOOD_WALL_PRESET_ID = "wall.wood_rect_stock"
WOOD_WALL_PRESET_REVISION = "1.0.0"
WALL_GENERATOR_VERSION = "oabm-wall-producer/1.0.0"

_VALUE_UNITS = {
    "length": "m",
    "count": "each",
    "token": "1",
    "boolean": "1",
}


def _strict_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    missing = required - set(value)
    extra = set(value) - required
    if missing or extra:
        raise ContractError(
            f"{label} keys are invalid (missing={sorted(missing)!r}, extra={sorted(extra)!r})"
        )


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be non-empty text")


def _validate_typed_value(value_kind: str, value: object, unit: str, label: str) -> None:
    if value_kind not in _VALUE_UNITS:
        raise ContractError(f"{label}.value_kind is unsupported")
    if unit != _VALUE_UNITS[value_kind]:
        raise ContractError(f"{label}.unit must be {_VALUE_UNITS[value_kind]!r}")
    if value_kind == "length":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise ContractError(f"{label}.value must be a finite nonnegative length")
    elif value_kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"{label}.value must be a nonnegative integer count")
    elif value_kind == "token":
        _text(value, f"{label}.value")
    elif not isinstance(value, bool):
        raise ContractError(f"{label}.value must be boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogSource:
    source_id: str
    title: str
    url: str
    locator: str
    use_note: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "title", "url", "locator", "use_note"):
            _text(getattr(self, field_name), f"CatalogSource.{field_name}")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "locator": self.locator,
            "use_note": self.use_note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalogSource":
        _strict_keys(value, {"source_id", "title", "url", "locator", "use_note"}, "CatalogSource")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterDefinition:
    """One typed default explicitly declared by an immutable preset revision."""

    parameter_id: str
    value_kind: str
    default_value: float | int | str | bool
    unit: str
    default_basis: str
    source_id: str | None
    note: str
    minimum: float | int | None = None
    maximum: float | int | None = None

    def __post_init__(self) -> None:
        _text(self.parameter_id, "ParameterDefinition.parameter_id")
        _text(self.default_basis, f"{self.parameter_id}.default_basis")
        _text(self.note, f"{self.parameter_id}.note")
        if self.default_basis not in {"published_dimension_modeling_assumption", "illustrative_modeling_assumption"}:
            raise ContractError(f"{self.parameter_id}.default_basis is unsupported")
        if self.default_basis == "published_dimension_modeling_assumption":
            _text(self.source_id, f"{self.parameter_id}.source_id")
        elif self.source_id is not None:
            raise ContractError(f"{self.parameter_id} illustrative assumption cannot claim a source_id")
        _validate_typed_value(self.value_kind, self.default_value, self.unit, self.parameter_id)
        for name, bound in (("minimum", self.minimum), ("maximum", self.maximum)):
            if bound is not None:
                if self.value_kind not in {"length", "count"}:
                    raise ContractError(f"{self.parameter_id}.{name} is unsupported for {self.value_kind}")
                _validate_typed_value(self.value_kind, bound, self.unit, f"{self.parameter_id}.{name}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ContractError(f"{self.parameter_id} minimum cannot exceed maximum")
        if self.minimum is not None and self.default_value < self.minimum:
            raise ContractError(f"{self.parameter_id} default is below its minimum")
        if self.maximum is not None and self.default_value > self.maximum:
            raise ContractError(f"{self.parameter_id} default is above its maximum")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_id": self.parameter_id,
            "value_kind": self.value_kind,
            "default_value": self.default_value,
            "unit": self.unit,
            "default_basis": self.default_basis,
            "source_id": self.source_id,
            "note": self.note,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParameterDefinition":
        _strict_keys(value, {
            "parameter_id", "value_kind", "default_value", "unit", "default_basis",
            "source_id", "note", "minimum", "maximum",
        }, "ParameterDefinition")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class WallPreset:
    preset_id: str
    preset_revision: str
    assembly_kind: str
    generator_kind: str
    structural_status: str
    material_policy: str
    stud_layout_rule: str
    opening_frame_rule: str
    panel_partition_rule: str
    drywall_faces: tuple[str, ...]
    parameters: tuple[ParameterDefinition, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "preset_id", "preset_revision", "assembly_kind", "generator_kind",
            "structural_status", "material_policy", "stud_layout_rule",
            "opening_frame_rule", "panel_partition_rule",
        ):
            _text(getattr(self, field_name), f"WallPreset.{field_name}")
        if self.assembly_kind != "wall":
            raise ContractError("this catalog version admits wall recipes only")
        if self.generator_kind != "wood_rect_stock_v1":
            raise ContractError("WallPreset.generator_kind is outside the finite v1 recipe set")
        if self.structural_status != "illustrative_nonstructural_modeling_only":
            raise ContractError("wall recipe must explicitly declare illustrative nonstructural status")
        if self.material_policy != "unknown_unless_separately_supplied":
            raise ContractError("wall recipe cannot infer a material identity")
        if self.stud_layout_rule != "regular_centers_with_explicit_end_stations_and_void_trim_v1":
            raise ContractError("unsupported finite stud-layout rule")
        if self.opening_frame_rule != "single_jamb_header_window_sill_and_cripples_v1":
            raise ContractError("unsupported finite opening-frame rule")
        if self.panel_partition_rule != "rectangular_grid_partition_subtract_openings_v1":
            raise ContractError("unsupported finite panel-partition rule")
        if not isinstance(self.drywall_faces, tuple) or self.drywall_faces != ("left", "right"):
            raise ContractError("v1 recipe supports exactly one gypsum panel layer on each explicit wall face")
        if not isinstance(self.parameters, tuple) or any(not isinstance(item, ParameterDefinition) for item in self.parameters):
            raise ContractError("WallPreset.parameters must be an immutable tuple of typed definitions")
        parameter_ids = tuple(item.parameter_id for item in self.parameters)
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ContractError("WallPreset parameter IDs must be unique")
        if parameter_ids != WALL_PARAMETER_IDS:
            raise ContractError("WallPreset parameter set must exactly match the finite v1 wall recipe")
        values = {item.parameter_id: item.default_value for item in self.parameters}
        if values["wall.stud.start_offset_m"] < values["wall.stock.wall_axis_width_m"] / 2:
            raise ContractError("wall preset start offset must keep the full default stud section inside the host")
        if values["wall.stud.end_offset_m"] < values["wall.stock.wall_axis_width_m"] / 2:
            raise ContractError("wall preset end offset must keep the full default stud section inside the host")
        if values["wall.stud.spacing_m"] < values["wall.stock.wall_axis_width_m"]:
            raise ContractError("wall preset default stud spacing cannot overlap its default member section")
        if (values["wall.drywall.thickness_m"] != values["wall.stack.face_left.layer_1_m"] or
                values["wall.drywall.thickness_m"] != values["wall.stack.face_right.layer_1_m"]):
            raise ContractError("v1 wall preset face-stack defaults must match its one drywall layer thickness")

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "preset_revision": self.preset_revision,
            "assembly_kind": self.assembly_kind,
            "generator_kind": self.generator_kind,
            "structural_status": self.structural_status,
            "material_policy": self.material_policy,
            "stud_layout_rule": self.stud_layout_rule,
            "opening_frame_rule": self.opening_frame_rule,
            "panel_partition_rule": self.panel_partition_rule,
            "drywall_faces": list(self.drywall_faces),
            "parameters": [item.to_dict() for item in self.parameters],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return stable portable JSON; callers receive a new string, not catalog state."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WallPreset":
        _strict_keys(value, {
            "preset_id", "preset_revision", "assembly_kind", "generator_kind",
            "structural_status", "material_policy", "stud_layout_rule",
            "opening_frame_rule", "panel_partition_rule", "drywall_faces", "parameters",
        }, "WallPreset")
        if not isinstance(value["drywall_faces"], list) or not isinstance(value["parameters"], list):
            raise ContractError("WallPreset drywall_faces and parameters must be arrays")
        values = dict(value)
        values["drywall_faces"] = tuple(values["drywall_faces"])
        values["parameters"] = tuple(ParameterDefinition.from_dict(item) for item in values["parameters"])
        return cls(**values)


WALL_PARAMETER_IDS = (
    "wall.stud.spacing_m",
    "wall.stud.start_offset_m",
    "wall.stud.end_offset_m",
    "wall.stock.wall_axis_width_m",
    "wall.stack.framing_depth_m",
    "wall.plate.thickness_m",
    "wall.plate.bottom_count",
    "wall.plate.top_count",
    "wall.opening.header_height_m",
    "wall.opening.sill_height_m",
    "wall.opening_clearance_m",
    "wall.drywall.thickness_m",
    "wall.stack.face_left.layer_1_m",
    "wall.stack.face_right.layer_1_m",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildingAssemblyPresetCatalog:
    catalog_id: str
    catalog_revision: str
    catalog_version: str
    sources: tuple[CatalogSource, ...]
    wall_presets: tuple[WallPreset, ...]

    def __post_init__(self) -> None:
        for field_name in ("catalog_id", "catalog_revision", "catalog_version"):
            _text(getattr(self, field_name), f"BuildingAssemblyPresetCatalog.{field_name}")
        if self.catalog_version != "1":
            raise ContractError("unsupported BuildingAssemblyPresetCatalog version")
        if not isinstance(self.sources, tuple) or any(not isinstance(item, CatalogSource) for item in self.sources):
            raise ContractError("catalog sources must be an immutable tuple of CatalogSource")
        if not isinstance(self.wall_presets, tuple) or not self.wall_presets or any(not isinstance(item, WallPreset) for item in self.wall_presets):
            raise ContractError("catalog wall_presets must be a non-empty immutable tuple")
        source_ids = [item.source_id for item in self.sources]
        preset_ids = [item.preset_id for item in self.wall_presets]
        if len(source_ids) != len(set(source_ids)):
            raise ContractError("catalog source IDs must be unique")
        if len(preset_ids) != len(set(preset_ids)):
            raise ContractError("catalog preset IDs must be unique")
        source_set = set(source_ids)
        for preset in self.wall_presets:
            for definition in preset.parameters:
                if definition.source_id is not None and definition.source_id not in source_set:
                    raise ContractError(f"{definition.parameter_id} references missing catalog source")

    def wall_preset(self, preset_id: str) -> WallPreset:
        matches = [item for item in self.wall_presets if item.preset_id == preset_id]
        if not matches:
            raise ContractError(f"unknown or unsupported wall preset {preset_id!r}")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "catalog_revision": self.catalog_revision,
            "sources": [item.to_dict() for item in self.sources],
            "wall_presets": [item.to_dict() for item in self.wall_presets],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return stable portable JSON without exposing mutable catalog state."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BuildingAssemblyPresetCatalog":
        _strict_keys(value, {"catalog_version", "catalog_id", "catalog_revision", "sources", "wall_presets"}, "BuildingAssemblyPresetCatalog")
        if not isinstance(value["sources"], list) or not isinstance(value["wall_presets"], list):
            raise ContractError("catalog sources and wall_presets must be arrays")
        return cls(
            catalog_version=value["catalog_version"],
            catalog_id=value["catalog_id"],
            catalog_revision=value["catalog_revision"],
            sources=tuple(CatalogSource.from_dict(item) for item in value["sources"]),
            wall_presets=tuple(WallPreset.from_dict(item) for item in value["wall_presets"]),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "BuildingAssemblyPresetCatalog":
        """Strictly decode catalog JSON, rejecting duplicate keys and unknown fields."""
        def unique_pairs(rows):
            document = {}
            for key, value in rows:
                if key in document:
                    raise ContractError(f"duplicate catalog JSON key {key!r}")
                document[key] = value
            return document

        try:
            document = json.loads(payload, object_pairs_hook=unique_pairs)
        except ContractError:
            raise
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ContractError(f"invalid building-assembly catalog JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ContractError("building-assembly catalog JSON root must be an object")
        return cls.from_dict(document)


def _wall_parameter(
    parameter_id: str,
    value_kind: str,
    default_value: float | int | str | bool,
    *,
    basis: str = "illustrative_modeling_assumption",
    source_id: str | None = None,
    note: str,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
) -> ParameterDefinition:
    return ParameterDefinition(
        parameter_id=parameter_id,
        value_kind=value_kind,
        default_value=default_value,
        unit=_VALUE_UNITS[value_kind],
        default_basis=basis,
        source_id=source_id,
        note=note,
        minimum=minimum,
        maximum=maximum,
    )


DEFAULT_PRESET_CATALOG = BuildingAssemblyPresetCatalog(
    catalog_id=CATALOG_ID,
    catalog_revision=CATALOG_REVISION,
    catalog_version="1",
    sources=(
        CatalogSource(
            source_id="nist_ps20_25",
            title="NIST / U.S. Department of Commerce, Voluntary Product Standard PS 20-25",
            url="https://www.nist.gov/system/files/documents/2024/12/11/PS%2020-25%20Final.pdf",
            locator="Table 3, minimum-dressed dry 2x4 dimensions: 1.5 x 3.5 in (38.1 x 88.9 mm)",
            use_note="Minimum dressed sizes are parametric modeling dimensions, not field-measured lumber or a structural specification.",
        ),
        CatalogSource(
            source_id="usg_sheetrock_ultralight_141135",
            title="USG Sheetrock Brand UltraLight Panels, commodity code 141135",
            url="https://www.usg.com/en-US/p/product/sheetrock-brand-ultralight-panels-141135",
            locator="Product Specifications: listed 1/2-in (12.7-mm) panel thickness",
            use_note="Published thickness is used only as an editable parametric modeling assumption; no fire-rating or assembly claim.",
        ),
    ),
    wall_presets=(
        WallPreset(
            preset_id=WOOD_WALL_PRESET_ID,
            preset_revision=WOOD_WALL_PRESET_REVISION,
            assembly_kind="wall",
            generator_kind="wood_rect_stock_v1",
            structural_status="illustrative_nonstructural_modeling_only",
            material_policy="unknown_unless_separately_supplied",
            stud_layout_rule="regular_centers_with_explicit_end_stations_and_void_trim_v1",
            opening_frame_rule="single_jamb_header_window_sill_and_cripples_v1",
            panel_partition_rule="rectangular_grid_partition_subtract_openings_v1",
            drywall_faces=("left", "right"),
            parameters=(
                _wall_parameter("wall.stud.spacing_m", "length", 0.4064, note="Illustrative 16 in center spacing only; not a plan fact or code rule.", minimum=0.0508, maximum=2.0),
                _wall_parameter("wall.stud.start_offset_m", "length", 0.01905, note="Illustrative start station at half the 1.5 in wall-axis member width.", minimum=0.0, maximum=1.0),
                _wall_parameter("wall.stud.end_offset_m", "length", 0.01905, note="Illustrative end station at half the 1.5 in wall-axis member width.", minimum=0.0, maximum=1.0),
                _wall_parameter("wall.stock.wall_axis_width_m", "length", 0.0381, basis="published_dimension_modeling_assumption", source_id="nist_ps20_25", note="NIST minimum dressed dry 2x4 thickness 1.5 in = 38.1 mm, used as a specified parameterized section, not a measured piece.", minimum=0.005, maximum=0.5),
                _wall_parameter("wall.stack.framing_depth_m", "length", 0.0889, basis="published_dimension_modeling_assumption", source_id="nist_ps20_25", note="NIST minimum dressed dry 2x4 width 3.5 in = 88.9 mm, used as a parameterized framing-depth assumption.", minimum=0.01, maximum=1.0),
                _wall_parameter("wall.plate.thickness_m", "length", 0.0381, basis="published_dimension_modeling_assumption", source_id="nist_ps20_25", note="Use the cited 1.5 in minimum-dressed dimension as the illustrative vertical plate thickness; not a construction rule.", minimum=0.005, maximum=0.2),
                _wall_parameter("wall.plate.bottom_count", "count", 1, note="Illustrative one-layer bottom plate configuration; no fastening or structural claim.", minimum=1, maximum=2),
                _wall_parameter("wall.plate.top_count", "count", 2, note="Illustrative two-layer top plate configuration; no fastening or structural claim.", minimum=1, maximum=2),
                _wall_parameter("wall.opening.header_height_m", "length", 0.0381, note="Illustrative single rectangular header height; not selected for structural capacity.", minimum=0.005, maximum=0.2),
                _wall_parameter("wall.opening.sill_height_m", "length", 0.0381, note="Illustrative rectangular window-sill height; no flashing or structural claim.", minimum=0.005, maximum=0.2),
                _wall_parameter("wall.opening_clearance_m", "length", 0.0, note="No extra rough-frame clearance is represented in this illustrative recipe.", minimum=0.0, maximum=0.2),
                _wall_parameter("wall.drywall.thickness_m", "length", 0.0127, basis="published_dimension_modeling_assumption", source_id="usg_sheetrock_ultralight_141135", note="USG listed 1/2 in panel thickness, snapshotted as a parametric visualization value; no rated assembly claim.", minimum=0.001, maximum=0.1),
                _wall_parameter("wall.stack.face_left.layer_1_m", "length", 0.0127, basis="published_dimension_modeling_assumption", source_id="usg_sheetrock_ultralight_141135", note="One left-face layer copies the selected wall.drywall.thickness_m value in this one-layer recipe; both are parameterized assumptions.", minimum=0.001, maximum=0.1),
                _wall_parameter("wall.stack.face_right.layer_1_m", "length", 0.0127, basis="published_dimension_modeling_assumption", source_id="usg_sheetrock_ultralight_141135", note="One right-face layer copies the selected wall.drywall.thickness_m value in this one-layer recipe; both are parameterized assumptions.", minimum=0.001, maximum=0.1),
            ),
        ),
    ),
)


__all__ = [
    "CATALOG_ID", "CATALOG_REVISION", "WOOD_WALL_PRESET_ID",
    "WOOD_WALL_PRESET_REVISION", "WALL_GENERATOR_VERSION",
    "CatalogSource", "ParameterDefinition", "WallPreset",
    "BuildingAssemblyPresetCatalog", "DEFAULT_PRESET_CATALOG",
]
