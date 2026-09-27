"""Caller-supplied IfcGroups on export.

``to_ifc(..., groups=...)`` is the IFC counterpart of the GLB exporter's
``groups`` option: the caller names the groups (an alternate scope, a phase,
anything) and the adapter writes one standard ``IfcGroup`` per name with one
``IfcRelAssignsToGroup``, so a Bonsai or Revit user can select, isolate or
hide the whole scope in one click. These tests pin the default-off byte
guarantee, the exact expanded membership, deterministic identity, the
caller-error and import-side rules, and strict EXPRESS validity of the
grouped file. All content is the synthetic golden garage fixture.
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.validate
import pytest

from oabm.ifc import IfcAdapterError, canonical_id_to_ifc_guid, from_ifc, to_ifc
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"

_GROUP = ("device:garage-evse", "route:garage-panel-evse")
# route:garage-panel-evse spans six centerline points, so it has five segment
# products, plus its four fittings; a group over the route holds all of them.
_SEGMENT_KEYS = [f"route:garage-panel-evse#segment:{index}" for index in range(5)]
_MEMBER_KEYS = sorted(
    (
        "device:garage-evse",
        "fitting:garage-across",
        "fitting:garage-drop",
        "fitting:garage-return",
        "fitting:garage-rise",
        *_SEGMENT_KEYS,
    )
)


def _garage():
    case = next(
        item for item in load_golden_cases(GOLDEN_ROOT) if item.name == "synthetic-garage"
    )
    return load_golden_model(case)


def _bare_groups(ifc):
    """The plain IfcGroups in the file, excluding the IfcSystem subtypes."""

    return [item for item in ifc.by_type("IfcGroup") if item.is_a() == "IfcGroup"]


def _the_group(ifc, name="ALTERNATES"):
    groups = [item for item in _bare_groups(ifc) if item.Name == name]
    assert len(groups) == 1
    return groups[0]


def test_default_export_is_byte_identical(tmp_path: Path) -> None:
    model = _garage()
    to_ifc(model, tmp_path / "plain.ifc")
    to_ifc(model, tmp_path / "none.ifc", groups=None)
    to_ifc(model, tmp_path / "empty.ifc", groups={})
    assert (tmp_path / "plain.ifc").read_bytes() == (tmp_path / "none.ifc").read_bytes()
    assert (tmp_path / "plain.ifc").read_bytes() == (tmp_path / "empty.ifc").read_bytes()


def test_group_contents_are_exactly_the_expanded_members() -> None:
    ifc = to_ifc(_garage(), groups={"ALTERNATES": _GROUP})

    assert len(_bare_groups(ifc)) == 1
    group = _the_group(ifc)
    assert group.Name == "ALTERNATES"
    assert group.Description == "caller-supplied group"
    assert group.GlobalId == canonical_id_to_ifc_guid("group:ALTERNATES")

    # The group owns exactly one assignment relationship, with the device
    # product plus the route's five segment and four fitting products.
    (rel,) = list(group.IsGroupedBy)
    assert rel.is_a("IfcRelAssignsToGroup")
    assert rel.GlobalId == canonical_id_to_ifc_guid("group-rel:ALTERNATES")
    assert rel.Description == "caller-supplied group"
    assert rel.RelatingGroup == group
    expected = {canonical_id_to_ifc_guid(key) for key in _MEMBER_KEYS}
    assert len(rel.RelatedObjects) == len(_MEMBER_KEYS)
    assert {item.GlobalId for item in rel.RelatedObjects} == expected
    # The export passes the members sorted by canonical id; RelatedObjects is
    # a SET attribute, and the byte-determinism pass writes every relationship
    # SET sorted by STEP id, so the byte-identity tests pin the written order.
    step_order = sorted(rel.RelatedObjects, key=lambda item: item.id())
    assert list(rel.RelatedObjects) == step_order


def test_group_export_is_deterministic(tmp_path: Path) -> None:
    model = _garage()
    to_ifc(model, tmp_path / "first.ifc", groups={"ALTERNATES": _GROUP})
    to_ifc(model, tmp_path / "second.ifc", groups={"ALTERNATES": _GROUP})
    assert (tmp_path / "first.ifc").read_bytes() == (tmp_path / "second.ifc").read_bytes()

    # Reopened from disk, group and relationship identity is where the
    # derivation rules put it.
    written = ifcopenshell.open(str(tmp_path / "first.ifc"))
    group = _the_group(written)
    assert group.GlobalId == canonical_id_to_ifc_guid("group:ALTERNATES")
    (rel,) = list(group.IsGroupedBy)
    assert rel.GlobalId == canonical_id_to_ifc_guid("group-rel:ALTERNATES")


def test_id_in_two_groups_is_rejected(tmp_path: Path) -> None:
    model = _garage()
    with pytest.raises(ValueError) as excinfo:
        to_ifc(
            model,
            tmp_path / "dup.ifc",
            groups={
                "ALTERNATES": _GROUP,
                "PHASE-2": ("device:garage-evse",),
            },
        )
    assert isinstance(excinfo.value, IfcAdapterError)
    assert not (tmp_path / "dup.ifc").exists()


def test_unmatched_group_ids_are_ignored() -> None:
    ifc = to_ifc(
        _garage(),
        groups={
            "ALTERNATES": (*_GROUP, "wall:does-not-exist"),
            "WIRE": ("conductor:garage-l1",),
        },
    )
    alternates = _the_group(ifc)
    (rel,) = list(alternates.IsGroupedBy)
    assert {item.GlobalId for item in rel.RelatedObjects} == {
        canonical_id_to_ifc_guid(key) for key in _MEMBER_KEYS
    }

    # A conductor contributes its own product, and nothing else was created.
    wire = _the_group(ifc, "WIRE")
    (wire_rel,) = list(wire.IsGroupedBy)
    assert [item.GlobalId for item in wire_rel.RelatedObjects] == [
        canonical_id_to_ifc_guid("conductor:garage-l1")
    ]


def test_group_with_no_matching_members_gets_no_assignment() -> None:
    # RelatedObjects is SET [1:?], so an all-unmatched group cannot carry an
    # assignment; the bare IfcGroup keeps the caller's name visible instead
    # of silently dropping it.
    ifc = to_ifc(_garage(), groups={"PHANTOM": ("wall:does-not-exist",)})
    groups = _bare_groups(ifc)
    assert len(groups) == 1
    assert groups[0].Name == "PHANTOM"
    assert not groups[0].IsGroupedBy


def test_round_trip_is_unaffected_by_groups() -> None:
    model = _garage()
    grouped = from_ifc(
        to_ifc(
            model,
            groups={"ALTERNATES": _GROUP, "PANEL-ONLY": ("equip:garage-panel",)},
        )
    )
    assert grouped.to_dict() == model.to_dict()


def test_grouped_file_passes_strict_validation(tmp_path: Path) -> None:
    ifc = to_ifc(
        _garage(),
        tmp_path / "grouped.ifc",
        groups={"ALTERNATES": _GROUP, "PHANTOM": ("wall:does-not-exist",)},
    )
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []
