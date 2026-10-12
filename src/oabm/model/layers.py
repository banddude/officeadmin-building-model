"""Portable saved layer selections over explicit canonical entity membership.

Bounded first implementation under issues #237 and #227. A
:class:`LayerSelectionConfig` names layers, lists their canonical member ids
explicitly, and saves named presets that assign every layer one display
state. Nothing is classified from prose, geometry or nearest-object
proximity: membership is exactly what the configuration states, and labels
(``system``/``trade``/``phase``) are carried, never inferred.

:func:`resolve_preset` resolves one preset against a concrete
:class:`~oabm.model.BuildingModel` into an explicit visible/hidden/ghosted
partition of the entities the current GLB exporter draws as canonical nodes
(walls, slabs, spaces, electrical devices, electrical equipment, routes and
physical electrical boxes). The configuration is portable data, not part of
the canonical model: resolving it never mutates the model, and a layer whose
name suggests a system but whose resolved membership is empty never asserts
that the system is modeled.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .common import (
    ContractError, UnsupportedSchemaVersion, _validate_id,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from .model import BuildingModel

LAYER_SELECTIONS_SCHEMA_VERSION = "1.0.0"

STATE_VISIBLE = "visible"
STATE_HIDDEN = "hidden"
STATE_GHOSTED = "ghosted"
LAYER_STATES: frozenset[str] = frozenset({STATE_VISIBLE, STATE_HIDDEN, STATE_GHOSTED})

#: Policy for model entities no layer of the configuration assigns. The
#: default keeps today's viewer behaviour (everything a layer does not
#: mention stays visible); ``error`` refuses incomplete coverage outright.
UNASSIGNED_VISIBLE = "visible"
UNASSIGNED_HIDDEN = "hidden"
UNASSIGNED_GHOSTED = "ghosted"
UNASSIGNED_ERROR = "error"
UNASSIGNED_POLICIES: frozenset[str] = frozenset(
    {UNASSIGNED_VISIBLE, UNASSIGNED_HIDDEN, UNASSIGNED_GHOSTED, UNASSIGNED_ERROR}
)


def _require_id_tuple(value: Any, label: str) -> None:
    """An immutable id sequence: a tuple of canonical ids, never a string."""

    if isinstance(value, str) or not isinstance(value, tuple):
        raise ContractError(
            f"{label} must be a tuple of canonical id strings, got "
            f"{type(value).__name__}"
        )
    for item in value:
        if not isinstance(item, str) or isinstance(item, bool):
            raise ContractError(f"{label} entries must be strings")
        _validate_id(item, label)


@dataclass(frozen=True, slots=True, kw_only=True)
class LayerDefinition:
    """One named layer with explicit canonical entity membership."""

    id: str
    name: str
    member_ids: tuple[str, ...] = ()
    system: str | None = None
    trade: str | None = None
    phase: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.id, "layer id")
        _require_label(self.name, f"layer {self.id} name")
        _require_id_tuple(self.member_ids, f"layer {self.id} member_ids")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ContractError(
                f"layer {self.id} lists a member id more than once"
            )
        for label in ("system", "trade", "phase"):
            value = getattr(self, label)
            if value is not None:
                _require_label(value, f"layer {self.id} {label}")


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedPreset:
    """One named saved selection: a state for every layer, by layer id."""

    id: str
    name: str
    visible_layer_ids: tuple[str, ...] = ()
    hidden_layer_ids: tuple[str, ...] = ()
    ghosted_layer_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.id, "preset id")
        _require_label(self.name, f"preset {self.id} name")
        seen: dict[str, str] = {}
        for state, refs in (
            (STATE_VISIBLE, self.visible_layer_ids),
            (STATE_HIDDEN, self.hidden_layer_ids),
            (STATE_GHOSTED, self.ghosted_layer_ids),
        ):
            _require_id_tuple(refs, f"preset {self.id} {state}_layer_ids")
            for layer_id in refs:
                _validate_id(layer_id, f"preset {self.id} {state} layer id")
                if layer_id in seen:
                    raise ContractError(
                        f"preset {self.id} assigns layer {layer_id!r} both "
                        f"{seen[layer_id]!r} and {state!r}"
                    )
                seen[layer_id] = state

    def state_of(self, layer_id: str) -> str | None:
        if layer_id in self.visible_layer_ids:
            return STATE_VISIBLE
        if layer_id in self.hidden_layer_ids:
            return STATE_HIDDEN
        if layer_id in self.ghosted_layer_ids:
            return STATE_GHOSTED
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class LayerSelectionConfig:
    """Versioned portable configuration of layers and saved presets."""

    schema_version: str = LAYER_SELECTIONS_SCHEMA_VERSION
    layers: tuple[LayerDefinition, ...] = ()
    presets: tuple[SavedPreset, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != LAYER_SELECTIONS_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"unsupported layer selections schema_version "
                f"{self.schema_version!r}; this reader knows "
                f"{LAYER_SELECTIONS_SCHEMA_VERSION!r} only"
            )
        if isinstance(self.layers, (str, dict)) or not isinstance(self.layers, tuple):
            raise ContractError("layers must be a tuple of LayerDefinition")
        if any(not isinstance(layer, LayerDefinition) for layer in self.layers):
            raise ContractError("layers must contain only LayerDefinition records")
        if isinstance(self.presets, (str, dict)) or not isinstance(self.presets, tuple):
            raise ContractError("presets must be a tuple of SavedPreset")
        if any(not isinstance(preset, SavedPreset) for preset in self.presets):
            raise ContractError("presets must contain only SavedPreset records")
        by_id: dict[str, LayerDefinition] = {}
        by_name: dict[str, LayerDefinition] = {}
        for layer in self.layers:
            if layer.id in by_id:
                raise ContractError(f"duplicate layer id {layer.id!r}")
            if layer.name in by_name:
                raise ContractError(
                    f"duplicate layer name {layer.name!r} "
                    f"({by_name[layer.name].id!r} and {layer.id!r})"
                )
            by_id[layer.id] = layer
            by_name[layer.name] = layer
        preset_ids: set[str] = set()
        preset_names: set[str] = set()
        for preset in self.presets:
            if preset.id in preset_ids:
                raise ContractError(f"duplicate preset id {preset.id!r}")
            if preset.name in preset_names:
                raise ContractError(
                    f"duplicate preset name {preset.name!r}; "
                    f"{preset.id!r} repeats an earlier preset's name"
                )
            preset_ids.add(preset.id)
            preset_names.add(preset.name)
            # A preset is a total function over the configuration's layers:
            # every layer is named exactly once, so resolution has no silent
            # default state to guess.
            missing = [l.id for l in self.layers if preset.state_of(l.id) is None]
            if missing:
                raise ContractError(
                    f"preset {preset.id} assigns no state to layer(s) "
                    f"{', '.join(sorted(missing))}"
                )
            unknown = sorted(
                {lid for ref in (
                    preset.visible_layer_ids, preset.hidden_layer_ids,
                    preset.ghosted_layer_ids) for lid in ref}
                - set(by_id)
            )
            if unknown:
                raise ContractError(
                    f"preset {preset.id} references unknown layer id(s) "
                    f"{', '.join(unknown)}"
                )

    def layer(self, ref: str) -> LayerDefinition:
        """Resolve a layer by id, else by exact name; never by prefix."""

        for layer in self.layers:
            if layer.id == ref:
                return layer
        for layer in self.layers:
            if layer.name == ref:
                return layer
        raise ContractError(f"no layer {ref!r} in this configuration")

    def preset(self, ref: str) -> SavedPreset:
        """Resolve a preset by id first, then by exact name."""

        for candidate in self.presets:
            if candidate.id == ref:
                return candidate
        for candidate in self.presets:
            if candidate.name == ref:
                return candidate
        raise ContractError(f"no preset {ref!r} in this configuration")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "layers": [
                {
                    "id": layer.id,
                    "name": layer.name,
                    "member_ids": list(layer.member_ids),
                    **({
                        label: getattr(layer, label)
                        for label in ("system", "trade", "phase")
                        if getattr(layer, label) is not None
                    }),
                }
                for layer in self.layers
            ],
            "presets": [
                {
                    "id": preset.id,
                    "name": preset.name,
                    "visible_layer_ids": list(preset.visible_layer_ids),
                    "hidden_layer_ids": list(preset.hidden_layer_ids),
                    "ghosted_layer_ids": list(preset.ghosted_layer_ids),
                }
                for preset in self.presets
            ],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> LayerSelectionConfig:
        data = _require_object(document, "layer selections document")
        _require_fields(
            data, {"schema_version", "layers", "presets"}, "layer selections document",
        )
        version = data["schema_version"]
        if not isinstance(version, str):
            raise ContractError("schema_version must be a string")
        if version != LAYER_SELECTIONS_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"unsupported layer selections schema_version {version!r}; this "
                f"reader knows {LAYER_SELECTIONS_SCHEMA_VERSION!r} only"
            )
        raw_layers = _require_array(data["layers"], "layers")
        layers = tuple(_decode_layer(item) for item in raw_layers)
        raw_presets = (
            _require_array(data["presets"], "presets") if "presets" in data else []
        )
        presets = tuple(_decode_preset(item) for item in raw_presets)
        return cls(schema_version=version, layers=layers, presets=presets)

    @classmethod
    def from_json(cls, payload: str | bytes) -> LayerSelectionConfig:
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON: {exc}") from exc
        return cls.from_dict(document)

    @classmethod
    def load(cls, path: str | Path) -> LayerSelectionConfig:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def dump(self, path: str | Path, *, indent: int = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True, kw_only=True)
class PresetResolution:
    """Explicit visibility manifest a preset resolves to on one model.

    ``visible_ids``/``hidden_ids``/``ghosted_ids`` are a disjoint, complete
    partition of the drawable universe (the canonical node kinds the current
    GLB exporter draws), sorted and free of duplicates. ``unassigned_ids``
    reports every universe entity no layer of the configuration assigns, so
    incomplete coverage is always visible to the caller. Derived conductor
    wire nodes are not listed: they ride their route and are counted once,
    through the route id.
    """

    preset_id: str
    preset_name: str
    visible_ids: tuple[str, ...]
    hidden_ids: tuple[str, ...]
    ghosted_ids: tuple[str, ...]
    unassigned_ids: tuple[str, ...]
    empty_layer_ids: tuple[str, ...]
    layer_coverage: tuple[tuple[str, int], ...]
    system_coverage: tuple[tuple[str, int], ...]
    out_of_scope_member_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "visible_ids", "hidden_ids", "ghosted_ids", "unassigned_ids",
            "empty_layer_ids", "out_of_scope_member_ids",
        ):
            _require_id_tuple(getattr(self, field), field)
        for field in ("layer_coverage", "system_coverage"):
            value = getattr(self, field)
            if isinstance(value, (str, dict)) or not isinstance(value, tuple) or any(
                not isinstance(item, tuple) or len(item) != 2
                or not isinstance(item[0], str) or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                for item in value
            ):
                raise ContractError(
                    f"{field} must be a tuple of (string, count) pairs"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "visible_ids": list(self.visible_ids),
            "hidden_ids": list(self.hidden_ids),
            "ghosted_ids": list(self.ghosted_ids),
            "unassigned_ids": list(self.unassigned_ids),
            "empty_layer_ids": list(self.empty_layer_ids),
            "layer_coverage": dict(self.layer_coverage),
            "system_coverage": dict(self.system_coverage),
            "out_of_scope_member_ids": list(self.out_of_scope_member_ids),
        }


def resolve_preset(
    config: LayerSelectionConfig,
    model: "BuildingModel",
    *,
    preset: str,
    isolate: Iterable[str] = (),
    level_ids: Iterable[str] = (),
    unassigned: str,
) -> PresetResolution:
    """Resolve one saved preset against ``model`` deterministically.

    Set semantics, documented once here and applied in this order:

    1. Every configured member id must exist in the model; dangling or
       unknown level references are refused with precise diagnostics.
    2. An entity's state is the strictest state of the layers that contain
       it: ``hidden`` beats ``ghosted`` beats ``visible``.
    3. An entity in no layer of the whole configuration is *unassigned*.
       Incomplete coverage is a property of the configuration, so it is
       always reported in ``unassigned_ids``, independent of ``isolate``
       and of the level filter. The caller's ``unassigned`` policy (a
       required argument; there is no silent default) decides where
       unassigned entities land, or ``error`` refuses incomplete coverage
       outright, also under isolate.
    4. A nonempty ``isolate`` restricts the considered layers to the named
       ones: their members keep the strictest state among those layers only
       (with the preset's state for each), and every other entity is
       hidden. Reporting coverage stays a whole-configuration property.
    5. A nonempty ``level_ids`` filter keeps entities whose canonical level
       is in the filter and hides the rest. Routes resolve their level from
       the start port's owner (the same documented rule the GLB exporter
       uses for reference planes); an entity with no resolvable level is
       outside the filter and hidden.

    The result never duplicates an id across states, and permuting the
    order of layers, members or presets in the configuration cannot change
    it.
    """

    if unassigned not in UNASSIGNED_POLICIES:
        raise ContractError(
            f"unassigned policy must be one of {sorted(UNASSIGNED_POLICIES)!r}, "
            f"got {unassigned!r}"
        )
    saved = config.preset(preset)

    model_ids = _model_entity_ids(model)
    for layer in config.layers:
        for member_id in layer.member_ids:
            if member_id not in model_ids:
                raise ContractError(
                    f"layer {layer.id} member {member_id!r} does not exist "
                    "in the model"
                )

    levels = {level.id for level in model.levels}
    filter_levels = tuple(sorted(set(level_ids)))
    for level_id in filter_levels:
        if level_id not in levels:
            raise ContractError(f"level filter references missing level {level_id!r}")

    isolate_refs = tuple(isolate)
    isolate_layers: list[LayerDefinition] = []
    for ref in isolate_refs:
        try:
            isolate_layers.append(config.layer(ref))
        except ContractError:
            raise ContractError(f"isolate references unknown layer {ref!r}") from None

    universe = _universe(model)
    # Coverage is a whole-configuration property: an entity in no layer is
    # unassigned even when isolate hides it or the level filter would hide
    # it, so incomplete coverage can never disappear behind a display state.
    assigned_ids: set[str] = {
        member_id for layer in config.layers for member_id in layer.member_ids
    }
    unassigned_ids = sorted(entity_id for entity_id in universe if entity_id not in assigned_ids)
    if unassigned_ids and unassigned == UNASSIGNED_ERROR:
        raise ContractError(
            "configuration does not cover every drawable entity; "
            f"{len(unassigned_ids)} unassigned, including "
            + ", ".join(unassigned_ids[:5])
        )

    considered = isolate_layers if isolate_layers else list(config.layers)
    entity_state: dict[str, str] = {}
    for entity_id in universe:
        states = {
            saved.state_of(layer.id)
            for layer in considered
            if entity_id in layer.member_ids
        }
        states.discard(None)
        if states:
            entity_state[entity_id] = _strictest(states)  # type: ignore[arg-type]
        elif not isolate_layers:
            entity_state[entity_id] = unassigned
        else:
            # Isolate: everything outside the isolated layers is hidden by
            # definition, whether or not the configuration covers it. The
            # coverage report above still names it.
            entity_state[entity_id] = STATE_HIDDEN

    if filter_levels:
        for entity_id, entity_level in universe.items():
            if entity_level is None or entity_level not in filter_levels:
                entity_state[entity_id] = STATE_HIDDEN

    visible = sorted(i for i, s in entity_state.items() if s == STATE_VISIBLE)
    hidden = sorted(i for i, s in entity_state.items() if s == STATE_HIDDEN)
    ghosted = sorted(i for i, s in entity_state.items() if s == STATE_GHOSTED)

    layer_coverage = tuple(
        (layer.id, len(set(layer.member_ids) & universe.keys()))
        for layer in sorted(config.layers, key=lambda item: item.id)
    )
    empty_layer_ids = tuple(
        layer_id for layer_id, count in layer_coverage if count == 0
    )
    system_members: dict[str, set[str]] = {}
    for layer in config.layers:
        if layer.system is None:
            continue
        system_members.setdefault(layer.system, set()).update(
            set(layer.member_ids) & universe.keys()
        )
    system_coverage = tuple(
        (system, len(members))
        for system, members in sorted(system_members.items())
    )
    out_of_scope = sorted(
        {
            member_id
            for layer in config.layers
            for member_id in layer.member_ids
            if member_id not in universe
        }
    )

    return PresetResolution(
        preset_id=saved.id,
        preset_name=saved.name,
        visible_ids=tuple(visible),
        hidden_ids=tuple(hidden),
        ghosted_ids=tuple(ghosted),
        unassigned_ids=tuple(unassigned_ids),
        empty_layer_ids=empty_layer_ids,
        layer_coverage=layer_coverage,
        system_coverage=system_coverage,
        out_of_scope_member_ids=tuple(out_of_scope),
    )


def _strictest(states: set[str]) -> str:
    """hidden beats ghosted beats visible, whatever the layer order."""

    if STATE_HIDDEN in states:
        return STATE_HIDDEN
    if STATE_GHOSTED in states:
        return STATE_GHOSTED
    return STATE_VISIBLE


def _universe(model: "BuildingModel") -> dict[str, str | None]:
    """Drawable universe: id to canonical level id, sorted by id.

    These are exactly the canonical node kinds the current GLB exporter
    draws (walls, slabs, spaces, devices, equipment, routes, physical
    electrical boxes, and the 1.3 support components, interfaces and
    assemblies). Ceilings and openings are not drawn yet and are outside
    the universe; conductor wire nodes ride their route, and a support
    assembly node brings its component and interface children with it.
    """

    entries: dict[str, str | None] = {}
    for wall in model.walls:
        entries[wall.id] = wall.level_id
    for slab in model.slabs:
        entries[slab.id] = slab.level_id
    for space in model.spaces:
        entries[space.id] = space.level_id
    for device in model.electrical_devices:
        entries[device.id] = device.level_id
    for equipment in model.electrical_equipment:
        entries[equipment.id] = equipment.level_id
    for route in model.routes:
        entries[route.id] = _route_level_id(model, route)
    for box in model.electrical_boxes:
        entries[box.id] = box.level_id
    for component in model.support_components:
        entries[component.id] = component.level_id
    for interface in model.support_interfaces:
        entries[interface.id] = interface.level_id
    for assembly in model.support_assemblies:
        entries[assembly.id] = assembly.level_id
    return dict(sorted(entries.items()))


def _route_level_id(model: "BuildingModel", route: Any) -> str | None:
    """A route's level: the start port owner's level, else None.

    Deliberately the same documented rule the GLB exporter applies for its
    per-level reference planes, so the manifest and the viewer cannot
    disagree about which level a route belongs to.
    """

    port = next((p for p in model.ports if p.id == route.start_port_id), None)
    if port is None:
        return None
    owner = next(
        (
            item
            for item in (*model.electrical_equipment, *model.electrical_devices, *model.electrical_boxes)
            if item.id == port.owner_id
        ),
        None,
    )
    return getattr(owner, "level_id", None)


def _model_entity_ids(model: "BuildingModel") -> set[str]:
    """Every canonical entity id the model defines, any collection."""

    ids: set[str] = set()
    collections = (
        model.levels, model.spaces, model.walls, model.slabs, model.ceilings,
        model.openings, model.electrical_equipment, model.electrical_devices,
        model.ports, model.obstacles, model.route_constraints, model.routes,
        model.route_fittings, model.circuits, model.conductors,
        model.electrical_boxes, model.box_selection_outcomes,
        model.raceway_selections, model.raceway_selection_outcomes,
        model.support_assemblies, model.support_components,
        model.support_interfaces, model.support_selection_outcomes,
    )
    for collection in collections:
        for entity in collection:
            ids.add(entity.id)
    return ids


def _require_label(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a nonempty string")


def _require_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _require_array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    return value


def _require_fields(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ContractError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _decode_str_tuple(value: Any, label: str) -> tuple[str, ...]:
    raw = _require_array(value, label)
    for item in raw:
        _validate_id(item, f"{label} entry")
    return tuple(raw)


def _decode_layer(data: Any) -> LayerDefinition:
    data = _require_object(data, "layer")
    _require_fields(
        data,
        {"id", "name", "member_ids", "system", "trade", "phase"},
        "layer",
    )
    missing = sorted({"id", "name"} - set(data))
    if missing:
        raise ContractError(f"layer is missing required field(s): {', '.join(missing)}")
    kwargs: dict[str, Any] = {
        "id": data["id"],
        "name": data["name"],
    }
    if "member_ids" in data:
        kwargs["member_ids"] = _decode_str_tuple(data["member_ids"], "member_ids")
    for label in ("system", "trade", "phase"):
        if label in data:
            # The portable contract is "absent or a nonempty string": explicit
            # null states nothing and is refused, agreeing with the JSON
            # Schema, which has no null branch for these labels.
            _require_label(data[label], f"layer {label}")
            kwargs[label] = data[label]
    return LayerDefinition(**kwargs)


def _decode_preset(data: Any) -> SavedPreset:
    data = _require_object(data, "preset")
    _require_fields(
        data,
        {"id", "name", "visible_layer_ids", "hidden_layer_ids", "ghosted_layer_ids"},
        "preset",
    )
    missing = sorted({"id", "name"} - set(data))
    if missing:
        raise ContractError(f"preset is missing required field(s): {', '.join(missing)}")
    kwargs: dict[str, Any] = {"id": data["id"], "name": data["name"]}
    for field in ("visible_layer_ids", "hidden_layer_ids", "ghosted_layer_ids"):
        if field in data:
            kwargs[field] = _decode_str_tuple(data[field], field)
    return SavedPreset(**kwargs)
