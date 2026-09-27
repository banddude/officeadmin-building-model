"""Device-sheet selection (#180): every electrical sheet with devices, reasons for the rest."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_electrical import (
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfSymbolObservation,
    PdfTextObservation,
    SymbolRule,
    extract_pdf,
)
from oabm.importers.pdf_electrical.sheet_selection import (
    TITLE_BLOCK_BAND_FRACTION,
    _looks_tag_like,
    _sheet_identity,
    select_device_pages,
    sheet_identity,
)

ROOT = Path(__file__).resolve().parents[1]


def _annotation(
    writer: PdfWriter,
    *,
    native_id: str,
    subject: str,
    rect: tuple[float, float, float, float],
) -> object:
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Square"),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in rect]),
            NameObject("/NM"): TextStringObject(native_id),
            NameObject("/Subj"): TextStringObject(subject),
        }
    )
    return writer._add_object(annotation)


def _text_line(x: float, y: float, text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET\n".encode()


def _write_five_page_set(path: Path) -> None:
    """A synthetic five-page set shaped like the #180 acceptance list.

    Invented labels throughout. Device instances are carried by /Square
    annotations with stable /NM native identifiers and by tagged device text;
    the thermostat tag is deliberately left unrecognized by the default
    catalog so it stays unresolved instead of becoming a device.
    """

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    pages: list[list[bytes]] = []
    annotations: dict[int, list[object]] = {}

    # Page 1: E-110 power plan with two receptacle instances.
    pages.append(
        [
            _text_line(500, 40, "E-110"),
            _text_line(100, 430, "RECEPT-1"),
            _text_line(260, 430, "RECEPT-2"),
        ]
    )
    annotations[1] = [
        _annotation(
            writer,
            native_id="R1",
            subject="RECEPTACLE",
            rect=(100, 400, 112, 412),
        ),
        _annotation(
            writer,
            native_id="R2",
            subject="RECEPTACLE",
            rect=(260, 400, 272, 412),
        ),
    ]

    # Page 2: E-120 lighting plan with two tagged luminaires.
    pages.append(
        [
            _text_line(500, 40, "E-120"),
            _text_line(120, 340, "LTG-1"),
            _text_line(320, 340, "LTG-2"),
        ]
    )

    # Page 3: E-130 signal plan with low-voltage devices only; the
    # thermostat tag stays unrecognized by the default catalog.
    pages.append(
        [
            _text_line(500, 40, "E-130"),
            _text_line(340, 300, "TSTAT-1"),
        ]
    )
    annotations[3] = [
        _annotation(
            writer,
            native_id="D1",
            subject="DATA OUTLET",
            rect=(140, 300, 152, 312),
        ),
        _annotation(
            writer,
            native_id="SP1",
            subject="SPEAKER",
            rect=(340, 240, 352, 252),
        ),
    ]

    # Page 4: E-001 legend and notes with no device instances.
    pages.append(
        [
            _text_line(500, 40, "E-001"),
            _text_line(72, 700, "GENERAL NOTES"),
            _text_line(72, 680, "1. ALL WORK PER LOCAL CODES."),
        ]
    )

    # Page 5: M-101 mechanical sheet that still carries one device symbol.
    pages.append(
        [
            _text_line(500, 40, "M-101"),
            _text_line(160, 250, "RECEPT-9"),
        ]
    )
    annotations[5] = [
        _annotation(
            writer,
            native_id="R9",
            subject="RECEPTACLE",
            rect=(160, 220, 172, 232),
        ),
    ]

    for index, parts in enumerate(pages, start=1):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        content = DecodedStreamObject()
        content.set_data(b"".join(parts))
        page[NameObject("/Contents")] = writer._add_object(content)
        if index in annotations:
            page[NameObject("/Annots")] = ArrayObject(list(annotations[index]))

    with path.open("wb") as handle:
        writer.write(handle)


def test_five_page_set_selects_device_sheets_with_reasons(tmp_path: Path) -> None:
    pdf_path = tmp_path / "five-page-set.pdf"
    _write_five_page_set(pdf_path)
    document = extract_pdf(pdf_path, source_id="synthetic:five-page-set")

    selection = select_device_pages(document)

    assert selection.to_dict() == {
        "import_mode": "document",
        "import_fallback_reason": None,
        "pages": [
            {
                "page": 1,
                "sheet_id": "E-110",
                "discipline": "electrical",
                "device_count": 2,
                "recognized_device_count": 2,
                "symbol_like_count": 4,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 2,
                "sheet_id": "E-120",
                "discipline": "electrical",
                "device_count": 2,
                "recognized_device_count": 2,
                "symbol_like_count": 2,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 3,
                "sheet_id": "E-130",
                "discipline": "electrical",
                "device_count": 2,
                "recognized_device_count": 2,
                "symbol_like_count": 3,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 4,
                "sheet_id": "E-001",
                "discipline": "electrical",
                "device_count": 0,
                "recognized_device_count": 0,
                "symbol_like_count": 0,
                "included": False,
                "reason": "electrical_no_recognized_devices",
            },
            {
                "page": 5,
                "sheet_id": "M-101",
                "discipline": "mechanical",
                "device_count": 1,
                "recognized_device_count": 1,
                "symbol_like_count": 2,
                "included": False,
                "reason": "mechanical_sheet",
            },
        ]
    }
    # Two identical runs give identical output.
    assert select_device_pages(document).to_dict() == selection.to_dict()


def _document_with_pages(
    *pages: dict,
    page_size: tuple[float, float] = (612.0, 792.0),
) -> PdfElectricalDocument:
    texts: list[PdfTextObservation] = []
    symbols: list[PdfSymbolObservation] = []
    for page_index, page in enumerate(pages, start=1):
        for text in page.get("texts", ()):
            texts.append(
                PdfTextObservation(
                    element_id=f"p{page_index}:text:{text[0]}",
                    page=page_index,
                    text=text[1],
                    x_pt=text[2],
                    y_pt=text[3],
                    font_size_pt=10.0,
                )
            )
        for symbol in page.get("symbols", ()):
            symbols.append(
                PdfSymbolObservation(
                    element_id=f"p{page_index}:annotation:{symbol[0]}",
                    page=page_index,
                    name=symbol[1],
                    x_pt=symbol[2],
                    y_pt=symbol[3],
                    source_kind="annotation:square",
                    metadata={"native_id": symbol[0]},
                )
            )
    return PdfElectricalDocument(
        source_id="synthetic:sheet-selection",
        page_count=len(pages),
        texts=tuple(texts),
        symbols=tuple(symbols),
        page_provenance={
            page_index: {
                "page_rotation": 0,
                "displayed_page_width_pt": page_size[0],
                "displayed_page_height_pt": page_size[1],
                "coordinate_space": "displayed",
            }
            for page_index in range(1, len(pages) + 1)
        },
    )


def test_discipline_prefix_decides_even_when_the_sheet_is_not_electrical() -> None:
    document = _document_with_pages(
        {
            "texts": (("title", "P-401", 500.0, 40.0),),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 300.0),),
        },
    )

    (choice,) = select_device_pages(document).pages

    assert choice.sheet_id == "P-401"
    assert choice.discipline == "plumbing"
    assert choice.device_count == 1
    assert choice.included is False
    assert choice.reason == "plumbing_sheet"


def test_unknown_discipline_with_devices_is_surfaced_not_used() -> None:
    document = _document_with_pages(
        {"symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 300.0),)},
        {"texts": (("title", "S-900", 500.0, 40.0),)},
    )

    first, second = select_device_pages(document).pages

    assert first.sheet_id is None
    assert first.discipline == "unknown"
    assert first.device_count == 1
    assert first.included is False
    assert first.reason == "unknown_discipline_with_devices"
    assert (second.sheet_id, second.discipline, second.reason) == (
        "S-900",
        "structural",
        "structural_sheet",
    )


def test_unknown_page_without_devices_reports_unknown_sheet() -> None:
    document = _document_with_pages(
        {"texts": (("note", "COORDINATE WITH OTHER TRADES.", 72.0, 700.0),)},
    )

    (choice,) = select_device_pages(document).pages

    assert (choice.sheet_id, choice.discipline, choice.device_count) == (
        None,
        "unknown",
        0,
    )
    assert choice.included is False
    assert choice.reason == "unknown_sheet"


def test_discipline_word_fallback_when_no_sheet_number_is_printed() -> None:
    document = _document_with_pages(
        {
            "texts": (
                ("heading", "FIRE ALARM RISER DIAGRAM", 560.0, 60.0),
                ("device", "RECEPT-1", 120.0, 300.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 280.0),),
        },
        {
            "texts": (
                ("a", "PLUMBING", 560.0, 80.0),
                ("b", "MECHANICAL", 560.0, 60.0),
            ),
        },
    )

    first, second = select_device_pages(document).pages

    assert (first.sheet_id, first.discipline) == (None, "fire_protection")
    assert first.reason == "fire_protection_sheet"
    # Equal word counts fall back to the lexicographic discipline.
    assert (second.sheet_id, second.discipline) == (None, "mechanical")
    assert second.reason == "mechanical_sheet"


def test_sheet_id_preference_is_frequency_then_title_block_corner() -> None:
    document = _document_with_pages(
        {
            "texts": (
                ("reference", "SEE E-001 FOR GENERAL NOTES.", 72.0, 700.0),
                ("border", "E-210", 608.0, 780.0),
                ("title", "E-210", 500.0, 40.0),
                ("device", "RECEPT-1", 120.0, 300.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 280.0),),
        },
        {
            "texts": (
                ("top", "E-301", 100.0, 700.0),
                ("corner", "E-302", 500.0, 40.0),
            ),
        },
    )

    first, second = select_device_pages(document).pages

    # The repeated sheet number beats the single cross-reference.
    assert first.sheet_id == "E-210"
    assert first.discipline == "electrical"
    # A frequency tie is broken by the bottom-right title-block corner.
    assert second.sheet_id == "E-302"


def test_supplied_importer_is_used_for_device_counts() -> None:
    document = _document_with_pages(
        {
            "texts": (("title", "E-140", 500.0, 40.0),),
            "symbols": (("T1", "THERMOSTAT", 120.0, 300.0),),
        },
    )

    from oabm.importers.pdf_electrical import DEFAULT_SYMBOL_RULES

    extended = ElectricalPdfImporter(
        symbol_rules=(
            *DEFAULT_SYMBOL_RULES,
            SymbolRule(r"\bTHERMOSTAT\b", "device", "thermostat", 0.95),
        )
    )

    default_choice = select_device_pages(document).pages[0]
    extended_choice = select_device_pages(document, importer=extended).pages[0]

    assert default_choice.device_count == 0
    assert default_choice.reason == "electrical_no_recognized_devices"
    assert default_choice.recognized_device_count == 0
    assert default_choice.symbol_like_count == 1
    assert extended_choice.device_count == 1
    assert extended_choice.included is True
    assert extended_choice.reason == "electrical_with_devices"


def test_notes_naming_another_trade_do_not_decide_the_discipline() -> None:
    # No printed sheet number. General notes in the drawing area name the
    # mechanical trade several times; the title block names electrical.
    notes = tuple(
        (f"note{index}", text, 72.0, 700.0 - 20.0 * index)
        for index, text in enumerate(
            (
                "MECHANICAL PLACEHOLDER NOTE ALPHA",
                "MECHANICAL PLACEHOLDER NOTE BRAVO",
                "MECHANICAL PLACEHOLDER NOTE CHARLIE",
                "MECHANICAL PLACEHOLDER NOTE DELTA",
            )
        )
    )
    document = _document_with_pages(
        {
            "texts": (
                *notes,
                ("title", "ELECTRICAL POWER PLAN", 560.0, 50.0),
                ("device", "RECEPT-1", 120.0, 300.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 280.0),),
        },
    )

    (choice,) = select_device_pages(document).pages

    assert (choice.sheet_id, choice.discipline) == (None, "electrical")
    assert choice.included is True
    assert choice.reason == "electrical_with_devices"
    assert sheet_identity(document, 1) == (None, "electrical")


def test_title_block_band_uses_right_and_bottom_edges() -> None:
    inside = 612.0 * (1.0 - TITLE_BLOCK_BAND_FRACTION) + 1.0
    outside = 612.0 * (1.0 - TITLE_BLOCK_BAND_FRACTION) - 1.0
    bottom = 792.0 * TITLE_BLOCK_BAND_FRACTION - 1.0
    above = 792.0 * TITLE_BLOCK_BAND_FRACTION + 1.0
    document = _document_with_pages(
        # Right-edge strip, near the top of the sheet.
        {"texts": (("tb", "STRUCTURAL", inside, 700.0),)},
        # Bottom strip, near the left edge.
        {"texts": (("tb", "PLUMBING", 72.0, bottom),)},
        # Just outside both strips: drawing-area text never decides.
        {"texts": (("n", "PLUMBING", outside, above),)},
    )

    assert [sheet_identity(document, page) for page in (1, 2, 3)] == [
        (None, "structural"),
        (None, "plumbing"),
        (None, "unknown"),
    ]


def test_title_block_band_without_page_provenance_uses_text_extent() -> None:
    texts = (
        PdfTextObservation(
            element_id="p1:text:note",
            page=1,
            text="MECHANICAL NOTES: MECHANICAL UNITS BY OTHERS.",
            x_pt=100.0,
            y_pt=900.0,
            font_size_pt=10.0,
        ),
        PdfTextObservation(
            element_id="p1:text:title",
            page=1,
            text="ELECTRICAL",
            x_pt=1000.0,
            y_pt=500.0,
            font_size_pt=10.0,
        ),
    )
    document = PdfElectricalDocument(
        source_id="synthetic:no-provenance",
        page_count=1,
        texts=texts,
    )

    assert sheet_identity(document, 1) == (None, "electrical")


def test_sheet_identity_is_public_and_the_private_alias_is_kept() -> None:
    document = _document_with_pages(
        {"texts": (("title", "E-2.1", 500.0, 40.0), ("x", "SEE M-101", 72.0, 700.0))},
    )

    assert _sheet_identity is sheet_identity
    assert sheet_identity(document, 1) == ("E-2.1", "electrical")


def test_recall_disclosure_separates_empty_sheets_from_unrecognized_symbols() -> None:
    document = _document_with_pages(
        # Electrical sheet whose symbols and tags the default rules do not
        # recognize.
        {
            "texts": (
                ("title", "E-150", 500.0, 40.0),
                ("t1", "ZQ-1", 120.0, 320.0),
                ("t2", "ZQ-2", 220.0, 320.0),
                ("note", "ALL WORK PER LOCAL CODES.", 72.0, 700.0),
            ),
            "symbols": (
                ("Z1", "ZQ GLYPH", 120.0, 300.0),
                ("Z2", "ZQ GLYPH", 220.0, 300.0),
            ),
        },
        # Electrical legend sheet with nothing device-like on it.
        {
            "texts": (
                ("title", "E-001", 500.0, 40.0),
                ("note", "GENERAL NOTES", 72.0, 700.0),
            ),
        },
    )

    blind, empty = select_device_pages(document).pages

    assert (blind.recognized_device_count, blind.symbol_like_count) == (0, 4)
    assert blind.reason == "electrical_no_recognized_devices"
    assert blind.included is False
    assert (empty.recognized_device_count, empty.symbol_like_count) == (0, 0)
    assert empty.reason == "electrical_no_recognized_devices"


def test_tag_like_text_definition() -> None:
    for text in ("RECEPT-1", "LTG-2", "D1", "a", "WP", "GFI", "$3", "TSTAT-1"):
        assert _looks_tag_like(text), text
    for text in (
        "E-110",
        "M-2.1",
        "101",
        "GENERAL NOTES",
        "NOTES",
        "ALL WORK PER CODE.",
        "RECEPTACLE-100",
        "",
    ):
        assert not _looks_tag_like(text), text


def _page_restriction(document: PdfElectricalDocument, page: int) -> PdfElectricalDocument:
    return PdfElectricalDocument(
        source_id=document.source_id,
        page_count=document.page_count,
        texts=tuple(item for item in document.texts if item.page == page),
        symbols=tuple(item for item in document.symbols if item.page == page),
        vectors=tuple(item for item in document.vectors if item.page == page),
        page_provenance=dict(document.page_provenance),
    )


def _isolated_counts(document: PdfElectricalDocument) -> list[int]:
    importer = ElectricalPdfImporter()
    return [
        len(importer.import_document(_page_restriction(document, page)).electrical_devices)
        for page in range(1, document.page_count + 1)
    ]


class _CountingImporter(ElectricalPdfImporter):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def import_document(self, document, *args, **kwargs):  # type: ignore[override]
        self.calls += 1
        return super().import_document(document, *args, **kwargs)


def _synthetic_documents(tmp_path: Path) -> list[PdfElectricalDocument]:
    pdf_path = tmp_path / "five-page-set.pdf"
    _write_five_page_set(pdf_path)
    documents = [extract_pdf(pdf_path, source_id="synthetic:five-page-set")]
    documents.append(
        _document_with_pages(
            {
                "texts": (("title", "P-401", 500.0, 40.0),),
                "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 300.0),),
            },
            {"symbols": (("R2", "DUPLEX RECEPTACLE OUTLET", 120.0, 300.0),)},
            {
                "texts": (
                    ("title", "E-210", 500.0, 40.0),
                    ("device", "RECEPT-1", 120.0, 300.0),
                ),
                "symbols": (("R3", "DUPLEX RECEPTACLE OUTLET", 120.0, 280.0),),
            },
        )
    )
    fixture_root = ROOT / "fixtures" / "pdf_electrical"
    for name in (
        "geometry-only-power-sheet-with-legend.pdf",
        "geometry-only-power-sheet-circuit-homeruns.pdf",
        "two-page-sheet-local-legend.pdf",
    ):
        documents.append(
            extract_pdf(fixture_root / name, source_id=f"synthetic:{Path(name).stem}")
        )
    return documents


def test_single_import_counts_match_per_page_isolation(tmp_path: Path) -> None:
    for document in _synthetic_documents(tmp_path):
        importer = _CountingImporter()
        selection = select_device_pages(document, importer=importer)

        counts = [choice.recognized_device_count for choice in selection.pages]
        assert importer.calls == 1, document.source_id
        assert counts == _isolated_counts(document), document.source_id
        assert [choice.device_count for choice in selection.pages] == counts
        # Every device the whole import produced is attributed to one page.
        full = ElectricalPdfImporter().import_document(document)
        assert sum(counts) == len(full.electrical_devices), document.source_id


def test_cross_page_legend_devices_are_counted_on_their_own_page() -> None:
    # The legend lives on another sheet and is referenced explicitly, so a
    # page imported in isolation cannot resolve its symbols; the single
    # whole-document import does, exactly like the caller's own import.
    document = extract_pdf(
        ROOT / "fixtures" / "pdf_electrical" / "separate-sheet-explicit-legend-reference.pdf",
        source_id="synthetic:separate-sheet-explicit-legend-reference",
    )

    counts = [choice.recognized_device_count for choice in select_device_pages(document).pages]
    full = ElectricalPdfImporter().import_document(document)

    assert _isolated_counts(document) == [0, 0, 0]
    assert counts == [0, 6, 6]
    assert sum(counts) == len(full.electrical_devices)


# --- Sheet ids embedded in longer hyphenated tokens (PR #182 review) ---


def test_panel_circuit_callouts_do_not_outvote_the_title_block_sheet_id() -> None:
    # `P-1-12` is a panel/circuit callout, not plumbing sheet `P-1`. Before
    # the fix a trailing `\b` let the following hyphen end the match, so two
    # callouts outvoted the single title-block `E-201` and the electrical
    # sheet was excluded as a plumbing sheet.
    document = _document_with_pages(
        {
            "texts": (
                ("callout1", "PANEL P-1-12", 100.0, 600.0),
                ("callout2", "FED FROM P-1-12", 100.0, 560.0),
                ("title", "E-201", 1200.0, 20.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 300.0, 300.0),),
        },
        page_size=(1224.0, 792.0),
    )

    assert sheet_identity(document, 1) == ("E-201", "electrical")
    (choice,) = select_device_pages(document).pages
    assert (choice.sheet_id, choice.discipline) == ("E-201", "electrical")
    assert choice.device_count == 1
    assert choice.included is True
    assert choice.reason == "electrical_with_devices"


def test_many_letter_led_tags_do_not_outvote_the_sheet_id() -> None:
    document = _document_with_pages(
        {
            "texts": (
                *(
                    (f"tag{index}", "LF-1", 100.0 + 40.0 * index, 400.0)
                    for index in range(5)
                ),
                ("title", "E-201", 560.0, 40.0),
            ),
        },
    )

    assert sheet_identity(document, 1) == ("E-201", "electrical")


def test_hyphenated_callouts_alone_yield_no_sheet_id_candidate() -> None:
    for text in (
        "P-1-12",
        "PANEL P-1-12",
        "HP-E-3",
        "E-201-4",
        "E-2.1.3",
        "E-12345",
        "LF-1",
    ):
        document = _document_with_pages({"texts": (("only", text, 72.0, 700.0),)})
        assert sheet_identity(document, 1) == (None, "unknown"), text


def test_standalone_sheet_ids_are_still_recognised() -> None:
    cases = {
        "E-2.1": ("E-2.1", "electrical"),
        "ELEC-1": ("ELEC-1", "electrical"),
        "EL-3": ("EL-3", "electrical"),
        "SEE E-201.": ("E-201", "electrical"),
        "(FP-2)": ("FP-2", "fire_protection"),
        "SHEET M-101, NOTE 3": ("M-101", "mechanical"),
    }
    for text, expected in cases.items():
        document = _document_with_pages({"texts": (("only", text, 72.0, 700.0),)})
        assert sheet_identity(document, 1) == expected, text


def _write_rotated_title_block_page(path: Path) -> None:
    """One /Rotate 90 page, displayed 792 x 612 (landscape).

    The raw MediaBox is 612 x 792 (portrait). A displayed point (x, y) is
    stored at raw (612 - y, x). ELECTRICAL sits in the displayed bottom
    title-block band (raw right edge); MECHANICAL repeats ten times along the
    raw bottom edge, which displays as the left edge, outside the band.
    """

    raw_width, raw_height = 612.0, 792.0
    writer = PdfWriter()
    page = writer.add_blank_page(width=raw_width, height=raw_height)
    page[NameObject("/Rotate")] = NumberObject(90)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )

    def raw(x: float, y: float) -> tuple[float, float]:
        return raw_width - y, x

    lines = [_text_line(*raw(300.0, 30.0), "ELECTRICAL")]
    for index in range(10):
        lines.append(_text_line(*raw(30.0, 120.0 + 40.0 * index), "MECHANICAL"))
    stream = DecodedStreamObject()
    stream.set_data(b"".join(lines))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_rotated_page_title_block_band_uses_displayed_edges(tmp_path: Path) -> None:
    pdf_path = tmp_path / "rotated-title-block.pdf"
    _write_rotated_title_block_page(pdf_path)
    document = extract_pdf(pdf_path, source_id="synthetic:rotated-title-block")

    provenance = document.page_provenance[1]
    assert provenance["page_rotation"] == 90
    assert (
        provenance["displayed_page_width_pt"],
        provenance["displayed_page_height_pt"],
    ) == (792.0, 612.0)
    # The raw bottom edge holds MECHANICAL ten times; in displayed space it is
    # the left edge, outside the title-block band. The displayed bottom band
    # holds ELECTRICAL once.
    assert sheet_identity(document, 1) == (None, "electrical")


# --- Stop words in the tag-like recall disclosure (PR #182 review) ---


def test_short_english_words_are_not_tag_like() -> None:
    for word in (
        "AND", "THE", "ALL", "FOR", "OF", "SEE", "NOT", "TO", "AT", "IN",
        "ON", "BY", "OR", "NO", "AS", "IS", "BE", "IF", "UP", "SET", "PER",
        "VIA", "TYP", "EQ", "and", "The",
    ):
        assert not _looks_tag_like(word), word
    # Real short codes still count.
    for code in ("a", "WP", "GFI", "$3", "D1"):
        assert _looks_tag_like(code), code


def test_notes_only_sheet_emitted_word_by_word_has_low_symbol_like_count() -> None:
    note = (
        "SEE NOTE 3. ALL WORK TO BE DONE PER THE PLANS AND AS SET BY OTHERS "
        "IF NOT NOTED ON OR IN THE FIELD. VERIFY UP TO EQ SPACING, TYP. "
        "NO WORK IS FOR OF AT VIA"
    )
    words = tuple(
        (f"w{index}", word, 72.0 + 30.0 * (index % 15), 700.0 - 14.0 * (index // 15))
        for index, word in enumerate(note.split())
    )
    document = _document_with_pages(
        {"texts": (*words, ("title", "E-002", 560.0, 40.0))},
    )

    (choice,) = select_device_pages(document).pages

    assert (choice.sheet_id, choice.discipline) == ("E-002", "electrical")
    assert choice.symbol_like_count == 0
    assert choice.reason == "electrical_no_recognized_devices"


# --- Whole-document import refusal falls back to per-page imports (PR #182) ---


def _same_panel_on_two_sheets() -> PdfElectricalDocument:
    # One invented panel tag drawn on two electrical sheets, as a panel often
    # is on several plans. The importer correctly refuses to canonicalize one
    # identity recognized at two source locations.
    return _document_with_pages(
        {
            "texts": (
                ("title", "E-101", 560.0, 40.0),
                ("panel", "PANEL LP", 100.0, 500.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 300.0, 300.0),),
        },
        {
            "texts": (
                ("title", "E-102", 560.0, 40.0),
                ("panel", "PANEL LP", 100.0, 500.0),
            ),
            "symbols": (
                ("R2", "DUPLEX RECEPTACLE OUTLET", 300.0, 300.0),
                ("R3", "DUPLEX RECEPTACLE OUTLET", 400.0, 300.0),
            ),
        },
        {"texts": (("title", "M-101", 560.0, 40.0),)},
    )


def test_same_tagged_equipment_on_two_pages_falls_back_to_per_page_imports() -> None:
    document = _same_panel_on_two_sheets()
    # The plain whole-document import raises; before the fix selection did too.
    with pytest.raises(ElectricalPdfError, match="same stable semantic identity"):
        ElectricalPdfImporter().import_document(document)

    importer = _CountingImporter()
    selection = select_device_pages(document, importer=importer)

    assert importer.calls == 1 + document.page_count
    assert selection.import_mode == "per_page_fallback"
    assert selection.import_fallback_reason == (
        "ElectricalPdfError: the same stable semantic identity was recognized "
        "at multiple source locations"
    )
    counts = [choice.recognized_device_count for choice in selection.pages]
    assert counts == _isolated_counts(document) == [1, 2, 0]
    assert [
        (choice.sheet_id, choice.included, choice.reason) for choice in selection.pages
    ] == [
        ("E-101", True, "electrical_with_devices"),
        ("E-102", True, "electrical_with_devices"),
        ("M-101", False, "mechanical_sheet"),
    ]
    payload = selection.to_dict()
    assert payload["import_mode"] == "per_page_fallback"
    assert payload["import_fallback_reason"] == selection.import_fallback_reason
    # No source text leaks into the reason: not the tag, not the location keys.
    assert "LP" not in selection.import_fallback_reason
    assert "p1:" not in selection.import_fallback_reason
    # Deterministic.
    assert select_device_pages(document).to_dict() == payload


def test_document_import_success_reports_document_mode() -> None:
    document = _document_with_pages(
        {
            "texts": (
                ("title", "E-101", 560.0, 40.0),
                ("panel", "PANEL LP", 100.0, 500.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 300.0, 300.0),),
        },
    )
    importer = _CountingImporter()
    selection = select_device_pages(document, importer=importer)

    assert importer.calls == 1
    assert selection.import_mode == "document"
    assert selection.import_fallback_reason is None


class _RefusingImporter(ElectricalPdfImporter):
    """Refuses the whole document, and also any single page listed in ``refuse_pages``."""

    def __init__(self, refuse_pages: set[int]) -> None:
        super().__init__()
        self.refuse_pages = refuse_pages

    def import_document(self, document, *args, **kwargs):  # type: ignore[override]
        pages = {item.page for item in (*document.texts, *document.symbols)}
        if len(pages) > 1 or pages & self.refuse_pages:
            raise ElectricalPdfError("placeholder refusal quoting SOURCE-TAG-9")
        return super().import_document(document, *args, **kwargs)


def test_unknown_refusal_uses_a_generic_source_free_reason() -> None:
    document = _same_panel_on_two_sheets()

    selection = select_device_pages(document, importer=_RefusingImporter(set()))

    assert selection.import_mode == "per_page_fallback"
    assert selection.import_fallback_reason == (
        "ElectricalPdfError: the whole-document import refused the document"
    )
    assert "SOURCE-TAG-9" not in str(selection.to_dict())
    assert [choice.device_count for choice in selection.pages] == [1, 2, 0]


def test_page_whose_own_import_raises_is_reported_not_raised() -> None:
    document = _same_panel_on_two_sheets()

    selection = select_device_pages(document, importer=_RefusingImporter({2, 3}))

    assert selection.import_mode == "per_page_fallback"
    assert [
        (choice.device_count, choice.included, choice.reason)
        for choice in selection.pages
    ] == [
        (1, True, "electrical_with_devices"),
        (0, False, "import_failed"),
        (0, False, "mechanical_sheet"),
    ]


def test_non_importer_errors_are_not_swallowed() -> None:
    class _BrokenImporter(ElectricalPdfImporter):
        def import_document(self, document, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        select_device_pages(_same_panel_on_two_sheets(), importer=_BrokenImporter())
