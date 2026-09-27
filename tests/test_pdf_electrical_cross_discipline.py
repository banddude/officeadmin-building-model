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


# --- Shared sheet identity (#183 review) ------------------------------------
#
# Sheet ids and disciplines come from the lane's single shared function,
# sheet_selection.sheet_identity. The pages below are built so the removed
# second definition (any 1-3 letter + number token, smallest element id wins,
# E-prefix fullmatch against `E-n` / `En.n` only) picks the wrong token:
# fixture tags and project numbers carry smaller element ids than the
# title-block sheet number, and `E-2.1` did not match its electrical shape.

_PAGE_PROVENANCE = {
    "page_rotation": 0,
    "displayed_page_width_pt": 612.0,
    "displayed_page_height_pt": 792.0,
    "coordinate_space": "displayed",
}
# Synthetic project-number shapes, printed in every title block.
_PROJECT_NUMBER_TOKENS = ("PRJ-2317", "JN204")


def _text(page: int, index: int, text: str, x: float, y: float):
    from oabm.importers.pdf_electrical.importer import PdfTextObservation

    return PdfTextObservation(
        element_id=f"p{page}:text:{index:04d}",
        page=page,
        text=text,
        x_pt=x,
        y_pt=y,
        font_size_pt=8.0,
    )


def _annotation(page: int, index: int, subject: str, x: float, y: float):
    from oabm.importers.pdf_electrical.importer import PdfSymbolObservation

    return PdfSymbolObservation(
        element_id=f"p{page}:annotation:{index:04d}",
        page=page,
        name=subject,
        x_pt=x,
        y_pt=y,
        source_kind="annotation:square",
        metadata={
            "subject": subject,
            "native_id": f"SYN-{page}-{index}",
            "rect_pt": [x - 12.0, y - 12.0, x + 12.0, y + 12.0],
        },
    )


def _fixture_tag_texts(page: int, first_index: int, count: int) -> list:
    """``count`` lighting fixture tags (`LF-1` ...) spread over the plan."""

    return [
        _text(
            page,
            first_index + offset,
            f"LF-{offset % 6 + 1}",
            60.0 + 12.0 * (offset % 30),
            300.0 + 20.0 * (offset // 30),
        )
        for offset in range(count)
    ]


def _title_block(page: int, first_index: int, sheet_id: str | None) -> list:
    """Project numbers (twice each) and the sheet number, bottom right."""

    texts = []
    index = first_index
    for token in _PROJECT_NUMBER_TOKENS:
        for y in (70.0, 30.0):
            texts.append(_text(page, index, token, 470.0, y))
            index += 1
    if sheet_id is not None:
        texts.append(_text(page, index, sheet_id, 560.0, 36.0))
    return texts


def _fixture_tag_set() -> PdfElectricalDocument:
    """Page 1: E-2.1 legend. Page 2: M-4 plan. Page 3: no sheet number.

    Every page prints 32 fixture tags and two project numbers (each twice)
    before its sheet number, so each wrong token outnumbers and precedes the
    single title-block sheet number.
    """

    texts: list = []
    texts += _fixture_tag_texts(1, 1, 32)
    texts += [
        _text(1, 40, "POWER SYMBOL LEGEND", 60.0, 690.0),
        _text(1, 41, "DUCT SMOKE DETECTOR, WIRED BY E", 60.0, 660.0),
        _text(1, 42, "DUPLEX RECEPTACLE", 60.0, 630.0),
    ]
    texts += _title_block(1, 50, "E-2.1")
    texts += _fixture_tag_texts(2, 1, 32)
    texts += _title_block(2, 50, "M-4")
    texts += _fixture_tag_texts(3, 1, 32)
    texts += _title_block(3, 50, None)
    symbols = (
        _annotation(2, 1, "DSD", 112.0, 712.0),
        _annotation(2, 2, "REC", 232.0, 712.0),
    )
    return PdfElectricalDocument(
        source_id="synthetic:xdisc-shared-identity",
        page_count=3,
        texts=tuple(texts),
        symbols=symbols,
        page_provenance={page: dict(_PAGE_PROVENANCE) for page in (1, 2, 3)},
    )


def test_title_block_e_2_1_beats_thirty_fixture_tags_and_reads_the_legend() -> None:
    from oabm.importers.pdf_electrical.sheet_selection import sheet_identity

    document = _fixture_tag_set()
    assert sheet_identity(document, 1) == ("E-2.1", "electrical")
    scope = electrical_scope_types(document)
    # The E-2.1 sheet is electrical, so its legend rows are read.
    assert scope.defined == frozenset({"duct_smoke_detector", "receptacle_duplex"})
    assert scope.cross_discipline == frozenset({"duct_smoke_detector"})


@pytest.mark.parametrize("sheet_id", ["E-2.1", "E-201", "E-1", "EL-3", "ELEC-2.10"])
def test_electrical_sheet_numbers_are_recognised_as_electrical(sheet_id: str) -> None:
    document = PdfElectricalDocument(
        source_id="synthetic:xdisc-e-shapes",
        page_count=1,
        texts=(
            _text(1, 1, "DUPLEX RECEPTACLE", 60.0, 630.0),
            _text(1, 2, sheet_id, 560.0, 36.0),
        ),
        page_provenance={1: dict(_PAGE_PROVENANCE)},
    )
    assert printed_sheet_ids(document) == {1: sheet_id}
    assert electrical_scope_types(document).defined == frozenset(
        {"receptacle_duplex"}
    )


def test_non_electrical_sheet_rows_are_not_electrical_scope() -> None:
    document = PdfElectricalDocument(
        source_id="synthetic:xdisc-m-sheet",
        page_count=1,
        texts=(
            _text(1, 1, "DUPLEX RECEPTACLE", 60.0, 630.0),
            _text(1, 2, "M-2.1", 560.0, 36.0),
        ),
        page_provenance={1: dict(_PAGE_PROVENANCE)},
    )
    assert electrical_scope_types(document).defined == frozenset()


def test_project_number_on_every_page_never_becomes_the_sheet_id() -> None:
    from oabm.importers.pdf_electrical.sheet_selection import sheet_identity

    document = _fixture_tag_set()
    ids = printed_sheet_ids(document)
    # Page 3 prints only project numbers and fixture tags: no sheet id.
    assert ids == {1: "E-2.1", 2: "M-4"}
    for token in _PROJECT_NUMBER_TOKENS:
        assert token not in ids.values()
    assert not any(value.startswith("LF") for value in ids.values())
    # printed_sheet_ids is a view of the shared function, not a second rule.
    for page in range(1, document.page_count + 1):
        sheet_id, _discipline = sheet_identity(document, page)
        assert ids.get(page) == sheet_id
    assert sheet_identity(document, 3) == (None, "unknown")


def test_filter_label_is_the_shared_sheet_id_not_a_fixture_tag() -> None:
    document = _fixture_tag_set()
    scope = electrical_scope_types(document)
    model = ElectricalPdfImporter().import_document(
        document,
        page_type_filters={2: scope.cross_discipline, 3: scope.cross_discipline},
    )
    rows = _device_rows(model)
    assert [row["device_type"] for row in rows] == ["duct_smoke_detector"]
    assert rows[0]["attributes"]["cross_discipline_sheet"] == "M-4"
    assert rows[0]["confidence"] == CROSS_DISCIPLINE_MAX_CONFIDENCE
    excluded = [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_observations"]
        if row.get("kind") == "page_type_filter"
    ]
    assert [row["cross_discipline_sheet"] for row in excluded] == ["M-4"]
    assert excluded[0]["recognized_classification"]["canonical_type"] == "receptacle"
