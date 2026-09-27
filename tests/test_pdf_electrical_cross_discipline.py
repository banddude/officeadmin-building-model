"""Cross-discipline device scope (#181): duct smoke detectors.

A two-page synthetic set: an electrical legend sheet that claims duct smoke
detectors from the mechanical discipline, and a mechanical sheet whose duct
smoke and receptacle symbols are only imported when the caller scopes the
page to the electrical legend's cross-discipline types.
"""

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)

from oabm.importers.pdf_electrical.importer import (
    CROSS_DISCIPLINE_DEFAULT_TYPES,
    CROSS_DISCIPLINE_MAX_CONFIDENCE,
    DEFAULT_SYMBOL_RULES,
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    electrical_scope_types,
    extract_pdf,
    printed_sheet_ids,
)
from oabm.importers.pdf_electrical.importer import _classify_semantic_text


def _add_text(commands: list[str], x: float, y: float, text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    commands.append(
        f"BT /F1 8 Tf 1 0 0 1 {x:.3f} {y:.3f} Tm ({escaped}) Tj ET"
    )


def _add_annotation(
    writer: PdfWriter,
    page: DictionaryObject,
    *,
    rect: tuple[float, float, float, float],
    subject: str,
    native_id: str,
) -> None:
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Square"),
            NameObject("/Rect"): ArrayObject(
                [FloatObject(value) for value in rect]
            ),
            NameObject("/Subj"): TextStringObject(subject),
            NameObject("/NM"): TextStringObject(native_id),
        }
    )
    annots = page.get(NameObject("/Annots"))
    if annots is None:
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)])
    else:
        annots.append(writer._add_object(annotation))


def _write_legend_and_mechanical_pdf(path: Path) -> None:
    """Page 1: an E legend sheet. Page 2: an M sheet with three symbols."""

    writer = PdfWriter()
    legend_page = writer.add_blank_page(width=612, height=792)
    mech_page = writer.add_blank_page(width=612, height=792)

    commands: list[str] = []
    _add_text(commands, 540.0, 40.0, "E-1")
    _add_text(commands, 60.0, 690.0, "POWER SYMBOL LEGEND")
    _add_text(
        commands,
        60.0,
        660.0,
        "DUCT DETECTOR, BY ELECTRICAL",
    )
    _add_text(commands, 60.0, 630.0, "DUPLEX RECEPTACLE")
    stream = ("\n".join(commands) + "\n").encode("ascii")
    legend_page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): writer._add_object(
                        DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject(
                                    "/Helvetica"
                                ),
                            }
                        )
                    )
                }
            )
        }
    )
    from pypdf.generic import DecodedStreamObject

    content_stream = DecodedStreamObject()
    content_stream.set_data(stream)
    legend_page[NameObject("/Contents")] = writer._add_object(content_stream)

    mech_commands: list[str] = []
    _add_text(mech_commands, 540.0, 40.0, "M-4")
    mech_stream = DecodedStreamObject()
    mech_stream.set_data(("\n".join(mech_commands) + "\n").encode("ascii"))
    mech_page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): writer._add_object(
                        DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject(
                                    "/Helvetica"
                                ),
                            }
                        )
                    )
                }
            )
        }
    )
    mech_page[NameObject("/Contents")] = writer._add_object(mech_stream)

    _add_annotation(
        writer,
        mech_page,
        rect=(100.0, 700.0, 124.0, 724.0),
        subject="DSD",
        native_id="DS-1",
    )
    # Drawn farther apart than the symbol-label association radius, so two
    # same-type symbols stay two devices instead of one merged candidate.
    _add_annotation(
        writer,
        mech_page,
        rect=(320.0, 700.0, 344.0, 724.0),
        subject="DSD",
        native_id="DS-2",
    )
    _add_annotation(
        writer,
        mech_page,
        rect=(220.0, 700.0, 244.0, 724.0),
        subject="REC",
        native_id="R-1",
    )

    with path.open("wb") as handle:
        writer.write(handle)


@pytest.fixture(scope="module")
def two_page_document(tmp_path_factory: pytest.TempPathFactory) -> PdfElectricalDocument:
    directory = tmp_path_factory.mktemp("xdisc")
    path = directory / "legend-and-mechanical-sheets.pdf"
    _write_legend_and_mechanical_pdf(path)
    document = extract_pdf(path, source_id="synthetic:xdisc")
    assert document.page_count == 2
    return document


def _device_rows(model) -> list[dict]:
    return [
        {
            "device_type": device.device_type,
            "confidence": device.confidence,
            "attributes": dict(device.attributes["pdf_electrical"]),
        }
        for device in sorted(
            model.electrical_devices, key=lambda item: item.id
        )
    ]


def test_duct_smoke_rule_precedes_generic_smoke_rules() -> None:
    for text in (
        "DSD",
        "DUCT DETECTOR",
        "DUCT DETECTOR, BY ELECTRICAL",
        "DUCT SMOKE DETECTOR",
    ):
        classification, _ranked = _classify_semantic_text(
            text,
            DEFAULT_SYMBOL_RULES,
            ambiguity_margin=0.08,
        )
        assert classification is not None, text
        kind, canonical_type, _confidence = classification
        assert (kind, canonical_type) == ("device", "duct_smoke_detector"), text


def test_responsibility_phrase_paths() -> None:
    from oabm.importers.pdf_electrical.importer import (
        _row_claims_electrical_responsibility as claims,
    )

    assert claims("DUCT DETECTOR, BY ELECTRICAL")
    assert claims("DSD (WIRED BY E)")
    assert claims("FURNISHED BY MECH, INSTALLED BY E")
    assert not claims("FURNISHED BY MECH, INSTALLED BY MECH")
    assert not claims("DUPLEX RECEPTACLE")


def test_duct_smoke_is_never_typed_smoke_alarm_or_smoke_co() -> None:
    for text in ("DSD", "DUCT DETECTOR", "DUCT SMOKE DETECTOR"):
        _classification, ranked = _classify_semantic_text(
            text,
            DEFAULT_SYMBOL_RULES,
            ambiguity_margin=0.08,
        )
        types = {entry["canonical_type"] for entry in ranked}
        assert "smoke_alarm" not in types, text
        assert "smoke_co_alarm" not in types, text


def test_printed_sheet_ids_use_the_standalone_sheet_number(
    two_page_document: PdfElectricalDocument,
) -> None:
    assert printed_sheet_ids(two_page_document) == {1: "E-1", 2: "M-4"}


def test_electrical_scope_types_reads_legend_rows_on_e_sheets(
    two_page_document: PdfElectricalDocument,
) -> None:
    scope = electrical_scope_types(two_page_document)
    assert scope.defined == frozenset(
        {"duct_smoke_detector", "receptacle_duplex"}
    )
    # The duct smoke row carries "FURNISHED BY M, WIRED BY E"; the duplex
    # receptacle row carries no responsibility phrase. The documented default
    # set is always part of the cross-discipline claim.
    assert scope.cross_discipline == frozenset(
        {"duct_smoke_detector"}
    ) | CROSS_DISCIPLINE_DEFAULT_TYPES


def test_cross_discipline_filter_keeps_only_scoped_mechanical_devices(
    two_page_document: PdfElectricalDocument,
) -> None:
    scope = electrical_scope_types(two_page_document)
    model = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters={2: scope.cross_discipline},
    )
    rows = _device_rows(model)
    assert len(rows) == 2
    for row in rows:
        assert row["device_type"] == "duct_smoke_detector"
        assert row["attributes"]["cross_discipline_sheet"] == "M-4"
        assert row["confidence"] == CROSS_DISCIPLINE_MAX_CONFIDENCE
        assert row["confidence"] <= 0.6
    # The scoped provenance note is inferred, not an observation.
    scoped_methods = [
        provenance.method
        for device in model.electrical_devices
        for provenance in device.provenance
        if provenance.method == "cross-discipline-page-filter"
    ]
    assert len(scoped_methods) == 2
    for device in model.electrical_devices:
        note = [
            provenance
            for provenance in device.provenance
            if provenance.method == "cross-discipline-page-filter"
        ]
        assert len(note) == 1
        assert note[0].derivation == "inferred"
        assert note[0].attributes["page_type_filters"] == [
            "duct_smoke_detector"
        ]
    # No device or equipment comes from the unfiltered legend page.
    for device in model.electrical_devices:
        assert device.attributes["pdf_electrical"]["source_page"] == 2


def test_filtered_mechanical_receptacle_is_excluded_with_evidence(
    two_page_document: PdfElectricalDocument,
) -> None:
    scope = electrical_scope_types(two_page_document)
    model = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters={2: scope.cross_discipline},
    )
    excluded = [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_observations"]
        if row.get("kind") == "page_type_filter"
    ]
    assert len(excluded) == 1
    row = excluded[0]
    assert row["page"] == 2
    assert row["status"] == "excluded_by_page_type_filter"
    assert row["recognized_classification"]["canonical_type"] == "receptacle"
    assert row["cross_discipline_sheet"] == "M-4"
    assert not any(
        device.device_type == "receptacle" for device in model.electrical_devices
    )


def test_without_filters_the_default_behaviour_is_unchanged(
    two_page_document: PdfElectricalDocument,
) -> None:
    default_model = ElectricalPdfImporter().import_document(two_page_document)
    rows = _device_rows(default_model)
    assert len(rows) == 3
    by_type = sorted(row["device_type"] for row in rows)
    assert by_type == [
        "duct_smoke_detector",
        "duct_smoke_detector",
        "receptacle",
    ]
    for row in rows:
        assert "cross_discipline_sheet" not in row["attributes"]
        assert row["confidence"] > CROSS_DISCIPLINE_MAX_CONFIDENCE
    assert not any(
        provenance.method == "cross-discipline-page-filter"
        for device in default_model.electrical_devices
        for provenance in device.provenance
    )


@pytest.mark.parametrize("filters", [None, {}])
def test_none_and_empty_filters_are_byte_identical_to_the_default(
    two_page_document: PdfElectricalDocument,
    filters,
) -> None:
    default_model = ElectricalPdfImporter().import_document(two_page_document)
    filtered_model = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters=filters,
    )
    assert filtered_model.to_json() == default_model.to_json()


def test_identical_runs_stay_deterministic(
    two_page_document: PdfElectricalDocument,
) -> None:
    scope = electrical_scope_types(two_page_document)
    first = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters={2: scope.cross_discipline},
    )
    second = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters={2: scope.cross_discipline},
    )
    assert first.to_json() == second.to_json()


def test_page_type_filters_validation_fail_closed(
    two_page_document: PdfElectricalDocument,
) -> None:
    with pytest.raises(ElectricalPdfError):
        ElectricalPdfImporter().import_document(
            two_page_document,
            page_type_filters={3: frozenset({"duct_smoke_detector"})},
        )
    with pytest.raises(ElectricalPdfError):
        ElectricalPdfImporter().import_document(
            two_page_document,
            page_type_filters={0: frozenset({"duct_smoke_detector"})},
        )
    with pytest.raises(ElectricalPdfError):
        ElectricalPdfImporter().import_document(
            two_page_document,
            page_type_filters={2: "duct_smoke_detector"},
        )


def test_empty_filter_set_for_a_page_imports_nothing_from_it(
    two_page_document: PdfElectricalDocument,
) -> None:
    model = ElectricalPdfImporter().import_document(
        two_page_document,
        page_type_filters={2: frozenset()},
    )
    assert model.electrical_devices == ()
    excluded = [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_observations"]
        if row.get("kind") == "page_type_filter"
    ]
    assert len(excluded) == 3


def test_document_with_no_e_sheets_still_reports_the_default_set(
    tmp_path: Path,
) -> None:
    document = PdfElectricalDocument(
        source_id="synthetic:no-sheets",
        page_count=1,
    )
    scope = electrical_scope_types(document)
    assert scope.defined == frozenset()
    assert scope.cross_discipline == CROSS_DISCIPLINE_DEFAULT_TYPES
    assert printed_sheet_ids(document) == {}
