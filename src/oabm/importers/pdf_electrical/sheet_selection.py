"""Deterministic device-sheet selection for the electrical PDF lane.

Public callers need one explainable rule for which pages of a set carry
electrical devices: every electrical-discipline sheet that has devices is
included, and every other sheet is listed with the reason it was excluded
(#180). This module reads only what extraction already produced; it never
changes importer behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from oabm.importers.pdf_electrical.importer import (
    ElectricalPdfError,
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfTextObservation,
)

__all__ = [
    "DISCIPLINES",
    "DeviceSheetSelection",
    "IMPORT_MODES",
    "SheetChoice",
    "TITLE_BLOCK_BAND_FRACTION",
    "select_device_pages",
    "sheet_identity",
]

# Canonical discipline values, in the order they are documented.
DISCIPLINES: tuple[str, ...] = (
    "electrical",
    "mechanical",
    "plumbing",
    "fire_protection",
    "architectural",
    "structural",
    "civil",
    "unknown",
)

# Printed sheet-number prefix -> canonical discipline. Longest prefixes are
# tried first so `ELEC-1` is electrical, not an `E` sheet with a stray `LEC`.
_SHEET_PREFIX_DISCIPLINES: Mapping[str, str] = {
    "E": "electrical",
    "EL": "electrical",
    "ELEC": "electrical",
    "M": "mechanical",
    "P": "plumbing",
    "FP": "fire_protection",
    "FA": "fire_protection",
    "A": "architectural",
    "ID": "architectural",
    "S": "structural",
    "C": "civil",
}
# A printed sheet id is the discipline prefix, a separator, and the sheet
# number, e.g. `E-110`, `FP-2`, `E2.1`, `E 2.1` or `E–2.1` (#219). The
# unseparated and space-separated forms are accepted only with a dotted
# number (`E2.1`, `E 2.1`): the dot is what keeps them apart from the grid
# bubbles and device tags (`A1`, `P1`) that dominate ordinary plan
# annotation, so a bare letter+digit token (`E110`, `E 1`) is still not a
# sheet id. An en dash or em dash separator (`E–2.1`, `E—2.1`) is accepted
# the same way. The normalized id always uses the hyphen form, so every
# separator spelling maps to the same discipline.
#
# The discipline prefixes are exactly the documented list in
# :data:`_SHEET_PREFIX_DISCIPLINES`. A token with a non-standard prefix
# (`PP-1.0`, a pricing plan) matches no candidate, so the sheet stays
# ``unknown`` unless a discipline word inside the title-block band decides
# it; the prefix list is never extended by what a set happens to print.
#
# The id must stand alone as a token. It may not be preceded by a word
# character, hyphen, comma, or slash (`LF-1`, `HP-E-3`, and the `3/A-1`
# half of a detail reference hold no sheet id), and it may not be followed
# by a word character, a hyphen, `.digit`, or a comma/slash that continues
# into another number. That continuation rule is what rejects the
# multi-sheet callouts `A-1,3` and `P-1/12` (and a truncated `A-1` inside
# them) while a sentence comma still reads fine: `SHEET M-101, NOTE 3`
# keeps its id because the comma is followed by a word, not a digit. A
# plain trailing `\b` is not enough: a following hyphen satisfies it, so a
# panel/circuit callout such as `P-1-12` would yield the truncated id
# `P-1`. Sentence punctuation after the id (`SEE E-201.`) is still
# accepted.
_SHEET_ID_RE = re.compile(
    r"(?<![-\w.,/])(?P<prefix>ELEC|EL|E|FP|FA|ID|M|P|A|S|C)"
    r"(?:-(?P<number>\d{1,4}(?:\.\d{1,3})?)"
    r"|[\s–—]?(?P<dotted>\d{1,3}\.\d{1,3}))"
    r"(?![-\w]|\.\d|[,/]\d)",
    re.IGNORECASE,
)

# Title-block discipline words, used only when a page prints no sheet number.
_DISCIPLINE_WORD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bELECTRICAL?\b", re.IGNORECASE), "electrical"),
    (re.compile(r"\bMECHANICAL\b|\bHVAC\b", re.IGNORECASE), "mechanical"),
    (re.compile(r"\bPLUMBING\b", re.IGNORECASE), "plumbing"),
    (
        re.compile(r"\bFIRE\s+(?:PROTECTION|ALARM)\b", re.IGNORECASE),
        "fire_protection",
    ),
    (re.compile(r"\bARCHITECTURAL\b", re.IGNORECASE), "architectural"),
    (re.compile(r"\bSTRUCTURAL\b", re.IGNORECASE), "structural"),
    (re.compile(r"\bCIVIL\b", re.IGNORECASE), "civil"),
)

# An unnumbered drawing can name its trade in the drawing title even when
# that title is outside the narrow title-block band. Require a drawing kind
# as well as a discipline word so ordinary notes cannot supply an identity.
_DRAWING_TITLE_RE = re.compile(
    r"\b(?:PLAN|ELEVATION|SECTION|SCHEDULE|DIAGRAM|DETAIL|RCP)\b",
    re.IGNORECASE,
)
_DRAWING_NOTE_START_RE = re.compile(
    r"^(?:SEE|REFER|COORDINATE|NOTE|NOTES|VERIFY|PROVIDE)\b",
    re.IGNORECASE,
)

# The title block is taken to be the band along the displayed right edge or
# the displayed bottom edge of the sheet, each this fraction of the page's
# width or height. Only text inside the band can decide the discipline-word
# fallback, so general notes elsewhere on the sheet that mention another
# trade do not decide the sheet's discipline.
TITLE_BLOCK_BAND_FRACTION = 0.15

# The sheet-number cell holds the band's largest text: a band candidate at
# least this much taller than the runner-up is the sheet's own number, however
# often smaller references to other sheets repeat beside it (#219).
_SHEET_NUMBER_HEIGHT_RATIO = 1.5

# Text that looks like a device tag or symbol code: a single token of at most
# ten characters that is either a one-to-three character code (`a`, `WP`,
# `GFI`, `$3`) or a letter-led tag carrying a digit (`RECEPT-1`, `LTG-2`,
# `D1`). Used only for the `symbol_like_count` recall disclosure.
_TAG_LIKE_TEXT_RE = re.compile(
    r"[A-Z$][A-Z0-9$]{0,2}|[A-Z$][A-Z0-9$]*[-.]?\d[A-Z0-9.-]*",
    re.IGNORECASE,
)
_TAG_LIKE_TEXT_MAX_CHARS = 10
# Common short English words and drafting abbreviations that fit the
# one-to-three character code shape but are never device tags. Extractors
# that emit general notes word by word would otherwise inflate
# `symbol_like_count` on notes-only sheets. Compared case-insensitively.
# `A` is deliberately absent: a lone `a` is a common switch-leg symbol code.
_TAG_LIKE_STOP_WORDS: frozenset[str] = frozenset(
    {
        "AND", "THE", "ALL", "FOR", "OF", "SEE", "NOT", "TO", "AT", "IN",
        "ON", "BY", "OR", "NO", "AS", "IS", "BE", "IF", "UP", "SET", "PER",
        "VIA", "TYP", "EQ",
    }
)

ELECTRICAL_WITH_DEVICES = "electrical_with_devices"
ELECTRICAL_NO_RECOGNIZED_DEVICES = "electrical_no_recognized_devices"
UNKNOWN_DISCIPLINE_WITH_DEVICES = "unknown_discipline_with_devices"
IMPORT_FAILED = "import_failed"

# How device counts were obtained. `document`: one whole-document import.
# `per_page_fallback`: the whole-document import raised ElectricalPdfError
# (for example the same tagged panel drawn on two sheets, which the importer
# correctly refuses to canonicalize), so each page was imported on its own.
IMPORT_MODES: tuple[str, ...] = ("document", "per_page_fallback")

# Fixed, source-free summaries of known importer refusals, keyed by the
# importer's own message prefix. The fallback reason never echoes the error
# message itself, because importer messages quote source tags and text.
_KNOWN_IMPORT_REFUSALS: tuple[tuple[str, str], ...] = (
    (
        "the same stable semantic identity was recognized at multiple source locations",
        "the same stable semantic identity was recognized at multiple source locations",
    ),
)
_GENERIC_IMPORT_REFUSAL = "the whole-document import refused the document"


@dataclass(frozen=True, slots=True)
class SheetChoice:
    """The selection verdict for one page, in page order.

    ``device_count`` and ``recognized_device_count`` are the same number: the
    canonical devices the importer recognized on this page. The second name
    states the limit plainly, next to ``symbol_like_count`` (symbol
    observations plus tag-like texts the extractor saw on the page), so a
    caller can tell an empty sheet (both zero) from a sheet whose symbols
    the importer does not recognize (no devices, many symbol-like
    observations).
    """

    page: int
    sheet_id: str | None
    discipline: str
    device_count: int
    recognized_device_count: int
    symbol_like_count: int
    included: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "sheet_id": self.sheet_id,
            "discipline": self.discipline,
            "device_count": self.device_count,
            "recognized_device_count": self.recognized_device_count,
            "symbol_like_count": self.symbol_like_count,
            "included": self.included,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DeviceSheetSelection:
    """One :class:`SheetChoice` per page of the source document, in page order.

    ``import_mode`` is one of :data:`IMPORT_MODES`. ``import_fallback_reason``
    is ``None`` for a whole-document import; for the per-page fallback it is
    the error class and a fixed short summary (``"ElectricalPdfError: ..."``)
    that never contains source text.
    """

    pages: tuple[SheetChoice, ...]
    import_mode: str = "document"
    import_fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_mode": self.import_mode,
            "import_fallback_reason": self.import_fallback_reason,
            "pages": [choice.to_dict() for choice in self.pages],
        }


def select_device_pages(
    document: PdfElectricalDocument,
    *,
    importer: ElectricalPdfImporter | None = None,
) -> DeviceSheetSelection:
    """Select the device-bearing electrical sheets of ``document``.

    The document is imported once, with the default importer unless an
    ``importer`` is supplied, and each canonical device is counted on the
    source page its recognition recorded (``pdf_electrical.source_page``,
    else the page of its first provenance record); ``import_mode`` is then
    ``document``.

    Selection never raises because the importer refuses the document. When
    the whole-document import raises :class:`ElectricalPdfError` (typically
    the same tagged equipment drawn on two sheets), each page is imported on
    its own instead, with every other page's observations removed, and its
    device count is that import's device count; ``import_mode`` is then
    ``per_page_fallback`` and ``import_fallback_reason`` names the error
    class with a fixed, source-free summary. If a single page's own import
    also raises, that page's device count is 0 and an electrical or
    unknown-discipline page is excluded as ``import_failed``.

    Selection rules, in precedence order:

    - electrical discipline with at least one recognized device -> included,
      ``electrical_with_devices``;
    - electrical discipline with no recognized devices -> excluded,
      ``electrical_no_recognized_devices`` (compare ``symbol_like_count`` to
      tell an empty sheet from one whose symbols the importer does not
      recognize);
    - unknown discipline with at least one device -> excluded,
      ``unknown_discipline_with_devices``, so the sheet is surfaced rather
      than silently used;
    - any other discipline -> excluded, ``<discipline>_sheet``.

    In the per-page fallback, a page whose own import raised is excluded as
    ``import_failed`` when its discipline is electrical or unknown; any other
    discipline keeps ``<discipline>_sheet``.
    """

    active_importer = importer if importer is not None else ElectricalPdfImporter()
    import_mode = "document"
    import_fallback_reason: str | None = None
    failed_pages: set[int] = set()
    devices_per_page: dict[int, int] = {}
    try:
        model = active_importer.import_document(document)
    except ElectricalPdfError as error:
        import_mode = "per_page_fallback"
        import_fallback_reason = _import_fallback_reason(error)
        for page in range(1, document.page_count + 1):
            try:
                page_model = active_importer.import_document(
                    _page_restriction(document, page)
                )
            except ElectricalPdfError:
                failed_pages.add(page)
                continue
            devices_per_page[page] = len(page_model.electrical_devices)
    else:
        for device in model.electrical_devices:
            page = _device_source_page(device)
            if page is not None:
                devices_per_page[page] = devices_per_page.get(page, 0) + 1
    texts_by_page: dict[int, list[PdfTextObservation]] = {}
    for text in document.texts:
        texts_by_page.setdefault(text.page, []).append(text)
    symbols_per_page: dict[int, int] = {}
    for symbol in document.symbols:
        symbols_per_page[symbol.page] = symbols_per_page.get(symbol.page, 0) + 1

    choices: list[SheetChoice] = []
    for page in range(1, document.page_count + 1):
        page_texts = texts_by_page.get(page, [])
        sheet_id, discipline = _identity_from_texts(document, page, page_texts)
        symbol_like_count = symbols_per_page.get(page, 0) + sum(
            1 for text in page_texts if _looks_tag_like(text.text)
        )
        choices.append(
            _sheet_choice(
                page=page,
                sheet_id=sheet_id,
                discipline=discipline,
                device_count=devices_per_page.get(page, 0),
                symbol_like_count=symbol_like_count,
                import_failed=page in failed_pages,
            )
        )
    return DeviceSheetSelection(
        pages=tuple(choices),
        import_mode=import_mode,
        import_fallback_reason=import_fallback_reason,
    )


def _import_fallback_reason(error: ElectricalPdfError) -> str:
    """The error class and a fixed summary; never the message's source text."""

    message = str(error)
    summary = _GENERIC_IMPORT_REFUSAL
    for prefix, known_summary in _KNOWN_IMPORT_REFUSALS:
        if message.startswith(prefix):
            summary = known_summary
            break
    return f"{type(error).__name__}: {summary}"


def _page_restriction(
    document: PdfElectricalDocument,
    page: int,
) -> PdfElectricalDocument:
    """The document with every observation not on ``page`` removed.

    Page numbering and page provenance are kept exactly as extracted, so a
    restricted page is recognized under the same conditions as in the full
    document.
    """

    return PdfElectricalDocument(
        source_id=document.source_id,
        page_count=document.page_count,
        texts=tuple(item for item in document.texts if item.page == page),
        symbols=tuple(item for item in document.symbols if item.page == page),
        vectors=tuple(item for item in document.vectors if item.page == page),
        page_provenance=dict(document.page_provenance),
    )


def _device_source_page(device: Any) -> int | None:
    """The source page one imported device was recognized on."""

    lane = device.attributes.get("pdf_electrical")
    if isinstance(lane, Mapping):
        page = lane.get("source_page")
        if isinstance(page, int) and not isinstance(page, bool):
            return page
    for record in device.provenance:
        if record.page is not None:
            return record.page
    return None


def _looks_tag_like(text: str) -> bool:
    """Whether one text observation looks like a device tag or symbol code."""

    token = text.strip()
    if not token or len(token) > _TAG_LIKE_TEXT_MAX_CHARS:
        return False
    if _SHEET_ID_RE.fullmatch(token):
        return False
    if token.upper() in _TAG_LIKE_STOP_WORDS:
        return False
    return _TAG_LIKE_TEXT_RE.fullmatch(token) is not None


def _sheet_choice(
    *,
    page: int,
    sheet_id: str | None,
    discipline: str,
    device_count: int,
    symbol_like_count: int,
    import_failed: bool = False,
) -> SheetChoice:
    if import_failed and discipline in {"electrical", "unknown"}:
        included = False
        reason = IMPORT_FAILED
    elif discipline == "electrical":
        included = device_count >= 1
        reason = (
            ELECTRICAL_WITH_DEVICES if included else ELECTRICAL_NO_RECOGNIZED_DEVICES
        )
    elif discipline == "unknown" and device_count >= 1:
        included = False
        reason = UNKNOWN_DISCIPLINE_WITH_DEVICES
    else:
        included = False
        reason = f"{discipline}_sheet"
    return SheetChoice(
        page=page,
        sheet_id=sheet_id,
        discipline=discipline,
        device_count=device_count,
        recognized_device_count=device_count,
        symbol_like_count=symbol_like_count,
        included=included,
        reason=reason,
    )


def sheet_identity(
    document: PdfElectricalDocument,
    page: int,
) -> tuple[str | None, str]:
    """The printed sheet id and discipline of one page.

    This is the electrical lane's shared sheet-identity function.

    Discipline precedence: the prefix of the printed sheet number wins. The
    documented prefixes in :data:`_SHEET_PREFIX_DISCIPLINES` are the whole
    list: a non-standard prefix (``PP-1.0``, a pricing plan) matches no
    candidate, so such a sheet stays ``unknown`` unless a discipline word
    inside the title-block band decides it. When a page prints no sheet
    number, those discipline words decide (see
    :data:`TITLE_BLOCK_BAND_FRACTION`); ordinary notes elsewhere are
    ignored. When neither is present, a drawing title outside the band can
    supply a discipline if it names both a trade and a drawing kind (for
    example ``MECHANICAL PLAN``). The largest such title wins. Otherwise the
    discipline is ``unknown`` and the sheet id is ``None``.

    Separators: the canonical id is ``PREFIX-number`` (``E-110``, ``FP-2``).
    A dotted number also accepts a missing, space, or en/em dash separator
    (``E2.1``, ``E 2.1``, ``E–2.1``) and normalizes to the hyphen form, so
    every spelling maps to the same discipline; a bare undotted token still
    needs the hyphen, which keeps grid bubbles and device tags (``A1``,
    ``P1``) out. A callout is not a sheet id: the token may not touch a
    comma or slash on either side, so ``A-1,3`` and ``P-1/12`` (and ``3/A-1``)
    match nothing.

    Which candidate is the sheet's own number: the sheet-number cell holds
    the title-block band's largest text, so a band candidate printed clearly
    largest -- a text height at least :data:`_SHEET_NUMBER_HEIGHT_RATIO`
    times the runner-up's -- wins outright, however often smaller references
    to other sheets (``SEE E-3.1``) repeat. Ties and unknown heights fall
    back to frequency: the most frequent candidate, then the occurrence
    closest to the displayed bottom-right page corner, then lexicographically,
    so the choice is always deterministic. Proximity uses the displayed page
    size from extraction provenance when available, otherwise the extent of
    the page's own text positions.
    """

    texts = [item for item in document.texts if item.page == page]
    return _identity_from_texts(document, page, texts)


def _identity_from_texts(
    document: PdfElectricalDocument,
    page: int,
    texts: Sequence[PdfTextObservation],
) -> tuple[str | None, str]:
    if not texts:
        return None, "unknown"
    band = _title_block_band(document, page, texts)
    band_texts = [item for item in texts if _in_band(item.x_pt, item.y_pt, band)]
    candidates = _sheet_id_candidates(texts)
    band_candidates = _sheet_id_candidates(band_texts)
    if not band_candidates:
        band_discipline = _discipline_word_fallback(band_texts)
        if band_discipline != "unknown":
            return None, band_discipline
        outside_texts = [
            item for item in texts if not _in_band(item.x_pt, item.y_pt, band)
        ]
        title_discipline = _drawing_title_discipline(outside_texts)
        if title_discipline != "unknown":
            return None, title_discipline
    if candidates:
        sheet_id = _tally_candidates(
            candidates,
            corner=_title_block_corner(document, page, texts),
            band=_title_block_band(document, page, texts),
        )
        prefix = sheet_id.split("-", 1)[0]
        return sheet_id, _SHEET_PREFIX_DISCIPLINES[prefix]
    return None, "unknown"


# Kept for callers that used the private name before it became public.
_sheet_identity = sheet_identity


def _sheet_id_candidates(
    texts: Sequence[PdfTextObservation],
) -> list[tuple[str, float, float, float | None]]:
    """`(normalized id, x_pt, y_pt, text height)` for every match, in text order."""

    candidates: list[tuple[str, float, float, float | None]] = []
    for observation in texts:
        for match in _SHEET_ID_RE.finditer(observation.text):
            prefix = match.group("prefix").upper()
            number = match.group("number") or match.group("dotted")
            candidates.append(
                (
                    f"{prefix}-{number}",
                    observation.x_pt,
                    observation.y_pt,
                    observation.font_size_pt,
                )
            )
    return candidates


def _title_block_corner(
    document: PdfElectricalDocument,
    page: int,
    texts: Sequence[PdfTextObservation],
) -> tuple[float, float]:
    """The displayed bottom-right corner of the page, in page points."""

    _, min_y, max_x, _ = _page_extent(document, page, texts)
    return max_x, min_y


def _page_extent(
    document: PdfElectricalDocument,
    page: int,
    texts: Sequence[PdfTextObservation],
) -> tuple[float, float, float, float]:
    """`(min_x, min_y, max_x, max_y)` of the displayed page, in page points.

    Uses the displayed page size from extraction provenance when available,
    otherwise the extent of the page's own text positions.
    """

    provenance = document.page_provenance.get(page)
    width = provenance.get("displayed_page_width_pt") if provenance else None
    height = provenance.get("displayed_page_height_pt") if provenance else None
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return 0.0, 0.0, float(width), float(height)
    return (
        min(item.x_pt for item in texts),
        min(item.y_pt for item in texts),
        max(item.x_pt for item in texts),
        max(item.y_pt for item in texts),
    )


def _title_block_band(
    document: PdfElectricalDocument,
    page: int,
    texts: Sequence[PdfTextObservation],
) -> tuple[float, float, float, float]:
    """`(min_x, min_y, max_x, max_y)` of the page, naming the band's extent.

    The band itself is the strip along the displayed right edge plus the
    strip along the displayed bottom edge, each
    :data:`TITLE_BLOCK_BAND_FRACTION` of the page's width or height; a point
    is in the band when `_in_band` says so against these bounds.
    """

    return _page_extent(document, page, texts)


def _in_band(
    x_pt: float,
    y_pt: float,
    band: tuple[float, float, float, float],
) -> bool:
    """Whether one text position lies in the title-block band."""

    min_x, min_y, max_x, max_y = band
    right_band_start = max_x - TITLE_BLOCK_BAND_FRACTION * (max_x - min_x)
    bottom_band_end = min_y + TITLE_BLOCK_BAND_FRACTION * (max_y - min_y)
    return x_pt >= right_band_start or y_pt <= bottom_band_end


def _title_block_texts(
    document: PdfElectricalDocument,
    page: int,
    texts: Sequence[PdfTextObservation],
) -> list[PdfTextObservation]:
    """The texts inside the title-block band of one page.

    The band is the strip along the displayed right edge, plus the strip
    along the displayed bottom edge, each :data:`TITLE_BLOCK_BAND_FRACTION`
    of the page's width or height.
    """

    band = _title_block_band(document, page, texts)
    return [item for item in texts if _in_band(item.x_pt, item.y_pt, band)]


def _tally_candidates(
    candidates: Sequence[tuple[str, float, float, float | None]],
    *,
    corner: tuple[float, float],
    band: tuple[float, float, float, float],
) -> str:
    """Pick one sheet id: title block first, then most frequent, corner, lexical.

    The sheet's own number sits in the sheet-number cell of the title block:
    inside the band, it is the candidate printed clearly largest -- a text
    height at least :data:`_SHEET_NUMBER_HEIGHT_RATIO` times the runner-up's.
    Frequency alone would let keyed notes that repeat another sheet's number
    outvote it, so the large candidate wins outright. Without a clear height
    winner (equal sizes, or heights unknown to the extractor), the choice is
    the previous rule: most frequent, then the occurrence closest to the
    displayed bottom-right corner, then lexicographic.
    """

    tally: dict[str, int] = {}
    best_position: dict[str, float] = {}
    best_height: dict[str, float] = {}
    for sheet_id, x_pt, y_pt, height in candidates:
        tally[sheet_id] = tally.get(sheet_id, 0) + 1
        distance = _distance_to_corner(x_pt, y_pt, corner)
        if sheet_id not in best_position or distance < best_position[sheet_id]:
            best_position[sheet_id] = distance
        if height is not None and _in_band(x_pt, y_pt, band):
            if sheet_id not in best_height or height > best_height[sheet_id]:
                best_height[sheet_id] = height
    largest = sorted(
        best_height,
        key=lambda sheet_id: (-best_height[sheet_id], best_position[sheet_id], sheet_id),
    )
    if len(largest) >= 2 and best_height[largest[0]] >= (
        _SHEET_NUMBER_HEIGHT_RATIO * best_height[largest[1]]
    ):
        return largest[0]
    if len(largest) == 1:
        # One height-bearing candidate in the band: it is the sheet-number
        # cell, whatever repeats elsewhere without a height.
        return largest[0]
    return min(
        tally,
        key=lambda sheet_id: (
            -tally[sheet_id],
            best_position[sheet_id],
            sheet_id,
        ),
    )


def _distance_to_corner(
    x_pt: float,
    y_pt: float,
    corner: tuple[float, float],
) -> float:
    return ((x_pt - corner[0]) ** 2 + (y_pt - corner[1]) ** 2) ** 0.5


def _discipline_word_fallback(texts: Sequence[PdfTextObservation]) -> str:
    """The most frequent discipline word in ``texts``, ties lexicographic.

    Callers pass only the page's title-block texts.
    """

    tally: dict[str, int] = {}
    for observation in texts:
        for pattern, discipline in _DISCIPLINE_WORD_PATTERNS:
            hits = len(pattern.findall(observation.text))
            if hits:
                tally[discipline] = tally.get(discipline, 0) + hits
    if not tally:
        return "unknown"
    return min(tally, key=lambda discipline: (-tally[discipline], discipline))


def _drawing_title_discipline(texts: Sequence[PdfTextObservation]) -> str:
    """Discipline of the largest outside-band drawing title, if readable."""

    candidates: list[tuple[float, float, str]] = []
    for observation in texts:
        label = observation.text.strip()
        if (
            len(label) > 100
            or len(label.split()) > 10
            or _DRAWING_NOTE_START_RE.search(label)
            or not _DRAWING_TITLE_RE.search(label)
        ):
            continue
        discipline = _discipline_word_fallback((observation,))
        if discipline == "unknown":
            continue
        height = observation.font_size_pt or 0.0
        candidates.append((-height, observation.y_pt, discipline))
    if not candidates:
        return "unknown"
    return min(candidates)[2]
