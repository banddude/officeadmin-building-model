"""Explicit original-page applicability for registered electrical PDF intake."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.pdf_electrical import (
    DrawingRegionTransform,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfPageTransform,
    extract_pdf,
    import_pdf,
)
from oabm.importers.pdf_electrical.importer import _label_excluded_source_context
from oabm.model import validate_model

ROOT = Path(__file__).resolve().parents[1]
LEGEND_REFERENCE_PDF = ROOT / "fixtures/pdf_electrical/separate-sheet-explicit-legend-reference.pdf"
# Canonical JSON fingerprint produced by the pre-change current-main commit
# a38a6713e3885a66f12d06ecf5323d1228ee1544 with the legacy all-pages API.
LEGACY_REFERENCE_MODEL_SHA256 = "3301d1624ea80f852c19c2bb2802eb5bc9f017f3970d91bafe81fefc4b92ae6c"


def _write_27_page_source(path: Path, *, field_pages: tuple[int, ...] = (14, 16, 18)) -> None:
    """Create a real 27-page PDF with extracted field evidence on chosen pages."""
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    glyph = DecodedStreamObject()
    glyph.set_data(b"0 0 12 12 re S")
    glyph[NameObject("/Type")] = NameObject("/XObject")
    glyph[NameObject("/Subtype")] = NameObject("/Form")
    glyph[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(12), NumberObject(12)]
    )
    glyph_ref = writer._add_object(glyph)
    for page_number in range(1, 28):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            NameObject("/XObject"): DictionaryObject({NameObject("/EVSE1"): glyph_ref}),
        })
        if page_number not in field_pages:
            continue
        tag = f"EVSE-{page_number}"
        panel = f"LP{page_number}"
        commands = (
            f"BT /F1 10 Tf 1 0 0 1 72 700 Tm (PANEL {panel} 120/240V 1PH) Tj ET\n"
            f'BT /F1 9 Tf 1 0 0 1 205 505 Tm ({tag} +48" AFF WALL MTD) Tj ET\n'
            f"BT /F1 8 Tf 1 0 0 1 72 650 Tm (PANEL {panel} CKT 12 -> {tag} 240V 2P) Tj ET\n"
            "72 700 m 72 700 l 200 500 l S\n"
            "q 1 0 0 1 200 500 cm /EVSE1 Do Q\n"
        ).encode("ascii")
        content = DecodedStreamObject()
        content.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _excluded_reasons(page_count: int, selected: set[int]) -> dict[int, str]:
    return {
        page: "not included in this selected-page placement run; applicability not assessed"
        for page in range(1, page_count + 1)
        if page not in selected
    }


def test_generated_27_page_pdf_selects_original_16_and_18_and_preserves_legacy_default(tmp_path):
    path = tmp_path / "selected-original-pages.pdf"
    _write_27_page_source(path)
    assert not path.with_suffix(".expected.json").exists()
    document = extract_pdf(path, source_id="fixture:selected-original-pages-27")
    assert document.page_count == 27
    assert {text.page for text in document.texts if "EVSE-" in text.text} == {14, 16, 18}

    # Legacy omission of selected_pages still recognizes the full source exactly
    # as before and remains deterministic. No page-selection metadata is added.
    legacy = ElectricalPdfImporter().import_document(document)
    legacy_repeat = ElectricalPdfImporter().import_document(document, selected_pages=None)
    assert legacy.to_json() == legacy_repeat.to_json()
    assert {device.attributes["pdf_electrical"]["source_page"] for device in legacy.electrical_devices} == {14, 16, 18}
    assert "page_selection" not in legacy.attributes["pdf_electrical"]

    selected_pages = (16, 18)
    reasons = _excluded_reasons(document.page_count, set(selected_pages))
    transforms = {
        16: PdfPageTransform(frame_id="fixture:registered-frame", tx_m=1.0),
        18: PdfPageTransform(frame_id="fixture:registered-frame", tx_m=2.0),
    }
    selected = ElectricalPdfImporter().import_document(
        document,
        page_transforms=transforms,
        selected_pages=selected_pages,
        excluded_page_reasons=reasons,
    )
    validate_model(selected)
    assert {device.attributes["pdf_electrical"]["source_page"] for device in selected.electrical_devices} == {16, 18}
    assert {equipment.attributes["pdf_electrical"]["source_page"] for equipment in selected.electrical_equipment} == {16, 18}
    assert {device.id for device in selected.electrical_devices} == {
        device.id for device in legacy.electrical_devices
        if device.attributes["pdf_electrical"]["source_page"] in selected_pages
    }
    assert {equipment.id for equipment in selected.electrical_equipment} == {
        equipment.id for equipment in legacy.electrical_equipment
        if equipment.attributes["pdf_electrical"]["source_page"] in selected_pages
    }
    selected_permuted = ElectricalPdfImporter().import_document(
        document,
        page_transforms=transforms,
        selected_pages=(18, 16),
        excluded_page_reasons=reasons,
    )
    assert selected.to_json() == selected_permuted.to_json()
    class_wrapper = ElectricalPdfImporter().import_pdf(
        path,
        source_id="fixture:selected-original-pages-27",
        page_transforms=transforms,
        selected_pages=selected_pages,
        excluded_page_reasons=reasons,
    )
    function_wrapper = import_pdf(
        path,
        source_id="fixture:selected-original-pages-27",
        page_transforms=transforms,
        selected_pages=selected_pages,
        excluded_page_reasons=reasons,
    )
    assert selected.to_json() == class_wrapper.to_json() == function_wrapper.to_json()
    assert not any(
        provenance.page == 14
        for entity in (*selected.electrical_equipment, *selected.electrical_devices, *selected.circuits)
        for provenance in entity.provenance
    )
    assert selected.attributes["pdf_electrical"]["page_count"] == 27
    selection = selected.attributes["pdf_electrical"]["page_selection"]
    assert selection["basis"].startswith("caller-supplied")
    assert selection["selected_original_pages"] == [16, 18]
    assert {row["page"] for row in selection["excluded_original_pages"]} == set(range(1, 28)) - {16, 18}
    assert {device.attributes["pdf_electrical"]["source_page"] for device in selected.electrical_devices} == {
        provenance.page for device in selected.electrical_devices for provenance in device.provenance
        if provenance.page in {16, 18}
    }
    devices_by_page = {
        page: next(
            device for device in selected.electrical_devices
            if device.attributes["pdf_electrical"]["source_page"] == page
        )
        for page in selected_pages
    }
    for page, device in devices_by_page.items():
        lane = device.attributes["pdf_electrical"]
        transform = transforms[page]
        source_position = lane["source_position_pt"]
        expected = transform.apply(source_position["x"], source_position["y"])
        assert device.pose.position == expected
        assert lane["page_transform"]["tx_m"] == transform.tx_m
    assert devices_by_page[18].pose.position.x - devices_by_page[16].pose.position.x == pytest.approx(1.0)


def test_unselected_page_remains_legend_context_for_selected_devices():
    assert not LEGEND_REFERENCE_PDF.with_suffix(".expected.json").exists()
    document = extract_pdf(
        LEGEND_REFERENCE_PDF,
        source_id="fixture:separate-sheet-explicit-legend-reference",
    )
    assert document.page_count == 3
    legacy = ElectricalPdfImporter().import_document(document)
    assert hashlib.sha256(legacy.to_json().encode()).hexdigest() == LEGACY_REFERENCE_MODEL_SHA256
    selected = ElectricalPdfImporter().import_document(
        document,
        page_transforms={2: PdfPageTransform(frame_id="fixture:selected-frame")},
        selected_pages=(2,),
        excluded_page_reasons={
            1: "legend reference context only; not a field-placement page",
            3: "not included in this selected-page placement run",
        },
    )
    legacy_page_two = {
        device.id for device in legacy.electrical_devices
        if device.attributes["pdf_electrical"]["source_page"] == 2
    }
    selected_page_two = {
        device.id for device in selected.electrical_devices
        if device.attributes["pdf_electrical"]["source_page"] == 2
    }
    assert len(selected_page_two) == 6
    assert selected_page_two == legacy_page_two
    assert all(
        device.attributes["pdf_electrical"]["shape_recognition"]["legend_page"] == 1
        for device in selected.electrical_devices
    )
    assert {device.attributes["pdf_electrical"]["source_page"] for device in selected.electrical_devices} == {2}
    assert any(
        provenance.method == "pdf-sheet-legend-type-label" and provenance.page == 1
        for device in selected.electrical_devices
        for provenance in device.provenance
    )
    out_of_scope_diagnostics = [
        item for item in selected.attributes["pdf_electrical"]["unresolved_circuits"]
        if item["page"] == 1
    ]
    assert out_of_scope_diagnostics
    assert all(item["page_scope"] == "excluded-source-context" for item in out_of_scope_diagnostics)
    assert all(item["selection_exclusion_reason"] == "legend reference context only; not a field-placement page"
               for item in out_of_scope_diagnostics)
    validate_model(selected)


def test_excluded_context_annotation_preserves_source_scope_status():
    row = {
        "page": 1,
        "scope_status": "new",
        "scope_reason": "source work-status marker",
    }

    _label_excluded_source_context(
        [row], {1: "legend reference context only; not a field-placement page"}
    )

    assert row["scope_status"] == "new"
    assert row["scope_reason"] == "source work-status marker"
    assert row["page_scope"] == "excluded-source-context"
    assert row["selection_exclusion_reason"] == (
        "legend reference context only; not a field-placement page"
    )


def test_selected_region_assignment_failure_falls_back_only_for_selected_pages():
    document = extract_pdf(
        LEGEND_REFERENCE_PDF,
        source_id="fixture:separate-sheet-explicit-legend-reference",
    )
    region_transform = DrawingRegionTransform(
        source_bbox_pt=(0.0, 0.0, 1.0, 1.0),
        transform=PdfPageTransform(frame_id="fixture:region-frame"),
    )
    selected = ElectricalPdfImporter().import_document(
        document,
        page_transforms={2: (region_transform,)},
        selected_pages=(2,),
        excluded_page_reasons={
            1: "legend reference context only; not a field-placement page",
            3: "not included in this selected-page placement run",
        },
    )
    lane = selected.attributes["pdf_electrical"]
    assert lane["drawing_region_assignment"]["status"] == "unresolved"
    assert lane["registration_pending"] is True
    assert lane["spatial_status"] == "multi-page-local-best-effort-unregistered"
    assert {device.attributes["pdf_electrical"]["source_page"] for device in selected.electrical_devices} == {2}
    assert set(lane["best_effort_page_transforms"]) == {"2"}
    validate_model(selected)


@pytest.mark.parametrize(
    ("selected", "excluded", "message"),
    [
        ((), {}, "must not be empty"),
        ((16, 16), None, "duplicate pages"),
        ((28,), {}, "outside the source document"),
        ((True,), {}, "must contain only 1-based integer"),
        (("16",), {}, "must contain only 1-based integer"),
        ((16, 18), None, "must give a reason for every original page"),
        ((16, 18), {1: "reason"}, "must cover exactly"),
        ((16, 18), {1: "reason", 16: "wrong page", **{i: "r" for i in range(2, 28) if i not in {16, 18}}}, "includes selected page 16"),
        ((16,), {page: ("" if page == 1 else "reason") for page in range(1, 28) if page != 16}, "must be a non-empty reason"),
        ((16,), {True: "reason", **{page: "reason" for page in range(2, 28) if page != 16}}, "invalid page key True"),
        ((16,), {"1": "reason", **{page: "reason" for page in range(2, 28) if page != 16}}, "invalid page key '1'"),
    ],
)
def test_page_selection_rejects_invalid_or_incomplete_scope(tmp_path, selected, excluded, message):
    path = tmp_path / "selection-validation.pdf"
    _write_27_page_source(path, field_pages=())
    document = extract_pdf(path, source_id="fixture:selection-validation")
    with pytest.raises(ElectricalPdfError, match=message):
        ElectricalPdfImporter().import_document(
            document, selected_pages=selected, excluded_page_reasons=excluded,
        )


def test_selected_transform_coverage_must_match_selected_original_pages(tmp_path):
    path = tmp_path / "transform-validation.pdf"
    _write_27_page_source(path, field_pages=())
    document = extract_pdf(path, source_id="fixture:transform-validation")
    assert not path.with_suffix(".expected.json").exists()
    selected = (16, 18)
    reasons = _excluded_reasons(document.page_count, set(selected))
    transform = PdfPageTransform(frame_id="fixture:frame")
    with pytest.raises(ElectricalPdfError, match=r"cover every selected PDF page exactly: missing pages \[18\]"):
        ElectricalPdfImporter().import_document(
            document,
            page_transforms={16: transform},
            selected_pages=selected,
            excluded_page_reasons=reasons,
        )
    with pytest.raises(ElectricalPdfError, match=r"unknown or unselected pages \[14\]"):
        ElectricalPdfImporter().import_document(
            document,
            page_transforms={16: transform, 18: transform, 14: transform},
            selected_pages=selected,
            excluded_page_reasons=reasons,
        )
