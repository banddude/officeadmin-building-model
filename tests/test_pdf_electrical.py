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
    ElectricalInstanceHint,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfPageTransform,
    PdfVectorPathObservation,
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


def _write_synthetic_pdf(
    path: Path,
    *,
    unrelated_prefix: bool = False,
    duplicate_vector_point: bool = False,
) -> None:
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
    vector_path = (
        b"72 700 m 72 700 l 200 500 l S\n"
        if duplicate_vector_point
        else b"72 700 m 200 500 l S\n"
    )
    content.set_data(
        unrelated
        + b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (PANEL LP 120/240V 1PH) Tj ET\n"
        + b"BT /F1 9 Tf 1 0 0 1 205 505 Tm (EVSE-1 +48\\\" AFF WALL MTD) Tj ET\n"
        + b"BT /F1 8 Tf 1 0 0 1 72 650 Tm (PANEL LP CKT 12 -> EVSE-1 240V 2P) Tj ET\n"
        + vector_path
        + b"q 1 0 0 1 200 500 cm /EVSE1 Do Q\n"
    )
    page[NameObject("/Contents")] = writer._add_object(content)

    with path.open("wb") as handle:
        writer.write(handle)


def test_device_text_rules_do_not_match_longer_nontechnical_words() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:nontechnical-word-prefixes",
            "page_count": 1,
            "texts": [
                {"element_id": "t1", "page": 1, "text": "RECORD", "x_pt": 10, "y_pt": 10},
                {"element_id": "t2", "page": 1, "text": "RECESSED", "x_pt": 20, "y_pt": 20},
                {"element_id": "t3", "page": 1, "text": "LIGHTING", "x_pt": 30, "y_pt": 30},
                {"element_id": "t4", "page": 1, "text": "LIGHTINGS", "x_pt": 40, "y_pt": 40},
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.electrical_devices
    assert not model.electrical_equipment


def test_generic_device_class_labels_remain_unresolved_without_instance_identity() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:generic-device-labels",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:left",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 100.0,
                    "y_pt": 200.0,
                },
                {
                    "element_id": "p1:text:right",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 300.0,
                    "y_pt": 200.0,
                },
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.electrical_devices
    unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("reason")
        == "generic device class label has no stable instance identity"
    ]
    assert len(unresolved) == 2


def test_explicit_instance_hints_materialize_repeated_generic_devices_deterministically() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:hinted-generic-devices",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:left",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 100.0,
                    "y_pt": 200.0,
                },
                {
                    "element_id": "p1:text:right",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 300.0,
                    "y_pt": 200.0,
                },
            ],
        }
    )
    hints = (
        ElectricalInstanceHint(
            identity_key="receptacle:left",
            page=1,
            entity_kind="device",
            canonical_type="receptacle",
            tag="R-LEFT",
            x_pt=100.0,
            y_pt=200.0,
            source_element_id="p1:text:left",
            confidence=0.99,
        ),
        ElectricalInstanceHint(
            identity_key="receptacle:right",
            page=1,
            entity_kind="device",
            canonical_type="receptacle",
            tag="R-RIGHT",
            x_pt=300.0,
            y_pt=200.0,
            source_element_id="p1:text:right",
            confidence=0.99,
        ),
    )

    first = ElectricalPdfImporter(instance_hints=hints).import_document(document)
    reordered = ElectricalPdfImporter(instance_hints=tuple(reversed(hints))).import_document(
        PdfElectricalDocument(
            source_id=document.source_id,
            page_count=document.page_count,
            texts=tuple(reversed(document.texts)),
        )
    )

    assert {item.name for item in first.electrical_devices} == {"R-LEFT", "R-RIGHT"}
    assert {item.id for item in first.electrical_devices} == {
        item.id for item in reordered.electrical_devices
    }
    assert first.to_json() == reordered.to_json()
    assert all(
        any(source.source_kind == "caller-instance-hint" for source in item.provenance)
        for item in first.electrical_devices
    )
    assert not first.attributes["pdf_electrical"]["unresolved_observations"]


def test_instance_hint_rejects_unknown_claimed_source_element() -> None:
    document = PdfElectricalDocument(
        source_id="fixture:stale-instance-hint",
        page_count=1,
    )
    hint = ElectricalInstanceHint(
        identity_key="device:one",
        page=1,
        entity_kind="device",
        canonical_type="receptacle",
        x_pt=100.0,
        y_pt=100.0,
        source_element_id="p1:text:missing",
    )

    with pytest.raises(ElectricalPdfError, match="unknown source element"):
        ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)


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
    assert any(
        item.points_pt == ((72.0, 700.0), (200.0, 500.0))
        and not item.closed
        for item in extracted.vectors
    )

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


def test_pdf_extraction_collapses_consecutive_duplicate_vector_points(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "duplicate-vector-point.pdf"
    _write_synthetic_pdf(pdf_path, duplicate_vector_point=True)

    extracted = extract_pdf(
        pdf_path,
        source_id="synthetic:duplicate-vector-point",
    )

    assert any(
        item.points_pt == ((72.0, 700.0), (200.0, 500.0))
        and not item.closed
        for item in extracted.vectors
    )
    model = ElectricalPdfImporter().import_document(extracted)
    assert len(model.circuits) == 1
    validate_model(model)


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

def test_vector_topology_fixture_emits_ports_and_merges_with_callout_evidence() -> None:
    document = _load_fixture("vector-topology-sheet-e1.json")
    model = ElectricalPdfImporter().import_document(document)
    reordered = ElectricalPdfImporter().import_document(
        PdfElectricalDocument(
            source_id=document.source_id,
            page_count=document.page_count,
            texts=tuple(reversed(document.texts)),
            symbols=tuple(reversed(document.symbols)),
            vectors=tuple(reversed(document.vectors)),
        )
    )

    assert model.to_json() == reordered.to_json()
    assert len(model.electrical_equipment) == 1
    assert len(model.electrical_devices) == 2
    assert len(model.ports) == 3
    assert len(model.circuits) == 1
    assert not model.routes

    panel = model.electrical_equipment[0]
    evse_by_name = {device.name: device for device in model.electrical_devices}
    circuit = model.circuits[0]
    assert panel.equipment_type == "panelboard"
    assert set(evse_by_name) == {"EVSE-1", "EVSE-2"}
    assert evse_by_name["EVSE-2"].attributes["pdf_electrical"]["mounting_height_m"] == 1.2192
    assert evse_by_name["EVSE-2"].attributes["pdf_electrical"]["host_hint"] == "wall"
    assert "pdf-vector-symbol-outline" in {
        item.method for item in panel.provenance
    }

    assert circuit.circuit_number == "12"
    assert circuit.voltage_v == 240.0
    assert circuit.poles == 2
    assert len(circuit.load_port_ids) == 2
    assert circuit.attributes["pdf_electrical"]["evidence_methods"] == [
        "pdf-circuit-text-link",
        "pdf-topology-circuit-callout",
        "pdf-vector-topology-link",
    ]
    assert {
        item["method"]
        for item in circuit.attributes["pdf_electrical"]["evidence"]
    } == {
        "pdf-circuit-text-link",
        "pdf-vector-topology-link",
    }
    assert not model.attributes["pdf_electrical"]["unresolved_topology"]

    source_port = next(port for port in model.ports if port.id == circuit.source_port_id)
    assert source_port.owner_id == panel.id
    assert "pdf-vector-topology-link" in source_port.attributes["pdf_electrical"][
        "evidence_methods"
    ]
    assert {
        next(port for port in model.ports if port.id == port_id).owner_id
        for port_id in circuit.load_port_ids
    } == {device.id for device in model.electrical_devices}

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_vector_topology_can_resolve_incomplete_circuit_callout_without_inventing_route() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:vector-topology-incomplete-callout",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "PANEL LP",
                    "x_pt": 80,
                    "y_pt": 500,
                },
                {
                    "element_id": "p1:text:0020",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 300,
                    "y_pt": 500,
                },
                {
                    "element_id": "p1:text:0030",
                    "page": 1,
                    "text": "CKT 5 208V 2P",
                    "x_pt": 190,
                    "y_pt": 515,
                },
            ],
            "vectors": [
                {
                    "element_id": "p1:vector:0010",
                    "page": 1,
                    "points_pt": [[80, 500], [300, 500]],
                }
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert len(model.circuits) == 1
    assert len(model.ports) == 2
    assert not model.routes
    circuit = model.circuits[0]
    assert circuit.circuit_number == "5"
    assert circuit.voltage_v == 208.0
    assert circuit.poles == 2
    assert circuit.attributes["pdf_electrical"]["evidence_methods"] == [
        "pdf-topology-circuit-callout",
        "pdf-vector-topology-link",
    ]
    assert model.attributes["pdf_electrical"]["unresolved_circuits"] == []
    assert model.attributes["pdf_electrical"]["unresolved_topology"] == []
    validate_model(model)


def test_ambiguous_vector_topology_remains_unresolved_without_ports_or_circuit() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:ambiguous-vector-topology",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:0010",
                    "page": 1,
                    "text": "PANEL LP",
                    "x_pt": 80,
                    "y_pt": 500,
                },
                {
                    "element_id": "p1:text:0020",
                    "page": 1,
                    "text": "PANEL DP",
                    "x_pt": 80,
                    "y_pt": 450,
                },
                {
                    "element_id": "p1:text:0030",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 300,
                    "y_pt": 500,
                },
            ],
            "vectors": [
                {
                    "element_id": "p1:vector:0010",
                    "page": 1,
                    "points_pt": [[80, 500], [200, 500], [300, 500]],
                },
                {
                    "element_id": "p1:vector:0020",
                    "page": 1,
                    "points_pt": [[80, 450], [200, 450], [200, 500]],
                },
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.ports
    assert not model.circuits
    unresolved = model.attributes["pdf_electrical"]["unresolved_topology"]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "source_or_load_ambiguous"
    assert len(unresolved[0]["equipment_ids"]) == 2
    assert len(unresolved[0]["load_candidate_ids"]) == 1
    validate_model(model)


def test_vector_topology_semantic_ids_survive_unrelated_extraction_id_edits() -> None:
    document = _load_fixture("vector-topology-sheet-e1.json")
    edited_vectors = (
        PdfVectorPathObservation(
            element_id="p1:vector:0001-unrelated",
            page=1,
            points_pt=((500.0, 700.0), (540.0, 700.0)),
        ),
        *tuple(
            PdfVectorPathObservation(
                element_id=f"p1:vector:{100 + index:04d}",
                page=vector.page,
                points_pt=vector.points_pt,
                closed=vector.closed,
                source_kind=vector.source_kind,
                metadata=vector.metadata,
            )
            for index, vector in enumerate(reversed(document.vectors), start=1)
        ),
    )
    edited = PdfElectricalDocument(
        source_id=document.source_id,
        page_count=document.page_count,
        texts=tuple(reversed(document.texts)),
        symbols=document.symbols,
        vectors=edited_vectors,
    )

    original_model = ElectricalPdfImporter().import_document(document)
    edited_model = ElectricalPdfImporter().import_document(edited)

    assert [item.id for item in original_model.electrical_equipment] == [
        item.id for item in edited_model.electrical_equipment
    ]
    assert [item.id for item in original_model.electrical_devices] == [
        item.id for item in edited_model.electrical_devices
    ]
    assert [item.id for item in original_model.ports] == [
        item.id for item in edited_model.ports
    ]
    assert [item.id for item in original_model.circuits] == [
        item.id for item in edited_model.circuits
    ]



def test_conflicting_vector_circuit_numbers_quarantine_text_connectivity() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:vector-topology-conflicting-circuit-numbers",
            "page_count": 1,
            "texts": [
                {"element_id": "p1:text:0010", "page": 1, "text": "PANEL LP", "x_pt": 80, "y_pt": 500},
                {"element_id": "p1:text:0020", "page": 1, "text": "EVSE-1", "x_pt": 300, "y_pt": 500},
                {"element_id": "p1:text:0030", "page": 1, "text": "PANEL LP CKT 12 -> EVSE-1", "x_pt": 180, "y_pt": 515},
                {"element_id": "p1:text:0040", "page": 1, "text": "PANEL LP CKT 14 -> EVSE-1", "x_pt": 180, "y_pt": 485},
            ],
            "vectors": [
                {"element_id": "p1:vector:0010", "page": 1, "points_pt": [[80, 500], [300, 500]]}
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)
    reordered = ElectricalPdfImporter().import_document(
        PdfElectricalDocument(
            source_id=document.source_id,
            page_count=document.page_count,
            texts=tuple(reversed(document.texts)),
            symbols=document.symbols,
            vectors=tuple(reversed(document.vectors)),
        )
    )

    assert model.to_json() == reordered.to_json()
    assert not model.circuits
    assert not model.ports
    assert not model.routes

    unresolved = model.attributes["pdf_electrical"]["unresolved_topology"]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "conflicting_circuit_callouts"
    assert unresolved[0]["circuit_numbers"] == ["12", "14"]
    assert unresolved[0]["circuit_callout_ids"] == ["p1:text:0030", "p1:text:0040"]
    assert len(unresolved[0]["suppressed_circuit_ids"]) == 2
    validate_model(model)


def test_conflicting_vector_load_tag_quarantines_text_connectivity() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:vector-topology-conflicting-load-tag",
            "page_count": 1,
            "texts": [
                {"element_id": "p1:text:0010", "page": 1, "text": "PANEL LP", "x_pt": 80, "y_pt": 500},
                {"element_id": "p1:text:0020", "page": 1, "text": "EVSE-1", "x_pt": 300, "y_pt": 500},
                {"element_id": "p1:text:0030", "page": 1, "text": "EVSE-2", "x_pt": 300, "y_pt": 420},
                {"element_id": "p1:text:0040", "page": 1, "text": "PANEL LP CKT 12 -> EVSE-2", "x_pt": 180, "y_pt": 520},
            ],
            "vectors": [
                {"element_id": "p1:vector:0010", "page": 1, "points_pt": [[80, 500], [300, 500]]}
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert not model.circuits
    assert not model.ports
    assert not model.routes

    evse_by_name = {device.name: device for device in model.electrical_devices}
    unresolved = model.attributes["pdf_electrical"]["unresolved_topology"]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "load_callout_conflict"
    assert unresolved[0]["circuit_callout_ids"] == ["p1:text:0040"]
    assert unresolved[0]["contradictory_load_ids"] == [evse_by_name["EVSE-2"].id]
    assert len(unresolved[0]["suppressed_circuit_ids"]) == 1
    validate_model(model)


def test_topology_conflict_does_not_taint_ports_for_independent_circuit() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "synthetic:vector-topology-selective-quarantine",
            "page_count": 1,
            "texts": [
                {"element_id": "p1:text:0010", "page": 1, "text": "PANEL LP", "x_pt": 80, "y_pt": 500},
                {"element_id": "p1:text:0020", "page": 1, "text": "EVSE-1", "x_pt": 300, "y_pt": 500},
                {"element_id": "p1:text:0030", "page": 1, "text": "EVSE-2", "x_pt": 300, "y_pt": 420},
                {"element_id": "p1:text:0040", "page": 1, "text": "EVSE-3", "x_pt": 300, "y_pt": 700},
                {"element_id": "p1:text:0050", "page": 1, "text": "PANEL LP CKT 12 -> EVSE-2", "x_pt": 180, "y_pt": 520},
                {"element_id": "p1:text:0060", "page": 1, "text": "PANEL LP CKT 20 -> EVSE-3", "x_pt": 180, "y_pt": 700},
            ],
            "vectors": [
                {"element_id": "p1:vector:0010", "page": 1, "points_pt": [[80, 500], [300, 500]]}
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert len(model.circuits) == 1
    circuit = model.circuits[0]
    assert circuit.circuit_number == "20"
    assert len(model.ports) == 2

    devices = {device.name: device for device in model.electrical_devices}
    load_port = next(port for port in model.ports if port.id in circuit.load_port_ids)
    assert load_port.owner_id == devices["EVSE-3"].id
    assert all(
        provenance.source_element_id != "p1:text:0050"
        for port in model.ports
        for provenance in port.provenance
    )

    unresolved = model.attributes["pdf_electrical"]["unresolved_topology"]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "load_callout_conflict"
    assert unresolved[0]["circuit_callout_ids"] == ["p1:text:0050"]
    assert len(unresolved[0]["suppressed_circuit_ids"]) == 1
    validate_model(model)
