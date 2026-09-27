"""Caller-supplied element Status in the standard common property sets.

``to_ifc(..., element_status=...)`` is the IFC phase counterpart of the GLB
exporter's dim-existing option: the caller decides which canonical entity is
new, existing or to demolish, and the adapter writes the value as the
standard ``Status`` property of each product's applicable common property
set, so Bonsai and Revit can filter rework phase with no OABM knowledge.
These tests pin the default-off byte guarantee, the exact expanded placement
(outlet, light fixture, wall, and a route's segment products), the
caller-error rule, strict EXPRESS validity, the untouched canonical round
trip, byte determinism with pinned Status-set GlobalIds, and the
skip/ignore rules for statusless classes and unmatched ids. All content is
the synthetic commercial-TI golden fixture.
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

# The mixed acceptance case.  route:ti-r1 spans two centerline points with
# no fittings, so its expansion is exactly one segment product.
_MIXED_STATUS = {
    "device:ti-rec-1": "EXISTING",
    "device:ti-lum-1": "NEW",
    "wall:ti-south": "DEMOLISH",
    "route:ti-r1": "NEW",
}
# Canonical id -> (exact property set, exact Status value) on that product.
_EXPECTED_STATUS = {
    "device:ti-rec-1": ("Pset_OutletTypeCommon", "EXISTING"),
    "device:ti-lum-1": ("Pset_LightFixtureTypeCommon", "NEW"),
    "wall:ti-south": ("Pset_WallCommon", "DEMOLISH"),
    "route:ti-r1#segment:0": ("Pset_CableCarrierSegmentTypeCommon", "NEW"),
}


def _ti_model():
    case = next(
        item
        for item in load_golden_cases(GOLDEN_ROOT)
        if item.name == "commercial-ti-alternates"
    )
    return load_golden_model(case)


def _product(ifc, canonical_id: str):
    """The one product the adapter wrote for ``canonical_id``."""

    guid = canonical_id_to_ifc_guid(canonical_id)
    matches = [
        item
        for item in ifc.by_type("IfcProduct")
        if getattr(item, "GlobalId", None) == guid
    ]
    assert len(matches) == 1, canonical_id
    return matches[0]


def _status_properties(item) -> list[tuple[str, str]]:
    """Every (property set name, Status value) pair defined on ``item``."""

    found: list[tuple[str, str]] = []
    for rel in getattr(item, "IsDefinedBy", ()) or ():
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        definition = rel.RelatingPropertyDefinition
        if not definition.is_a("IfcPropertySet"):
            continue
        for prop in definition.HasProperties:
            if prop.Name == "Status":
                found.append((definition.Name, prop.NominalValue.wrappedValue))
    return sorted(found)


def test_default_export_is_byte_identical(tmp_path: Path) -> None:
    # The pre-existing determinism tests keep pinning the default bytes; here
    # the three no-status spellings must agree with each other, and the
    # default export must carry no Status property at all.
    model = _ti_model()
    to_ifc(model, tmp_path / "plain.ifc")
    to_ifc(model, tmp_path / "none.ifc", element_status=None)
    to_ifc(model, tmp_path / "empty.ifc", element_status={})
    assert (tmp_path / "plain.ifc").read_bytes() == (tmp_path / "none.ifc").read_bytes()
    assert (tmp_path / "plain.ifc").read_bytes() == (tmp_path / "empty.ifc").read_bytes()

    default = ifcopenshell.open(str(tmp_path / "plain.ifc"))
    carriers = [
        item.GlobalId
        for item in default.by_type("IfcProduct")
        if _status_properties(item)
    ]
    assert carriers == []


def test_mixed_statuses_land_on_exactly_the_right_products() -> None:
    ifc = to_ifc(_ti_model(), element_status=_MIXED_STATUS)

    for canonical_id, (pset_name, value) in _EXPECTED_STATUS.items():
        product = _product(ifc, canonical_id)
        assert _status_properties(product) == [(pset_name, value)], canonical_id

    # No product beyond the four above carries a Status property: the route
    # id expanded to its one segment, and every other element, system and
    # space is untouched.
    carriers = {
        item.GlobalId
        for item in ifc.by_type("IfcProduct")
        if _status_properties(item)
    }
    assert carriers == {
        canonical_id_to_ifc_guid(canonical_id) for canonical_id in _EXPECTED_STATUS
    }


def test_unknown_status_value_raises(tmp_path: Path) -> None:
    model = _ti_model()
    with pytest.raises(IfcAdapterError) as excinfo:
        to_ifc(
            model,
            tmp_path / "bogus.ifc",
            element_status={
                "device:ti-rec-1": "EXISTING",
                "device:ti-lum-1": "BOGUS",
            },
        )
    assert "BOGUS" in str(excinfo.value)
    assert not (tmp_path / "bogus.ifc").exists()


def test_status_file_passes_strict_validation(tmp_path: Path) -> None:
    ifc = to_ifc(_ti_model(), tmp_path / "status.ifc", element_status=_MIXED_STATUS)
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []


def test_round_trip_is_unaffected_by_statuses() -> None:
    model = _ti_model()
    statused = from_ifc(to_ifc(model, element_status=_MIXED_STATUS))
    assert statused.to_dict() == model.to_dict()


def test_status_export_is_deterministic_and_pset_ids_stable(
    tmp_path: Path,
) -> None:
    model = _ti_model()
    to_ifc(model, tmp_path / "first.ifc", element_status=_MIXED_STATUS)
    to_ifc(model, tmp_path / "second.ifc", element_status=_MIXED_STATUS)
    assert (tmp_path / "first.ifc").read_bytes() == (tmp_path / "second.ifc").read_bytes()

    # Reopened from disk, each Status set keeps its GlobalId pinned from
    # ``status-pset:`` plus its owner's canonical id.
    written = ifcopenshell.open(str(tmp_path / "first.ifc"))
    for canonical_id, (pset_name, _) in _EXPECTED_STATUS.items():
        product = _product(written, canonical_id)
        psets = [
            rel.RelatingPropertyDefinition
            for rel in product.IsDefinedBy
            if rel.is_a("IfcRelDefinesByProperties")
            and rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
            and rel.RelatingPropertyDefinition.Name == pset_name
        ]
        assert len(psets) == 1, canonical_id
        assert psets[0].GlobalId == canonical_id_to_ifc_guid(f"status-pset:{canonical_id}")


def test_unmatched_ids_and_statusless_classes_are_skipped() -> None:
    ifc = to_ifc(
        _ti_model(),
        element_status={
            "wall:does-not-exist": "DEMOLISH",
            "space:ti-lobby": "EXISTING",
            "device:ti-rec-1": "EXISTING",
        },
    )
    # An id matching nothing is ignored; a space has no Status-bearing
    # common set, so it is skipped without error and without a set.
    assert _status_properties(_product(ifc, "space:ti-lobby")) == []
    assert _status_properties(_product(ifc, "device:ti-rec-1")) == [
        ("Pset_OutletTypeCommon", "EXISTING")
    ]
    carriers = {
        item.GlobalId
        for item in ifc.by_type("IfcProduct")
        if _status_properties(item)
    }
    assert carriers == {canonical_id_to_ifc_guid("device:ti-rec-1")}
