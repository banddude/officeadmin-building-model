"""Device-sheet selection (#180): every electrical sheet with devices, reasons for the rest."""

from __future__ import annotations

from pathlib import Path

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
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfSymbolObservation,
    PdfTextObservation,
    SymbolRule,
    extract_pdf,
)
from oabm.importers.pdf_electrical.sheet_selection import (
    select_device_pages,
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
        "pages": [
            {
                "page": 1,
                "sheet_id": "E-110",
                "discipline": "electrical",
                "device_count": 2,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 2,
                "sheet_id": "E-120",
                "discipline": "electrical",
                "device_count": 2,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 3,
                "sheet_id": "E-130",
                "discipline": "electrical",
                "device_count": 2,
                "included": True,
                "reason": "electrical_with_devices",
            },
            {
                "page": 4,
                "sheet_id": "E-001",
                "discipline": "electrical",
                "device_count": 0,
                "included": False,
                "reason": "electrical_no_devices",
            },
            {
                "page": 5,
                "sheet_id": "M-101",
                "discipline": "mechanical",
                "device_count": 1,
                "included": False,
                "reason": "mechanical_sheet",
            },
        ]
    }
    # Two identical runs give identical output.
    assert select_device_pages(document).to_dict() == selection.to_dict()


def _document_with_pages(*pages: dict) -> PdfElectricalDocument:
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
                "displayed_page_width_pt": 612.0,
                "displayed_page_height_pt": 792.0,
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
                ("heading", "FIRE ALARM RISER DIAGRAM", 72.0, 700.0),
                ("device", "RECEPT-1", 120.0, 300.0),
            ),
            "symbols": (("R1", "DUPLEX RECEPTACLE OUTLET", 120.0, 280.0),),
        },
        {
            "texts": (
                ("a", "PLUMBING", 72.0, 700.0),
                ("b", "MECHANICAL", 72.0, 680.0),
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
    assert default_choice.reason == "electrical_no_devices"
    assert extended_choice.device_count == 1
    assert extended_choice.included is True
    assert extended_choice.reason == "electrical_with_devices"
