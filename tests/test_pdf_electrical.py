import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.pdf_electrical import (
    POINT_TO_M,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfPageTransform,
    extract_pdf,
)
from oabm.model import BuildingModel, stable_id, validate_model

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "pdf_electrical"
SCHEMA_PATH = ROOT / "contracts" / "oabm-model-v1.schema.json"


def _load_fixture(name: str) -> PdfElectricalDocument:
    return PdfElectricalDocument.from_dict(
        json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    )


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _write_synthetic_pdf(path: Path, *, unrelated_prefix: bool = False) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    symbol = DecodedStreamObject()
    symbol.set_data(b"0 0 12 12 re S")
    symbol[NameObject("/Type")] = NameObject("/XObject")
    symbol[NameObject("/Subtype")] = NameObject("/Form")
    symbol[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(12), NumberObject(12)]
    )
    symbol_ref = writer._add_object(symbol)

    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            NameObject("/XObject"): DictionaryObject({NameObject("/EVSE1"): symbol_ref}),
        }
    )

    content = DecodedStreamObject()
    unrelated = (
        b"BT /F1 7 Tf 1 0 0 1 500 750 Tm (GENERAL NOTE UNRELATED) Tj ET\n"
        b"0 0 5 5 re S\n"
        if unrelated_prefix
        else b""
    )
    content.set_data(
        unrelated
        + b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (PANEL LP 120/240V 1PH) Tj ET\n"
        + b"BT /F1 9 Tf 1 0 0 1 205 505 Tm (EVSE-1 +48\\\" AFF WALL MTD) Tj ET\n"
        + b"BT /F1 8 Tf 1 0 0 1 72 650 Tm (PANEL LP CKT 12 -> EVSE-1 240V 2P) Tj ET\n"
        + b"q 1 0 0 1 200 500 cm /EVSE1 Do Q\n"
    )
    page[NameObject("/Contents")] = writer._add_object(content)

    with path.open("wb") as handle:
        writer.write(handle)


def test_fixture_emits_canonical_equipment_devices_circuit_and_metadata() -> None:
    model = ElectricalPdfImporter().import_document(
        _load_fixture("synthetic-sheet-e1.json")
    )

    assert len(model.electrical_equipment) == 1
    assert len(model.electrical_devices) == 1
    assert len(model.ports) == 2
    assert len(model.circuits) == 1
    assert not model.levels
    assert not model.spaces
    assert not model.walls
    assert not model.routes

    panel = model.electrical_equipment[0]
    evse = model.electrical_devices[0]
    circuit = model.circuits[0]

    assert panel.equipment_type == "panelboard"
    assert panel.name == "LP"
    assert panel.level_id is None
    assert panel.space_id is None
    assert panel.host_id is None

    assert evse.device_type == "evse"
    assert evse.name == "EVSE-1"
    assert evse.host_id is None
    lane = evse.attributes["pdf_electrical"]
    assert lane["mounting_height_m"] == 1.2192
    assert lane["host_hint"] == "wall"
    assert lane["spatial_status"] == "single-page-local-unregistered"
    assert lane["symbol_names"] == ["/EVSE1"]
    assert "NOTE: EVSE SHALL BE WALL MOUNTED" in lane["annotations"]
    assert 0.0 < evse.confidence <= 1.0
    provenance_methods = {item.method for item in evse.provenance}
    assert {
        "pdf-text-pattern",
        "pdf-symbol-catalog",
        "pdf-nearby-annotation",
    }.issubset(provenance_methods)
    assert all(
        item.source_id == "synthetic:electrical-sheet-e1" and item.page == 1
        for item in evse.provenance
    )
    assert evse.pose.position.x == 300.0 * POINT_TO_M
    assert evse.pose.position.y == 500.0 * POINT_TO_M
    assert evse.pose.position.z == 0.0

    assert circuit.circuit_number == "12"
    assert circuit.voltage_v == 240.0
    assert circuit.poles == 2
    assert circuit.confidence == 0.92
    assert circuit.provenance[0].method == "pdf-circuit-text-link"
    assert circuit.provenance[0].confidence == 0.92
    assert circuit.source_port_id in {port.id for port in model.ports}
    assert set(circuit.load_port_ids).issubset({port.id for port in model.ports})
    assert model.attributes["pdf_electrical"]["registration_pending"] is True

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_ambiguity_and_incomplete_circuit_are_preserved_without_inventing_links() -> None:
    model = ElectricalPdfImporter().import_document(
        _load_fixture("ambiguous-sheet-e1.json")
    )

    assert len(model.electrical_devices) == 1
    assert not model.electrical_equipment
    assert not model.ports
    assert not model.circuits

    receptacle = model.electrical_devices[0]
    lane = receptacle.attributes["pdf_electrical"]
    assert lane["mounting_height_candidates_m"] == [1.2192, 1.3716]
    assert lane["mounting_height_status"] == "ambiguous"

    model_lane = model.attributes["pdf_electrical"]
    assert len(model_lane["unresolved_observations"]) == 1
    unresolved_symbol = model_lane["unresolved_observations"][0]
    assert unresolved_symbol["name"] == "/SW"
    candidate_types = {
        (item["entity_kind"], item["canonical_type"])
        for item in unresolved_symbol["classification_candidates"]
    }
    assert candidate_types == {
        ("device", "switch"),
        ("equipment", "switchboard"),
    }

    assert len(model_lane["unresolved_circuits"]) == 1
    unresolved_circuit = model_lane["unresolved_circuits"][0]
    assert unresolved_circuit["circuit_number"] == "7"
    assert unresolved_circuit["missing"] == ["source_panel"]


def test_stable_identity_and_output_are_repeatable_across_observation_order() -> None:
    document = _load_fixture("synthetic-sheet-e1.json")
    reversed_document = PdfElectricalDocument(
        source_id=document.source_id,
        page_count=document.page_count,
        texts=tuple(reversed(document.texts)),
        symbols=tuple(reversed(document.symbols)),
    )

    first = ElectricalPdfImporter().import_document(document)
    second = ElectricalPdfImporter().import_document(reversed_document)

    assert first.to_json() == second.to_json()
    assert [item.id for item in first.electrical_equipment] == [
        item.id for item in second.electrical_equipment
    ]
    assert [item.id for item in first.electrical_devices] == [
        item.id for item in second.electrical_devices
    ]
    assert [item.id for item in first.circuits] == [item.id for item in second.circuits]


def test_pdf_extraction_and_import_work_end_to_end(tmp_path: Path) -> None:
    pdf_path = tmp_path / "synthetic-electrical.pdf"
    _write_synthetic_pdf(pdf_path)

    extracted = extract_pdf(pdf_path, source_id="synthetic:e2e")
    assert extracted.page_count == 1
    assert any(item.name == "/EVSE1" for item in extracted.symbols)
    assert any("PANEL LP" in item.text for item in extracted.texts)

    model = ElectricalPdfImporter().import_pdf(
        pdf_path,
        source_id="synthetic:e2e",
    )
    assert len(model.electrical_equipment) == 1
    assert len(model.electrical_devices) == 1
    assert len(model.circuits) == 1
    assert model.electrical_devices[0].attributes["pdf_electrical"]["symbol_names"] == [
        "/EVSE1"
    ]

    reparsed = BuildingModel.from_json(model.to_json())
    assert reparsed.to_dict() == model.to_dict()


def test_unrecognized_graphic_is_preserved_as_unresolved_source_observation() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:unknown-symbol",
            "page_count": 1,
            "symbols": [
                {
                    "element_id": "p1:xobject:1",
                    "page": 1,
                    "name": "/SYM42",
                    "x_pt": 10,
                    "y_pt": 20,
                }
            ],
        }
    )
    model = ElectricalPdfImporter().import_document(document)

    assert not model.electrical_devices
    assert not model.electrical_equipment
    unresolved = model.attributes["pdf_electrical"]["unresolved_observations"]
    assert unresolved == [
        {
            "kind": "symbol",
            "page": 1,
            "source_element_id": "p1:xobject:1",
            "name": "/SYM42",
            "source_kind": "form-xobject",
            "position_pt": {"x": 10.0, "y": 20.0},
            "classification_candidates": [],
            "metadata": {},
        }
    ]


def test_circuit_callout_does_not_materialize_phantom_objects() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:circuit-reference-only",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "PANEL LP CKT 12 -> EVSE-1 240V 2P",
                    "x_pt": 120,
                    "y_pt": 620,
                }
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.electrical_equipment
    assert not model.electrical_devices
    assert not model.ports
    assert not model.circuits

    unresolved = model.attributes["pdf_electrical"]["unresolved_circuits"]
    assert len(unresolved) == 1
    assert unresolved[0]["panel_tag"] == "LP"
    assert unresolved[0]["load_ids"] == []
    assert unresolved[0]["missing"] == ["source_panel", "load"]


def test_feet_inches_mounting_height_is_not_double_counted() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:feet-inches-mounting",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "EVSE-1 4'-0\" AFF WALL MTD",
                    "x_pt": 200,
                    "y_pt": 300,
                }
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert len(model.electrical_devices) == 1
    lane = model.electrical_devices[0].attributes["pdf_electrical"]
    assert lane["mounting_height_m"] == 1.2192
    assert "mounting_height_candidates_m" not in lane


def test_semantic_ids_survive_unrelated_pdf_text_and_graphics_edits(tmp_path: Path) -> None:
    original_path = tmp_path / "original.pdf"
    edited_path = tmp_path / "edited.pdf"
    _write_synthetic_pdf(original_path)
    _write_synthetic_pdf(edited_path, unrelated_prefix=True)

    original_extracted = extract_pdf(original_path, source_id="synthetic:stable-edit")
    edited_extracted = extract_pdf(edited_path, source_id="synthetic:stable-edit")

    original_evse_text = next(
        item for item in original_extracted.texts if "EVSE-1" in item.text and "CKT" not in item.text
    )
    edited_evse_text = next(
        item for item in edited_extracted.texts if "EVSE-1" in item.text and "CKT" not in item.text
    )
    original_symbol = next(item for item in original_extracted.symbols if item.name == "/EVSE1")
    edited_symbol = next(item for item in edited_extracted.symbols if item.name == "/EVSE1")
    assert original_evse_text.element_id != edited_evse_text.element_id
    assert original_symbol.element_id != edited_symbol.element_id

    original = ElectricalPdfImporter().import_document(original_extracted)
    edited = ElectricalPdfImporter().import_document(edited_extracted)

    assert [item.id for item in original.electrical_equipment] == [
        item.id for item in edited.electrical_equipment
    ]
    assert [item.id for item in original.electrical_devices] == [
        item.id for item in edited.electrical_devices
    ]
    assert [item.id for item in original.ports] == [item.id for item in edited.ports]
    assert [item.id for item in original.circuits] == [item.id for item in edited.circuits]


def test_repeated_semantic_circuit_callouts_merge_loads_and_evidence() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:repeated-circuit",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "PANEL LP 120/240V 1PH",
                    "x_pt": 72,
                    "y_pt": 700,
                },
                {
                    "element_id": "p1:text:0020",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 200,
                    "y_pt": 500,
                },
                {
                    "element_id": "p1:text:0030",
                    "page": 1,
                    "text": "EVSE-2",
                    "x_pt": 300,
                    "y_pt": 500,
                },
                {
                    "element_id": "p1:text:0040",
                    "page": 1,
                    "text": "PANEL LP CKT 12 -> EVSE-1 240V 2P",
                    "x_pt": 72,
                    "y_pt": 650,
                },
                {
                    "element_id": "p1:text:0050",
                    "page": 1,
                    "text": "PANEL LP CKT 12 -> EVSE-2 240V 2P",
                    "x_pt": 72,
                    "y_pt": 625,
                },
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert len(model.circuits) == 1
    circuit = model.circuits[0]
    assert len(circuit.load_port_ids) == 2
    assert {item.source_element_id for item in circuit.provenance} == {
        "p1:text:0040",
        "p1:text:0050",
    }
    evidence = circuit.attributes["pdf_electrical"]["evidence"]
    assert [item["source_element_id"] for item in evidence] == [
        "p1:text:0040",
        "p1:text:0050",
    ]
    source_port = next(port for port in model.ports if port.id == circuit.source_port_id)
    assert {item.source_element_id for item in source_port.provenance} == {
        "p1:text:0040",
        "p1:text:0050",
    }


def test_unregistered_multi_page_coordinates_never_share_a_canonical_frame() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:multi-page",
            "page_count": 2,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 100,
                    "y_pt": 100,
                },
                {
                    "element_id": "p2:text:0010",
                    "page": 2,
                    "text": "EVSE-2",
                    "x_pt": 100,
                    "y_pt": 100,
                },
            ],
        }
    )

    with pytest.raises(ElectricalPdfError, match="multi-page electrical PDFs require"):
        ElectricalPdfImporter().import_document(document)

    frame_id = stable_id("frame", "synthetic:registered-multi-page")
    model = ElectricalPdfImporter().import_document(
        document,
        page_transforms={
            1: PdfPageTransform(frame_id=frame_id),
            2: PdfPageTransform(frame_id=frame_id, tx_m=10.0),
        },
    )

    assert model.coordinate_system.frame_id == frame_id
    assert model.attributes["pdf_electrical"]["registration_pending"] is False
    assert model.attributes["pdf_electrical"]["page_transforms_supplied"] is True
    positions = {
        device.name: (device.pose.position.x, device.pose.position.y)
        for device in model.electrical_devices
    }
    assert positions["EVSE-2"][0] - positions["EVSE-1"][0] == pytest.approx(10.0)
    assert positions["EVSE-2"][1] == pytest.approx(positions["EVSE-1"][1])
    validate_model(model)

def test_recognized_symbol_without_stable_identity_stays_unresolved() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:unstable-symbol-identity",
            "page_count": 1,
            "symbols": [
                {
                    "element_id": "p1:xobject:00042",
                    "page": 1,
                    "name": "/EVSE1",
                    "x_pt": 100,
                    "y_pt": 100,
                }
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.electrical_devices
    unresolved = model.attributes["pdf_electrical"]["unresolved_observations"]
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "unresolved_identity"
    assert unresolved[0]["recognized_classification"]["canonical_type"] == "evse"
