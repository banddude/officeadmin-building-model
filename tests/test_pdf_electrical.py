import json
import math
import re
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.pdf_electrical import importer as pdf_electrical_importer
from oabm.importers.pdf_electrical import (
    POINT_TO_M,
    ElectricalInstanceHint,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfPageTransform,
    PdfSymbolObservation,
    PdfTextObservation,
    PdfVectorPathObservation,
    extract_pdf,
)
from oabm.model import BuildingModel, stable_id, validate_model

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "pdf_electrical"
CAD_GEOMETRY_FIXTURE = ROOT / "fixtures" / "pdf_architecture" / "v1" / "cad-export-geometry-only.pdf"
LEGEND_SHAPE_FIXTURE = FIXTURE_DIR / "geometry-only-power-sheet-with-legend.pdf"
TWO_PAGE_LEGEND_LOCALITY_FIXTURE = FIXTURE_DIR / "two-page-sheet-local-legend.pdf"
EDGE_LEGEND_BLOCK_FIXTURE = FIXTURE_DIR / "geometry-only-power-sheet-edge-legend.pdf"
DENSE_LEGEND_BLOCK_FIXTURE = FIXTURE_DIR / "geometry-only-power-sheet-dense-legend.pdf"
SEPARATE_LEGEND_REFERENCE_FIXTURE = FIXTURE_DIR / "separate-sheet-explicit-legend-reference.pdf"
NOTES_COLUMN_LEGEND_FIXTURE = (
    FIXTURE_DIR / "geometry-only-power-sheet-notes-column-legend.pdf"
)
REAL_CAD_GLYPH_FIXTURE = (
    FIXTURE_DIR / "geometry-only-power-sheet-real-cad-glyphs.pdf"
)
CIRCUIT_HOMERUN_FIXTURE = (
    FIXTURE_DIR / "geometry-only-power-sheet-circuit-homeruns.pdf"
)
INNER_VIEW_BORDER_LEGEND_FIXTURE = (
    FIXTURE_DIR / "geometry-only-power-sheet-inner-view-border-legend.pdf"
)
INNER_VIEW_BORDER_ROTATED_LEGEND_FIXTURE = (
    FIXTURE_DIR / "geometry-only-power-sheet-inner-view-border-legend-rotate-270.pdf"
)
ROTATED_LEGEND_FIXTURES = (
    (
        "geometry-only-power-sheet-notes-column-legend",
        NOTES_COLUMN_LEGEND_FIXTURE,
    ),
    ("geometry-only-power-sheet-edge-legend", EDGE_LEGEND_BLOCK_FIXTURE),
    ("geometry-only-power-sheet-dense-legend", DENSE_LEGEND_BLOCK_FIXTURE),
)
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


def test_geometry_only_power_sheet_matches_drawn_glyphs_to_its_own_legend() -> None:
    assert not LEGEND_SHAPE_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        LEGEND_SHAPE_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-with-legend",
    )
    repeated = extract_pdf(
        LEGEND_SHAPE_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-with-legend",
    )

    assert extracted == repeated
    assert not extracted.symbols
    assert {item.text for item in extracted.texts} == {
        "ELECTRICAL SYMBOL LEGEND",
        "GFCI",
        "JBOX",
        "LIGHT",
    }
    # The plan field is geometry only. All semantic text is confined to the
    # drawn legend block, well away from the six field glyphs.
    assert all(item.x_pt >= 350.0 for item in extracted.texts)

    model = ElectricalPdfImporter().import_document(extracted)

    assert len(model.electrical_devices) == 6
    assert not model.electrical_equipment
    device_types = [device.device_type for device in model.electrical_devices]
    assert device_types.count("receptacle") == 2
    assert device_types.count("junction_box") == 2
    assert device_types.count("luminaire") == 2
    assert all(device.confidence >= 0.9 for device in model.electrical_devices)

    for device in model.electrical_devices:
        lane = device.attributes["pdf_electrical"]
        shape = lane["shape_recognition"]
        assert shape["method"] == "sheet-legend-geometry-match"
        assert shape["legend_label"] in {"GFCI", "JBOX", "LIGHT"}
        assert shape["shape_signature"]
        assert shape["source_geometry_key"]
        methods = {item.method for item in device.provenance}
        assert "pdf-legend-shape-match" in methods
        assert "pdf-sheet-legend-type-label" in methods
        assert "pdf-text-pattern" not in methods

    unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("kind") == "vector_cluster"
    ]
    assert len(unresolved) == 2
    assert len({item["shape_signature"] for item in unresolved}) == 1
    assert {
        item["reason"] for item in unresolved
    } == {"glyph cluster has no unique matching type in the sheet legend"}

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\n".join(error.message for error in errors)


def test_legend_shape_device_ids_ignore_vector_extraction_ids_and_order() -> None:
    extracted = extract_pdf(
        LEGEND_SHAPE_FIXTURE,
        source_id="fixture:legend-shape-stable-identity",
    )
    renamed_vectors = tuple(
        PdfVectorPathObservation(
            element_id=f"renamed:vector:{index:04d}",
            page=vector.page,
            points_pt=vector.points_pt,
            closed=vector.closed,
            source_kind=vector.source_kind,
            metadata=vector.metadata,
        )
        for index, vector in enumerate(reversed(extracted.vectors), start=1)
    )
    edited = PdfElectricalDocument(
        source_id=extracted.source_id,
        page_count=extracted.page_count,
        texts=tuple(reversed(extracted.texts)),
        symbols=extracted.symbols,
        vectors=renamed_vectors,
    )

    original = ElectricalPdfImporter().import_document(extracted)
    reordered = ElectricalPdfImporter().import_document(edited)

    original_ids = {
        (
            device.device_type,
            round(device.attributes["pdf_electrical"]["source_position_pt"]["x"], 6),
            round(device.attributes["pdf_electrical"]["source_position_pt"]["y"], 6),
        ): device.id
        for device in original.electrical_devices
    }
    reordered_ids = {
        (
            device.device_type,
            round(device.attributes["pdf_electrical"]["source_position_pt"]["x"], 6),
            round(device.attributes["pdf_electrical"]["source_position_pt"]["y"], 6),
        ): device.id
        for device in reordered.electrical_devices
    }
    assert original_ids == reordered_ids


def test_legend_shape_matching_never_inherits_another_pages_legend() -> None:
    assert not TWO_PAGE_LEGEND_LOCALITY_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        TWO_PAGE_LEGEND_LOCALITY_FIXTURE,
        source_id="fixture:two-page-sheet-local-legend",
    )

    assert extracted.page_count == 2
    assert {item.page for item in extracted.texts} == {1}

    model = ElectricalPdfImporter().import_document(extracted)

    devices_by_page = {
        page: [
            device
            for device in model.electrical_devices
            if device.attributes["pdf_electrical"]["source_page"] == page
        ]
        for page in (1, 2)
    }
    assert len(devices_by_page[1]) == 6
    assert devices_by_page[2] == []

    page_two_unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("kind") == "vector_cluster" and item.get("page") == 2
    ]
    assert len(page_two_unresolved) == 8
    assert {
        item["reason"] for item in page_two_unresolved
    } == {
        "page has no recognized legend; glyph remains unresolved and "
        "cross-page legend inheritance is disabled"
    }
    assert all(
        item["recognition_provenance"] == {
            "method": "sheet-local-legend-geometry-match",
            "legend_scope": "same-page-only",
            "page": 2,
            "page_has_recognized_legend": False,
        }
        for item in page_two_unresolved
    )
    assert all(
        provenance.page == 1
        for device in model.electrical_devices
        for provenance in device.provenance
        if provenance.method == "pdf-sheet-legend-type-label"
    )

    validate_model(model)


def test_edge_titled_legend_block_is_detected_inside_sheet_frame() -> None:
    assert not EDGE_LEGEND_BLOCK_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        EDGE_LEGEND_BLOCK_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-edge-legend",
    )
    repeated = extract_pdf(
        EDGE_LEGEND_BLOCK_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-edge-legend",
    )

    assert extracted == repeated
    assert not extracted.symbols
    assert all(
        text.x_pt >= 590.0 or text.text == "SYMBOLS"
        for text in extracted.texts
    )
    assert {text.text for text in extracted.texts} >= {
        "SYMBOLS",
        "GFCI",
        "JBOX",
        "LIGHT",
        "POWER PLAN",
        "E2.1",
    }
    heading = next(text for text in extracted.texts if text.text == "SYMBOLS")
    labels = [
        text for text in extracted.texts if text.text in {"GFCI", "JBOX", "LIGHT"}
    ]
    assert min(
        ((heading.x_pt - label.x_pt) ** 2 + (heading.y_pt - label.y_pt) ** 2) ** 0.5
        for label in labels
    ) > 320.0
    assert any(
        not vector.closed
        and vector.points_pt == ((370.0, 515.0), (600.0, 390.0))
        for vector in extracted.vectors
    )

    model = ElectricalPdfImporter().import_document(extracted)
    lane = model.attributes["pdf_electrical"]
    assert len(model.electrical_devices) == 6
    assert len(model.electrical_devices) > 2
    assert [device.device_type for device in model.electrical_devices].count("receptacle") == 2
    assert [device.device_type for device in model.electrical_devices].count("junction_box") == 2
    assert [device.device_type for device in model.electrical_devices].count("luminaire") == 2
    assert lane["legend_recognition"]["regions"] == [
        {
            "page": 1,
            "method": "title-match",
            "confidence": 0.98,
            "heading_element_id": "p1:text:0004",
            "heading_text": "SYMBOLS",
            "row_count": 3,
            "classified_row_count": 3,
            "sheet_aliases": ["E2.1"],
        }
    ]
    assert lane["legend_recognition"]["explicit_cross_sheet_references"] == []
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"][
            "legend_detection_method"
        ]
        == "title-match"
        for device in model.electrical_devices
    )
    validate_model(model)


def _legend_shape_matched_devices(model: BuildingModel) -> list:
    return [
        device
        for device in model.electrical_devices
        if device.attributes.get("pdf_electrical", {})
        .get("shape_recognition", {})
        .get("method")
        == "sheet-legend-geometry-match"
    ]


@pytest.mark.parametrize(
    "heading_text",
    ["KEYNOTE SYMBOLS", "PANEL SCHEDULE SYMBOLS"],
)
def test_rejected_symbol_heading_context_yields_no_legend_devices(
    heading_text: str,
) -> None:
    extracted = extract_pdf(
        EDGE_LEGEND_BLOCK_FIXTURE,
        source_id=f"fixture:rejected-legend-heading:{heading_text}",
    )
    edited = PdfElectricalDocument(
        source_id=extracted.source_id,
        page_count=extracted.page_count,
        texts=tuple(
            replace(observation, text=heading_text)
            if observation.text == "SYMBOLS"
            else observation
            for observation in extracted.texts
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
    )

    model = ElectricalPdfImporter().import_document(edited)

    assert _legend_shape_matched_devices(model) == []
    assert model.attributes["pdf_electrical"]["legend_recognition"]["regions"] == []


@pytest.mark.parametrize(
    "heading_text",
    ["KEYNOTES", "PANEL SCHEDULE"],
)
def test_dense_cluster_under_rejected_section_heading_yields_no_legend_devices(
    heading_text: str,
) -> None:
    extracted = extract_pdf(
        DENSE_LEGEND_BLOCK_FIXTURE,
        source_id=f"fixture:rejected-dense-context:{heading_text}",
    )
    row_labels = [
        observation
        for observation in extracted.texts
        if observation.text in {"GFCI", "JBOX", "LIGHT"}
    ]
    section_heading = PdfTextObservation(
        element_id="p1:text:test-section-heading",
        page=1,
        text=heading_text,
        x_pt=min(observation.x_pt for observation in row_labels) - 48.0,
        y_pt=max(observation.y_pt for observation in row_labels) + 54.0,
        font_size_pt=12.0,
    )
    edited = PdfElectricalDocument(
        source_id=extracted.source_id,
        page_count=extracted.page_count,
        texts=(*extracted.texts, section_heading),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
    )

    model = ElectricalPdfImporter().import_document(edited)

    assert _legend_shape_matched_devices(model) == []
    assert model.attributes["pdf_electrical"]["legend_recognition"]["regions"] == []


def test_dense_table_legend_block_is_detected_without_a_heading() -> None:
    assert not DENSE_LEGEND_BLOCK_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        DENSE_LEGEND_BLOCK_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-dense-legend",
    )
    assert "LEGEND" not in {text.text.upper() for text in extracted.texts}
    assert "SYMBOL" not in {text.text.upper() for text in extracted.texts}
    assert "SYMBOLS" not in {text.text.upper() for text in extracted.texts}

    model = ElectricalPdfImporter().import_document(extracted)
    lane = model.attributes["pdf_electrical"]
    assert len(model.electrical_devices) == 6
    regions = lane["legend_recognition"]["regions"]
    assert len(regions) == 1
    assert regions[0]["method"] == "dense-table-cluster"
    assert regions[0]["heading_element_id"] is None
    assert regions[0]["row_count"] == 3
    assert regions[0]["classified_row_count"] == 3
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"][
            "legend_detection_method"
        ]
        == "dense-table-cluster"
        for device in model.electrical_devices
    )
    validate_model(model)


def test_explicit_separate_legend_sheet_reference_never_inherits_silently() -> None:
    assert not SEPARATE_LEGEND_REFERENCE_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        SEPARATE_LEGEND_REFERENCE_FIXTURE,
        source_id="fixture:separate-sheet-explicit-legend-reference",
    )
    assert extracted.page_count == 3

    model = ElectricalPdfImporter().import_document(extracted)
    lane = model.attributes["pdf_electrical"]
    references = lane["legend_recognition"]["explicit_cross_sheet_references"]
    assert {(row["page"], row["target_alias"], row["legend_page"]) for row in references} == {
        (2, "E-001", 1),
        (3, "GENERAL NOTES AND LEGEND", 1),
    }

    devices_by_page = {
        page: [
            device
            for device in model.electrical_devices
            if device.attributes["pdf_electrical"]["source_page"] == page
        ]
        for page in (1, 2, 3)
    }
    assert devices_by_page[1] == []
    assert len(devices_by_page[2]) == 6
    assert len(devices_by_page[3]) == 6
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"]["legend_scope"]
        == "explicit-cross-sheet-reference"
        for device in (*devices_by_page[2], *devices_by_page[3])
    )
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"]["legend_page"] == 1
        for device in (*devices_by_page[2], *devices_by_page[3])
    )
    for page in (2, 3):
        for device in devices_by_page[page]:
            reference_provenance = [
                item
                for item in device.provenance
                if item.method == "pdf-explicit-legend-sheet-reference"
            ]
            label_provenance = [
                item
                for item in device.provenance
                if item.method == "pdf-sheet-legend-type-label"
            ]
            assert len(reference_provenance) == 1
            assert reference_provenance[0].page == page
            assert reference_provenance[0].attributes["legend_page"] == 1
            assert len(label_provenance) == 1
            assert label_provenance[0].page == 1

    validate_model(model)



def test_notes_column_symbol_function_legend_records_field_status() -> None:
    assert not NOTES_COLUMN_LEGEND_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        NOTES_COLUMN_LEGEND_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-notes-column-legend",
    )
    repeated = extract_pdf(
        NOTES_COLUMN_LEGEND_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-notes-column-legend",
    )
    assert extracted == repeated
    assert {
        (
            symbol.source_kind,
            symbol.metadata.get("contents"),
        )
        for symbol in extracted.symbols
    } == {
        ("annotation:square", "CR"),
        ("annotation:square", "TV"),
    }

    expected_labels = {
        "receptacle_duplex": "duplex electrical outlet",
        "receptacle_quad": "quadruplex electrical outlet",
        "data_outlet": "telephone and/or data outlet",
        "combination_outlet": (
            "combination duplex electrical and tele/data outlet"
        ),
        "junction_box_power": (
            "electrical J-box to feed furniture system, number adjacent "
            "indicates number of circuits"
        ),
        "junction_box_data": (
            "TELE/DATA J-BOX TO FEED FURNITURE SYSTEM W/PULLSTRING ABOVE "
            "CEILING. NUMBER ADJACENT TO SYMBOL INDICATES NUMBER OF LINES "
            "SERVED"
        ),
        "access_control_device": (
            "card reader electric lock release with electric hinge"
        ),
        "catv_outlet": "cable TV outlet",
    }
    texts = {observation.text for observation in extracted.texts}
    assert {
        "ARCHITECT STAMP",
        "SYNTHETIC PUBLIC-SAFE FIXTURE",
        "KEY NOTES",
        "GENERAL NOTES",
        "LEGEND",
        "SYMBOL",
        "FUNCTION",
        "E",
        "N",
        '+44"',
        "EXISTING TO REMAIN",
        "NEW",
        "SYNTHETIC PROJECT",
        "ISSUE: TEST",
        "A2.1",
    } <= texts

    symbol_header = next(
        observation for observation in extracted.texts
        if observation.text == "SYMBOL"
    )
    title_block_sheet = next(
        observation for observation in extracted.texts
        if observation.text == "A2.1"
    )
    assert symbol_header.x_pt > 590.0
    assert symbol_header.y_pt > title_block_sheet.y_pt + 200.0

    model = ElectricalPdfImporter().import_document(extracted)
    lane = model.attributes["pdf_electrical"]
    assert len(model.electrical_devices) == 16, {
        "device_types": [device.device_type for device in model.electrical_devices],
        "legend_recognition": lane["legend_recognition"],
        "unresolved_vector_clusters": [
            item
            for item in lane["unresolved_observations"]
            if item.get("kind") == "vector_cluster"
        ],
    }
    assert not model.electrical_equipment

    devices_by_type = {
        canonical_type: [
            device
            for device in model.electrical_devices
            if device.device_type == canonical_type
        ]
        for canonical_type in expected_labels
    }
    assert set(device.device_type for device in model.electrical_devices) == set(
        expected_labels
    )
    assert all(len(devices) == 2 for devices in devices_by_type.values())
    assert all(
        {
            device.attributes["pdf_electrical"]["status"]
            for device in devices
        }
        == {"E", "N"}
        for devices in devices_by_type.values()
    )
    assert all(
        device.attributes["pdf_electrical"]["status_meaning"]
        in {"existing_to_remain", "new"}
        for device in model.electrical_devices
    )
    assert all(
        any(
            provenance.method == "pdf-field-status-tag"
            for provenance in device.provenance
        )
        for device in model.electrical_devices
    )
    assert all(
        device.attributes["pdf_electrical"]["legend_row_label"]
        == expected_labels[device.device_type]
        and device.attributes["pdf_electrical"]["device_type_label"]
        == expected_labels[device.device_type]
        for device in model.electrical_devices
    )

    annotation_devices = {
        device.attributes["pdf_electrical"].get("annotation_code"): device
        for device in model.electrical_devices
        if device.attributes["pdf_electrical"].get("annotation_code")
    }
    assert set(annotation_devices) == {"CR", "TV"}
    assert annotation_devices["CR"].device_type == "access_control_device"
    assert annotation_devices["TV"].device_type == "catv_outlet"
    assert annotation_devices["CR"].attributes["pdf_electrical"]["status"] == "E"
    assert annotation_devices["TV"].attributes["pdf_electrical"]["status"] == "E"
    for code, device in annotation_devices.items():
        recognition = device.attributes["pdf_electrical"]["annotation_recognition"]
        assert recognition["method"] == "annotation-code"
        assert recognition["annotation_code"] == code
        assert recognition["legend_row_label"] == expected_labels[device.device_type]
        assert any(
            provenance.method == "annotation-code"
            and provenance.attributes["annotation_code"] == code
            for provenance in device.provenance
        )

    regions = lane["legend_recognition"]["regions"]
    assert len(regions) == 1
    assert regions[0]["method"] == "symbol-function-table"
    assert regions[0]["heading_text"] == "LEGEND"
    assert regions[0]["row_count"] == 8
    assert regions[0]["classified_row_count"] == 8
    assert len(regions[0]["header_element_ids"]) == 2

    for device in model.electrical_devices:
        shape = device.attributes["pdf_electrical"]["shape_recognition"]
        assert shape["legend_detection_method"] == "symbol-function-table"
        assert shape["canonical_type"] == device.device_type
        assert shape["legend_row_label"] == expected_labels[device.device_type]
        diagnostics = shape["match_diagnostics"]
        assert diagnostics["nearest_prototype"]["canonical_type"] == device.device_type
        assert diagnostics["score"] >= pdf_electrical_importer._GLYPH_MATCH_SCORE_MIN
        assert diagnostics["second_best"] is not None
        assert diagnostics["non_unique_reason"] is None

    # Status, mounting-height and circuit-count text are field modifiers, not
    # legend labels and do not affect source glyph type matching.
    without_modifiers = PdfElectricalDocument(
        source_id=extracted.source_id + ":without-field-modifiers",
        page_count=extracted.page_count,
        texts=tuple(
            observation
            for observation in extracted.texts
            if observation.text not in {"E", "N", '+44"', "1", "2", "3"}
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
        page_provenance=extracted.page_provenance,
    )
    without_modifier_model = ElectricalPdfImporter().import_document(
        without_modifiers
    )
    assert {
        (
            device.device_type,
            device.attributes["pdf_electrical"]["shape_recognition"][
                "source_geometry_key"
            ],
        )
        for device in without_modifier_model.electrical_devices
    } == {
        (
            device.device_type,
            device.attributes["pdf_electrical"]["shape_recognition"][
                "source_geometry_key"
            ],
        )
        for device in model.electrical_devices
    }

    # SYMBOL | FUNCTION is sufficient even without a separate LEGEND title.
    without_legend_title = PdfElectricalDocument(
        source_id=extracted.source_id + ":without-legend-title",
        page_count=extracted.page_count,
        texts=tuple(
            observation
            for observation in extracted.texts
            if observation.text != "LEGEND"
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
        page_provenance=extracted.page_provenance,
    )
    header_only_model = ElectricalPdfImporter().import_document(
        without_legend_title
    )
    assert len(header_only_model.electrical_devices) == 16
    assert header_only_model.attributes["pdf_electrical"][
        "legend_recognition"
    ]["regions"][0]["method"] == "symbol-function-table"

    # With all actual legend signals removed, nearby notes must not become a
    # legend heading. This also protects the #58 fail-closed behavior.
    notes_only = PdfElectricalDocument(
        source_id=extracted.source_id + ":notes-only",
        page_count=extracted.page_count,
        texts=tuple(
            observation
            for observation in extracted.texts
            if observation.text not in {
                "LEGEND",
                "SYMBOL",
                "FUNCTION",
                *expected_labels.values(),
            }
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
        page_provenance=extracted.page_provenance,
    )
    notes_only_model = ElectricalPdfImporter().import_document(notes_only)
    assert notes_only_model.attributes["pdf_electrical"][
        "legend_recognition"
    ]["regions"] == []

    validate_model(model)



def test_real_cad_glyph_variants_clear_ninety_percent_with_confidence() -> None:
    assert not REAL_CAD_GLYPH_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        REAL_CAD_GLYPH_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-real-cad-glyphs",
    )
    repeated = extract_pdf(
        REAL_CAD_GLYPH_FIXTURE,
        source_id="fixture:geometry-only-power-sheet-real-cad-glyphs",
    )
    assert extracted == repeated

    model = ElectricalPdfImporter().import_document(extracted)
    shape_devices = _legend_shape_matched_devices(model)
    expected = [
        ("receptacle_duplex", 100.0, 520.0),
        ("receptacle_duplex", 300.0, 520.0),
        ("receptacle_quad", 100.0, 468.0),
        ("receptacle_quad", 300.0, 468.0),
        ("data_outlet", 100.0, 416.0),
        ("data_outlet", 300.0, 416.0),
        ("combination_outlet", 100.0, 364.0),
        ("combination_outlet", 300.0, 364.0),
        ("junction_box_power", 100.0, 312.0),
        ("junction_box_power", 300.0, 312.0),
        ("junction_box_data", 100.0, 260.0),
        ("junction_box_data", 300.0, 260.0),
        ("access_control_device", 100.0, 208.0),
        ("access_control_device", 300.0, 208.0),
        ("catv_outlet", 100.0, 156.0),
        ("catv_outlet", 300.0, 156.0),
        ("receptacle_duplex", 70.0, 92.0),
        ("receptacle_quad", 84.5, 92.0),
        ("data_outlet", 195.0, 92.0),
        ("combination_outlet", 209.825, 92.0),
        ("junction_box_power", 320.0, 92.0),
        ("junction_box_data", 334.5, 92.0),
        ("access_control_device", 445.0, 92.0),
        ("catv_outlet", 459.5, 92.0),
    ]

    unmatched_expected = list(expected)
    correct = 0
    for device in shape_devices:
        position = device.attributes["pdf_electrical"]["source_position_pt"]
        index = min(
            range(len(unmatched_expected)),
            key=lambda item_index: (
                (position["x"] - unmatched_expected[item_index][1]) ** 2
                + (position["y"] - unmatched_expected[item_index][2]) ** 2
            ),
        )
        expected_type, expected_x, expected_y = unmatched_expected.pop(index)
        assert math.hypot(
            position["x"] - expected_x,
            position["y"] - expected_y,
        ) <= 8.0
        correct += device.device_type == expected_type

        shape = device.attributes["pdf_electrical"]["shape_recognition"]
        diagnostics = shape["match_diagnostics"]
        assert diagnostics["score"] is not None
        assert diagnostics["margin"] is not None
        assert diagnostics["confidence"] == {
            "score": diagnostics["score"],
            "margin": diagnostics["margin"],
        }
        assert shape["confidence"] == diagnostics["confidence"]
        assert diagnostics["score"] >= pdf_electrical_importer._GLYPH_MATCH_ABSOLUTE_FLOOR
        assert (
            diagnostics["score"] >= pdf_electrical_importer._GLYPH_MATCH_STRONG_SCORE
            or diagnostics["margin"] >= pdf_electrical_importer._GLYPH_MATCH_MARGIN_MIN
            or diagnostics.get("tie_breaker") == "differentiating-stroke-count"
        )

    assert correct / len(expected) >= 0.90
    assert len(shape_devices) >= math.ceil(0.90 * len(expected))

    cleanup_actions = {
        action
        for device in shape_devices
        for action in device.attributes["pdf_electrical"]["shape_recognition"].get(
            "cluster_cleanup",
            (),
        )
    }
    assert {
        "removed-leader-lines",
        "split-oversized-connected-components",
        "merged-undersized-neighbor",
        "stripped-text-glyphs",
        "recorded-stripped-text-tags",
    } <= cleanup_actions
    assert any(
        "E1"
        in device.attributes["pdf_electrical"]["shape_recognition"].get("tags", ())
        for device in shape_devices
    )
    validate_model(model)


def _issue70_negative_probe_document(
    probe_name: str,
) -> PdfElectricalDocument:
    extracted = extract_pdf(
        NOTES_COLUMN_LEGEND_FIXTURE,
        source_id=f"fixture:issue70-negative:{probe_name}",
    )
    kept_vectors = []
    for vector in extracted.vectors:
        bbox = pdf_electrical_importer._vector_bbox(vector)
        extent = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if bbox[0] >= 580.0 or extent > pdf_electrical_importer._GLYPH_PATH_MAX_EXTENT_PT:
            kept_vectors.append(vector)

    if probe_name == "north-arrow":
        probe_vectors = (
            PdfVectorPathObservation(
                element_id="p1:issue70:north:triangle",
                page=1,
                points_pt=((100.0, 506.0), (106.0, 494.0), (94.0, 494.0)),
                closed=True,
            ),
            PdfVectorPathObservation(
                element_id="p1:issue70:north:stem",
                page=1,
                points_pt=((100.0, 494.0), (100.0, 486.0)),
            ),
        )
    elif probe_name == "section-bubble":
        probe_vectors = (
            PdfVectorPathObservation(
                element_id="p1:issue70:section:ring",
                page=1,
                points_pt=tuple(
                    (
                        100.0 + 7.0 * math.cos(2.0 * math.pi * index / 12.0),
                        500.0 + 7.0 * math.sin(2.0 * math.pi * index / 12.0),
                    )
                    for index in range(12)
                ),
                closed=True,
            ),
            PdfVectorPathObservation(
                element_id="p1:issue70:section:divider",
                page=1,
                points_pt=((94.0, 500.0), (106.0, 500.0)),
            ),
        )
    elif probe_name == "keynote-hexagon":
        probe_vectors = (
            PdfVectorPathObservation(
                element_id="p1:issue70:keynote:hexagon",
                page=1,
                points_pt=tuple(
                    (
                        100.0 + 7.0 * math.cos(2.0 * math.pi * index / 6.0),
                        500.0 + 7.0 * math.sin(2.0 * math.pi * index / 6.0),
                    )
                    for index in range(6)
                ),
                closed=True,
            ),
        )
    else:  # pragma: no cover - test helper contract
        raise AssertionError(f"unknown probe {probe_name}")

    return PdfElectricalDocument(
        source_id=extracted.source_id,
        page_count=1,
        texts=tuple(
            observation
            for observation in extracted.texts
            if observation.x_pt >= 580.0
        ),
        symbols=(),
        vectors=tuple((*kept_vectors, *probe_vectors)),
        page_provenance=extracted.page_provenance,
    )


@pytest.mark.parametrize(
    "probe_name",
    ("north-arrow", "section-bubble", "keynote-hexagon"),
)
def test_issue70_non_device_symbol_probes_still_yield_zero_devices(
    probe_name: str,
) -> None:
    model = ElectricalPdfImporter().import_document(
        _issue70_negative_probe_document(probe_name)
    )
    assert model.electrical_devices == ()
    assert model.electrical_equipment == ()
    assert _legend_shape_matched_devices(model) == []



def test_homeruns_circuit_tags_and_panel_schedule_resolve_fail_closed() -> None:
    assert CIRCUIT_HOMERUN_FIXTURE.exists()
    assert not CIRCUIT_HOMERUN_FIXTURE.with_suffix(".expected.json").exists()

    extracted = extract_pdf(
        CIRCUIT_HOMERUN_FIXTURE,
        source_id="fixture:issue72-circuit-homeruns",
    )
    repeated = extract_pdf(
        CIRCUIT_HOMERUN_FIXTURE,
        source_id="fixture:issue72-circuit-homeruns",
    )
    assert extracted == repeated

    model = ElectricalPdfImporter().import_document(extracted)

    assert len(model.electrical_equipment) == 1
    assert model.electrical_equipment[0].equipment_type == "panelboard"
    assert model.electrical_equipment[0].name == "LP"
    assert {circuit.circuit_number for circuit in model.circuits} == {
        "1",
        "3",
        "5",
        "7",
    }
    assert len(model.circuits) == 4
    # Each circuit owns one panel source port. The three homerun circuits
    # share the same three loads but keep distinct load ports per circuit;
    # circuit 7 has one direct-tagged load.
    assert len(model.ports) == 14

    homeruns = [
        circuit for circuit in model.circuits if circuit.circuit_number in {"1", "3", "5"}
    ]
    direct = next(
        circuit for circuit in model.circuits if circuit.circuit_number == "7"
    )
    assert all(len(circuit.load_port_ids) == 3 for circuit in homeruns)
    assert len(direct.load_port_ids) == 1
    assert all(
        circuit.attributes["pdf_electrical"]["evidence_methods"]
        == ["pdf-homerun-annotation"]
        for circuit in homeruns
    )
    raceway_groups = {
        row["shared_raceway_group"]
        for circuit in homeruns
        for row in circuit.attributes["pdf_electrical"]["evidence"]
    }
    assert len(raceway_groups) == 1
    assert direct.attributes["pdf_electrical"]["evidence_methods"] == [
        "pdf-device-circuit-tag"
    ]
    assert all(
        row["schedule_validated"]
        for circuit in model.circuits
        for row in circuit.attributes["pdf_electrical"]["evidence"]
    )

    misses = model.attributes["pdf_electrical"]["unresolved_circuits"]
    reason_codes = {row.get("reason_code") for row in misses}
    # The dimension leader is an arrowed leader that touches no device, so
    # `arrow_not_associated_to_device` is its accurate code. `no_panel_token`
    # is reserved for an arrowed run that DOES reach a device but carries no
    # panel annotation; see
    # test_homerun_reaching_a_device_without_an_annotation_reports_no_panel_token.
    assert "arrow_not_associated_to_device" in reason_codes
    assert "panel_id_not_recognized" in reason_codes
    assert "circuit_outside_panel_schedule" in reason_codes
    assert all(circuit.circuit_number != "2" for circuit in model.circuits)
    assert all(circuit.circuit_number != "99" for circuit in model.circuits)
    assert all(
        circuit.name is None or not circuit.name.startswith("ZZ ")
        for circuit in model.circuits
    )

    validate_model(model)
    errors = sorted(
        _schema_validator().iter_errors(model.to_dict()),
        key=lambda error: list(error.path),
    )
    assert not errors, "\\n".join(error.message for error in errors)


def _write_circuit_probe_pdf(
    path: Path, body: bytes, *, schedule_cells: bool = True
) -> None:
    """Write a one-page power sheet with explicit synthetic schedule cells.

    Source PDF only, per #60: the test reads it back through ``extract_pdf``
    and no paired ``.expected.json`` is ever written.
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
    # Source-PDF schedule rows are drawn in cells within a table frame. Tests
    # that insert a numbered note deliberately do not draw a cell around it.
    cells = []
    row_positions = []
    if schedule_cells:
        for match in re.finditer(
            rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm \(\d+ RECEPTACLE LOAD\) Tj",
            body,
        ):
            row_positions.append((float(match.group(1)), float(match.group(2))))
        heading = re.search(
            rb"1 0 0 1 ([\d.]+) ([\d.]+) Tm \(PANEL [A-Z0-9_.-]+ SCHEDULE\) Tj",
            body,
        )
        if heading is not None and row_positions:
            row_positions.sort(key=lambda position: -position[1])
            frame_left = min(float(heading.group(1)), *(x for x, _y in row_positions)) - 10
            frame_right = max(x for x, _y in row_positions) + 150
            frame_top = float(heading.group(2)) + 10
            row_tops = [y + 5 for _x, y in row_positions]
            row_bottoms = row_tops[1:] + [row_positions[-1][1] - 5]
            frame_bottom = row_bottoms[-1]
            cells.append(
                f"{frame_left:g} {row_tops[0]:g} "
                f"{frame_right - frame_left:g} {frame_top - row_tops[0]:g} re S\n".encode()
            )
            for row_top, row_bottom in zip(row_tops, row_bottoms):
                cells.append(
                    f"{frame_left:g} {row_bottom:g} "
                    f"{frame_right - frame_left:g} {row_top - row_bottom:g} re S\n".encode()
                )
            cells.append(
                f"{frame_left:g} {frame_bottom:g} "
                f"{frame_right - frame_left:g} {frame_top - frame_bottom:g} re S\n".encode()
            )
    content = DecodedStreamObject()
    content.set_data(body + b"".join(cells))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _circuit_probe_model(
    path: Path, body: bytes, source_id: str, *, schedule_cells: bool = True
):
    _write_circuit_probe_pdf(path, body, schedule_cells=schedule_cells)
    assert not path.with_suffix(".expected.json").exists()
    return ElectricalPdfImporter().import_document(
        extract_pdf(path, source_id=source_id)
    )


def test_device_tag_sharing_a_panel_name_prefix_creates_no_circuit(
    tmp_path: Path,
) -> None:
    """A panel named ``EVSE`` must not turn ``EVSE-1`` into circuit 1.

    ``EVSE-1`` is a device identity tag and states no circuit assignment, so
    resolving it would invent a circuit number no annotation states.
    """
    model = _circuit_probe_model(
        tmp_path / "panel-name-collides-with-device-tag.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL EVSE 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL EVSE SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-panel-device-prefix-collision",
    )

    assert [entity.name for entity in model.electrical_equipment] == ["EVSE"]
    assert [device.name for device in model.electrical_devices] == ["EVSE-1"]
    assert model.circuits == ()
    assert model.ports == ()


def test_two_competing_homerun_annotations_resolve_no_circuit(
    tmp_path: Path,
) -> None:
    """An ambiguous homerun must not be rescued by the direct-tag pass.

    Both annotations are diagnosed as conflicting, and neither may then be
    re-read as a direct device circuit tag.
    """
    model = _circuit_probe_model(
        tmp_path / "competing-homerun-annotations.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"180 450 m 300 450 l S\n"
        b"294 446 m 300 450 l 294 454 l S\n"
        # Both annotations sit close enough to the device that the
        # direct-device-tag pass would otherwise pick them up.
        b"BT /F1 8 Tf 1 0 0 1 190 470 Tm (LP-1) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 200 485 Tm (LP-3) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 495 Tm (3 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-competing-homerun-annotations",
    )

    assert model.circuits == ()
    assert model.ports == ()
    misses = model.attributes["pdf_electrical"]["unresolved_circuits"]
    conflicted = [
        row for row in misses if row.get("reason_code") == "conflicting_homerun_annotations"
    ]
    assert {row["source_text"] for row in conflicted} == {"LP-1", "LP-3"}
    # The conflicted texts must not reappear as resolved direct tags.
    assert all(row["status"] == "unresolved" for row in conflicted)


def test_malformed_circuit_list_fails_closed_instead_of_resolving_prefix(
    tmp_path: Path,
) -> None:
    """``LP-1,X`` must not silently resolve as ``LP-1``."""
    model = _circuit_probe_model(
        tmp_path / "unparseable-circuit-list.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"180 450 m 300 450 l S\n"
        b"294 446 m 300 450 l 294 454 l S\n"
        b"BT /F1 9 Tf 1 0 0 1 305 452 Tm (LP-1,X) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-unparseable-circuit-list",
    )

    assert model.circuits == ()
    assert model.ports == ()
    misses = model.attributes["pdf_electrical"]["unresolved_circuits"]
    assert any(row.get("reason_code") == "unparseable_circuit_list" for row in misses)
    assert all(row.get("circuit_numbers") != [1] for row in misses)


def test_homerun_reaching_a_device_without_an_annotation_reports_no_panel_token(
    tmp_path: Path,
) -> None:
    """An arrowed run that reaches a device but names no panel fails closed.

    This is the case #72 means by `no panel token`: a homerun IS found, and the
    part that failed is the missing panel designation. An arrowed leader that
    reaches no device at all is a dimension or annotation leader and gets
    `arrow_not_associated_to_device` instead.
    """
    model = _circuit_probe_model(
        tmp_path / "homerun-without-annotation.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"180 450 m 300 450 l S\n"
        b"294 446 m 300 450 l 294 454 l S\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-homerun-without-annotation",
    )

    assert model.circuits == ()
    assert model.ports == ()
    misses = model.attributes["pdf_electrical"]["unresolved_circuits"]
    assert any(row.get("reason_code") == "no_panel_token" for row in misses)


def test_unclaimed_closed_path_touching_branch_fails_closed(tmp_path: Path) -> None:
    """A device's own outline is lawful; a separate crossing enclosure is not."""
    base = (
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"180 450 m 300 450 l S\n"
        b"294 446 m 300 450 l 294 454 l S\n"
        b"BT /F1 9 Tf 1 0 0 1 305 452 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n"
    )
    clean = _circuit_probe_model(
        tmp_path / "device-outline-only.pdf", base, "fixture:issue72-device-outline-only"
    )
    assert [circuit.circuit_number for circuit in clean.circuits] == ["1"]

    crossing = _circuit_probe_model(
        tmp_path / "unclaimed-closed-crossing.pdf",
        base + b"235 430 30 40 re S\n",
        "fixture:issue72-unclaimed-closed-crossing",
    )
    assert crossing.circuits == ()
    assert crossing.ports == ()
    assert any(
        row.get("reason_code") == "branch_run_not_isolated"
        and row.get("cycle_element_id")
        for row in crossing.attributes["pdf_electrical"]["unresolved_circuits"]
    )


def test_schedule_row_position_cannot_validate_an_absent_circuit(tmp_path: Path) -> None:
    """Moving the same schedule row cannot turn LP-99 into a valid circuit."""
    for delta in (239, 240, 241, 242):
        row_y = 550 - delta
        model = _circuit_probe_model(
            tmp_path / f"schedule-row-delta-{delta}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
            b"175 445 10 10 re S\n"
            b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-99) Tj ET\n"
            b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
            + f"BT /F1 8 Tf 1 0 0 1 600 {row_y} Tm (1 RECEPTACLE LOAD) Tj ET\n".encode(),
            f"fixture:issue72-schedule-row-delta-{delta}",
        )
        assert model.circuits == (), f"LP-99 resolved when schedule row moved {delta} pt"
        assert any(
            row.get("source_text") == "LP-99"
            and row.get("reason_code") == "circuit_outside_panel_schedule"
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        )


def test_unrelated_numbered_note_cannot_validate_a_panel_circuit(tmp_path: Path) -> None:
    """A note in another column or a separated block is not a schedule row."""
    body = (
        b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-99) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 525 Tm (1 RECEPTACLE LOAD) Tj ET\n"
    )
    for label, extra in (
        ("control", b""),
        ("unrelated-99", b"BT /F1 8 Tf 1 0 0 1 60 120 Tm (99 DETAIL NOTE) Tj ET\n"),
        ("unrelated-42", b"BT /F1 8 Tf 1 0 0 1 60 120 Tm (42 DETAIL NOTE) Tj ET\n"),
        ("aligned-but-separated", b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"),
        ("aligned-before-row", b"BT /F1 8 Tf 1 0 0 1 600 540 Tm (99 DETAIL NOTE) Tj ET\n"),
        ("aligned-near-row", b"BT /F1 8 Tf 1 0 0 1 600 528 Tm (99 DETAIL NOTE) Tj ET\n"),
        (
            "boxed-note-with-page-border",
            b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
            b"595 115 145 10 re S\n"
            b"5 5 780 600 re S\n",
        ),
        (
            "full-column-box-with-page-border",
            b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
            b"590 115 160 10 re S\n"
            b"5 5 780 600 re S\n",
        ),
        (
            "full-column-box-with-notes-frame",
            b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
            b"590 115 160 10 re S\n"
            b"590 100 160 460 re S\n",
        ),
    ):
        model = _circuit_probe_model(
            tmp_path / f"schedule-note-{label}.pdf",
            body + extra,
            f"fixture:issue72-schedule-note-{label}",
        )
        assert model.circuits == (), label
        assert any(
            row.get("source_text") == "LP-99"
            and row.get("reason_code") == "circuit_outside_panel_schedule"
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        ), label

    spare = _circuit_probe_model(
        tmp_path / "ruled-spare-row.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 525 Tm (1 SPARE) Tj ET\n"
        b"590 530 160 30 re S\n"
        b"590 520 160 10 re S\n"
        b"590 520 160 40 re S\n",
        "fixture:issue72-ruled-spare-row",
        schedule_cells=False,
    )
    assert [circuit.circuit_number for circuit in spare.circuits] == ["1"]

    valid = _circuit_probe_model(
        tmp_path / "schedule-cell-real-circuit.pdf",
        body.replace(b"(LP-99)", b"(LP-1)"),
        "fixture:issue72-schedule-cell-real-circuit",
    )
    assert [circuit.circuit_number for circuit in valid.circuits] == ["1"]

    distant_row = _circuit_probe_model(
        tmp_path / "distant-cell-plus-numbered-note.pdf",
        body.replace(b"600 525 Tm (1 RECEPTACLE LOAD)", b"600 250 Tm (1 RECEPTACLE LOAD)")
        + b"BT /F1 8 Tf 1 0 0 1 600 10 Tm (99 DETAIL NOTE) Tj ET\n",
        "fixture:issue72-distant-cell-plus-numbered-note",
    )
    assert distant_row.circuits == ()
    assert any(
        row.get("source_text") == "LP-99"
        and row.get("reason_code") == "circuit_outside_panel_schedule"
        for row in distant_row.attributes["pdf_electrical"]["unresolved_circuits"]
    )

    # A tall same-width notes frame is not schedule ownership by itself,
    # even when it contains the heading and a boxed numbered note.
    standalone = _circuit_probe_model(
        tmp_path / "standalone-notes-frame.pdf",
        body
        + b"590 530 160 30 re S\n"
        + b"590 520 160 10 re S\n"
        + b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
        + b"590 115 160 10 re S\n"
        + b"590 100 160 460 re S\n",
        "fixture:issue72-standalone-notes-frame",
        schedule_cells=False,
    )
    assert standalone.circuits == ()
    assert any(
        row.get("source_text") == "LP-99"
        and row.get("reason_code") == "circuit_outside_panel_schedule"
        for row in standalone.attributes["pdf_electrical"]["unresolved_circuits"]
    )
    standalone_valid = _circuit_probe_model(
        tmp_path / "standalone-notes-frame-valid.pdf",
        body.replace(b"(LP-99)", b"(LP-1)")
        + b"590 530 160 30 re S\n"
        + b"590 520 160 10 re S\n"
        + b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
        + b"590 115 160 10 re S\n"
        + b"590 100 160 460 re S\n",
        "fixture:issue72-standalone-notes-frame-valid",
        schedule_cells=False,
    )
    assert [circuit.circuit_number for circuit in standalone_valid.circuits] == ["1"]


def test_moving_a_complete_ruled_schedule_does_not_change_its_circuit(
    tmp_path: Path,
) -> None:
    """Source-drawn grid edges, not a point-distance cutoff, own the row."""
    for y in (525, 524, 250):
        model = _circuit_probe_model(
            tmp_path / f"translated-ruled-schedule-{y}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
            b"175 445 10 10 re S\n"
            b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-1) Tj ET\n"
            b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
            + f"BT /F1 8 Tf 1 0 0 1 600 {y} Tm (1 RECEPTACLE LOAD) Tj ET\n".encode(),
            f"fixture:issue72-translated-ruled-schedule-{y}",
        )
        assert [circuit.circuit_number for circuit in model.circuits] == ["1"], y


def test_ruled_note_block_is_not_a_panel_schedule_load_row(tmp_path: Path) -> None:
    """A note label stays a note even inside a schedule-shaped outline."""
    for label, extra in (
        ("bare", b""),
        ("general-notes", b"BT /F1 8 Tf 1 0 0 1 600 300 Tm (GENERAL NOTES) Tj ET\n"),
    ):
        model = _circuit_probe_model(
            tmp_path / f"ruled-note-block-{label}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
            b"175 445 10 10 re S\n"
            b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-99) Tj ET\n"
            b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 600 120 Tm (99 DETAIL NOTE) Tj ET\n"
            b"590 125 160 435 re S\n"
            b"590 115 160 10 re S\n"
            b"590 100 160 460 re S\n"
            + extra,
            f"fixture:issue72-ruled-note-block-{label}",
            schedule_cells=False,
        )
        assert model.circuits == (), label
        assert any(
            row.get("source_text") == "LP-99"
            and row.get("reason_code") == "circuit_outside_panel_schedule"
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        ), label


def test_unruled_numeric_text_cannot_validate_a_present_schedule(tmp_path: Path) -> None:
    """A heading plus a number or isolated box do not prove table ownership."""
    body = (
        b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 525 Tm (1 RECEPTACLE LOAD) Tj ET\n"
    )
    for label, extra in (
        ("unruled", b""),
        ("isolated-box", b"595 520 145 10 re S\n"),
    ):
        model = _circuit_probe_model(
            tmp_path / f"{label}-schedule-number.pdf",
            body + extra,
            f"fixture:issue72-{label}-schedule-number",
            schedule_cells=False,
        )
        assert model.circuits == (), label
        assert any(
            row.get("source_text") == "LP-1"
            and row.get("reason_code") == "circuit_outside_panel_schedule"
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        ), label


def test_unparsed_present_schedule_does_not_become_absent(tmp_path: Path) -> None:
    """A visible schedule with no parseable rows cannot waive validation."""
    model = _circuit_probe_model(
        tmp_path / "schedule-heading-without-rows.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 560 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 9 Tf 1 0 0 1 192 451 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 550 Tm (PANEL LP SCHEDULE) Tj ET\n",
        "fixture:issue72-schedule-heading-without-rows",
    )
    assert model.circuits == ()
    assert any(
        row.get("source_text") == "LP-1"
        and row.get("reason_code") == "circuit_outside_panel_schedule"
        for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
    )


def test_arrow_and_branch_scale_do_not_set_hard_cutoffs(tmp_path: Path) -> None:
    """V shape and positive branch length matter, not a fixed point size."""
    for name, end_x, arrow in (
        ("short-branch", 183.9, b"178 448 m 183.9 450 l 178 452 l S\n"),
        ("small-arrow-base", 300.0, b"294 448.05 m 300 450 l 294 451.95 l S\n"),
        ("large-arrow-legs", 300.0, b"286 448 m 300 450 l 286 452 l S\n"),
    ):
        model = _circuit_probe_model(
            tmp_path / f"scaled-{name}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
            b"175 445 10 10 re S\n"
            + f"180 450 m {end_x} 450 l S\n".encode()
            + arrow
            + f"BT /F1 9 Tf 1 0 0 1 {end_x + 5} 452 Tm (LP-1) Tj ET\n".encode()
            + b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
            f"fixture:issue72-scaled-{name}",
        )
        assert [circuit.circuit_number for circuit in model.circuits] == ["1"], name


def test_direct_tag_beyond_old_fixed_radius_can_resolve(tmp_path: Path) -> None:
    """A corroborated direct tag uses the lane's annotation association rule."""
    model = _circuit_probe_model(
        tmp_path / "direct-tag-dx-43.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 9 Tf 1 0 0 1 213 451 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-direct-tag-dx-43",
    )
    assert [circuit.circuit_number for circuit in model.circuits] == ["1"]


def test_lawful_tee_serving_two_devices_still_resolves(tmp_path: Path) -> None:
    """Branch wiring tees. That must not be mistaken for architecture.

    A run that splits to serve two devices is ordinary, lawful wiring. An
    earlier version of the isolation guard rejected any component containing a
    degree-3 junction, which is exactly what a tee is, so it discarded valid
    circuits the same way the arbitrary size and reach cutoffs did before it.

    The guard now keys on cycles instead: wiring distributes radially and never
    loops back on itself, while architectural linework encloses space. This
    pins the lawful side of that distinction.
    """
    model = _circuit_probe_model(
        tmp_path / "lawful-tee.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 540 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 170 462 Tm (EVSE-1) Tj ET\n"
        b"175 445 10 10 re S\n"
        b"BT /F1 8 Tf 1 0 0 1 170 392 Tm (EVSE-2) Tj ET\n"
        b"175 375 10 10 re S\n"
        b"180 450 m 260 450 l S\n"
        b"180 380 m 260 380 l S\n"
        b"260 450 m 260 380 l S\n"
        b"260 415 m 340 415 l S\n"
        b"334 411 m 340 415 l 334 419 l S\n"
        b"BT /F1 9 Tf 1 0 0 1 345 417 Tm (LP-1) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 600 540 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 600 515 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        "fixture:issue72-lawful-tee",
    )

    assert {circuit.circuit_number for circuit in model.circuits} == {"1"}
    circuit = model.circuits[0]
    assert len(circuit.load_port_ids) == 2, (
        "both devices on the teed run belong to the circuit"
    )
    assert not [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        if row.get("reason_code") == "branch_run_not_isolated"
    ]


def test_tee_drawn_as_three_segments_from_one_point_still_resolves(
    tmp_path: Path,
) -> None:
    """The natural way to draw a tee must resolve too.

    Three segments leaving a single point is how a tap is normally drawn, and
    it is the case that killed the first two versions of this guard. Each
    segment touches the other two, so a graph whose nodes are VECTORS sees a
    triangle and calls it a loop. With contact points as nodes it is one node
    of degree three: a tree, and lawful.
    """
    model = _circuit_probe_model(
        tmp_path / "tee-three-segments-one-point.pdf",
        b"BT /F1 10 Tf 1 0 0 1 70 600 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
        b"BT /F1 10 Tf 1 0 0 1 700 600 Tm (PANEL LP SCHEDULE) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 700 575 Tm (1 RECEPTACLE LOAD) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 130 417 Tm (EVSE-1) Tj ET\n"
        b"135 395 10 10 re S\n"
        b"140 400 m 220 400 l S\n"
        b"220 400 m 300 460 l S\n"
        b"220 400 m 300 340 l S\n"
        b"294 456 m 300 460 l 292 461 l S\n"
        b"BT /F1 9 Tf 1 0 0 1 308 462 Tm (LP-1) Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 295 337 Tm (EVSE-2) Tj ET\n"
        b"295 335 10 10 re S\n",
        "fixture:issue72-tee-three-segments",
    )

    assert model.circuits, (
        "three segments leaving one point is a tap, not an enclosure"
    )
    assert not [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
        if row.get("reason_code") == "branch_run_not_isolated"
    ]


def test_collinear_run_with_a_drafting_gap_is_not_read_as_a_loop(
    tmp_path: Path,
) -> None:
    """A small gap in one run must not look like an enclosure.

    Component assembly joins path ends within the snap tolerance, so two
    collinear segments with a drafting gap are one run, and the isolation check
    has to agree. Clustering their contact points by rounding into fixed bins
    does not agree: two points a hair under the tolerance apart can straddle a
    bin boundary, split into two nodes, and turn that single run into a
    two-edge "loop" which is then rejected.

    Geometry from the review that found it. The gaps sit either side of the
    boundary on purpose.
    """
    for gap_start, gap_end in ((180.0, 183.5), (181.0, 184.0), (180.0, 180.5)):
        model = _circuit_probe_model(
            tmp_path / f"drafting-gap-{gap_start}-{gap_end}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 55 650 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 10 Tf 1 0 0 1 700 650 Tm (PANEL LP SCHEDULE) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 700 625 Tm (1 RECEPTACLE LOAD) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 110 400 Tm (EVSE-1) Tj ET\n"
            b"105 390 10 10 re S\n"
            + f"110 400 m {gap_start} 400 l S\n".encode()
            + f"{gap_end} 400 m 260 400 l S\n".encode()
            + b"254 396 m 260 400 l 254 404 l S\n"
            b"BT /F1 9 Tf 1 0 0 1 268 402 Tm (LP-1) Tj ET\n",
            f"fixture:issue72-drafting-gap-{gap_start}-{gap_end}",
        )
        gap = round(gap_end - gap_start, 2)
        assert model.circuits, f"a {gap} pt gap broke one run into a loop"
        assert not [
            row
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
            if row.get("reason_code") == "branch_run_not_isolated"
        ], f"a {gap} pt gap was misread as enclosed area"


def test_overprinted_duplicate_strokes_do_not_read_as_an_enclosure(
    tmp_path: Path,
) -> None:
    """The same edge drawn twice encloses nothing.

    Exported CAD routinely paints a line more than once, sometimes offset by a
    fraction of a point. Those are parallel edges between the same two nodes,
    and parallel edges bound zero area, so they must not be mistaken for a
    loop.

    Two things make that work: each member contributes its own midpoint as a
    node, so coincident strokes collapse onto one edge while two genuinely
    different paths between the same ends keep distinct midpoints; and an edge
    already drawn is not counted a second time.
    """
    for label, second in (
        ("exact", "140 400 m 220 400 l S\n"),
        ("offset-1pt", "140 401 m 220 401 l S\n"),
    ):
        model = _circuit_probe_model(
            tmp_path / f"duplicate-stroke-{label}.pdf",
            b"BT /F1 10 Tf 1 0 0 1 55 650 Tm (PANEL LP 120/208V 3PH) Tj ET\n"
            b"BT /F1 10 Tf 1 0 0 1 800 650 Tm (PANEL LP SCHEDULE) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 800 625 Tm (1 RECEPTACLE LOAD) Tj ET\n"
            b"BT /F1 8 Tf 1 0 0 1 130 404 Tm (EVSE-1) Tj ET\n"
            b"135 395 10 10 re S\n"
            b"140 400 m 220 400 l S\n"
            + second.encode()
            + b"214 396 m 220 400 l 214 404 l S\n"
            b"BT /F1 9 Tf 1 0 0 1 228 402 Tm (LP-1) Tj ET\n",
            f"fixture:issue72-duplicate-stroke-{label}",
        )

        assert model.circuits, f"{label} duplicate stroke was read as an enclosure"
        assert not [
            row
            for row in model.attributes["pdf_electrical"]["unresolved_circuits"]
            if row.get("reason_code") == "branch_run_not_isolated"
        ], f"{label} duplicate stroke wrongly rejected"


def test_branch_run_absorbed_into_a_wall_network_fails_closed(
    tmp_path: Path,
) -> None:
    """A run that has walked into the architecture must never become a circuit.

    Geometry taken from the independent review that found this defect: a short
    real branch crosses an OPEN wall grid carrying eleven further recognized
    loads. Without the guard the importer produces one circuit with twelve
    loads and no diagnostic at all.

    Two things this pins deliberately. The linework is open, so a rule that
    only inspected closed paths missed it, which is what the first version of
    this guard did. And the reject is structural rather than a size cutoff:
    branch wiring is radial and never closes a loop, while a wall grid
    encloses space and therefore does.

    Must not depend on any pilot's counts, and must keep holding until the
    full discriminator in #76 lands.
    """
    body = [
        b"BT /F1 10 Tf 1 0 0 1 70 600 Tm (PANEL LP 120/208V 3PH) Tj ET\n",
        b"BT /F1 10 Tf 1 0 0 1 700 600 Tm (PANEL LP SCHEDULE) Tj ET\n",
        b"BT /F1 8 Tf 1 0 0 1 700 575 Tm (1 RECEPTACLE LOAD) Tj ET\n",
        # the genuine short branch, arrowed and annotated
        b"BT /F1 8 Tf 1 0 0 1 130 417 Tm (EVSE-1) Tj ET\n",
        b"135 395 10 10 re S\n",
        b"140 400 m 230 400 l S\n",
        b"224 396 m 230 400 l 224 404 l S\n",
        b"BT /F1 9 Tf 1 0 0 1 238 402 Tm (LP-1) Tj ET\n",
    ]
    # An open wall mesh the branch crosses. No closed paths anywhere in it.
    for x in (180, 260, 340, 420, 500, 580, 660):
        body.append(f"{x} 260 m {x} 540 l S\n".encode())
    for y in (300, 400, 500):
        body.append(f"180 {y} m 660 {y} l S\n".encode())
    # Further recognized loads sitting on the grid, which a bogus circuit
    # would sweep up.
    positions = [
        (255, 300), (335, 300), (415, 300), (495, 300),
        (575, 300), (655, 300), (255, 500), (335, 500),
        (415, 500), (495, 500), (575, 500),
    ]
    for index, (cx, cy) in enumerate(positions, start=2):
        body.append(
            f"BT /F1 8 Tf 1 0 0 1 {cx - 5} {cy + 17} Tm (EVSE-{index}) Tj ET\n".encode()
        )
        body.append(f"{cx - 5} {cy - 5} 10 10 re S\n".encode())

    model = _circuit_probe_model(
        tmp_path / "branch-absorbed-into-wall-network.pdf",
        b"".join(body),
        "fixture:issue72-branch-absorbed-into-network",
    )

    # Twelve devices are recognized; the point is that none of them get
    # circuited off geometry that is not wiring.
    assert len(model.electrical_devices) == 12
    assert model.circuits == (), (
        "a run absorbed into looping architecture must not circuit the devices "
        "that sprawl happened to cover"
    )
    assert model.ports == ()
    misses = model.attributes["pdf_electrical"]["unresolved_circuits"]
    rejected = [
        row for row in misses if row.get("reason_code") == "branch_run_not_isolated"
    ]
    assert rejected, "the absorbed run must be rejected explicitly, not silently"
    assert all(row.get("cycle_element_id") for row in rejected)


def test_circuit_homerun_fixture_is_a_structurally_valid_pdf() -> None:
    """The committed fixture must parse without reader error recovery.

    It was previously written with a wrong ``/Length`` and an unusable xref,
    so ``extract_pdf`` only succeeded because pypdf silently repaired it.
    """
    reader = PdfReader(str(CIRCUIT_HOMERUN_FIXTURE), strict=True)
    assert len(reader.pages) == 1


def test_unresolved_legend_glyph_records_nearest_two_match_diagnostics() -> None:
    extracted = extract_pdf(
        LEGEND_SHAPE_FIXTURE,
        source_id="fixture:legend-shape-match-diagnostics",
    )
    model = ElectricalPdfImporter().import_document(extracted)
    unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("kind") == "vector_cluster"
        and item.get("reason")
        == "glyph cluster has no unique matching type in the sheet legend"
    ]
    assert unresolved
    for item in unresolved:
        diagnostics = item["match_diagnostics"]
        assert diagnostics == item["recognition_provenance"]["match_diagnostics"]
        assert diagnostics["nearest_prototype"] is not None
        assert diagnostics["score"] is not None
        assert diagnostics["second_best"] is not None
        assert diagnostics["non_unique_reason"]


@pytest.mark.parametrize(
    "fixture",
    tuple(sorted(FIXTURE_DIR.glob("*.pdf")))
    + tuple(sorted(FIXTURE_DIR.glob("*.json"))),
    ids=lambda path: path.name,
)
def test_every_unresolved_vector_cluster_has_issue68_match_diagnostics(
    fixture: Path,
) -> None:
    document = (
        extract_pdf(fixture, source_id=f"fixture:{fixture.stem}:issue68-diagnostics")
        if fixture.suffix == ".pdf"
        else PdfElectricalDocument.from_dict(
            json.loads(fixture.read_text(encoding="utf-8"))
        )
    )
    model = ElectricalPdfImporter().import_document(document)
    unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("kind") == "vector_cluster"
    ]
    required = {
        "nearest_type",
        "nearest_score",
        "second_type",
        "second_score",
        "threshold",
        "absolute_floor",
        "margin_threshold",
        "strong_score_threshold",
        "margin",
        "confidence",
        "reason",
        "bbox_size_pt",
        "stroke_count",
    }
    allowed_reasons = {
        "below threshold",
        "tie within margin",
        "cluster too large",
        "cluster too small",
        "contains text",
    }
    for item in unresolved:
        diagnostics = item["match_diagnostics"]
        assert required <= diagnostics.keys()
        assert diagnostics["threshold"] == pdf_electrical_importer._GLYPH_MATCH_SCORE_MIN
        assert (
            diagnostics["absolute_floor"]
            == pdf_electrical_importer._GLYPH_MATCH_ABSOLUTE_FLOOR
        )
        assert (
            diagnostics["margin_threshold"]
            == pdf_electrical_importer._GLYPH_MATCH_MARGIN_MIN
        )
        assert (
            diagnostics["strong_score_threshold"]
            == pdf_electrical_importer._GLYPH_MATCH_STRONG_SCORE
        )
        assert diagnostics["confidence"] == {
            "score": diagnostics["score"],
            "margin": diagnostics["margin"],
        }
        assert diagnostics["reason"] in allowed_reasons
        assert set(diagnostics["bbox_size_pt"]) == {"width", "height"}
        assert diagnostics["bbox_size_pt"]["width"] >= 0.0
        assert diagnostics["bbox_size_pt"]["height"] >= 0.0
        assert diagnostics["stroke_count"] >= 1


@pytest.mark.parametrize(
    ("code", "expected_type", "x_pt", "y_pt"),
    (
        ("J", "junction_box_power", 84.0, 280.0),
        ("JB", "junction_box_power", 84.0, 280.0),
        ("D", "data_outlet", 84.0, 390.0),
        ("DATA", "data_outlet", 84.0, 390.0),
        ("QUADRUPLEX", "receptacle_quad", 300.0, 500.0),
    ),
)
def test_square_annotation_legend_codes_classify_without_text_duplicates(
    code: str,
    expected_type: str,
    x_pt: float,
    y_pt: float,
) -> None:
    extracted = extract_pdf(
        NOTES_COLUMN_LEGEND_FIXTURE,
        source_id=f"fixture:annotation-code-{code.lower()}",
    )
    element_id = f"p1:annotation:issue68:{code.lower()}"
    symbol = PdfSymbolObservation(
        element_id=element_id,
        page=1,
        name="/Square",
        x_pt=x_pt,
        y_pt=y_pt,
        source_kind="annotation:square",
        metadata={"contents": code},
    )
    code_text = PdfTextObservation(
        element_id=f"{element_id}:text",
        page=1,
        text=code,
        x_pt=x_pt,
        y_pt=y_pt,
    )
    document = replace(
        extracted,
        symbols=tuple((*extracted.symbols, symbol)),
        texts=tuple((*extracted.texts, code_text)),
    )
    model = ElectricalPdfImporter().import_document(document)
    matched = [
        device
        for device in model.electrical_devices
        if any(
            provenance.source_element_id == element_id
            and provenance.method == "annotation-code"
            for provenance in device.provenance
        )
    ]
    assert len(matched) == 1
    assert matched[0].device_type == expected_type
    assert matched[0].attributes["pdf_electrical"]["annotation_code"] == code
    assert matched[0].attributes["pdf_electrical"]["status"] == "E"

    unresolved_ids = {
        item.get("source_element_id")
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
    }
    assert element_id not in unresolved_ids
    assert code_text.element_id not in unresolved_ids


def test_unmatched_square_annotation_code_remains_unresolved_with_code() -> None:
    extracted = extract_pdf(
        NOTES_COLUMN_LEGEND_FIXTURE,
        source_id="fixture:annotation-code-unmatched",
    )
    unmatched = PdfSymbolObservation(
        element_id="p1:annotation:9999",
        page=1,
        name="/Square",
        x_pt=520.0,
        y_pt=500.0,
        source_kind="annotation:square",
        metadata={"contents": "ZZ"},
    )
    document = replace(
        extracted,
        symbols=tuple((*extracted.symbols, unmatched)),
    )
    model = ElectricalPdfImporter().import_document(document)
    rows = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("source_element_id") == unmatched.element_id
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "symbol"
    assert rows[0]["annotation_code"] == "ZZ"
    assert rows[0]["annotation_code_recognition"]["normalized_code"] == "ZZ"
    assert rows[0]["status"] == "unresolved_classification"



@pytest.mark.parametrize(
    ("fixture", "rotation"),
    (
        (INNER_VIEW_BORDER_LEGEND_FIXTURE, 0),
        (INNER_VIEW_BORDER_ROTATED_LEGEND_FIXTURE, 270),
    ),
)
def test_notes_column_legend_uses_outer_sheet_border_not_inner_view_border(
    fixture: Path,
    rotation: int,
) -> None:
    assert fixture.exists()
    assert not fixture.with_suffix(".expected.json").exists()

    source_id = "fixture:geometry-only-power-sheet-inner-view-border-legend"
    extracted = extract_pdf(fixture, source_id=source_id)
    repeated = extract_pdf(fixture, source_id=source_id)
    assert extracted == repeated
    assert extracted.page_provenance == {
        1: {
            "page_rotation": rotation,
            "displayed_page_width_pt": 792.0,
            "displayed_page_height_pt": 612.0,
            "coordinate_space": "displayed",
        }
    }

    media_box = (0.0, 0.0, 792.0, 612.0)
    frame = pdf_electrical_importer._page_frame_bbox(
        extracted.vectors,
        page=1,
        media_box_pt=media_box,
    )
    assert frame == pytest.approx((18.0, 18.0, 774.0, 594.0))

    def vector_bbox(
        vector: PdfVectorPathObservation,
    ) -> tuple[float, float, float, float]:
        xs = [point[0] for point in vector.points_pt]
        ys = [point[1] for point in vector.points_pt]
        return min(xs), min(ys), max(xs), max(ys)

    # The plan viewport is intentionally the only large closed rectangle.
    # The actual sheet edge is four independent long rules.
    assert any(
        vector.closed
        and vector_bbox(vector) == pytest.approx((40.0, 260.0, 560.0, 580.0))
        for vector in extracted.vectors
    )
    sheet_edges = {
        ((18.0, 18.0), (774.0, 18.0)),
        ((774.0, 18.0), (774.0, 594.0)),
        ((774.0, 594.0), (18.0, 594.0)),
        ((18.0, 594.0), (18.0, 18.0)),
    }
    extracted_open_edges = {
        vector.points_pt
        for vector in extracted.vectors
        if not vector.closed and vector.points_pt in sheet_edges
    }
    assert extracted_open_edges == sheet_edges

    model = ElectricalPdfImporter().import_document(extracted)
    lane = model.attributes["pdf_electrical"]
    assert len(model.electrical_devices) == 6
    assert len(model.electrical_devices) > 2
    assert {
        device.device_type
        for device in model.electrical_devices
    } == {
        "receptacle",
        "junction_box",
        "luminaire",
        "disconnect",
        "switch",
        "evse",
    }
    regions = lane["legend_recognition"]["regions"]
    assert len(regions) == 1
    assert regions[0]["method"] == "symbol-function-table"
    assert regions[0]["heading_text"] == "LEGEND"
    assert regions[0]["row_count"] == 6
    assert regions[0]["classified_row_count"] == 6
    assert lane["legend_recognition"]["frame_rederivations"] == []
    validate_model(model)


def test_page_frame_falls_back_to_media_box_when_only_inner_view_border_exists() -> None:
    extracted = extract_pdf(
        INNER_VIEW_BORDER_LEGEND_FIXTURE,
        source_id="fixture:inner-view-border-media-fallback",
    )
    outer_edges = {
        ((18.0, 18.0), (774.0, 18.0)),
        ((774.0, 18.0), (774.0, 594.0)),
        ((774.0, 594.0), (18.0, 594.0)),
        ((18.0, 594.0), (18.0, 18.0)),
    }
    without_sheet_border = tuple(
        vector
        for vector in extracted.vectors
        if vector.points_pt not in outer_edges
    )

    frame = pdf_electrical_importer._page_frame_bbox(
        without_sheet_border,
        page=1,
        media_box_pt=(0.0, 0.0, 792.0, 612.0),
    )
    assert frame == (0.0, 0.0, 792.0, 612.0)


def _with_zero_page_rotation(value):
    if isinstance(value, dict):
        return {
            key: (
                0
                if key == "page_rotation"
                else _with_zero_page_rotation(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_with_zero_page_rotation(item) for item in value]
    return value


def _device_rotation_signature(
    model: BuildingModel,
) -> list[tuple[str, str | None, str | None]]:
    return sorted(
        (
            device.device_type,
            device.attributes["pdf_electrical"].get("status"),
            device.attributes["pdf_electrical"].get("status_meaning"),
        )
        for device in model.electrical_devices
    )


@pytest.mark.parametrize(
    ("fixture_stem", "original_fixture"),
    ROTATED_LEGEND_FIXTURES,
)
@pytest.mark.parametrize("rotation", (90, 270))
def test_rotated_power_sheet_matches_unrotated_displayed_space(
    fixture_stem: str,
    original_fixture: Path,
    rotation: int,
) -> None:
    rotated_fixture = FIXTURE_DIR / f"{fixture_stem}-rotate-{rotation}.pdf"
    assert rotated_fixture.exists()
    assert not rotated_fixture.with_suffix(".expected.json").exists()

    source_id = f"fixture:{fixture_stem}:rotation-regression"
    original = extract_pdf(original_fixture, source_id=source_id)
    rotated = extract_pdf(rotated_fixture, source_id=source_id)

    assert original.page_provenance == {
        1: {
            "page_rotation": 0,
            "displayed_page_width_pt": 792.0,
            "displayed_page_height_pt": 612.0,
            "coordinate_space": "displayed",
        }
    }
    assert rotated.page_provenance == {
        1: {
            "page_rotation": rotation,
            "displayed_page_width_pt": 792.0,
            "displayed_page_height_pt": 612.0,
            "coordinate_space": "displayed",
        }
    }

    # The copies store their source content sideways and use /Rotate to display
    # exactly like the original. Extraction must erase that storage difference
    # before legend detection, clustering, and geometry identity see it.
    assert replace(rotated, page_provenance=original.page_provenance) == original

    original_model = ElectricalPdfImporter().import_document(original)
    rotated_model = ElectricalPdfImporter().import_document(rotated)

    assert (
        rotated_model.attributes["pdf_electrical"]["legend_recognition"]
        == original_model.attributes["pdf_electrical"]["legend_recognition"]
    )
    assert len(rotated_model.electrical_devices) == len(
        original_model.electrical_devices
    )
    assert _device_rotation_signature(rotated_model) == _device_rotation_signature(
        original_model
    )

    rotated_page_provenance = [
        provenance
        for provenance in rotated_model.provenance
        if provenance.method == "pypdf-page-display-normalization"
    ]
    assert len(rotated_page_provenance) == 1
    assert rotated_page_provenance[0].page == 1
    assert rotated_page_provenance[0].attributes["page_rotation"] == rotation

    # This compares canonical IDs, entity provenance, legend provenance,
    # status provenance, source geometry fingerprints, and all other model data.
    # Only the source page's recorded rotation may differ.
    assert _with_zero_page_rotation(rotated_model.to_dict()) == (
        _with_zero_page_rotation(original_model.to_dict())
    )
    validate_model(rotated_model)


def test_explicit_page_transform_is_applied_after_display_rotation_normalization() -> None:
    source_id = "fixture:notes-column-explicit-displayed-transform"
    original = extract_pdf(NOTES_COLUMN_LEGEND_FIXTURE, source_id=source_id)
    rotated = extract_pdf(
        FIXTURE_DIR / "geometry-only-power-sheet-notes-column-legend-rotate-270.pdf",
        source_id=source_id,
    )
    frame_id = stable_id("frame", source_id)
    transform = PdfPageTransform(
        frame_id=frame_id,
        m11_m_per_pt=0.0,
        m12_m_per_pt=-POINT_TO_M,
        m21_m_per_pt=POINT_TO_M,
        m22_m_per_pt=0.0,
        tx_m=17.0,
        ty_m=23.0,
    )

    original_model = ElectricalPdfImporter().import_document(
        original,
        page_transforms={1: transform},
    )
    rotated_model = ElectricalPdfImporter().import_document(
        rotated,
        page_transforms={1: transform},
    )

    assert _with_zero_page_rotation(rotated_model.to_dict()) == (
        _with_zero_page_rotation(original_model.to_dict())
    )
    assert rotated_model.coordinate_system.frame_id == frame_id
    assert rotated_model.attributes["pdf_electrical"]["page_transforms_supplied"] is True
    assert rotated_model.attributes["pdf_electrical"]["registration_mode"] == (
        "explicit-page-transforms"
    )


def test_cad_export_bezier_and_filled_paths_survive_electrical_extraction() -> None:
    document = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )
    repeated = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )

    assert document == repeated
    assert not document.texts
    assert not document.symbols
    assert len(document.vectors) == 11
    assert [vector.metadata["paint_operator"] for vector in document.vectors] == [
        *("S",) * 9,
        "f",
        "f*",
    ]

    curve = document.vectors[0]
    assert curve.metadata["geometry_kind"] == "bezier-flattened"
    assert curve.metadata["curve_flatten_steps"] == 8
    assert curve.metadata["curve_commands"] == [
        {
            "operator": "c",
            "control_points_pt": [[34.0, 24.0], [48.0, 38.0]],
            "end_pt": [66.0, 38.0],
        },
        {
            "operator": "c",
            "control_points_pt": [[78.0, 38.0], [90.0, 24.0]],
            "end_pt": [108.0, 24.0],
        },
    ]
    assert len(curve.points_pt) == 17
    assert curve.points_pt[0] == (18.0, 24.0)
    assert curve.points_pt[4] == pytest.approx((41.25, 31.0))
    assert curve.points_pt[8] == (66.0, 38.0)
    assert curve.points_pt[12] == pytest.approx((84.75, 31.0))
    assert curve.points_pt[-1] == (108.0, 24.0)
    assert curve.points_pt[4] != pytest.approx((42.0, 31.0))

    wall_faces = document.vectors[1:9]
    assert all(vector.closed is False for vector in wall_faces)
    assert {vector.points_pt for vector in wall_faces} == {
        ((40.0, 40.0), (240.0, 40.0)),
        ((40.0, 44.0), (240.0, 44.0)),
        ((236.0, 40.0), (236.0, 140.0)),
        ((240.0, 40.0), (240.0, 140.0)),
        ((40.0, 136.0), (240.0, 136.0)),
        ((40.0, 140.0), (240.0, 140.0)),
        ((40.0, 40.0), (40.0, 140.0)),
        ((44.0, 40.0), (44.0, 140.0)),
    }

    assert document.vectors[9].closed is True
    assert document.vectors[9].points_pt == (
        (132.0, 24.0),
        (154.0, 24.0),
        (154.0, 46.0),
        (132.0, 46.0),
    )
    filled_curve = document.vectors[10]
    assert filled_curve.closed is True
    assert filled_curve.metadata["geometry_kind"] == "bezier-flattened"
    assert filled_curve.metadata["curve_commands"] == [
        {
            "operator": "c",
            "control_points_pt": [[42.0, 118.0], [66.0, 118.0]],
            "end_pt": [84.0, 92.0],
        },
        {
            "operator": "c",
            "control_points_pt": [[102.0, 66.0], [126.0, 66.0]],
            "end_pt": [144.0, 92.0],
        },
    ]
    assert len(filled_curve.points_pt) == 19
    assert filled_curve.points_pt[0] == (24.0, 92.0)
    assert filled_curve.points_pt[4] == pytest.approx((54.0, 111.5))
    assert filled_curve.points_pt[8] == (84.0, 92.0)
    assert filled_curve.points_pt[12] == pytest.approx((114.0, 72.5))
    assert filled_curve.points_pt[16:] == (
        (144.0, 92.0),
        (144.0, 124.0),
        (24.0, 124.0),
    )


def test_bezier_vector_is_not_reinterpreted_as_straight_topology() -> None:
    extracted = extract_pdf(
        CAD_GEOMETRY_FIXTURE,
        source_id="fixture:cad-export-geometry-only",
    )
    curve = extracted.vectors[0]
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:bezier-topology-fail-closed",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:panel",
                    "page": 1,
                    "text": "PANEL LP",
                    "x_pt": 18.0,
                    "y_pt": 24.0,
                },
                {
                    "element_id": "p1:text:load",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 108.0,
                    "y_pt": 24.0,
                },
            ],
            "vectors": [
                {
                    "element_id": curve.element_id,
                    "page": curve.page,
                    "points_pt": [list(point) for point in curve.points_pt],
                    "closed": curve.closed,
                    "source_kind": curve.source_kind,
                    "metadata": dict(curve.metadata),
                }
            ],
        }
    )

    model = ElectricalPdfImporter().import_document(document)

    assert len(model.electrical_equipment) == 1
    assert len(model.electrical_devices) == 1
    assert not model.ports
    assert not model.circuits
    assert not model.routes
    assert model.attributes["pdf_electrical"]["unresolved_topology"] == []


def test_v_and_y_bezier_controls_survive_extraction(tmp_path: Path) -> None:
    path = tmp_path / "bezier-shorthands.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=160, height=100)
    content = DecodedStreamObject()
    content.set_data(
        b"10 10 m 20 30 40 40 v S\n"
        b"60 10 m 70 30 90 40 y S\n"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)

    document = extract_pdf(path, source_id="fixture:bezier-shorthands")

    assert len(document.vectors) == 2
    first, second = document.vectors
    assert first.metadata["curve_commands"] == [
        {
            "operator": "v",
            "control_points_pt": [[10.0, 10.0], [20.0, 30.0]],
            "end_pt": [40.0, 40.0],
        }
    ]
    assert second.metadata["curve_commands"] == [
        {
            "operator": "y",
            "control_points_pt": [[70.0, 30.0], [90.0, 40.0]],
            "end_pt": [90.0, 40.0],
        }
    ]
    assert len(first.points_pt) == 9
    assert len(second.points_pt) == 9


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


def test_instance_hint_rejects_mismatched_source_type() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:hint-source-type-mismatch",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:light",
                    "page": 1,
                    "text": "LIGHT",
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
            ],
        }
    )
    hint = ElectricalInstanceHint(
        identity_key="receptacle:one",
        page=1,
        entity_kind="device",
        canonical_type="receptacle",
        x_pt=100.0,
        y_pt=100.0,
        source_element_id="p1:text:light",
    )

    model = ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)

    assert not model.electrical_devices
    unresolved = model.attributes["pdf_electrical"]["unresolved_observations"]
    rejected = [
        item
        for item in unresolved
        if item.get("status") == "rejected_instance_hint"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "claimed source semantics do not match instance hint"
    assert any(
        item.get("source_element_id") == "p1:text:light"
        and item.get("status") == "unresolved_identity"
        for item in unresolved
    )


def test_instance_hint_rejects_mismatched_source_location() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:hint-source-location-mismatch",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:receptacle",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
            ],
        }
    )
    hint = ElectricalInstanceHint(
        identity_key="receptacle:one",
        page=1,
        entity_kind="device",
        canonical_type="receptacle",
        x_pt=140.0,
        y_pt=100.0,
        source_element_id="p1:text:receptacle",
    )

    model = ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)

    assert not model.electrical_devices
    unresolved = model.attributes["pdf_electrical"]["unresolved_observations"]
    rejected = [
        item
        for item in unresolved
        if item.get("status") == "rejected_instance_hint"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "claimed source position does not agree with instance hint"
    assert any(
        item.get("source_element_id") == "p1:text:receptacle"
        and item.get("status") == "unresolved_identity"
        for item in unresolved
    )


def test_instance_hint_duplicate_source_claims_fail_closed_deterministically() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:duplicate-hint-source-claim",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:receptacle",
                    "page": 1,
                    "text": "GFCI",
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
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
            y_pt=100.0,
            source_element_id="p1:text:receptacle",
        ),
        ElectricalInstanceHint(
            identity_key="receptacle:right",
            page=1,
            entity_kind="device",
            canonical_type="receptacle",
            tag="R-RIGHT",
            x_pt=100.0,
            y_pt=100.0,
            source_element_id="p1:text:receptacle",
        ),
    )

    first = ElectricalPdfImporter(instance_hints=hints).import_document(document)
    reordered = ElectricalPdfImporter(instance_hints=tuple(reversed(hints))).import_document(
        document
    )

    assert first.to_json() == reordered.to_json()
    assert not first.electrical_devices
    unresolved = first.attributes["pdf_electrical"]["unresolved_observations"]
    rejected = [
        item
        for item in unresolved
        if item.get("status") == "rejected_instance_hint"
    ]
    assert len(rejected) == 2
    assert {
        item["reason"] for item in rejected
    } == {"claimed source element is claimed by multiple instance hints"}
    assert all(
        item["claiming_hint_identity_keys"]
        == ["receptacle:left", "receptacle:right"]
        for item in rejected
    )
    assert any(
        item.get("source_element_id") == "p1:text:receptacle"
        and item.get("status") == "unresolved_identity"
        for item in unresolved
    )


def test_rejected_instance_hint_preserves_original_source_semantics() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:rejected-hint-preserves-source",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:panel",
                    "page": 1,
                    "text": "PANEL LP",
                    "x_pt": 10.0,
                    "y_pt": 10.0,
                }
            ],
        }
    )
    hint = ElectricalInstanceHint(
        identity_key="receptacle:one",
        page=1,
        entity_kind="device",
        canonical_type="receptacle",
        x_pt=10.0,
        y_pt=10.0,
        source_element_id="p1:text:panel",
    )

    model = ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)

    assert not model.electrical_devices
    assert len(model.electrical_equipment) == 1
    panel = model.electrical_equipment[0]
    assert panel.equipment_type == "panelboard"
    assert panel.name == "LP"
    assert {
        item.source_element_id for item in panel.provenance
    } == {"p1:text:panel"}
    assert panel.attributes["pdf_electrical"]["stable_identity_key"] == (
        "tag:equipment:panelboard:LP"
    )
    rejected = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("status") == "rejected_instance_hint"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "claimed source semantics do not match instance hint"


def test_instance_hint_rejects_source_that_already_has_stable_semantic_identity() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:hint-not-needed-for-stable-source",
            "page_count": 1,
            "texts": [
                {
                    "element_id": "p1:text:evse",
                    "page": 1,
                    "text": "EVSE-1",
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
            ],
        }
    )
    hint = ElectricalInstanceHint(
        identity_key="evse:caller-owned",
        page=1,
        entity_kind="device",
        canonical_type="evse",
        tag="HINTED-EVSE",
        x_pt=100.0,
        y_pt=100.0,
        source_element_id="p1:text:evse",
    )

    model = ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)

    assert [item.name for item in model.electrical_devices] == ["EVSE-1"]
    assert model.electrical_devices[0].attributes["pdf_electrical"][
        "stable_identity_key"
    ] == "tag:device:evse:EVSE-1"
    rejected = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if item.get("status") == "rejected_instance_hint"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == (
        "claimed source already has stable semantic identity and does not need an "
        "instance hint"
    )


def test_instance_hint_accepts_compatible_identityless_symbol_claim() -> None:
    document = PdfElectricalDocument.from_dict(
        {
            "source_id": "fixture:hinted-identityless-symbol",
            "page_count": 1,
            "symbols": [
                {
                    "element_id": "p1:symbol:evse",
                    "page": 1,
                    "name": "/EVSE1",
                    "x_pt": 100.0,
                    "y_pt": 100.0,
                }
            ],
        }
    )
    hint = ElectricalInstanceHint(
        identity_key="evse:west",
        page=1,
        entity_kind="device",
        canonical_type="evse",
        tag="EVSE-WEST",
        x_pt=100.0,
        y_pt=100.0,
        source_element_id="p1:symbol:evse",
        confidence=0.99,
    )

    model = ElectricalPdfImporter(instance_hints=(hint,)).import_document(document)

    assert [item.name for item in model.electrical_devices] == ["EVSE-WEST"]
    device = model.electrical_devices[0]
    assert device.attributes["pdf_electrical"]["stable_identity_key"] == "hint:evse:west"
    assert device.attributes["pdf_electrical"]["source_element_ids"] == [
        "p1:symbol:evse"
    ]
    assert {
        item.source_kind for item in device.provenance
    } == {"caller-instance-hint"}
    assert not model.attributes["pdf_electrical"]["unresolved_observations"]


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


def test_unregistered_multi_page_uses_separated_best_effort_page_tiles() -> None:
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

    best_effort = ElectricalPdfImporter().import_document(document)
    lane = best_effort.attributes["pdf_electrical"]
    assert lane["registration_pending"] is True
    assert lane["page_transforms_supplied"] is False
    assert lane["registration_mode"] == (
        "deterministic-separated-page-local-best-effort"
    )
    assert set(lane["best_effort_page_transforms"]) == {"1", "2"}
    assert lane["best_effort_page_transforms"]["1"]["tx_m"] == 0.0
    assert lane["best_effort_page_transforms"]["2"]["tx_m"] == pytest.approx(100.0)

    best_effort_positions = {
        device.name: (device.pose.position.x, device.pose.position.y)
        for device in best_effort.electrical_devices
    }
    assert best_effort_positions["EVSE-2"][0] - best_effort_positions["EVSE-1"][0] == (
        pytest.approx(100.0)
    )
    assert best_effort_positions["EVSE-2"][1] == pytest.approx(
        best_effort_positions["EVSE-1"][1]
    )
    assert {
        item.page
        for item in best_effort.provenance
        if item.method == "deterministic per-page best-effort unregistered placement"
    } == {1, 2}
    assert all(
        device.attributes["pdf_electrical"]["spatial_status"]
        == "multi-page-local-best-effort-unregistered"
        for device in best_effort.electrical_devices
    )
    validate_model(best_effort)

    frame_id = stable_id("frame", "synthetic:registered-multi-page")
    registered = ElectricalPdfImporter().import_document(
        document,
        page_transforms={
            1: PdfPageTransform(frame_id=frame_id),
            2: PdfPageTransform(frame_id=frame_id, tx_m=10.0),
        },
    )

    assert registered.coordinate_system.frame_id == frame_id
    assert registered.attributes["pdf_electrical"]["registration_pending"] is False
    assert registered.attributes["pdf_electrical"]["page_transforms_supplied"] is True
    assert registered.attributes["pdf_electrical"]["registration_mode"] == (
        "explicit-page-transforms"
    )
    assert registered.attributes["pdf_electrical"]["best_effort_page_transforms"] == {}
    registered_positions = {
        device.name: (device.pose.position.x, device.pose.position.y)
        for device in registered.electrical_devices
    }
    assert registered_positions["EVSE-2"][0] - registered_positions["EVSE-1"][0] == (
        pytest.approx(10.0)
    )
    assert registered_positions["EVSE-2"][1] == pytest.approx(
        registered_positions["EVSE-1"][1]
    )
    validate_model(registered)


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
