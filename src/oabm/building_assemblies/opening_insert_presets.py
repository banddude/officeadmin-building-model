"""Immutable, finite opening-insert preset recipes for issue #356.

These are explicitly selected *modeling* recipes.  They are not manufacturer
catalog entries and carry no material, fire, egress, swing, structural, or code
approval facts.  The executable recipe tokens are intentionally closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from oabm.model import ContractError


OPENING_INSERT_CATALOG_ID = "catalog:oabm-opening-inserts"
OPENING_INSERT_CATALOG_REVISION = "1.0.0"
OPENING_INSERT_GENERATOR_VERSION = "oabm-opening-insert-producer/1.0.0"

DOOR_SINGLE_LEAF_PRESET_ID = "opening_insert.door.simple_single_leaf"
DOOR_SINGLE_LEAF_PRESET_REVISION = "1.0.0"
WINDOW_FIXED_GLAZED_PRESET_ID = "opening_insert.window.fixed_glazed"
WINDOW_FIXED_GLAZED_PRESET_REVISION = "1.0.0"

_DOOR_PARAMETERS = (
    "door.frame.face_width_m",
    "door.leaf.thickness_m",
    "door.leaf.side_gap_m",
    "door.leaf.head_gap_m",
    "door.leaf.bottom_gap_m",
)
_WINDOW_PARAMETERS = (
    "window.frame.face_width_m",
    "window.glazing.thickness_m",
    "window.glazing.edge_gap_m",
)
_MAX_PRESETS = 2
_MAX_PARAMETERS = 5


def _strict_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise OpeningInsertPresetError(f"{label} must be an object")
    missing, extra = required - set(value), set(value) - required
    if missing or extra:
        raise OpeningInsertPresetError(
            f"{label} keys are invalid (missing={sorted(missing)!r}, extra={sorted(extra)!r})"
        )


class OpeningInsertPresetError(ContractError):
    """A selected opening-insert recipe or typed value is unsupported."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InsertParameterDefinition:
    """One immutable, editable length assumption in an insert preset."""

    parameter_id: str
    default_value: float
    note: str
    minimum: float = 0.0
    maximum: float = 0.5

    def __post_init__(self) -> None:
        if (not isinstance(self.parameter_id, str) or not self.parameter_id.strip() or
                len(self.parameter_id) > 2048):
            raise OpeningInsertPresetError("insert parameter_id must be non-empty text")
        if not isinstance(self.note, str) or not self.note.strip() or len(self.note) > 2048:
            raise OpeningInsertPresetError(f"{self.parameter_id} requires an explicit assumption note")
        for label, value in (("default_value", self.default_value),
                             ("minimum", self.minimum), ("maximum", self.maximum)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise OpeningInsertPresetError(f"{self.parameter_id}.{label} must be a finite metre value")
        if self.minimum < 0 or self.maximum < self.minimum:
            raise OpeningInsertPresetError(f"{self.parameter_id} has invalid nonnegative length bounds")
        if not self.minimum <= self.default_value <= self.maximum:
            raise OpeningInsertPresetError(f"{self.parameter_id} default is outside its bounds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_id": self.parameter_id,
            "value_kind": "length",
            "default_value": self.default_value,
            "unit": "m",
            "default_basis": "illustrative_modeling_assumption",
            "source_id": None,
            "note": self.note,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InsertParameterDefinition":
        _strict_keys(value, {
            "parameter_id", "value_kind", "default_value", "unit", "default_basis",
            "source_id", "note", "minimum", "maximum",
        }, "InsertParameterDefinition")
        if (value["value_kind"] != "length" or value["unit"] != "m" or
                value["default_basis"] != "illustrative_modeling_assumption" or
                value["source_id"] is not None):
            raise OpeningInsertPresetError(
                "insert parameter definitions require explicit length/metre illustrative assumptions"
            )
        return cls(
            parameter_id=value["parameter_id"],
            default_value=value["default_value"],
            note=value["note"],
            minimum=value["minimum"],
            maximum=value["maximum"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningInsertPreset:
    """One versioned, closed-token recipe interpreted by the pure producer."""

    preset_id: str
    preset_revision: str
    opening_type: str
    generator_kind: str
    structural_status: str
    material_policy: str
    parameters: tuple[InsertParameterDefinition, ...]

    def __post_init__(self) -> None:
        for label in ("preset_id", "preset_revision", "opening_type", "generator_kind",
                      "structural_status", "material_policy"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise OpeningInsertPresetError(f"OpeningInsertPreset.{label} must be non-empty text")
        expected = {
            "door_closed_single_leaf_v1": ("door", _DOOR_PARAMETERS, DOOR_SINGLE_LEAF_PRESET_ID),
            "window_fixed_single_glazing_v1": ("window", _WINDOW_PARAMETERS, WINDOW_FIXED_GLAZED_PRESET_ID),
        }.get(self.generator_kind)
        if expected is None or self.opening_type != expected[0] or self.preset_id != expected[2]:
            raise OpeningInsertPresetError("insert preset uses an unsupported opening type or generator token")
        if self.structural_status != "illustrative_modeling_only":
            raise OpeningInsertPresetError("insert preset cannot claim structural adequacy or approval")
        if self.material_policy != "unknown_unless_separately_supplied":
            raise OpeningInsertPresetError("insert preset cannot infer a material identity")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > _MAX_PARAMETERS:
            raise OpeningInsertPresetError("insert preset has too many or invalid parameter definitions")
        if any(not isinstance(row, InsertParameterDefinition) for row in self.parameters):
            raise OpeningInsertPresetError("insert preset parameters must be typed definitions")
        if tuple(row.parameter_id for row in self.parameters) != expected[1]:
            raise OpeningInsertPresetError("insert preset parameter IDs must be complete and in canonical order")

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "preset_revision": self.preset_revision,
            "opening_type": self.opening_type,
            "generator_kind": self.generator_kind,
            "structural_status": self.structural_status,
            "material_policy": self.material_policy,
            "parameters": [row.to_dict() for row in self.parameters],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpeningInsertPreset":
        _strict_keys(value, {
            "preset_id", "preset_revision", "opening_type", "generator_kind",
            "structural_status", "material_policy", "parameters",
        }, "OpeningInsertPreset")
        parameters = value["parameters"]
        if not isinstance(parameters, list) or len(parameters) > _MAX_PARAMETERS:
            raise OpeningInsertPresetError("OpeningInsertPreset.parameters must be a bounded JSON array")
        return cls(
            preset_id=value["preset_id"],
            preset_revision=value["preset_revision"],
            opening_type=value["opening_type"],
            generator_kind=value["generator_kind"],
            structural_status=value["structural_status"],
            material_policy=value["material_policy"],
            parameters=tuple(InsertParameterDefinition.from_dict(row) for row in parameters),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OpeningInsertPresetCatalog:
    """Small immutable catalog isolated from the existing wall preset catalog."""

    catalog_id: str
    catalog_revision: str
    presets: tuple[OpeningInsertPreset, ...]

    def __post_init__(self) -> None:
        for label in ("catalog_id", "catalog_revision"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise OpeningInsertPresetError(f"catalog {label} must be non-empty text")
        if not isinstance(self.presets, tuple):
            raise OpeningInsertPresetError("catalog presets must be an immutable tuple of typed recipes")
        if len(self.presets) > _MAX_PRESETS:
            raise OpeningInsertPresetError(f"catalog preset count exceeds the finite limit {_MAX_PRESETS}")
        if any(not isinstance(row, OpeningInsertPreset) for row in self.presets):
            raise OpeningInsertPresetError("catalog presets must be an immutable tuple of typed recipes")
        ids = tuple(row.preset_id for row in self.presets)
        if len(ids) != len(set(ids)):
            raise OpeningInsertPresetError("catalog preset identities must be unique")
        if not ids:
            raise OpeningInsertPresetError("opening-insert catalog must contain at least one finite preset")

    def opening_insert_preset(self, preset_id: str) -> OpeningInsertPreset:
        for row in self.presets:
            if row.preset_id == preset_id:
                return row
        raise OpeningInsertPresetError(f"unsupported opening-insert preset {preset_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_revision": self.catalog_revision,
            "presets": [row.to_dict() for row in self.presets],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpeningInsertPresetCatalog":
        _strict_keys(value, {"catalog_id", "catalog_revision", "presets"},
                     "OpeningInsertPresetCatalog")
        presets = value["presets"]
        if not isinstance(presets, list) or len(presets) > _MAX_PRESETS:
            raise OpeningInsertPresetError(
                f"OpeningInsertPresetCatalog.presets must be a JSON array of at most {_MAX_PRESETS} recipes"
            )
        return cls(
            catalog_id=value["catalog_id"],
            catalog_revision=value["catalog_revision"],
            presets=tuple(OpeningInsertPreset.from_dict(row) for row in presets),
        )


DEFAULT_OPENING_INSERT_CATALOG = OpeningInsertPresetCatalog(
    catalog_id=OPENING_INSERT_CATALOG_ID,
    catalog_revision=OPENING_INSERT_CATALOG_REVISION,
    presets=(
        OpeningInsertPreset(
            preset_id=DOOR_SINGLE_LEAF_PRESET_ID,
            preset_revision=DOOR_SINGLE_LEAF_PRESET_REVISION,
            opening_type="door",
            generator_kind="door_closed_single_leaf_v1",
            structural_status="illustrative_modeling_only",
            material_policy="unknown_unless_separately_supplied",
            parameters=(
                InsertParameterDefinition(parameter_id="door.frame.face_width_m", default_value=0.0508,
                    minimum=0.001, maximum=0.25,
                    note=("Illustrative frame face width; editable modeling assumption, "
                          "not a manufacturer dimension.")),
                InsertParameterDefinition(parameter_id="door.leaf.thickness_m", default_value=0.035,
                    minimum=0.001, maximum=0.15,
                    note=("Illustrative flat leaf thickness; editable modeling assumption, "
                          "not a manufacturer dimension.")),
                InsertParameterDefinition(parameter_id="door.leaf.side_gap_m", default_value=0.003,
                    maximum=0.1, note="Illustrative clearance at each leaf side; editable modeling assumption."),
                InsertParameterDefinition(parameter_id="door.leaf.head_gap_m", default_value=0.003,
                    maximum=0.1, note="Illustrative clearance below the head; editable modeling assumption."),
                InsertParameterDefinition(parameter_id="door.leaf.bottom_gap_m", default_value=0.006,
                    maximum=0.1, note="Illustrative clearance above the sill; editable modeling assumption."),
            ),
        ),
        OpeningInsertPreset(
            preset_id=WINDOW_FIXED_GLAZED_PRESET_ID,
            preset_revision=WINDOW_FIXED_GLAZED_PRESET_REVISION,
            opening_type="window",
            generator_kind="window_fixed_single_glazing_v1",
            structural_status="illustrative_modeling_only",
            material_policy="unknown_unless_separately_supplied",
            parameters=(
                InsertParameterDefinition(parameter_id="window.frame.face_width_m", default_value=0.0508,
                    minimum=0.001, maximum=0.25,
                    note=("Illustrative perimeter-frame face width; editable modeling assumption, "
                          "not a manufacturer dimension.")),
                InsertParameterDefinition(parameter_id="window.glazing.thickness_m", default_value=0.006,
                    minimum=0.001, maximum=0.1,
                    note=("Illustrative single flat glazing thickness; editable modeling assumption, "
                          "not a manufacturer dimension.")),
                InsertParameterDefinition(parameter_id="window.glazing.edge_gap_m", default_value=0.003,
                    maximum=0.1,
                    note="Illustrative glazing clearance at each frame edge; editable modeling assumption."),
            ),
        ),
    ),
)


__all__ = [
    "OPENING_INSERT_CATALOG_ID", "OPENING_INSERT_CATALOG_REVISION",
    "OPENING_INSERT_GENERATOR_VERSION", "DOOR_SINGLE_LEAF_PRESET_ID",
    "DOOR_SINGLE_LEAF_PRESET_REVISION", "WINDOW_FIXED_GLAZED_PRESET_ID",
    "WINDOW_FIXED_GLAZED_PRESET_REVISION", "OpeningInsertPresetError",
    "InsertParameterDefinition", "OpeningInsertPreset", "OpeningInsertPresetCatalog",
    "DEFAULT_OPENING_INSERT_CATALOG",
]
