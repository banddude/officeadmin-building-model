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
    "select_device_pages",
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
_SHEET_ID_RE = re.compile(
    r"\b(?P<prefix>ELEC|EL|E|FP|FA|ID|M|P|A|S|C)-(?P<number>\d{1,4}(?:\.\d{1,3})?)\b",
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

ELECTRICAL_WITH_DEVICES = "electrical_with_devices"
ELECTRICAL_NO_DEVICES = "electrical_no_devices"
UNKNOWN_DISCIPLINE_WITH_DEVICES = "unknown_discipline_with_devices"


@dataclass(frozen=True, slots=True)
class SheetChoice:
    """The selection verdict for one page, in page order."""

    page: int
    sheet_id: str | None
    discipline: str
    device_count: int
    included: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "sheet_id": self.sheet_id,
            "discipline": self.discipline,
            "device_count": self.device_count,
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

    Per page, in page order, the device count is the number of canonical
    devices the importer produces for that page alone: the document is
    restricted to the page (other pages' observations removed) and imported
    with the default importer unless an ``importer`` is supplied. Selection
    rules, in precedence order:

    - electrical discipline with at least one device -> included,
      ``electrical_with_devices``;
    - electrical discipline with no devices -> excluded,
      ``electrical_no_devices``;
    - unknown discipline with at least one device -> excluded,
      ``unknown_discipline_with_devices``, so the sheet is surfaced rather
      than silently used;
    - any other discipline -> excluded, ``<discipline>_sheet``.
    """

    active_importer = importer if importer is not None else ElectricalPdfImporter()
    choices: list[SheetChoice] = []
    for page in range(1, document.page_count + 1):
        restriction = _page_restriction(document, page)
        model = active_importer.import_document(restriction)
        device_count = len(model.electrical_devices)
        sheet_id, discipline = _sheet_identity(document, page)
        choices.append(
            _sheet_choice(
                page=page,
                sheet_id=sheet_id,
                discipline=discipline,
                device_count=device_count,
            )
        )
    return DeviceSheetSelection(pages=tuple(choices))


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


def _sheet_choice(
    *,
    page: int,
    sheet_id: str | None,
    discipline: str,
    device_count: int,
) -> SheetChoice:
    if discipline == "electrical":
        included = device_count >= 1
        reason = ELECTRICAL_WITH_DEVICES if included else ELECTRICAL_NO_DEVICES
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
        included=included,
        reason=reason,
    )


def _sheet_identity(
    document: PdfElectricalDocument,
    page: int,
) -> tuple[str | None, str]:
    """The printed sheet id and discipline of one page.

    Discipline precedence: the prefix of the printed sheet number wins. When
    a page prints no sheet number, title-block discipline words decide. When
    neither is present the discipline is ``unknown`` and the sheet id is
    ``None``.

    One printed sheet number usually repeats (title block, border callouts),
    so the page's sheet id is the most frequent candidate. Ties are broken by
    title-block position: the candidate whose occurrence sits closest to the
    displayed bottom-right page corner, then lexicographically, so the choice
    is always deterministic. Proximity uses the displayed page size from
    extraction provenance when available, otherwise the extent of the page's
    own text positions.
    """

    texts = [item for item in document.texts if item.page == page]
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
    return None, _discipline_word_fallback(texts)


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

    provenance = document.page_provenance.get(page)
    width = provenance.get("displayed_page_width_pt") if provenance else None
    height = provenance.get("displayed_page_height_pt") if provenance else None
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        return float(width), 0.0
    # Without extraction provenance, fall back to the extent of the page's
    # own text positions.
    max_x = max(item.x_pt for item in texts)
    min_y = min(item.y_pt for item in texts)
    return max_x, min_y


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
    """The most frequent title-block discipline word, ties lexicographic."""

    tally: dict[str, int] = {}
    for observation in texts:
        for pattern, discipline in _DISCIPLINE_WORD_PATTERNS:
            hits = len(pattern.findall(observation.text))
            if hits:
                tally[discipline] = tally.get(discipline, 0) + hits
    if not tally:
        return "unknown"
    return min(tally, key=lambda discipline: (-tally[discipline], discipline))
