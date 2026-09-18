"""Vector/text extraction from PDF pages.

pdfplumber is intentionally used only to turn PDF primitives into deterministic
source observations.  Semantic interpretation remains in ``importer.py``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

import pdfplumber

from .types import (
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    PdfTextObservation,
)


def _token(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value)).strip("-")[:80]


def _element_id(kind: str, page_number: int, signature: str) -> str:
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
    return f"p{page_number}:{kind}:{digest}"


def _native_id(obj: dict[str, object], kind: str) -> str | None:
    mcid = obj.get("mcid")
    if isinstance(mcid, int):
        tag = _token(obj.get("tag") or kind)
        return f"mcid:{mcid}:{tag}"
    return None


def _group_words(page: object, page_number: int) -> tuple[PdfTextObservation, ...]:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)  # type: ignore[attr-defined]
    rows: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        top = float(word["top"])
        if not rows or abs(top - float(rows[-1][0]["top"])) > 2.5:
            rows.append([word])
        else:
            rows[-1].append(word)

    result: list[PdfTextObservation] = []
    page_height = float(page.height)  # type: ignore[attr-defined]
    for row in rows:
        row.sort(key=lambda item: float(item["x0"]))
        text = " ".join(str(item["text"]) for item in row).strip()
        if not text:
            continue
        x0 = min(float(item["x0"]) for item in row)
        x1 = max(float(item["x1"]) for item in row)
        top = min(float(item["top"]) for item in row)
        bottom = max(float(item["bottom"]) for item in row)
        y0 = page_height - bottom
        y1 = page_height - top
        signature = f"{text}|{x0:.3f}|{y0:.3f}|{x1:.3f}|{y1:.3f}"
        result.append(
            PdfTextObservation(
                element_id=_element_id("text", page_number, signature),
                text=text,
                bbox_pt=(x0, y0, x1, y1),
            )
        )
    return tuple(result)


def _unique_rects(rects: Iterable[dict[str, object]], page_number: int) -> tuple[PdfRectObservation, ...]:
    seen: set[tuple[float, float, float, float]] = set()
    result: list[PdfRectObservation] = []
    for obj in rects:
        bbox = (
            round(float(obj["x0"]), 4),
            round(float(obj["y0"]), 4),
            round(float(obj["x1"]), 4),
            round(float(obj["y1"]), 4),
        )
        if bbox in seen or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        seen.add(bbox)
        signature = "|".join(f"{value:.4f}" for value in bbox)
        result.append(
            PdfRectObservation(
                element_id=_element_id("rect", page_number, signature),
                bbox_pt=bbox,
                native_id=_native_id(obj, "rect"),
            )
        )
    return tuple(sorted(result, key=lambda item: item.bbox_pt))


def _unique_lines(lines: Iterable[dict[str, object]], page_number: int) -> tuple[PdfLineObservation, ...]:
    seen: set[tuple[float, float, float, float]] = set()
    result: list[PdfLineObservation] = []
    for obj in lines:
        a = (round(float(obj["x0"]), 4), round(float(obj["y0"]), 4))
        b = (round(float(obj["x1"]), 4), round(float(obj["y1"]), 4))
        if a == b:
            continue
        start, end = sorted((a, b))
        signature_tuple = (start[0], start[1], end[0], end[1])
        if signature_tuple in seen:
            continue
        seen.add(signature_tuple)
        signature = "|".join(f"{value:.4f}" for value in signature_tuple)
        result.append(
            PdfLineObservation(
                element_id=_element_id("line", page_number, signature),
                start_pt=start,
                end_pt=end,
                native_id=_native_id(obj, "line"),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.start_pt, item.end_pt)))


def extract_pdf(path: str | Path, *, source_id: str | None = None) -> PdfDocumentObservation:
    pdf_path = Path(path)
    payload = pdf_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    logical_source_id = source_id or f"pdf:{_token(pdf_path.stem) or 'document'}"

    pages: list[PdfPageObservation] = []
    with pdfplumber.open(pdf_path) as document:
        for index, page in enumerate(document.pages, start=1):
            pages.append(
                PdfPageObservation(
                    page_number=index,
                    width_pt=float(page.width),
                    height_pt=float(page.height),
                    texts=_group_words(page, index),
                    lines=_unique_lines(page.lines, index),
                    rects=_unique_rects(page.rects, index),
                )
            )
    return PdfDocumentObservation(
        source_id=logical_source_id,
        content_sha256=digest,
        pages=tuple(pages),
    )
