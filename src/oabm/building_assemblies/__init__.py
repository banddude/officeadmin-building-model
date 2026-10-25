"""Finite preset library and deterministic building-assembly producers."""

from .catalog import (
    CATALOG_ID,
    CATALOG_REVISION,
    DEFAULT_PRESET_CATALOG,
    WALL_GENERATOR_VERSION,
    WOOD_WALL_PRESET_ID,
    WOOD_WALL_PRESET_REVISION,
    BuildingAssemblyPresetCatalog,
    CatalogSource,
    ParameterDefinition,
    WallPreset,
)
from .wall import (
    ParameterOverride,
    WallAssemblyRequest,
    WallPresetError,
    WallPresetResolution,
    build_wall_assembly,
    resolve_wall_preset,
)

__all__ = [
    "CATALOG_ID", "CATALOG_REVISION", "DEFAULT_PRESET_CATALOG",
    "WALL_GENERATOR_VERSION", "WOOD_WALL_PRESET_ID", "WOOD_WALL_PRESET_REVISION",
    "BuildingAssemblyPresetCatalog", "CatalogSource", "ParameterDefinition", "WallPreset",
    "ParameterOverride", "WallAssemblyRequest", "WallPresetError", "WallPresetResolution",
    "build_wall_assembly", "resolve_wall_preset",
]
