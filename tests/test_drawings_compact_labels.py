"""Opt-in compact label provider tests (issue #207).

The default label provider must stay unchanged; the compact provider swaps
long legend-description names for tags or short type codes.
"""

from __future__ import annotations

import pytest

from oabm.drawings.generator import compact_label_provider, default_label_provider
from oabm.model import (
    ElectricalDevice,
    ElectricalEquipment,
    Opening,
    Point3,
    Polygon3D,
    Pose,
    Polyline3D,
    Size3,
    Space,
    Wall,
)

LONG_DESCRIPTION = "SAMPLE DESCRIPTION OF A WALL RECEPTACLE, SYNTHETIC"


def _device(
    device_id: str,
    *,
    device_type: str,
    name: str | None = None,
    attributes: dict | None = None,
) -> ElectricalDevice:
    return ElectricalDevice(
        id=device_id,
        name=name,
        device_type=device_type,
        pose=Pose(position=Point3(x=1.0, y=1.0, z=1.2)),
        level_id="level:ground",
        attributes=attributes or {},
    )


def _equipment(equipment_id: str, *, equipment_type: str) -> ElectricalEquipment:
    return ElectricalEquipment(
        id=equipment_id,
        equipment_type=equipment_type,
        pose=Pose(position=Point3(x=2.0, y=1.0, z=1.8)),
        level_id="level:ground",
    )


def _wall(wall_id: str, *, name: str | None = None) -> Wall:
    return Wall(
        id=wall_id,
        name=name,
        level_id="level:ground",
        centerline=Polyline3D(points=(Point3(x=0.0, y=0.0, z=0.0), Point3(x=3.0, y=0.0, z=0.0))),
        thickness_m=0.1,
        height_m=2.7,
    )


def _space(space_id: str, *, name: str | None = None) -> Space:
    return Space(
        id=space_id,
        name=name,
        level_id="level:ground",
        footprint=Polygon3D(
            points=(
                Point3(x=0.0, y=0.0, z=0.0),
                Point3(x=4.0, y=0.0, z=0.0),
                Point3(x=4.0, y=3.0, z=0.0),
            )
        ),
    )


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [
        ("receptacle_duplex", "DUP"),
        ("receptacle_quad", "QUAD"),
        ("data_outlet", "DATA"),
        ("luminaire", "LT"),
        ("exit_sign", "EXIT"),
        ("junction_box_power", "JB"),
        ("floor_box", "FB"),
        ("switch", "S"),
        ("smoke_detector", "SD"),
        ("duct_smoke_detector", "DSD"),
        ("panelboard", "PNL"),
    ],
)
def test_documented_abbreviations(device_type: str, expected: str) -> None:
    assert compact_label_provider(_device(f"d:{device_type}", device_type=device_type), "plan") == expected


def test_tagged_long_name_device_labels_with_tag() -> None:
    device = _device(
        "d:tagged",
        device_type="receptacle_duplex",
        name=LONG_DESCRIPTION,
        attributes={"tag": "LF-3"},
    )
    assert default_label_provider(device, "plan") == LONG_DESCRIPTION
    assert compact_label_provider(device, "plan") == "LF-3"


def test_nested_pdf_electrical_tag_is_used() -> None:
    device = _device(
        "d:nested",
        device_type="receptacle_duplex",
        name=LONG_DESCRIPTION,
        attributes={"pdf_electrical": {"tag": "RX-12"}},
    )
    assert compact_label_provider(device, "plan") == "RX-12"


def test_top_level_tag_wins_over_nested() -> None:
    device = _device(
        "d:both",
        device_type="receptacle_duplex",
        attributes={"tag": "A1", "pdf_electrical": {"tag": "B2"}},
    )
    assert compact_label_provider(device, "plan") == "A1"


def test_untagged_duplex_abbreviates() -> None:
    device = _device("d:plain", device_type="receptacle_duplex", name=LONG_DESCRIPTION)
    assert compact_label_provider(device, "plan") == "DUP"


def test_unknown_type_falls_back_to_truncated_uppercase() -> None:
    long_type = _device("d:unknown", device_type="temperature_sensor")
    assert compact_label_provider(long_type, "plan") == "TEMPERAT"
    short_type = _device("d:bell", device_type="bell")
    assert compact_label_provider(short_type, "plan") == "BELL"


def test_overlong_tag_is_not_short_so_type_wins() -> None:
    device = _device(
        "d:long-tag",
        device_type="receptacle_duplex",
        attributes={"tag": "SIXTEEN-CHARS"},
    )
    assert compact_label_provider(device, "plan") == "DUP"


def test_equipment_uses_equipment_type() -> None:
    panel = _equipment("e:panel", equipment_type="panelboard")
    assert compact_label_provider(panel, "plan") == "PNL"
    odd = _equipment("e:odd", equipment_type="motor_controller")
    assert compact_label_provider(odd, "plan") == "MOTOR_CO"


def test_wall_gets_no_label_even_when_named() -> None:
    wall = _wall("wall:a", name="SAMPLE PARTITION WALL DESCRIPTION, SYNTHETIC")
    assert compact_label_provider(wall, "plan") is None
    assert default_label_provider(wall, "plan") == "SAMPLE PARTITION WALL DESCRIPTION, SYNTHETIC"


def test_named_space_labels_with_its_name() -> None:
    space = _space("space:suite-200", name="OFFICE (SYNTHETIC)")
    assert default_label_provider(space, "plan") == "OFFICE (SYNTHETIC)"
    assert compact_label_provider(space, "plan") == "OFFICE (SYNTHETIC)"
    assert compact_label_provider(space, "plan") == default_label_provider(space, "plan")


def test_unnamed_space_falls_back_to_id_like_default() -> None:
    space = _space("space:office-100")
    assert default_label_provider(space, "plan") == "space:office-100"
    assert compact_label_provider(space, "plan") == "space:office-100"
    assert compact_label_provider(space, "plan") == default_label_provider(space, "plan")


def test_other_entities_follow_the_default_provider() -> None:
    named_opening = Opening(
        id="opening:a",
        name="SAMPLE OPENING DESCRIPTION, SYNTHETIC",
        host_id="wall:a",
        opening_type="door",
        pose=Pose(position=Point3(x=1.0, y=0.0, z=1.05)),
        size=Size3(x=0.9, y=0.12, z=2.1),
    )
    assert compact_label_provider(named_opening, "plan") == "SAMPLE OPENING DESCRIPTION, SYNTHETIC"
    assert compact_label_provider(named_opening, "plan") == default_label_provider(named_opening, "plan")
    unnamed = Opening(
        id="opening:b",
        host_id="wall:a",
        opening_type="window",
        pose=Pose(position=Point3(x=2.0, y=0.0, z=1.05)),
        size=Size3(x=1.2, y=0.12, z=1.4),
    )
    assert compact_label_provider(unnamed, "plan") is None


def test_provider_is_deterministic() -> None:
    device = _device(
        "d:again",
        device_type="receptacle_duplex",
        name=LONG_DESCRIPTION,
        attributes={"tag": "LF-3"},
    )
    first = compact_label_provider(device, "plan")
    second = compact_label_provider(device, "plan")
    assert first == second == "LF-3"
