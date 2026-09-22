"""Issue #75: every record says how the thing came to exist, not just where from.

A 3D visual must never present geometry we synthesized as if a source showed it.
`source_kind` cannot carry that distinction, because a port invented so a circuit
has an endpoint still carries the importer's own `source_kind`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import (
    DERIVATION_CLASSES,
    DERIVATION_INFERRED,
    DERIVATION_OBSERVED,
    DERIVATION_USER,
    BuildingModel,
    ContractError,
    CoordinateSystem,
    ElectricalDevice,
    ElectricalEquipment,
    Point3,
    Port,
    Pose,
    Provenance,
    Vector3,
    is_observed,
    stable_id,
)
from oabm.routing import route_between_ports

def _write_power_sheet(path: Path) -> None:
    """A source PDF with one panel, one device, and an explicit circuit callout.

    Built here rather than committed so this test does not depend on any other
    lane's fixture. Read back through `extract_pdf`, per #60.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=792, height=612)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (PANEL LP 120/240V 1PH) Tj ET\n"
        b"BT /F1 9 Tf 1 0 0 1 205 505 Tm (EVSE-1) Tj ET\n"
        b"200 500 10 10 re S\n"
        b"BT /F1 8 Tf 1 0 0 1 72 650 Tm (PANEL LP CKT 12 -> EVSE-1 240V 2P) Tj ET\n"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def test_derivation_accepts_only_the_three_classes() -> None:
    assert DERIVATION_CLASSES == {"observed", "user", "inferred"}
    for value in sorted(DERIVATION_CLASSES):
        assert Provenance(source_kind="k", source_id="s", derivation=value).derivation == value
    assert Provenance(source_kind="k", source_id="s").derivation is None


@pytest.mark.parametrize("value", ["guessed", "OBSERVED", "source", "", "assumed"])
def test_unknown_derivation_is_rejected(value: str) -> None:
    with pytest.raises(ContractError):
        Provenance(source_kind="k", source_id="s", derivation=value)


def test_is_observed_fails_closed() -> None:
    observed = Provenance(source_kind="k", source_id="s", derivation=DERIVATION_OBSERVED)
    inferred = Provenance(source_kind="k", source_id="s", derivation=DERIVATION_INFERRED)
    unset = Provenance(source_kind="k", source_id="s")

    assert is_observed((observed,))
    # Absence of a claim is not a claim.
    assert not is_observed((unset,))
    assert not is_observed(())
    # One synthesized record taints the whole set; a renderer must not call the
    # result observed just because some of its evidence was.
    assert not is_observed((observed, inferred))
    assert not is_observed((observed, unset))


def test_derivation_survives_serialization_round_trip() -> None:
    record = Provenance(
        source_kind="router",
        source_id="m",
        derivation=DERIVATION_INFERRED,
    )
    model = BuildingModel(
        model_id="m",
        coordinate_system=CoordinateSystem(frame_id="model"),
        provenance=(record,),
    )
    restored = BuildingModel.from_dict(model.to_dict())
    assert restored.provenance[0].derivation == DERIVATION_INFERRED
    assert restored.to_dict() == model.to_dict()


def _routing_model() -> BuildingModel:
    prov = (Provenance(source_kind="synthetic", source_id="fixture:routing", derivation=DERIVATION_USER),)
    panel = ElectricalEquipment(
        id=stable_id("equipment", "prov:panel"),
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.0, y=0.0, z=1.0)),
        provenance=prov,
    )
    device = ElectricalDevice(
        id=stable_id("device", "prov:device"),
        device_type="receptacle",
        pose=Pose(position=Point3(x=3.0, y=2.0, z=1.0)),
        provenance=prov,
    )
    source_port = Port(
        id=stable_id("port", "prov:source"),
        owner_id=panel.id,
        domain="electrical",
        role="source",
        pose=panel.pose,
        direction=Vector3(x=1.0, y=0.0, z=0.0),
        provenance=prov,
    )
    load_port = Port(
        id=stable_id("port", "prov:load"),
        owner_id=device.id,
        domain="electrical",
        role="load",
        pose=device.pose,
        direction=Vector3(x=-1.0, y=0.0, z=0.0),
        provenance=prov,
    )
    return BuildingModel(
        model_id="prov-routing",
        coordinate_system=CoordinateSystem(frame_id="model"),
        electrical_equipment=(panel,),
        electrical_devices=(device,),
        ports=(source_port, load_port),
        provenance=prov,
    )


def test_router_output_is_always_inferred() -> None:
    """No source shows a computed centerline, so it can never be observed."""
    model = _routing_model()
    route, fittings = route_between_ports(
        model,
        stable_id("port", "prov:source"),
        stable_id("port", "prov:load"),
        "emt",
    )

    assert route.provenance
    assert all(record.derivation == DERIVATION_INFERRED for record in route.provenance)
    assert not is_observed(route.provenance)
    for fitting in fittings:
        assert fitting.provenance
        assert all(
            record.derivation == DERIVATION_INFERRED for record in fitting.provenance
        )
        assert not is_observed(fitting.provenance)


def test_recognized_devices_are_observed_but_synthesized_ports_are_not(
    tmp_path: Path,
) -> None:
    """The decisive case from #75.

    The devices on this sheet are genuinely read off the drawing. The ports are
    invented so the resolved circuits have endpoints; nothing draws them. Before
    this field, both carried the same observed-looking `source_kind`.
    """
    sheet = tmp_path / "provenance-power-sheet.pdf"
    _write_power_sheet(sheet)
    assert not sheet.with_suffix(".expected.json").exists()
    model = ElectricalPdfImporter().import_document(
        extract_pdf(sheet, source_id="fixture:prov-derivation")
    )

    assert model.electrical_devices
    for device in model.electrical_devices:
        assert is_observed(device.provenance), device.id
    for equipment in model.electrical_equipment:
        assert is_observed(equipment.provenance), equipment.id

    assert model.ports
    for port in model.ports:
        assert not is_observed(port.provenance), (
            f"{port.id} is synthesized for circuit semantics and must never "
            "read as observed"
        )
        assert any(
            record.derivation == DERIVATION_INFERRED for record in port.provenance
        )
