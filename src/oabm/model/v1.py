"""Canonical model contract v1 public surface."""

from .common import (
    DERIVATION_CLASSES, DERIVATION_INFERRED, DERIVATION_OBSERVED, DERIVATION_USER,
    SCHEMA_VERSION, PHYSICAL_SCHEMA_VERSION, RACEWAY_SCHEMA_VERSION, SUPPORT_SCHEMA_VERSION, PHYSICAL_SCHEMA_VERSIONS, RACEWAY_SCHEMA_VERSIONS, SUPPORTED_SCHEMA_VERSIONS, SUPPORT_SCHEMA_VERSIONS, STRUCTURAL_SCHEMA_VERSION,
    Box3D, ContractError, CoordinateSystem, Entity, Geometry3D,
    Point3, Polygon3D, Polyline3D, Pose, Provenance, Quaternion, Size3,
    UnsupportedSchemaVersion, Vector3, is_observed, provenance_applies_to, stable_id,
)
from .entities import (
    WALL_CONSTRUCTION_TOKENS, Ceiling, Circuit, Conductor, ElectricalDevice,
    ElectricalEquipment, Level, Obstacle, Opening, Port, Route, RouteConstraint,
    RouteFitting, Slab, Space, Wall,
)
from .model import BuildingModel, upgrade_to_v1_1, upgrade_to_v1_2, upgrade_to_v1_3, upgrade_to_v1_4, validate_model
from .physical import BOX_MOUNTINGS, CatalogProduct, ElectricalBox, BoxSelectionOutcome

from .raceway import RuleReference, RacewaySelection, RacewaySelectionOutcome
from .layers import (
    LAYER_SELECTIONS_SCHEMA_VERSION, LAYER_STATES,
    UNASSIGNED_ERROR, UNASSIGNED_GHOSTED, UNASSIGNED_HIDDEN, UNASSIGNED_POLICIES,
    UNASSIGNED_VISIBLE, STATE_GHOSTED, STATE_HIDDEN, STATE_VISIBLE,
    LayerDefinition, LayerSelectionConfig, PresetResolution, SavedPreset,
    resolve_preset,
)

from .structural import STRUCTURAL_MEMBER_KINDS, StructuralMember
from .supports import SupportAssembly, SupportComponent, SupportInterface, SupportSelectionOutcome

__all__ = [name for name in globals() if not name.startswith("_")]
