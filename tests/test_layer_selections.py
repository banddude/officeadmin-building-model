"""Portable saved layer selections: strict config, deterministic resolver, GLB handoff."""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from oabm.exports import layer_glb_plan, to_glb
from oabm.model import (
    BuildingModel,
    CatalogProduct,
    ContractError,
    ElectricalDevice,
    ElectricalEquipment,
    LayerDefinition,
    LayerSelectionConfig,
    Level,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    PresetResolution,
    Provenance,
    RuleReference,
    Route,
    SavedPreset,
    Size3,
    Slab,
    Space,
    SupportAssembly,
    SupportComponent,
    SupportInterface,
    SupportSelectionOutcome,
    UNASSIGNED_ERROR,
    UNASSIGNED_GHOSTED,
    UNASSIGNED_HIDDEN,
    UNASSIGNED_VISIBLE,
    Vector3,
    Wall,
    resolve_preset,
    upgrade_to_v1_1,
    upgrade_to_v1_2,
    upgrade_to_v1_3,
)

ROOT = Path(__file__).resolve().parents[1]
_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_UNIVERSE = 12  # 3 walls, 2 slabs, 1 space, 3 devices, 1 equipment, 2 routes


# ---------------------------------------------------------------- synthetic model


def _two_level_model() -> BuildingModel:
    panel = ElectricalEquipment(
        id="equip:one-panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.5, y=0.1, z=1.5)),
        level_id="level:one",
    )
    devices = (
        ElectricalDevice(
            id="device:one-recp", device_type="receptacle",
            pose=Pose(position=Point3(x=2.0, y=0.2, z=1.2)), level_id="level:one",
        ),
        # A ceiling device with no recorded level: a level filter must hide it.
        ElectricalDevice(
            id="device:one-em", device_type="occupancy_sensor",
            pose=Pose(position=Point3(x=3.0, y=2.0, z=2.9)), level_id=None,
        ),
        ElectricalDevice(
            id="device:two-recp", device_type="receptacle",
            pose=Pose(position=Point3(x=1.5, y=0.2, z=5.2)), level_id="level:two",
        ),
    )
    ports = (
        Port(
            id="port:panel-out", owner_id="equip:one-panel", domain="electrical",
            role="load", pose=Pose(position=Point3(x=0.5, y=0.1, z=1.4)),
            direction=Vector3(x=0, y=0, z=-1),
        ),
        Port(
            id="port:recp-in", owner_id="device:one-recp", domain="electrical",
            role="line", pose=Pose(position=Point3(x=2.0, y=0.2, z=1.2)),
            direction=Vector3(x=0, y=0, z=1),
        ),
        Port(
            id="port:two-in", owner_id="device:two-recp", domain="electrical",
            role="line", pose=Pose(position=Point3(x=1.5, y=0.2, z=5.2)),
            direction=Vector3(x=0, y=0, z=1),
        ),
    )
    routes = (
        Route(
            id="route:one", route_type="emt",
            start_port_id="port:panel-out", end_port_id="port:recp-in",
            centerline=Polyline3D(points=(
                Point3(x=0.5, y=0.1, z=1.4),
                Point3(x=0.5, y=0.1, z=2.6),
                Point3(x=2.0, y=0.2, z=2.6),
                Point3(x=2.0, y=0.2, z=1.2),
            )),
            nominal_diameter_m=0.02,
        ),
        Route(
            id="route:two", route_type="emt",
            start_port_id="port:panel-out", end_port_id="port:two-in",
            centerline=Polyline3D(points=(
                Point3(x=0.5, y=0.1, z=1.4),
                Point3(x=0.5, y=0.1, z=5.4),
                Point3(x=1.5, y=0.2, z=5.4),
                Point3(x=1.5, y=0.2, z=5.2),
            )),
            nominal_diameter_m=0.02,
        ),
    )

    def _wall(wall_id: str, level_id: str, construction: str | None, y: float) -> Wall:
        return Wall(
            id=wall_id, level_id=level_id,
            centerline=Polyline3D(points=(
                Point3(x=0, y=y, z=0), Point3(x=4, y=y, z=0),
            )),
            thickness_m=0.1, height_m=3.0, construction=construction,
        )

    def _slab(slab_id: str, level_id: str, z: float) -> Slab:
        return Slab(
            id=slab_id, level_id=level_id,
            footprint=Polygon3D(points=(
                Point3(x=0, y=0, z=z), Point3(x=4, y=0, z=z), Point3(x=4, y=4, z=z),
            )),
            thickness_m=0.2,
        )

    return BuildingModel(
        model_id="model:layers-demo",
        levels=(
            Level(id="level:one", elevation_m=0.0, height_m=3.0),
            Level(id="level:two", elevation_m=4.0, height_m=3.0),
        ),
        walls=(
            _wall("wall:one-a", "level:one", "framed", 0.0),
            _wall("wall:one-glass", "level:one", "glazed", 1.0),
            _wall("wall:two-a", "level:two", "framed", 0.0),
        ),
        slabs=(_slab("slab:one", "level:one", 0.0), _slab("slab:two", "level:two", 4.0)),
        spaces=(
            Space(
                id="space:one", level_id="level:one",
                footprint=Polygon3D(points=(
                    Point3(x=0, y=0, z=0), Point3(x=4, y=0, z=0), Point3(x=4, y=4, z=0),
                )),
            ),
        ),
        electrical_devices=devices,
        electrical_equipment=(panel,),
        ports=ports,
        routes=routes,
    )


def _demo_config() -> LayerSelectionConfig:
    return LayerSelectionConfig.from_dict({
        "schema_version": "1.0.0",
        "layers": [
            {
                "id": "layer:power",
                "name": "Power",
                "member_ids": [
                    "device:one-recp", "device:two-recp", "route:one", "route:two",
                    "equip:one-panel",
                ],
                "system": "power",
                "trade": "electrical",
            },
            {
                "id": "layer:lighting",
                "name": "Lighting",
                "member_ids": ["device:one-em"],
                "system": "lighting",
            },
            {
                "id": "layer:phase-1",
                "name": "Phase 1",
                "member_ids": ["device:one-recp", "route:one"],
                "phase": "phase-1",
            },
            {
                "id": "layer:arch",
                "name": "Architecture",
                "member_ids": [
                    "wall:one-a", "wall:one-glass", "wall:two-a",
                    "slab:one", "slab:two", "space:one",
                ],
                "trade": "architecture",
            },
            {
                "id": "layer:fire-alarm",
                "name": "Fire alarm",
                "system": "fire_alarm",
            },
        ],
        "presets": [
            {
                "id": "preset:all",
                "name": "All visible",
                "visible_layer_ids": [
                    "layer:power", "layer:lighting", "layer:phase-1",
                    "layer:arch", "layer:fire-alarm",
                ],
            },
            {
                "id": "preset:phase-1",
                "name": "Phase 1 only",
                "visible_layer_ids": ["layer:power", "layer:phase-1"],
                "hidden_layer_ids": ["layer:lighting", "layer:fire-alarm"],
                "ghosted_layer_ids": ["layer:arch"],
            },
            {
                "id": "preset:ghost-power",
                "name": "Power ghosted",
                "visible_layer_ids": ["layer:lighting", "layer:arch"],
                "hidden_layer_ids": ["layer:phase-1", "layer:fire-alarm"],
                "ghosted_layer_ids": ["layer:power"],
            },
        ],
    })


def _assert_partition(resolution) -> None:
    visible = set(resolution.visible_ids)
    hidden = set(resolution.hidden_ids)
    ghosted = set(resolution.ghosted_ids)
    assert not (visible & hidden or visible & ghosted or hidden & ghosted)
    assert len(visible) + len(hidden) + len(ghosted) == _UNIVERSE
    for ids in (resolution.visible_ids, resolution.hidden_ids, resolution.ghosted_ids):
        assert list(ids) == sorted(ids)
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------- strict decoding


def test_round_trip_and_schema(tmp_path: Path) -> None:
    config = _demo_config()
    assert LayerSelectionConfig.from_json(config.to_json()) == config
    path = tmp_path / "layers.json"
    config.dump(path)
    assert LayerSelectionConfig.load(path) == config
    schema = json.loads((ROOT / "contracts/oabm-layers-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(config.to_json()))


@pytest.mark.parametrize("mutation,label", [
    (lambda d: d.update({"schema_version": "9.9.9"}), "schema_version"),
    (lambda d: d.update({"unknown": []}), "unknown"),
    (lambda d: d["layers"][0].update({"guess": True}), "unknown"),
    (lambda d: d["presets"][0].update({"default_layer_id": "layer:power"}), "unknown"),
    (lambda d: d["layers"][1].update({"name": "Power"}), "duplicate layer name"),
    (lambda d: d["layers"].append({**d["layers"][0], "name": "Other"}), "duplicate layer id"),
    (lambda d: d["presets"].append({**d["presets"][0], "name": "Other"}), "duplicate preset id"),
    (
        lambda d: d["layers"][0].update(
            {"member_ids": d["layers"][0]["member_ids"] + ["device:one-recp"]}
        ),
        "more than once",
    ),
    (lambda d: d["presets"][0].update({"hidden_layer_ids": ["layer:phase-1"]}), "both"),
    (lambda d: d["presets"][0].update({"hidden_layer_ids": ["layer:nope"]}), "unknown layer id"),
    (
        lambda d: d["presets"][1].update({"visible_layer_ids": ["layer:power"]}),
        "assigns no state",
    ),
    (lambda d: d["layers"][0].update({"id": "1bad"}), "must match"),
    (lambda d: d["layers"][0].update({"system": 12}), "nonempty string"),
    (lambda d: d["layers"][0].update({"system": None}), "nonempty string"),
    (lambda d: d["layers"][0].update({"member_ids": [42]}), "must match"),
    (lambda d: d["layers"][0].update({"member_ids": "abc"}), "must be an array"),
    (
        lambda d: d["presets"][0].update({"visible_layer_ids": "layer:power"}),
        "must be an array",
    ),
    (lambda d: d["layers"][0].pop("name"), "missing required field"),
])
def test_strict_config_rejection(mutation, label) -> None:
    document = json.loads(_demo_config().to_json())
    mutation(document)
    with pytest.raises(ContractError, match=label):
        LayerSelectionConfig.from_dict(document)


def test_invalid_json_and_non_object_root() -> None:
    with pytest.raises(ContractError, match="invalid JSON"):
        LayerSelectionConfig.from_json("{nope")
    with pytest.raises(ContractError, match="must be an object"):
        LayerSelectionConfig.from_dict([1, 2])


def test_missing_field_diagnostics_name_the_fields() -> None:
    with pytest.raises(ContractError, match=r"layer is missing required field\(s\): id"):
        LayerSelectionConfig.from_dict({
            "schema_version": "1.0.0", "layers": [{"name": "No id"}],
        })
    with pytest.raises(
        ContractError, match=r"preset is missing required field\(s\): id, name",
    ):
        LayerSelectionConfig.from_dict({
            "schema_version": "1.0.0", "layers": [], "presets": [{}],
        })


def test_constructors_reject_scalar_and_mutable_id_sequences() -> None:
    with pytest.raises(ContractError, match="tuple of canonical id strings"):
        LayerDefinition(id="layer:x", name="X", member_ids="abc")
    with pytest.raises(ContractError, match="tuple of canonical id strings"):
        LayerDefinition(id="layer:x", name="X", member_ids=["device:a"])
    with pytest.raises(ContractError, match="tuple of canonical id strings"):
        SavedPreset(id="preset:x", name="X", visible_layer_ids="layer:power")
    with pytest.raises(ContractError, match="layers must be a tuple"):
        LayerSelectionConfig(
            layers=[LayerDefinition(id="layer:x", name="X")],
        )
    with pytest.raises(ContractError, match="presets must be a tuple"):
        LayerSelectionConfig(presets=[SavedPreset(id="preset:x", name="X")])
    with pytest.raises(ContractError, match="tuple of canonical id strings"):
        replace(
            PresetResolution(
                preset_id="preset:x", preset_name="X",
                visible_ids=(), hidden_ids=(), ghosted_ids=(),
                unassigned_ids=(), empty_layer_ids=(),
                layer_coverage=(), system_coverage=(),
                out_of_scope_member_ids=(),
            ),
            visible_ids=["device:a"],
        )


def test_constructor_decoder_and_schema_agree() -> None:
    schema = json.loads((ROOT / "contracts/oabm-layers-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    cases = [
        (_demo_config().to_dict(), True),
        ({"schema_version": "1.0.0", "layers": []}, True),
        ({"schema_version": "1.0.0", "layers": [], "presets": []}, True),
        # Explicit null labels: rejected by the decoder and the schema alike.
        (
            {
                "schema_version": "1.0.0",
                "layers": [{"id": "layer:x", "name": "X", "system": None}],
            },
            False,
        ),
        ({"schema_version": "1.0.0", "layers": [{"name": "no id"}]}, False),
        ({"schema_version": "1.0.0", "layers": [{"id": "layer:x"}]}, False),
        ({"schema_version": "1.0.0", "layers": [{"id": "layer:x", "name": ""}]}, False),
        ({"schema_version": "2.0.0", "layers": []}, False),
        ({"schema_version": "1.0.0", "layers": "abc"}, False),
        (
            {
                "schema_version": "1.0.0",
                "layers": [{"id": "layer:x", "name": "X", "extra": 1}],
            },
            False,
        ),
        (
            {
                "schema_version": "1.0.0",
                "layers": [{"id": "layer:x", "name": "X", "member_ids": "abc"}],
            },
            False,
        ),
    ]
    for document, expected_valid in cases:
        schema_valid = validator.is_valid(document)
        decode_error = None
        try:
            LayerSelectionConfig.from_dict(document)
        except ContractError as error:
            decode_error = str(error)
        assert schema_valid is expected_valid, (document, schema_valid, decode_error)
        assert (decode_error is None) is expected_valid, (document, schema_valid, decode_error)


# ---------------------------------------------------------------- resolution


def test_unassigned_policy_is_a_required_argument() -> None:
    model = _two_level_model()
    with pytest.raises(TypeError):
        resolve_preset(_demo_config(), model, preset="preset:all")


def test_preset_resolution_partition_and_overlap_precedence() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="Phase 1 only", unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert resolution.preset_id == "preset:phase-1"
    assert set(resolution.visible_ids) == {
        "device:one-recp", "device:two-recp", "equip:one-panel", "route:one", "route:two",
    }
    assert set(resolution.hidden_ids) == {"device:one-em"}
    assert set(resolution.ghosted_ids) == {
        "wall:one-a", "wall:one-glass", "wall:two-a", "slab:one", "slab:two", "space:one",
    }
    assert resolution.unassigned_ids == ()
    assert resolution.empty_layer_ids == ("layer:fire-alarm",)
    assert dict(resolution.system_coverage) == {
        "fire_alarm": 0, "lighting": 1, "power": 5,
    }
    assert dict(resolution.layer_coverage)["layer:phase-1"] == 2


def test_full_coverage_and_missing_system_never_claims_modeled() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert resolution.preset_id == "preset:all"
    assert not resolution.unassigned_ids
    assert "layer:fire-alarm" in resolution.empty_layer_ids
    assert dict(resolution.system_coverage)["fire_alarm"] == 0


def test_ghost_overlap_hidden_wins() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:ghost-power", unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    # device:one-recp and route:one sit in power (ghosted) and phase-1
    # (hidden): the stricter state wins.
    assert "device:one-recp" in resolution.hidden_ids
    assert "route:one" in resolution.hidden_ids
    assert set(resolution.ghosted_ids) == {
        "device:two-recp", "equip:one-panel", "route:two",
    }


def _partial_config() -> LayerSelectionConfig:
    document = json.loads(_demo_config().to_json())
    # Drop slab:two from every layer: the configuration no longer covers it.
    document["layers"][3]["member_ids"] = [
        m for m in document["layers"][3]["member_ids"] if m != "slab:two"
    ]
    return LayerSelectionConfig.from_dict(document)


@pytest.mark.parametrize("policy,state_attr", [
    (UNASSIGNED_VISIBLE, "visible_ids"),
    (UNASSIGNED_HIDDEN, "hidden_ids"),
    (UNASSIGNED_GHOSTED, "ghosted_ids"),
])
def test_unassigned_policy_places_the_uncovered(policy, state_attr) -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _partial_config(), model, preset="preset:all", unassigned=policy,
    )
    _assert_partition(resolution)
    assert resolution.unassigned_ids == ("slab:two",)
    assert "slab:two" in getattr(resolution, state_attr)


def test_unassigned_error_refuses_incomplete_coverage() -> None:
    model = _two_level_model()
    with pytest.raises(ContractError, match="slab:two"):
        resolve_preset(
            _partial_config(), model, preset="preset:all", unassigned=UNASSIGNED_ERROR,
        )


def test_isolate_reports_whole_configuration_coverage_and_hides_correctly() -> None:
    model = _two_level_model()
    # Without isolate the same configuration leaves slab:two unassigned.
    plain = resolve_preset(
        _partial_config(), model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE,
    )
    assert plain.unassigned_ids == ("slab:two",)
    # Isolate must not make the coverage gap disappear: it is a property of
    # the whole configuration, reported identically under isolate.
    isolated = resolve_preset(
        _partial_config(), model, preset="preset:all",
        isolate=("layer:power",), unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(isolated)
    assert isolated.unassigned_ids == plain.unassigned_ids == ("slab:two",)
    assert "slab:two" in isolated.hidden_ids


def test_unassigned_error_fires_under_isolate_too() -> None:
    model = _two_level_model()
    with pytest.raises(ContractError, match="slab:two"):
        resolve_preset(
            _partial_config(), model, preset="preset:all",
            isolate=("layer:power",), unassigned=UNASSIGNED_ERROR,
        )


def test_isolate_restricts_states_and_hides_everything_outside() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all",
        isolate=("layer:power",), unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert set(resolution.visible_ids) == {
        "device:one-recp", "device:two-recp", "equip:one-panel", "route:one", "route:two",
    }
    assert set(resolution.hidden_ids) == {
        "device:one-em", "wall:one-a", "wall:one-glass", "wall:two-a",
        "slab:one", "slab:two", "space:one",
    }
    assert resolution.ghosted_ids == ()
    assert "layer:fire-alarm" in resolution.empty_layer_ids


def test_isolate_of_an_empty_layer_renders_nothing() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all",
        isolate=("layer:fire-alarm",), unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert resolution.visible_ids == () and resolution.ghosted_ids == ()
    assert len(resolution.hidden_ids) == _UNIVERSE


def test_isolate_by_name_keeps_preset_state_within_isolate() -> None:
    model = _two_level_model()
    # The phase 1 preset ghosts architecture; isolating it by name keeps
    # that ghosted state and hides everything outside the layer.
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:phase-1",
        isolate=("Architecture",), unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert set(resolution.ghosted_ids) == {
        "wall:one-a", "wall:one-glass", "wall:two-a", "slab:one", "slab:two", "space:one",
    }
    assert resolution.visible_ids == ()
    assert set(resolution.hidden_ids) == {
        "device:one-recp", "device:one-em", "device:two-recp",
        "equip:one-panel", "route:one", "route:two",
    }


def test_level_filter_keeps_filtered_levels_and_resolves_route_levels() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all",
        level_ids=("level:one",), unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert "wall:two-a" in resolution.hidden_ids
    assert "slab:two" in resolution.hidden_ids
    assert "device:two-recp" in resolution.hidden_ids
    # route:two rides from the level-one panel, so its resolved level keeps
    # it visible even though its far end is upstairs.
    assert "route:two" in resolution.visible_ids
    # No recorded level means outside the filter.
    assert "device:one-em" in resolution.hidden_ids


def test_isolate_and_level_filter_compose() -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all",
        isolate=("layer:power",), level_ids=("level:two",),
        unassigned=UNASSIGNED_VISIBLE,
    )
    _assert_partition(resolution)
    assert set(resolution.visible_ids) == {"device:two-recp"}


def test_order_independence_and_determinism() -> None:
    model = _two_level_model()
    config = _demo_config()
    document = json.loads(config.to_json())
    document["layers"] = list(reversed(document["layers"]))
    for layer in document["layers"]:
        layer["member_ids"] = list(reversed(layer["member_ids"]))
    document["presets"] = list(reversed(document["presets"]))
    permuted = LayerSelectionConfig.from_dict(document)
    for preset in ("preset:all", "preset:phase-1", "preset:ghost-power"):
        first = resolve_preset(
            config, model, preset=preset, unassigned=UNASSIGNED_VISIBLE,
        ).to_dict()
        second = resolve_preset(
            permuted, model, preset=preset, unassigned=UNASSIGNED_VISIBLE,
        ).to_dict()
        assert first == second
    again = resolve_preset(
        config, model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
    ).to_dict()
    assert again == resolve_preset(
        config, model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
    ).to_dict()


def test_dangling_and_unknown_references_refuse_with_diagnostics() -> None:
    model = _two_level_model()
    document = json.loads(_demo_config().to_json())
    document["layers"][0]["member_ids"].append("device:vanishing")
    config = LayerSelectionConfig.from_dict(document)
    with pytest.raises(ContractError, match="layer:power"):
        resolve_preset(config, model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE)
    with pytest.raises(ContractError, match="no preset"):
        resolve_preset(
            _demo_config(), model, preset="preset:none", unassigned=UNASSIGNED_VISIBLE,
        )
    with pytest.raises(ContractError, match="missing level"):
        resolve_preset(
            _demo_config(), model, preset="preset:all",
            level_ids=("level:attic",), unassigned=UNASSIGNED_VISIBLE,
        )
    with pytest.raises(ContractError, match="unknown layer"):
        resolve_preset(
            _demo_config(), model, preset="preset:all",
            isolate=("layer:nope",), unassigned=UNASSIGNED_VISIBLE,
        )
    with pytest.raises(ContractError, match="unassigned policy"):
        resolve_preset(
            _demo_config(), model, preset="preset:all", unassigned="dim",
        )


def test_out_of_scope_members_are_reported_not_resolved() -> None:
    model = _two_level_model()
    document = json.loads(_demo_config().to_json())
    document["layers"][0]["member_ids"].append("port:panel-out")
    config = LayerSelectionConfig.from_dict(document)
    resolution = resolve_preset(
        config, model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE,
    )
    # The port exists but is not a drawable node, so it is reported as out
    # of scope and never enters the visible/hidden/ghosted partition.
    assert resolution.out_of_scope_member_ids == ("port:panel-out",)
    assert "port:panel-out" not in {
        *resolution.visible_ids, *resolution.hidden_ids, *resolution.ghosted_ids,
    }


def test_member_that_does_not_exist_in_the_model_refuses() -> None:
    model = _two_level_model()
    document = json.loads(_demo_config().to_json())
    document["layers"][0]["member_ids"].append("not-in-model")
    config = LayerSelectionConfig.from_dict(document)
    with pytest.raises(ContractError, match="does not exist in the model"):
        resolve_preset(config, model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE)


# ---------------------------------------------------------------- canonical outputs unchanged


def test_resolution_never_mutates_the_model_or_its_quantities() -> None:
    from dataclasses import asdict

    from oabm.quantities import extract_quantities

    model = _two_level_model()
    before_json = model.to_json()
    before_quantities = json.dumps(asdict(extract_quantities(model)), sort_keys=True)
    resolve_preset(
        _demo_config(), model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
    )
    assert model.to_json() == before_json
    assert json.dumps(asdict(extract_quantities(model)), sort_keys=True) == before_quantities


def test_default_glb_bytes_unchanged_by_layer_work(tmp_path: Path) -> None:
    model = _two_level_model()
    plain = tmp_path / "plain.glb"
    to_glb(model, plain)
    plain_bytes = plain.read_bytes()
    resolve_preset(
        _demo_config(), model, preset="preset:all", unassigned=UNASSIGNED_VISIBLE,
    )
    again = tmp_path / "again.glb"
    to_glb(model, again)
    assert again.read_bytes() == plain_bytes


# ---------------------------------------------------------------- current main physical collections


def _physical_model() -> BuildingModel:
    from oabm.electrical import (
        BoxCatalogEntry, BoxIntent, RacewayCatalogEntry, RacewayIntent, RacewayRule,
        select_electrical_boxes, select_raceways,
    )
    from oabm.model import RuleReference

    base = BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")
    product = CatalogProduct(
        catalog_id="synthetic", catalog_revision="1", product_id="layers-demo",
    )
    provenance = (
        Provenance(source_kind="synthetic", source_id="fixture:layers", derivation="user"),
    )
    box_entry = BoxCatalogEntry(
        product=product, box_type="synthetic-box", size=Size3(x=0.3, y=0.12, z=0.5),
        provenance=provenance,
    )
    box_intent = BoxIntent(
        intent_id="intent:box", occupant_ids=(base.electrical_devices[0].id,),
        actor_id="synthetic-reviewer", product=product, host_id="wall:south",
        mounting="flush", pose=Pose(position=Point3(x=5.2, y=-0.01, z=1.2)),
    )
    with_box = select_electrical_boxes(
        upgrade_to_v1_1(base), intents=(box_intent,), catalog=(box_entry,),
    )
    route = with_box.routes[0]
    basis = RuleReference(authority="synthetic-company", revision="1", rule_id="explicit-product-a")
    raceway_entry = RacewayCatalogEntry(
        product=product, route_type=route.route_type, wiring_method="raceway",
        designation_kind="trade_size", designation="synthetic trade A",
        nominal_diameter_m=route.nominal_diameter_m, provenance=provenance,
    )
    raceway_rule = RacewayRule(
        reference=basis, product=product, route_type=route.route_type,
        provenance=provenance,
    )
    raceway_intent = RacewayIntent(
        intent_id="intent:raceway", route_id=route.id,
        actor_id="synthetic-reviewer", product=product, basis=basis,
    )
    return select_raceways(
        upgrade_to_v1_2(with_box), intents=(raceway_intent,),
        catalog=(raceway_entry,), rules=(raceway_rule,),
    )


def test_physical_collections_counted_once() -> None:
    model = _physical_model()
    assert model.schema_version == "1.2.0"
    box_id = model.electrical_boxes[0].id
    selection_id = model.raceway_selections[0].id
    document = {
        "schema_version": "1.0.0",
        "layers": [
            {
                "id": "layer:demo",
                "name": "Demo",
                "member_ids": [
                    box_id,
                    model.electrical_devices[0].id,
                    model.electrical_equipment[0].id,
                    model.routes[0].id,
                    selection_id,  # a selection is not a drawable node
                ],
            },
            {
                "id": "layer:rest",
                "name": "Rest",
                "member_ids": [wall.id for wall in model.walls]
                + [model.spaces[0].id, model.slabs[0].id],
            },
        ],
        "presets": [
            {
                "id": "preset:demo",
                "name": "Demo",
                "visible_layer_ids": ["layer:demo", "layer:rest"],
            },
        ],
    }
    config = LayerSelectionConfig.from_dict(document)
    resolution = resolve_preset(config, model, preset="preset:demo", unassigned="hidden")
    drawable = (
        len(model.walls) + len(model.slabs) + len(model.spaces)
        + len(model.electrical_devices) + len(model.electrical_equipment)
        + len(model.routes) + len(model.electrical_boxes) + len(model.openings)
    )
    assert len(resolution.visible_ids) == drawable
    assert len({
        *resolution.visible_ids, *resolution.hidden_ids, *resolution.ghosted_ids,
    }) == drawable + len(model.ceilings)
    assert set(resolution.out_of_scope_member_ids) == {selection_id}
    assert dict(resolution.layer_coverage)["layer:demo"] == 4


# ---------------------------------------------------------------- 1.3 shared supports


def _supports_model() -> BuildingModel:
    base = upgrade_to_v1_3(BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json"))
    basis = RuleReference(authority="synthetic-company", revision="1", rule_id="strut-a")
    assembly_id = "support-assembly:strut"
    return replace(
        base,
        support_assemblies=(
            SupportAssembly(
                id=assembly_id, intent_id="intent:strut", assembly_type="strut",
                level_id="level:ground", supported_object_ids=("device:evse",),
                component_ids=("support-component:strut",),
                interface_ids=("support-interface:base",), basis=basis,
            ),
        ),
        support_components=(
            SupportComponent(
                id="support-component:strut", assembly_id=assembly_id,
                part_key="part:strut", level_id="level:ground", category="support",
                component_type="strut",
                product=CatalogProduct(
                    catalog_id="synthetic", catalog_revision="1", product_id="strut",
                ),
                pose=Pose(position=Point3(x=5.2, y=0.0, z=2.0)),
                size=Size3(x=0.1, y=0.1, z=0.6),
            ),
        ),
        support_interfaces=(
            SupportInterface(
                id="support-interface:base", assembly_id=assembly_id,
                interface_key="interface:base", level_id="level:ground",
                host_id="wall:south", substrate="steel",
                pose=Pose(position=Point3(x=5.2, y=0.0, z=2.0)),
                component_ids=("support-component:strut",),
            ),
        ),
        support_selection_outcomes=(
            SupportSelectionOutcome(
                id="support-outcome:strut", intent_id="intent:strut",
                supported_object_ids=("device:evse",), status="selected",
                selected_assembly_id=assembly_id, requested_level_id="level:ground",
                requested_basis=basis,
            ),
        ),
    )


def _supports_config() -> LayerSelectionConfig:
    return LayerSelectionConfig.from_dict({
        "schema_version": "1.0.0",
        "layers": [
            {
                "id": "layer:electrical",
                "name": "Electrical",
                "member_ids": ["device:evse", "equip:panel", "route:panel-evse"],
            },
            {
                "id": "layer:supports",
                "name": "Supports",
                "member_ids": [
                    "support-assembly:strut",
                    "support-component:strut",
                    "support-interface:base",
                ],
            },
            {
                "id": "layer:arch",
                "name": "Architecture",
                "member_ids": [f"wall:{name}" for name in ("south", "east", "north", "west")]
                + ["space:garage", "slab:garage", "ceiling:garage", "opening:door"],
            },
        ],
        "presets": [
            {
                "id": "preset:all",
                "name": "All",
                "visible_layer_ids": ["layer:electrical", "layer:supports", "layer:arch"],
            },
            {
                "id": "preset:no-supports",
                "name": "No supports",
                "visible_layer_ids": ["layer:electrical", "layer:arch"],
                "hidden_layer_ids": ["layer:supports"],
            },
            {
                "id": "preset:ghost-supports",
                "name": "Ghost supports",
                "visible_layer_ids": ["layer:electrical", "layer:arch"],
                "ghosted_layer_ids": ["layer:supports"],
            },
        ],
    })


def test_support_records_join_the_universe_once_and_group_as_a_tree(tmp_path: Path) -> None:
    model = _supports_model()
    resolution = resolve_preset(
        _supports_config(), model, preset="preset:all", unassigned="hidden",
    )
    drawable = (
        len(model.walls) + len(model.slabs) + len(model.spaces)
        + len(model.electrical_devices) + len(model.electrical_equipment)
        + len(model.routes) + len(model.electrical_boxes)
        + len(model.support_components) + len(model.support_interfaces)
        + len(model.support_assemblies) + len(model.openings) + len(model.ceilings)
    )
    assert len(resolution.visible_ids) == drawable
    assert {
        "support-assembly:strut", "support-component:strut", "support-interface:base",
    } <= set(resolution.visible_ids)

    plan = layer_glb_plan(model, resolution)
    target = tmp_path / "supports.glb"
    summary = to_glb(
        model, target, groups=plan.groups, hidden_groups=plan.hidden_groups,
    )
    gltf = _parse_glb(target)["gltf"]
    names = [node["name"] for node in gltf["nodes"]]
    # Each support record is exported exactly once.
    for support_id in ("support-assembly:strut", "support-component:strut",
                       "support-interface:base"):
        assert names.count(support_id) == 1
    # The scene is a tree: no node index is a child of two parents. When the
    # caller group takes the assembly and its members, the exporter keeps the
    # scene a tree by moving every node under the group exactly once; the
    # assembly's logical membership stays in its canonical extras.
    parent_of: dict[int, int] = {}
    for index, node in enumerate(gltf["nodes"]):
        for child in node.get("children", ()):
            assert child not in parent_of, f"node {child} has two parents"
            parent_of[child] = index
    group_index = names.index("group:layer:preset:all:visible")
    grouped = {names[i] for i in gltf["nodes"][group_index]["children"]}
    assert {
        "support-assembly:strut", "support-component:strut", "support-interface:base",
    } <= grouped
    assembly_extras = gltf["nodes"][names.index("support-assembly:strut")]["extras"]
    assert assembly_extras["component_ids"] == ["support-component:strut"]
    assert assembly_extras["interface_ids"] == ["support-interface:base"]
    assert summary["unmatched_group_ids"] == 0


def test_hidden_support_group_hides_the_whole_assembly_subtree(tmp_path: Path) -> None:
    model = _supports_model()
    resolution = resolve_preset(
        _supports_config(), model, preset="preset:no-supports", unassigned="hidden",
    )
    assert set(resolution.hidden_ids) == {
        "support-assembly:strut", "support-component:strut", "support-interface:base",
    }
    plan = layer_glb_plan(model, resolution)
    target = tmp_path / "no-supports.glb"
    to_glb(model, target, groups=plan.groups, hidden_groups=plan.hidden_groups)
    gltf = _parse_glb(target)["gltf"]
    names = [node["name"] for node in gltf["nodes"]]
    hidden_index = names.index("group:layer:preset:no-supports:hidden")
    hidden_children = {
        names[i] for i in gltf["nodes"][hidden_index].get("children", ())
    }
    assert hidden_children == {
        "support-assembly:strut", "support-component:strut", "support-interface:base",
    }
    assert gltf["extensionsUsed"] == ["KHR_node_visibility"]
    assert "extensionsRequired" not in gltf


def test_support_ghosts_are_supported(tmp_path: Path) -> None:
    model = _supports_model()
    resolution = resolve_preset(
        _supports_config(), model, preset="preset:ghost-supports", unassigned="hidden",
    )
    plan = layer_glb_plan(model, resolution)
    assert plan.unsupported_ghost_ids == ()


def test_mixed_architecture_and_support_ghosts_are_honest_in_actual_glb(
    tmp_path: Path,
) -> None:
    model = _supports_model()
    document = json.loads(_supports_config().to_json())
    document["presets"][2]["visible_layer_ids"] = ["layer:electrical"]
    document["presets"][2]["ghosted_layer_ids"] = ["layer:arch", "layer:supports"]
    config = LayerSelectionConfig.from_dict(document)
    resolution = resolve_preset(
        config, model, preset="preset:ghost-supports", unassigned="hidden",
    )
    plan = layer_glb_plan(model, resolution)

    architectural_ids = {
        "wall:south", "wall:east", "wall:north", "wall:west",
        "slab:garage", "space:garage", "ceiling:garage", "opening:door",
    }
    unsupported_support_ids = {
        "support-assembly:strut", "support-component:strut", "support-interface:base",
    }
    assert set(plan.dimmed_ids) == architectural_ids | unsupported_support_ids
    assert plan.unsupported_ghost_ids == ()

    result = _export(tmp_path, model, plan, "mixed-ghost-supports.glb")
    gltf, summary = result["gltf"], result["summary"]
    assert summary["dimmed"] == len(architectural_ids) - 2 + 1
    nodes = {node["name"]: node for node in gltf["nodes"]}
    for node_name in architectural_ids - {"ceiling:garage", "opening:door"}:
        material = _node_material(gltf, node_name)
        assert material["alphaMode"] == "BLEND"
        assert material["pbrMetallicRoughness"]["baseColorFactor"][3] == pytest.approx(0.3)
        assert nodes[node_name]["extras"]["display"] == "dimmed (caller-supplied)"
    material = _node_material(gltf, "support-component:strut")
    assert material["alphaMode"] == "BLEND"
    assert material["pbrMetallicRoughness"]["baseColorFactor"][3] == pytest.approx(.3)
    for node_name in {"support-assembly:strut", "support-interface:base"}:
        assert "mesh" not in nodes[node_name]


def test_support_selection_outcomes_are_out_of_scope_members() -> None:
    model = _supports_model()
    document = json.loads(_supports_config().to_json())
    document["layers"][0]["member_ids"].append("support-outcome:strut")
    config = LayerSelectionConfig.from_dict(document)
    resolution = resolve_preset(
        config, model, preset="preset:all", unassigned="hidden",
    )
    assert "support-outcome:strut" in resolution.out_of_scope_member_ids


# ---------------------------------------------------------------- dimmed route wires


def _glb_materials(path: Path) -> dict[str, tuple[str, float]]:
    data = path.read_bytes()
    json_length, _ = struct.unpack_from("<II", data, 12)
    gltf = json.loads(data[20:20 + json_length])
    by_name: dict[str, tuple[str, float]] = {}
    for node in gltf["nodes"]:
        if "mesh" not in node:
            continue
        primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
        material = gltf["materials"][primitive["material"]]
        by_name[node["name"]] = (
            material["name"],
            material["pbrMetallicRoughness"]["baseColorFactor"][3],
        )
    return by_name


def test_dimmed_route_ghosts_its_conductor_wires(tmp_path: Path) -> None:
    """The supervisor's reproduced defect: ghosting a route must ghost the
    three conductor wires drawn inside it, not leave them at alpha 1.0."""

    model = BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json")
    wire_names = (
        "conductor:l1#route:panel-evse#0",
        "conductor:l2#route:panel-evse#0",
        "conductor:egc#route:panel-evse#0",
    )
    plain = tmp_path / "plain.glb"
    plain_summary = to_glb(model, plain)
    materials = _glb_materials(plain)
    assert materials["route:panel-evse"] == ("route", 1.0)
    assert "dimmed_conductor_wires" not in plain_summary
    for name in wire_names:
        assert materials[name][1] == 1.0

    dimmed = tmp_path / "dimmed.glb"
    summary = to_glb(model, dimmed, dimmed_ids=("route:panel-evse",))
    materials = _glb_materials(dimmed)
    assert materials["route:panel-evse"] == ("route-dimmed", 0.3)
    for name, material_name in (
        (wire_names[0], "wire-phase-1-dimmed"),
        (wire_names[1], "wire-phase-2-dimmed"),
        (wire_names[2], "wire-ground-dimmed"),
    ):
        assert materials[name] == (material_name, 0.3)
    assert summary["dimmed"] == 1
    assert summary["dimmed_conductor_wires"] == 3


def test_undimmed_route_wires_stay_opaque_next_to_a_dimmed_route(tmp_path: Path) -> None:
    model = _two_level_model()
    plain = tmp_path / "plain.glb"
    to_glb(model, plain)
    dimmed = tmp_path / "dimmed.glb"
    to_glb(model, dimmed, dimmed_ids=("route:one",))
    plain_materials = _glb_materials(plain)
    dimmed_materials = _glb_materials(dimmed)
    assert dimmed_materials["route:one"][1] == 0.3
    assert dimmed_materials["route:two"] == plain_materials["route:two"]
    assert dimmed_materials["route:two"][1] == 1.0


# ---------------------------------------------------------------- GLB adapter


def _by_name(parsed: dict) -> dict[str, int]:
    return {node["name"]: index for index, node in enumerate(parsed["gltf"]["nodes"])}


def _parse_glb(path: Path) -> dict:
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<III", data, 0)
    assert magic == _GLB_MAGIC and version == 2 and total == len(data)
    json_length, json_type = struct.unpack_from("<II", data, 12)
    assert json_type == _CHUNK_JSON
    return {"gltf": json.loads(data[20:20 + json_length])}


def _node_material(gltf: dict, node_name: str) -> dict:
    by_name = {node["name"]: index for index, node in enumerate(gltf["nodes"])}
    node = gltf["nodes"][by_name[node_name]]
    primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
    return gltf["materials"][primitive["material"]]


def _export(tmp_path: Path, model: BuildingModel, plan, name: str) -> dict:
    summary = to_glb(
        model, tmp_path / name,
        groups=plan.groups,
        hidden_groups=plan.hidden_groups,
        dimmed_ids=plan.dimmed_ids,
    )
    return {"gltf": _parse_glb(tmp_path / name)["gltf"], "summary": summary}


def test_architectural_ghosts_render_in_actual_glb_without_changing_facts(
    tmp_path: Path,
) -> None:
    model = _two_level_model()
    inferred_wall = replace(
        model.walls[0],
        provenance=(Provenance(
            source_kind="synthetic", source_id="fixture:wall-ghost",
            derivation="inferred",
        ),),
    )
    model = replace(model, walls=(inferred_wall, *model.walls[1:]))
    before_model = model.to_json()
    from oabm.quantities import extract_quantities

    before_quantities = json.dumps(asdict(extract_quantities(model)), sort_keys=True)
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
    )
    plan = layer_glb_plan(model, resolution)
    assert plan.unsupported_ghost_ids == ()

    color = (0.44, 0.46, 0.48)
    options = {
        "groups": plan.groups,
        "hidden_groups": plan.hidden_groups,
        "dimmed_ids": plan.dimmed_ids,
        "dimmed_alpha": 0.2,
        "dimmed_color": color,
    }
    target = tmp_path / "architectural-ghosts.glb"
    summary = to_glb(model, target, **options)
    gltf = _parse_glb(target)["gltf"]
    repeat = tmp_path / "architectural-ghosts-repeat.glb"
    to_glb(model, repeat, **options)
    assert target.read_bytes() == repeat.read_bytes()
    assert summary["dimmed"] == 6
    expected_materials = {
        "wall:one-a": "wall-dimmed",
        "wall:two-a": "wall-dimmed",
        "wall:one-glass": "wall-glazed-dimmed",
        "slab:one": "slab-dimmed",
        "slab:two": "slab-dimmed",
        "space:one": "space-dimmed",
    }
    by_name = {node["name"]: node for node in gltf["nodes"]}
    for node_name, material_name in expected_materials.items():
        material = _node_material(gltf, node_name)
        assert material["name"] == material_name
        assert material["alphaMode"] == "BLEND"
        expected_color = (0.70, 0.82, 0.88) if node_name == "wall:one-glass" else color
        assert material["pbrMetallicRoughness"]["baseColorFactor"] == pytest.approx(
            [*expected_color, 0.2],
        )
        assert by_name[node_name]["extras"]["display"] == "dimmed (caller-supplied)"
    # Dimming changes the derived display only; recorded provenance stays present.
    assert by_name["wall:one-a"]["extras"]["derivation"] == "inferred"
    assert model.to_json() == before_model
    assert json.dumps(asdict(extract_quantities(model)), sort_keys=True) == before_quantities


def test_hidden_wins_over_ghosted_overlap_in_actual_glb(tmp_path: Path) -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:ghost-power", unassigned=UNASSIGNED_VISIBLE,
    )
    assert {"device:one-recp", "route:one"} <= set(resolution.hidden_ids)
    plan = layer_glb_plan(model, resolution)
    result = _export(tmp_path, model, plan, "hidden-over-ghosted.glb")
    gltf = result["gltf"]
    by_name = {node["name"]: index for index, node in enumerate(gltf["nodes"])}
    hidden_group = gltf["nodes"][by_name["group:layer:preset:ghost-power:hidden"]]
    assert hidden_group["extensions"] == {"KHR_node_visibility": {"visible": False}}
    hidden_names = {gltf["nodes"][index]["name"] for index in hidden_group["children"]}
    assert {"device:one-recp", "route:one"} <= hidden_names
    # These overlapping power members are hidden, not dimmed by the other layer.
    assert "display" not in gltf["nodes"][by_name["device:one-recp"]]["extras"]
    assert "display" not in gltf["nodes"][by_name["route:one"]]["extras"]
    assert _node_material(gltf, "device:one-recp")["name"] == "outlet"
    assert _node_material(gltf, "route:one")["name"] == "route"


def test_hidden_state_lands_as_a_hidden_group_in_actual_glb_output(tmp_path: Path) -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
    )
    plan = layer_glb_plan(model, resolution)
    assert plan.hidden_groups == {"layer:preset:phase-1:hidden"}
    assert plan.dimmed_ids == frozenset(resolution.ghosted_ids)
    # Architectural walls, slabs and the space now have visible ghost styles.
    assert plan.unsupported_ghost_ids == ()
    result = _export(tmp_path, model, plan, "phased.glb")
    gltf, summary = result["gltf"], result["summary"]
    by_name = _by_name(result)
    assert gltf["extensionsUsed"] == ["KHR_node_visibility"]
    assert "extensionsRequired" not in gltf
    hidden_node = gltf["nodes"][by_name["group:layer:preset:phase-1:hidden"]]
    assert hidden_node["extras"]["hidden_by_default"] is True
    assert hidden_node["extensions"] == {"KHR_node_visibility": {"visible": False}}
    hidden_children = {gltf["nodes"][i]["name"] for i in hidden_node["children"]}
    assert hidden_children == {"device:one-em"}
    assert summary["groups"] == {
        "layer:preset:phase-1:visible": 5,
        "layer:preset:phase-1:ghosted": 6,
        "layer:preset:phase-1:hidden": 1,
    }
    assert summary["unmatched_group_ids"] == 0
    assert summary["dimmed"] == 6


def test_supported_ghosts_dim_and_unsupported_are_disclosed(tmp_path: Path) -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:ghost-power", unassigned=UNASSIGNED_VISIBLE,
    )
    plan = layer_glb_plan(model, resolution)
    # Devices, equipment and routes all have a real ghost style, wires included.
    assert plan.unsupported_ghost_ids == ()
    result = _export(tmp_path, model, plan, "ghosted.glb")
    assert result["summary"]["dimmed"] == 3
    # The key is absent while no wire is dimmed, like the other option keys.
    assert "dimmed_conductor_wires" not in result["summary"]
    assert result["summary"]["emphasized"] == 0


def test_isolate_export_hides_everything_outside_the_selection(tmp_path: Path) -> None:
    model = _two_level_model()
    resolution = resolve_preset(
        _demo_config(), model, preset="preset:all",
        isolate=("layer:power",), unassigned=UNASSIGNED_VISIBLE,
    )
    plan = layer_glb_plan(model, resolution)
    result = _export(tmp_path, model, plan, "isolated.glb")
    gltf = result["gltf"]
    by_name = _by_name(result)
    hidden_children = set()
    for name in plan.hidden_groups:
        hidden_children.update(gltf["nodes"][by_name[f"group:{name}"]]["children"])
    hidden_names = {gltf["nodes"][i]["name"] for i in hidden_children}
    assert hidden_names == {
        "device:one-em", "wall:one-a", "wall:one-glass", "wall:two-a",
        "slab:one", "slab:two", "space:one",
    }


def test_plan_exports_are_byte_deterministic_across_permutations(tmp_path: Path) -> None:
    model = _two_level_model()
    config = _demo_config()
    document = json.loads(config.to_json())
    document["layers"] = list(reversed(document["layers"]))
    for layer in document["layers"]:
        layer["member_ids"] = list(reversed(layer["member_ids"]))
    permuted = LayerSelectionConfig.from_dict(document)
    payloads = []
    for index, source in enumerate((config, permuted)):
        resolution = resolve_preset(
            source, model, preset="preset:phase-1", unassigned=UNASSIGNED_VISIBLE,
        )
        plan = layer_glb_plan(model, resolution)
        target = tmp_path / f"run-{index}.glb"
        to_glb(
            model, target,
            groups=plan.groups,
            hidden_groups=plan.hidden_groups,
            dimmed_ids=plan.dimmed_ids,
        )
        payloads.append(target.read_bytes())
    assert payloads[0] == payloads[1]


def test_empty_plan_keeps_default_export_bytes(tmp_path: Path) -> None:
    model = _two_level_model()
    plain = tmp_path / "plain.glb"
    to_glb(model, plain)
    planned = tmp_path / "planned.glb"
    to_glb(model, planned, groups={}, hidden_groups=(), dimmed_ids=())
    assert planned.read_bytes() == plain.read_bytes()


def test_default_export_byte_fingerprint_is_unchanged(tmp_path: Path) -> None:
    model = _two_level_model()
    target = tmp_path / "default-fingerprint.glb"
    to_glb(model, target)
    payload = target.read_bytes()
    assert len(payload) == 20_132
    assert hashlib.sha256(payload).hexdigest() == (
        "99fe288dff2c7454bf2034dc90414d1ad89ddf11e5ebd659cc57f8fc20ffa755"
    )
