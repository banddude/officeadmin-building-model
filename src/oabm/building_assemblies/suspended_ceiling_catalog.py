"""Finite immutable modeling preset for the first suspended-ceiling producer.

The preset is deliberately small: it describes an illustrative nominal
24 in (609.6 mm) module grid, one simple parameterized T section, rectangular lay-in tile panels and
vertical straight hanger-wire segments.  Catalog envelope facts are not a
manufacturer-exact section drawing, physical tile cut size, attachment design,
structural approval or code/listing statement.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from oabm.model import ContractError

from .catalog import CatalogSource, ParameterDefinition


SUSPENDED_CEILING_CATALOG_ID = "catalog:oabm-suspended-ceiling"
SUSPENDED_CEILING_CATALOG_REVISION = "1.0.0"
SUSPENDED_CEILING_CATALOG_VERSION = "1"
SUSPENDED_CEILING_PRESET_ID = "ceiling.tbar_609_6mm_module_illustrative"
SUSPENDED_CEILING_PRESET_REVISION = "1.0.0"
SUSPENDED_CEILING_GENERATOR_VERSION = "oabm-suspended-ceiling-producer/1.0.0"
CEILING_DATUM_OFFSET_KEY = "ceiling.tile_top_offset_above_source_top_m"

SUSPENDED_CEILING_PARAMETER_IDS = (
    "ceiling.module_spacing_m",
    "ceiling.runner.profile_width_m",
    "ceiling.runner.profile_depth_m",
    "ceiling.runner.web_thickness_m",
    "ceiling.runner.flange_thickness_m",
    "ceiling.tile.thickness_m",
    "ceiling.tile.edge_clearance_m",
    "ceiling.hanger.wire_diameter_m",
    "ceiling.hanger.spacing_m",
    "ceiling.hanger.end_offset_m",
    "ceiling.tile_top_offset_above_source_top_m",
)

_LENGTH_UNITS = {
    "length": "m",
    "angle": "rad",
    "count": "each",
    "token": "1",
    "material_ref": "1",
    "boolean": "1",
}


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be non-empty text")


@dataclass(frozen=True, slots=True, kw_only=True)
class SuspendedCeilingPreset:
    """A versioned, executable finite recipe; no arbitrary rule language."""

    preset_id: str
    preset_revision: str
    assembly_kind: str
    generator_kind: str
    material_policy: str
    profile_geometry_basis: str
    tile_geometry_basis: str
    edge_policy: str
    parameters: tuple[ParameterDefinition, ...]
    tile_top_offset_parameter_id: str
    tile_top_offset_value_kind: str
    tile_top_offset_unit: str
    tile_top_offset_minimum_m: float
    tile_top_offset_maximum_m: float
    tile_top_offset_note: str
    tile_top_offset_with_observed_thickness_m: float
    tile_top_offset_without_thickness_formula: str
    description: str

    def __post_init__(self) -> None:
        for label in (
            "preset_id", "preset_revision", "assembly_kind", "generator_kind",
            "material_policy", "profile_geometry_basis", "tile_geometry_basis",
            "edge_policy", "description", "tile_top_offset_parameter_id",
            "tile_top_offset_value_kind", "tile_top_offset_unit", "tile_top_offset_note",
            "tile_top_offset_without_thickness_formula",
        ):
            _text(getattr(self, label), f"SuspendedCeilingPreset.{label}")
        if self.assembly_kind != "suspended_ceiling":
            raise ContractError("ceiling preset assembly_kind must be suspended_ceiling")
        if self.generator_kind not in {
                "rectangular_tbar_609_6mm_module_grid_v1",
                "rectangular_tbar_module_grid_v1",
        }:
            raise ContractError("ceiling preset generator_kind is outside the finite v1 recipe set")
        if self.material_policy != "unknown_unless_separately_supplied":
            raise ContractError("ceiling preset cannot infer material identity")
        if self.profile_geometry_basis != "illustrative_simple_t_profile_not_manufacturer_exact":
            raise ContractError("ceiling preset profile must disclose its simplified T geometry")
        if self.tile_geometry_basis != "listed_product_thickness_and_nominal_module_not_measured_cut_size":
            raise ContractError("ceiling preset tile must disclose its nominal module approximation")
        if self.edge_policy != "cut_tiles_without_perimeter_angle_v1":
            raise ContractError("ceiling preset edge policy is outside the finite v1 recipe set")
        if (isinstance(self.tile_top_offset_with_observed_thickness_m, bool) or
                not isinstance(self.tile_top_offset_with_observed_thickness_m, (int, float))):
            raise ContractError("ceiling preset observed-thickness top offset must be numeric")
        if self.tile_top_offset_with_observed_thickness_m != 0.0:
            raise ContractError("v1 with-thickness default must align the tile top with the source Ceiling top face")
        if self.tile_top_offset_without_thickness_formula != (
                "resolved(ceiling.tile.thickness_m) + resolved(ceiling.runner.flange_thickness_m)"):
            raise ContractError("v1 missing-thickness offset must expose the flange-bottom datum formula")
        if (self.tile_top_offset_parameter_id != CEILING_DATUM_OFFSET_KEY or
                self.tile_top_offset_value_kind != "length" or self.tile_top_offset_unit != "m" or
                self.tile_top_offset_minimum_m != -10.0 or self.tile_top_offset_maximum_m != 10.0):
            raise ContractError("v1 tile-top offset must be the finite signed length parameter declared by the preset")
        if not isinstance(self.parameters, tuple) or any(
                not isinstance(row, ParameterDefinition) for row in self.parameters):
            raise ContractError("ceiling preset parameters must be an immutable tuple of typed definitions")
        parameter_ids = tuple(row.parameter_id for row in self.parameters)
        if parameter_ids != SUSPENDED_CEILING_PARAMETER_IDS[:-1]:
            raise ContractError("ceiling preset parameter set must exactly match the finite v1 recipe")
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ContractError("ceiling preset parameter IDs must be unique")
        values = {row.parameter_id: row.default_value for row in self.parameters}
        width = values["ceiling.runner.profile_width_m"]
        module = values["ceiling.module_spacing_m"]
        if (self.generator_kind == "rectangular_tbar_609_6mm_module_grid_v1" and
                module != 0.6096):
            raise ContractError(
                "nominal 609.6 mm generator kind requires its exact 0.6096 m built-in module default"
            )
        flange = values["ceiling.runner.flange_thickness_m"]
        depth = values["ceiling.runner.profile_depth_m"]
        web = values["ceiling.runner.web_thickness_m"]
        tile = values["ceiling.tile.thickness_m"]
        if not 0 < flange < depth or not 0 < web < width < module:
            raise ContractError("ceiling preset T profile/module dimensions are inconsistent")
        if (abs(self.tile_top_offset_with_observed_thickness_m) > 1e-12 or
                tile + flange <= 0):
            raise ContractError("ceiling preset tile datum policy has invalid dimensions")

    def parameter(self, parameter_id: str) -> ParameterDefinition:
        for row in self.parameters:
            if row.parameter_id == parameter_id:
                return row
        raise ContractError(f"unsupported suspended-ceiling parameter {parameter_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "preset_revision": self.preset_revision,
            "assembly_kind": self.assembly_kind,
            "generator_kind": self.generator_kind,
            "material_policy": self.material_policy,
            "profile_geometry_basis": self.profile_geometry_basis,
            "tile_geometry_basis": self.tile_geometry_basis,
            "edge_policy": self.edge_policy,
            "parameters": [row.to_dict() for row in self.parameters],
            "tile_top_offset_parameter_id": self.tile_top_offset_parameter_id,
            "tile_top_offset_value_kind": self.tile_top_offset_value_kind,
            "tile_top_offset_unit": self.tile_top_offset_unit,
            "tile_top_offset_minimum_m": self.tile_top_offset_minimum_m,
            "tile_top_offset_maximum_m": self.tile_top_offset_maximum_m,
            "tile_top_offset_note": self.tile_top_offset_note,
            "tile_top_offset_with_observed_thickness_m": self.tile_top_offset_with_observed_thickness_m,
            "tile_top_offset_without_thickness_formula": self.tile_top_offset_without_thickness_formula,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SuspendedCeilingPresetCatalog:
    catalog_id: str
    catalog_revision: str
    catalog_version: str
    sources: tuple[CatalogSource, ...]
    presets: tuple[SuspendedCeilingPreset, ...]

    def __post_init__(self) -> None:
        for label in ("catalog_id", "catalog_revision", "catalog_version"):
            _text(getattr(self, label), f"SuspendedCeilingPresetCatalog.{label}")
        if self.catalog_version != SUSPENDED_CEILING_CATALOG_VERSION:
            raise ContractError("unsupported suspended-ceiling catalog version")
        if not isinstance(self.sources, tuple) or any(not isinstance(row, CatalogSource) for row in self.sources):
            raise ContractError("ceiling catalog sources must be an immutable tuple of CatalogSource")
        if not isinstance(self.presets, tuple) or len(self.presets) != 1 or any(
                not isinstance(row, SuspendedCeilingPreset) for row in self.presets):
            raise ContractError("v1 ceiling catalog must contain exactly one immutable preset")
        source_ids = tuple(row.source_id for row in self.sources)
        preset_ids = tuple(row.preset_id for row in self.presets)
        if len(source_ids) != len(set(source_ids)) or len(preset_ids) != len(set(preset_ids)):
            raise ContractError("ceiling catalog source and preset IDs must be unique")
        known_sources = set(source_ids)
        for preset in self.presets:
            for definition in preset.parameters:
                if definition.source_id is not None and definition.source_id not in known_sources:
                    raise ContractError(f"{definition.parameter_id} references a missing catalog source")

    def suspended_ceiling_preset(self, preset_id: str) -> SuspendedCeilingPreset:
        for preset in self.presets:
            if preset.preset_id == preset_id:
                return preset
        raise ContractError(f"unknown or unsupported suspended-ceiling preset {preset_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "catalog_id": self.catalog_id,
            "catalog_revision": self.catalog_revision,
            "sources": [row.to_dict() for row in self.sources],
            "presets": [row.to_dict() for row in self.presets],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Deterministic portable catalog representation for review/audit."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)


def _param(
    parameter_id: str,
    value: float,
    *,
    note: str,
    source_id: str | None = None,
    basis: str = "illustrative_modeling_assumption",
    minimum: float = 0.0,
    maximum: float,
) -> ParameterDefinition:
    return ParameterDefinition(
        parameter_id=parameter_id,
        value_kind="length",
        default_value=value,
        unit="m",
        default_basis=basis,
        source_id=source_id,
        note=note,
        minimum=minimum,
        maximum=maximum,
    )


_SOURCES = (
    CatalogSource(
        source_id="armstrong_prelude_xl_bpcs_3344_226",
        title="Armstrong Prelude XL 15/16 exposed tee data sheet, BPCS-3344-226",
        url="https://www.armstrongceilings.com/content/dam/armstrongceilings/commercial/north-america/data-sheets/prelude-xl-data-sheet.pdf",
        locator="Page 2 item-envelope dimensions for 7300 / XL7341 / XL7328; page 4 simplified section illustration",
        use_note="Published overall envelopes only. The complete section points, sheet thickness, bend radii and slot geometry are not supplied; v1 uses a visibly simplified parameterized T, not manufacturer-exact geometry.",
    ),
    CatalogSource(
        source_id="armstrong_ultima_1910",
        title="Armstrong ULTIMA Lay-In and Tegular, item 1910",
        url="https://www.armstrongceilings.com/commercial/en/commercial-ceilings-walls/ultima-lay-in-and-tegular-ceiling-tiles/item/1910.html",
        locator="Product header lists nominal 24 x 24 in dimensions and 3/4 in thickness",
        use_note="Listed dimensions are treated as an editable nominal module/thickness assumption. Actual physical cut size and bearing-edge geometry are not established.",
    ),
    CatalogSource(
        source_id="qsw_ceiling_wire_submittal",
        title="Quality Steel & Wire ceiling hanger wire submittal",
        url="https://qswmfg.com/wp-content/uploads/2018/05/Ceiling-Hanger-Wire-Submittal-Form.pdf",
        locator="Specs and Support Properties, source-listed 12-gauge row, 0.105 in diameter",
        use_note="The diameter is specific to this named product source; it is not a universal gauge conversion and says nothing about anchor selection, capacity or structural approval.",
    ),
)


DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG = SuspendedCeilingPresetCatalog(
    catalog_id=SUSPENDED_CEILING_CATALOG_ID,
    catalog_revision=SUSPENDED_CEILING_CATALOG_REVISION,
    catalog_version=SUSPENDED_CEILING_CATALOG_VERSION,
    sources=_SOURCES,
    presets=(SuspendedCeilingPreset(
        preset_id=SUSPENDED_CEILING_PRESET_ID,
        preset_revision=SUSPENDED_CEILING_PRESET_REVISION,
        assembly_kind="suspended_ceiling",
        generator_kind="rectangular_tbar_609_6mm_module_grid_v1",
        material_policy="unknown_unless_separately_supplied",
        profile_geometry_basis="illustrative_simple_t_profile_not_manufacturer_exact",
        tile_geometry_basis="listed_product_thickness_and_nominal_module_not_measured_cut_size",
        edge_policy="cut_tiles_without_perimeter_angle_v1",
        parameters=(
            _param(
                "ceiling.module_spacing_m", 0.6096, maximum=1.5,
                basis="published_dimension_modeling_assumption",
                source_id="armstrong_ultima_1910",
                note="Nominal 24 in module from one published item; not a verified physical cut size, grid layout rule or construction requirement.",
            ),
            _param(
                "ceiling.runner.profile_width_m", 0.0238125, maximum=0.15,
                basis="published_dimension_modeling_assumption",
                source_id="armstrong_prelude_xl_bpcs_3344_226",
                note="15/16 in listed overall envelope used as the simple T flange width only; no exact section is supplied.",
                minimum=0.005,
            ),
            _param(
                "ceiling.runner.profile_depth_m", 0.0428625, maximum=0.25,
                basis="published_dimension_modeling_assumption",
                source_id="armstrong_prelude_xl_bpcs_3344_226",
                note="1.6875 in listed 7300/XL7341 overall envelope used as simple T depth only; profile shape is not manufacturer-exact.",
                minimum=0.01,
            ),
            _param(
                "ceiling.runner.web_thickness_m", 0.003175, maximum=0.05,
                note="Illustrative editable web thickness for the simple T profile; not a catalog sheet-metal gauge.",
                minimum=0.0005,
            ),
            _param(
                "ceiling.runner.flange_thickness_m", 0.003175, maximum=0.05,
                note="Illustrative editable vertical flange thickness for the simple T profile; not a product bend/metal thickness.",
                minimum=0.0005,
            ),
            _param(
                "ceiling.tile.thickness_m", 0.01905, maximum=0.1,
                basis="published_dimension_modeling_assumption",
                source_id="armstrong_ultima_1910",
                note="Listed 3/4 in product thickness used as a parametric tile thickness assumption; actual installed piece tolerance/edge geometry is not represented.",
                minimum=0.001,
            ),
            _param(
                "ceiling.tile.edge_clearance_m", 0.003175, maximum=0.02,
                note=("Illustrative per-edge clearance from each T-runner centerline, resolved in metres. "
                      "It is not a manufacturer-verified tile cut dimension; the selected value is also "
                      "used to prove positive plan overlap with the simple T flange."),
                minimum=0.0,
            ),
            _param(
                "ceiling.hanger.wire_diameter_m", 0.002667, maximum=0.05,
                basis="published_dimension_modeling_assumption",
                source_id="qsw_ceiling_wire_submittal",
                note="QSW source-listed 0.105 in diameter for its named 12-gauge product only; not a universal gauge conversion, capacity or attachment approval.",
                minimum=0.0001,
            ),
            _param(
                "ceiling.hanger.spacing_m", 1.2, maximum=6.0,
                note="Illustrative hanger station spacing for visualization; project substrate, layout, capacity and code review are not modeled.",
                minimum=0.1,
            ),
            _param(
                "ceiling.hanger.end_offset_m", 0.3, maximum=3.0,
                note="Illustrative end offset for hanger stations along a continuous modeled main runner; not a manufacturer's layout rule.",
                minimum=0.0,
            ),
        ),
        tile_top_offset_parameter_id=CEILING_DATUM_OFFSET_KEY,
        tile_top_offset_value_kind="length",
        tile_top_offset_unit="m",
        tile_top_offset_minimum_m=-10.0,
        tile_top_offset_maximum_m=10.0,
        tile_top_offset_note=(
            "Conditional explicit placement rule in this immutable preset: when Ceiling.thickness_m is observed, align tile top with the source Ceiling top face; without a source thickness, use resolved tile thickness + resolved runner flange thickness so the flange underside aligns with the source footprint datum."
        ),
        tile_top_offset_with_observed_thickness_m=0.0,
        tile_top_offset_without_thickness_formula=(
            "resolved(ceiling.tile.thickness_m) + resolved(ceiling.runner.flange_thickness_m)"
        ),
        description=(
            "Illustrative rectangular lattice on a 24-inch (609.6 mm) pitch. Each tee uses the chosen finite section; a panel occupies each cut bay. Straight wires end on the slab soffit and a named tee stem. Boundary trim, joint tabs and fixings are not modeled. These shapes do not certify loads, fire resistance or regulatory compliance. The existing slab record owns the deck; no second deck object is emitted."
        ),
    ),),
)


def _require_reserved_builtin_identities(catalog: SuspendedCeilingPresetCatalog) -> None:
    """Prevent edited content from impersonating canonical built-in IDs."""
    builtin = DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG
    if (catalog.catalog_id, catalog.catalog_revision) == (
            builtin.catalog_id, builtin.catalog_revision) and catalog != builtin:
        raise ContractError(
            "reserved built-in suspended-ceiling catalog identity requires exact canonical content"
        )
    builtin_preset = builtin.presets[0]
    builtin_sources = {source.source_id: source for source in builtin.sources}
    caller_sources = {source.source_id: source for source in catalog.sources}
    for candidate in catalog.presets:
        if (candidate.preset_id, candidate.preset_revision) == (
                builtin_preset.preset_id, builtin_preset.preset_revision) and candidate != builtin_preset:
            raise ContractError(
                "reserved built-in suspended-ceiling preset identity requires exact canonical content"
            )
        referenced_sources = {definition.source_id for definition in candidate.parameters
                              if definition.source_id is not None}
        if any(source_id in builtin_sources and
               caller_sources.get(source_id) != builtin_sources[source_id]
               for source_id in referenced_sources):
            raise ContractError(
                "reserved built-in suspended-ceiling preset sources require exact canonical content"
            )


__all__ = [
    "SUSPENDED_CEILING_CATALOG_ID", "SUSPENDED_CEILING_CATALOG_REVISION",
    "SUSPENDED_CEILING_CATALOG_VERSION", "SUSPENDED_CEILING_PRESET_ID",
    "SUSPENDED_CEILING_PRESET_REVISION", "SUSPENDED_CEILING_GENERATOR_VERSION",
    "CEILING_DATUM_OFFSET_KEY", "SUSPENDED_CEILING_PARAMETER_IDS", "SuspendedCeilingPreset",
    "SuspendedCeilingPresetCatalog", "DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG",
]
