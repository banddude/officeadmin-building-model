"""Caller-supplied group splitting for takeoff quantities.

Synthetic in-code model: one base receptacle with its conduit run and a hot
conductor, one alternate-scope receptacle with its own run and hot conductor,
a shared equipment-ground conductor over both runs, and one elbow per run.
The caller names group membership; quantities only split, never merge a
group into the base.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from oabm.model import (
    BuildingModel,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    RouteFitting,
    Vector3,
)
from oabm.quantities import extract_quantities

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "quantities" / "synthetic-route-model.json"

GROUP_MEMBERS = ("device:rec-alt", "route:alt-run", "cond:alt-hot")
GROUPS = {"ALTERNATES": GROUP_MEMBERS}


def _pose(x: float, y: float, z: float) -> Pose:
    return Pose(position=Point3(x=x, y=y, z=z))


def _model() -> BuildingModel:
    provenance = (
        Provenance(
            source_kind="synthetic",
            source_id="fixture:quantity-groups-v1",
            method="hand-authored quantity-groups test fixture",
        ),
    )
    panel = ElectricalEquipment(
        id="equip:panel",
        name="Panel",
        equipment_type="panelboard",
        pose=_pose(0, 0, 0),
        provenance=provenance,
        rated_voltage_v=240,
    )
    base_rec = ElectricalDevice(
        id="device:rec-base",
        name="Receptacle base",
        device_type="duplex-receptacle",
        pose=_pose(4, 0, 2),
        provenance=provenance,
    )
    alt_rec = ElectricalDevice(
        id="device:rec-alt",
        name="Receptacle alternate",
        device_type="duplex-receptacle",
        pose=_pose(7, 0, 3),
        provenance=provenance,
    )
    ports = (
        Port(
            id="port:panel-out",
            owner_id="equip:panel",
            domain="power",
            role="source",
            pose=_pose(0, 0, 1),
            direction=Vector3(x=1, y=0, z=0),
            nominal_diameter_m=0.021,
            provenance=provenance,
        ),
        Port(
            id="port:rec-base-in",
            owner_id="device:rec-base",
            domain="power",
            role="sink",
            pose=_pose(4, 0, 2),
            direction=Vector3(x=0, y=0, z=1),
            nominal_diameter_m=0.021,
            provenance=provenance,
        ),
        Port(
            id="port:rec-alt-in",
            owner_id="device:rec-alt",
            domain="power",
            role="sink",
            pose=_pose(7, 0, 3),
            direction=Vector3(x=0, y=0, z=1),
            nominal_diameter_m=0.021,
            provenance=provenance,
        ),
    )
    base_run = Route(
        id="route:base-run",
        route_type="emt",
        nominal_diameter_m=0.021,
        start_port_id="port:panel-out",
        end_port_id="port:rec-base-in",
        centerline=Polyline3D(
            points=(
                Point3(x=0, y=0, z=1),
                Point3(x=4, y=0, z=1),
                Point3(x=4, y=0, z=2),
            )
        ),
        fitting_ids=("fit:base-elbow",),
        provenance=provenance,
    )
    alt_run = Route(
        id="route:alt-run",
        route_type="emt",
        nominal_diameter_m=0.021,
        start_port_id="port:panel-out",
        end_port_id="port:rec-alt-in",
        centerline=Polyline3D(
            points=(
                Point3(x=0, y=0, z=1),
                Point3(x=7, y=0, z=1),
                Point3(x=7, y=0, z=3),
            )
        ),
        fitting_ids=("fit:alt-elbow",),
        provenance=provenance,
    )
    fittings = (
        RouteFitting(
            id="fit:base-elbow",
            route_id="route:base-run",
            fitting_type="elbow",
            pose=_pose(4, 0, 1),
            nominal_diameter_m=0.021,
            angle_radians=1.5707963267948966,
            provenance=provenance,
        ),
        RouteFitting(
            id="fit:alt-elbow",
            route_id="route:alt-run",
            fitting_type="elbow",
            pose=_pose(7, 0, 1),
            nominal_diameter_m=0.021,
            angle_radians=1.5707963267948966,
            provenance=provenance,
        ),
    )
    circuit = Circuit(
        id="circuit:recs",
        source_port_id="port:panel-out",
        load_port_ids=("port:rec-base-in", "port:rec-alt-in"),
        voltage_v=120,
        poles=1,
        provenance=provenance,
    )
    conductors = (
        Conductor(
            id="cond:base-hot",
            circuit_id="circuit:recs",
            role="hot",
            material="copper",
            size="#12 AWG",
            insulation="THHN",
            route_ids=("route:base-run",),
            provenance=provenance,
        ),
        Conductor(
            id="cond:alt-hot",
            circuit_id="circuit:recs",
            role="hot",
            material="copper",
            size="#10 AWG",
            insulation="THHN",
            route_ids=("route:alt-run",),
            provenance=provenance,
        ),
        Conductor(
            id="cond:shared-ground",
            circuit_id="circuit:recs",
            role="equipment-ground",
            material="copper",
            size="#12 AWG",
            insulation="THHN",
            route_ids=("route:base-run", "route:alt-run"),
            provenance=provenance,
        ),
    )
    return BuildingModel(
        model_id="model:quantity-groups-v1",
        name="Synthetic quantity-groups fixture",
        electrical_equipment=(panel,),
        electrical_devices=(base_rec, alt_rec),
        ports=ports,
        routes=(base_run, alt_run),
        route_fittings=fittings,
        circuits=(circuit,),
        conductors=conductors,
        provenance=provenance,
    )


def test_default_output_stays_plain() -> None:
    model = BuildingModel.load(FIXTURE)
    plain = extract_quantities(model)
    for supplied in (None, {}):
        assert (
            extract_quantities(model, groups=supplied).to_json(indent=None)
            == plain.to_json(indent=None)
        )
    payload = plain.to_dict()
    assert "groups" not in payload
    assert "unmatched_group_ids" not in payload
    assert all(item.group is None for item in plain.items)
    assert all("group" not in item.to_dict() for item in plain.items)


def test_group_splits_base_and_alternate_lines() -> None:
    report = extract_quantities(_model(), groups=GROUPS)

    receptacles = [
        item
        for item in report.items
        if item.category == "device" and item.item_type == "duplex-receptacle"
    ]
    assert [(item.group, item.quantity) for item in receptacles] == [
        (None, 1.0),
        ("ALTERNATES", 1.0),
    ]

    runs = [item for item in report.items if item.category == "route_length"]
    assert [(item.group, item.quantity) for item in runs] == [
        (None, 5.0),
        ("ALTERNATES", 9.0),
    ]

    hots = [
        item
        for item in report.items
        if item.category == "conductor_length" and item.item_type == "hot"
    ]
    assert [(item.group, item.quantity) for item in hots] == [
        (None, 5.0),
        ("ALTERNATES", 9.0),
    ]

    payload = report.to_dict()
    assert payload["groups"] == {"ALTERNATES": {"entities": 3, "items": 5}}
    assert payload["unmatched_group_ids"] == 0
    # "group" appears exactly on the grouped lines and nowhere else.
    assert all(
        ("group" in item.to_dict()) == (item.group is not None)
        for item in report.items
    )


def test_base_lines_come_first_then_groups_sorted_by_name() -> None:
    report = extract_quantities(
        _model(),
        groups={"ALTERNATES": GROUP_MEMBERS, "BONUS": ("equip:panel",)},
    )
    sequence = [item.group for item in report.items]
    assert sequence == sorted(sequence, key=lambda g: (g is not None, g or ""))
    assert sequence[-1] == "BONUS"
    assert sequence.count(None) == 5
    assert sequence.count("ALTERNATES") == 5
    assert sequence.count("BONUS") == 1


def test_fitting_and_unlisted_conductor_inherit_route_group() -> None:
    report = extract_quantities(_model(), groups=GROUPS)

    elbows = [item for item in report.items if item.category == "fitting"]
    assert [(item.group, item.quantity) for item in elbows] == [
        (None, 1.0),
        ("ALTERNATES", 1.0),
    ]

    # The shared ground conductor is named by no group: it splits per route,
    # 5 m over the base run and 9 m over the alternate run.
    grounds = [item for item in report.items if item.item_type == "equipment-ground"]
    assert [(item.group, item.quantity) for item in grounds] == [
        (None, 5.0),
        ("ALTERNATES", 9.0),
    ]


def test_conductor_membership_beats_route_group() -> None:
    # cond:base-hot is named by the caller even though its route is base, so
    # its whole contribution moves; the unnamed alternate hot inherits its
    # route's group.  Different conductor sizes keep the two lines separate.
    report = extract_quantities(
        _model(),
        groups={"ALTERNATES": ("device:rec-alt", "route:alt-run", "cond:base-hot")},
    )
    hots = [
        item
        for item in report.items
        if item.category == "conductor_length" and item.item_type == "hot"
    ]
    assert [
        (dict(item.variant)["size"], item.group, item.quantity) for item in hots
    ] == [
        ("#10 AWG", "ALTERNATES", 9.0),
        ("#12 AWG", "ALTERNATES", 5.0),
    ]


def test_fitting_membership_beats_route_group() -> None:
    report = extract_quantities(
        _model(),
        groups={"ALTERNATES": ("device:rec-alt", "route:alt-run"), "EXTRAS": ("fit:alt-elbow",)},
    )
    elbows = [item for item in report.items if item.category == "fitting"]
    assert [(item.group, item.quantity) for item in elbows] == [
        (None, 1.0),
        ("EXTRAS", 1.0),
    ]


def test_id_in_two_groups_is_rejected() -> None:
    with pytest.raises(ValueError, match="both group"):
        extract_quantities(
            _model(),
            groups={"ALTERNATES": ("device:rec-alt",), "OPTION": ("device:rec-alt",)},
        )


def test_repeated_ids_within_one_group_collapse() -> None:
    report = extract_quantities(
        _model(),
        groups={"ALTERNATES": ("device:rec-alt", "device:rec-alt", "route:alt-run")},
    )
    payload = report.to_dict()
    assert payload["unmatched_group_ids"] == 0
    assert payload["groups"]["ALTERNATES"]["entities"] == 2
    assert payload["groups"]["ALTERNATES"]["items"] == 5


def test_unmatched_group_ids_are_ignored_and_counted() -> None:
    report = extract_quantities(
        _model(),
        groups={
            "ALTERNATES": (
                "device:rec-alt",
                "route:alt-run",
                "wall:not-modeled",
                "device:also-not-modeled",
            )
        },
    )
    payload = report.to_dict()
    assert payload["unmatched_group_ids"] == 2
    assert payload["groups"]["ALTERNATES"]["entities"] == 2


def test_group_names_must_be_non_empty_strings() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        extract_quantities(_model(), groups={"": ("device:rec-alt",)})
    with pytest.raises(ValueError, match="non-empty"):
        extract_quantities(_model(), groups={3: ("device:rec-alt",)})


def test_grouped_takeoff_is_deterministic() -> None:
    groups = {"ALTERNATES": GROUP_MEMBERS, "BONUS": ("equip:panel",)}
    model = _model()
    first = extract_quantities(model, groups=groups).to_json(indent=None)
    second = extract_quantities(model, groups=groups).to_json(indent=None)
    assert first == second

    shuffled = replace(
        model,
        electrical_equipment=tuple(reversed(model.electrical_equipment)),
        electrical_devices=tuple(reversed(model.electrical_devices)),
        ports=tuple(reversed(model.ports)),
        routes=tuple(reversed(model.routes)),
        route_fittings=tuple(reversed(model.route_fittings)),
        conductors=tuple(reversed(model.conductors)),
    )
    assert extract_quantities(shuffled, groups=groups).to_json(indent=None) == first

    reordered = extract_quantities(
        model,
        groups={"BONUS": ("equip:panel",), "ALTERNATES": GROUP_MEMBERS},
    ).to_json(indent=None)
    assert reordered == first
