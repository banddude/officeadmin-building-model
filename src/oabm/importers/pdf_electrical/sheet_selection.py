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
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfTextObservation,
)

__all__ = [
    "DISCIPLINES",
    "DeviceSheetSelection",
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
# A printed sheet id is the discipline prefix, a hyphen, and the sheet
# number, e.g. `E-110` or `FP-2`. Unseparated (`E110`) and space-separated
# (`E 110`) forms are deliberately not sheet ids: unseparated letter+digit
# tokens are dominated by grid bubbles and device tags (`A1`, `P1`), so
# accepting them would misread ordinary plan annotation as sheet numbers.
#
# The id must stand alone as a token. It may not be preceded by a word
# character or hyphen (`LF-1` and `HP-E-3` hold no sheet id), and it may not
# be followed by a word character, a hyphen, or `.digit`. A plain trailing
# `\b` is not enough: a following hyphen satisfies it, so a panel/circuit
# callout such as `P-1-12` would yield the truncated id `P-1`, and two such
# callouts would outvote the single title-block sheet number. Sentence
# punctuation after the id (`SEE E-201.`) is still accepted.
_SHEET_ID_RE = re.compile(
    r"(?<![-\w])(?P<prefix>ELEC|EL|E|FP|FA|ID|M|P|A|S|C)"
    r"-(?P<number>\d{1,4}(?:\.\d{1,3})?)(?![-\w]|\.\d)",
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

# The title block is taken to be the band along the displayed right edge or
# the displayed bottom edge of the sheet, each this fraction of the page's
# width or height. Only text inside the band can decide the discipline-word
# fallback, so general notes elsewhere on the sheet that mention another
# trade do not decide the sheet's discipline.
TITLE_BLOCK_BAND_FRACTION = 0.15

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
    """One :class:`SheetChoice` per page of the source document, in page order."""

    pages: tuple[SheetChoice, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"pages": [choice.to_dict() for choice in self.pages]}


def select_device_pages(
    document: PdfElectricalDocument,
    *,
    importer: ElectricalPdfImporter | None = None,
) -> DeviceSheetSelection:
    """Select the device-bearing electrical sheets of ``document``.

    The document is imported once, with the default importer unless an
    ``importer`` is supplied, and each canonical device is counted on the
    source page its recognition recorded (``pdf_electrical.source_page``,
    else the page of its first provenance record). Selection rules, in
    precedence order:

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
    """

    active_importer = importer if importer is not None else ElectricalPdfImporter()
    model = active_importer.import_document(document)
    devices_per_page: dict[int, int] = {}
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
            )
        )
    return DeviceSheetSelection(pages=tuple(choices))


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
) -> SheetChoice:
    if discipline == "electrical":
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

    Discipline precedence: the prefix of the printed sheet number wins. When
    a page prints no sheet number, discipline words inside the title-block
    band decide (see :data:`TITLE_BLOCK_BAND_FRACTION`); words elsewhere on
    the sheet are ignored. When neither is present the discipline is
    ``unknown`` and the sheet id is ``None``.

    One printed sheet number usually repeats (title block, border callouts),
    so the page's sheet id is the most frequent candidate. Ties are broken by
    title-block position: the candidate whose occurrence sits closest to the
    displayed bottom-right page corner, then lexicographically, so the choice
    is always deterministic. Proximity uses the displayed page size from
    extraction provenance when available, otherwise the extent of the page's
    own text positions.
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
    candidates = _sheet_id_candidates(texts)
    if candidates:
        sheet_id = _tally_candidates(
            candidates,
            corner=_title_block_corner(document, page, texts),
        )
        prefix = sheet_id.split("-", 1)[0]
        return sheet_id, _SHEET_PREFIX_DISCIPLINES[prefix]
    return None, _discipline_word_fallback(
        _title_block_texts(document, page, texts)
    )


# Kept for callers that used the private name before it became public.
_sheet_identity = sheet_identity


def _sheet_id_candidates(
    texts: Sequence[PdfTextObservation],
) -> list[tuple[str, float, float]]:
    """`(normalized id, x_pt, y_pt)` for every sheet-number match, in text order."""

    candidates: list[tuple[str, float, float]] = []
    for observation in texts:
        for match in _SHEET_ID_RE.finditer(observation.text):
            prefix = match.group("prefix").upper()
            number = match.group("number")
            candidates.append(
                (f"{prefix}-{number}", observation.x_pt, observation.y_pt)
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

    min_x, min_y, max_x, max_y = _page_extent(document, page, texts)
    right_band_start = max_x - TITLE_BLOCK_BAND_FRACTION * (max_x - min_x)
    bottom_band_end = min_y + TITLE_BLOCK_BAND_FRACTION * (max_y - min_y)
    return [
        item
        for item in texts
        if item.x_pt >= right_band_start or item.y_pt <= bottom_band_end
    ]


def _tally_candidates(
    candidates: Sequence[tuple[str, float, float]],
    *,
    corner: tuple[float, float],
) -> str:
    """Pick one sheet id: most frequent, then closest to the corner, then lexical."""

    tally: dict[str, int] = {}
    best_position: dict[str, float] = {}
    for sheet_id, x_pt, y_pt in candidates:
        tally[sheet_id] = tally.get(sheet_id, 0) + 1
        distance = _distance_to_corner(x_pt, y_pt, corner)
        if sheet_id not in best_position or distance < best_position[sheet_id]:
            best_position[sheet_id] = distance
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
