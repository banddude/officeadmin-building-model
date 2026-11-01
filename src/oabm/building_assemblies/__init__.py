"""Finite preset library and deterministic building-assembly producers."""

from .catalog import (
    CATALOG_ID,
    CATALOG_REVISION,
    DEFAULT_PRESET_CATALOG,
    LEGACY_PRESET_CATALOG,
    LEGACY_WALL_GENERATOR_VERSION,
    WALL_GENERATOR_VERSION,
    WOOD_WALL_PRESET_ID,
    WOOD_WALL_PRESET_REVISION,
    BuildingAssemblyPresetCatalog,
    CatalogSource,
    ParameterDefinition,
    WallPreset,
)
from .wall import (
    OpeningFramingSpecification,
    ParameterOverride,
    WallAssemblyRequest,
    WallPresetError,
    WallPresetResolution,
    build_wall_assembly,
    resolve_wall_preset,
)

from .opening_insert_presets import (
    DEFAULT_OPENING_INSERT_CATALOG,
    DOOR_SINGLE_LEAF_PRESET_ID,
    WINDOW_FIXED_GLAZED_PRESET_ID,
    OpeningInsertPresetCatalog,
)
from .opening_inserts import (
    OpeningInsertError,
    OpeningInsertParameterOverride,
    OpeningInsertAssemblyRequest,
    OpeningInsertPresetResolution,
    resolve_opening_insert_preset,
    build_opening_insert_assembly,
)
from .suspended_ceiling_catalog import (
    DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG,
    SuspendedCeilingPresetCatalog,
)
from .suspended_ceiling import (
    CeilingParameterOverride,
    SuspendedCeilingAssemblyRequest,
    SuspendedCeilingPresetError,
    SuspendedCeilingPresetResolution,
    resolve_suspended_ceiling_preset,
    build_suspended_ceiling_assembly,
)

__all__ = [
    "CATALOG_ID", "CATALOG_REVISION", "DEFAULT_PRESET_CATALOG", "LEGACY_PRESET_CATALOG",
    "WALL_GENERATOR_VERSION", "WOOD_WALL_PRESET_ID", "WOOD_WALL_PRESET_REVISION",
    "LEGACY_WALL_GENERATOR_VERSION",
    "BuildingAssemblyPresetCatalog", "CatalogSource", "ParameterDefinition", "WallPreset",
    "ParameterOverride", "OpeningFramingSpecification", "WallAssemblyRequest", "WallPresetError", "WallPresetResolution",
    "build_wall_assembly", "resolve_wall_preset",
    "DEFAULT_OPENING_INSERT_CATALOG", "DOOR_SINGLE_LEAF_PRESET_ID",
    "WINDOW_FIXED_GLAZED_PRESET_ID", "OpeningInsertPresetCatalog",
    "OpeningInsertError", "OpeningInsertParameterOverride",
    "OpeningInsertAssemblyRequest", "OpeningInsertPresetResolution",
    "resolve_opening_insert_preset", "build_opening_insert_assembly",
    "DEFAULT_SUSPENDED_CEILING_PRESET_CATALOG", "SuspendedCeilingPresetCatalog",
    "CeilingParameterOverride", "SuspendedCeilingAssemblyRequest",
    "SuspendedCeilingPresetError", "SuspendedCeilingPresetResolution",
    "resolve_suspended_ceiling_preset", "build_suspended_ceiling_assembly",
]

from .regeneration import AssemblyRegenerationError, regenerate_building_assemblies
__all__ += ["AssemblyRegenerationError", "regenerate_building_assemblies"]
