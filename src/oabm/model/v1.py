"""Canonical model contract v1 public surface."""

from .common import (
    DERIVATION_CLASSES, DERIVATION_INFERRED, DERIVATION_OBSERVED, DERIVATION_USER,
    SCHEMA_VERSION, PHYSICAL_SCHEMA_VERSION, RACEWAY_SCHEMA_VERSION, SUPPORT_SCHEMA_VERSION, PHYSICAL_SCHEMA_VERSIONS, RACEWAY_SCHEMA_VERSIONS, SUPPORTED_SCHEMA_VERSIONS, SUPPORT_SCHEMA_VERSIONS, STRUCTURAL_SCHEMA_VERSION, STRUCTURAL_SCHEMA_VERSIONS, FITTING_SCHEMA_VERSION, FITTING_SCHEMA_VERSIONS, ASSEMBLY_SCHEMA_VERSION, ASSEMBLY_SCHEMA_VERSIONS,
    Box3D, ContractError, CoordinateSystem, Entity, Geometry3D,
    Point3, Polygon3D, Polyline3D, Pose, Provenance, Quaternion, Size3,
    UnsupportedSchemaVersion, Vector3, is_observed, provenance_applies_to, stable_id,
)
from .entities import (
    WALL_CONSTRUCTION_TOKENS, Ceiling, Circuit, Conductor, ElectricalDevice,
    ElectricalEquipment, Level, Obstacle, Opening, Port, Route, RouteConstraint,
    RouteFitting, Slab, Space, Wall,
)
from .model import BuildingModel, upgrade_to_v1_1, upgrade_to_v1_2, upgrade_to_v1_3, upgrade_to_v1_4, upgrade_to_v1_5, upgrade_to_v1_6, validate_model
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
from .fittings import (AcceptedODRange, FITTING_CONNECTION_METHODS, FITTING_GEOMETRY_BASES,
                       FITTING_INTERFACE_ROLES, FITTING_ROUTE_TYPE, FITTING_SELECTION_REASONS,
                       FITTING_TYPES, FITTING_WIRING_METHODS, FittingInterface,
                       FittingSelection, FittingSelectionOutcome)
from .assemblies import (
    ASSEMBLY_KINDS, HOST_REPRESENTATIONS, ASSEMBLY_COMPONENT_KINDS,
    GEOMETRY_BASES, MATERIAL_BASES, PARAMETER_KINDS, PARAMETER_ORIGINS,
    ASSEMBLY_MAX_PARAMETERS, ASSEMBLY_MAX_COMPONENTS, ASSEMBLY_MAX_PROFILE_POINTS,
    ASSEMBLY_MAX_PANEL_VERTICES, ASSEMBLY_MAX_WIRE_SEGMENTS,
    AssemblyGeometry, AssemblyComponent, BuildingAssembly, HangerAttachment,
    PanelGeometry, Point2D, Profile2D, ProfileExtrusion, ProfileOutline2D,
    ResolvedParameter, WallAssemblyFrame, WireSweep,
    stable_assembly_component_id, point_on_component_surface, point_on_slab_surface,
)
from .assembly_validation import (
    compute_assembly_input_digest, compute_assembly_output_digest,
    compute_assembly_digests, component_world_points,
)

__all__ = [name for name in globals() if not name.startswith("_")]
