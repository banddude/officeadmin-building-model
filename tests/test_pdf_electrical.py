import json
from dataclasses import replace
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
    FIXTURE_DIR / "geometry-only-power-sheet-notes-column-legend.json"
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

    extracted = PdfElectricalDocument.from_dict(
        json.loads(NOTES_COLUMN_LEGEND_FIXTURE.read_text(encoding="utf-8"))
    )
    repeated = PdfElectricalDocument.from_dict(
        json.loads(NOTES_COLUMN_LEGEND_FIXTURE.read_text(encoding="utf-8"))
    )
    assert extracted == repeated
    assert not extracted.symbols

    texts = {observation.text for observation in extracted.texts}
    assert {
        "KEY NOTES",
        "GENERAL NOTES",
        "LEGEND",
        "SYMBOL",
        "FUNCTION",
        "E",
        "N",
        "R",
        '+44"',
        '+66"',
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
    assert len(model.electrical_devices) == 6
    assert len(model.electrical_devices) > 2
    assert not model.electrical_equipment

    devices_by_type = {
        device.device_type: device
        for device in model.electrical_devices
    }
    assert set(devices_by_type) == {
        "receptacle",
        "junction_box",
        "luminaire",
        "disconnect",
        "switch",
        "evse",
    }
    assert {
        device_type: device.attributes["pdf_electrical"]["status"]
        for device_type, device in devices_by_type.items()
    } == {
        "receptacle": "E",
        "junction_box": "N",
        "luminaire": "R",
        "disconnect": "N",
        "switch": "E",
        "evse": "R",
    }
    assert {
        device_type: device.attributes["pdf_electrical"]["status_meaning"]
        for device_type, device in devices_by_type.items()
    } == {
        "receptacle": "existing_to_remain",
        "junction_box": "new",
        "luminaire": "existing_to_be_removed",
        "disconnect": "new",
        "switch": "existing_to_remain",
        "evse": "existing_to_be_removed",
    }
    assert all(
        any(
            provenance.method == "pdf-field-status-tag"
            for provenance in device.provenance
        )
        for device in model.electrical_devices
    )

    regions = lane["legend_recognition"]["regions"]
    assert len(regions) == 1
    assert regions[0]["method"] == "symbol-function-table"
    assert regions[0]["heading_text"] == "LEGEND"
    assert regions[0]["row_count"] == 6
    assert regions[0]["classified_row_count"] == 6
    assert len(regions[0]["header_element_ids"]) == 2
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"][
            "legend_detection_method"
        ]
        == "symbol-function-table"
        for device in model.electrical_devices
    )

    # E/N/R and bare mounting-height tags are modifiers, not glyph geometry.
    without_modifiers = PdfElectricalDocument(
        source_id=extracted.source_id + ":without-field-modifiers",
        page_count=extracted.page_count,
        texts=tuple(
            observation
            for observation in extracted.texts
            if observation.text not in {"E", "N", "R", '+44"', '+66"'}
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
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
    )
    header_only_model = ElectricalPdfImporter().import_document(
        without_legend_title
    )
    assert len(header_only_model.electrical_devices) == 6
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
            if observation.text not in {"LEGEND", "SYMBOL", "FUNCTION"}
        ),
        symbols=extracted.symbols,
        vectors=extracted.vectors,
    )
    notes_only_model = ElectricalPdfImporter().import_document(notes_only)
    assert notes_only_model.attributes["pdf_electrical"][
        "legend_recognition"
    ]["regions"] == []

    validate_model(model)


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
