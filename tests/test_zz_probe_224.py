"""Probes for #224 item gaps (scratch; will be replaced by real tests)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from oabm.importers.pdf_architecture import importer as architecture_importer
from oabm.importers.pdf_architecture.importer import classify_page, import_observations
from oabm.importers.pdf_architecture.types import (
    PdfDocumentObservation, PdfLineObservation, PdfPageObservation, PdfTextObservation,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "filled_targets", ROOT / "tests/test_pdf_architecture_filled_targets.py",
)
ft = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ft)

FIXTURE = ft.FIXTURE
MPP = ft.MPP
_text = ft._text
_strip = ft._strip
_plan = ft._plan
_electrical = ft._electrical
_document = ft._document


def _two_page_document(pages: tuple[PdfPageObservation, ...], key: str) -> PdfDocumentObservation:
    return PdfDocumentObservation(
        source_id=key,
        content_sha256="a" * 64,
        pages=pages,
    )


def _retitle(page: PdfPageObservation, key: str, value: str, x: float, y: float) -> PdfPageObservation:
    texts = tuple(t for t in page.texts if t.element_id != key)
    return replace_page_texts(page, (*texts, _text(key, value, x, y)))


def replace_page_texts(page: PdfPageObservation, texts) -> PdfPageObservation:
    from dataclasses import replace as dc_replace
    return dc_replace(page, texts=tuple(texts))


def test_probe_site_plan_first_does_not_displace_floor_plan() -> None:
    # Page 1: a SITE PLAN (sparse site geometry, its own scale, no walls).
    site_lines = []
    for i, (x0, y0, x1, y1) in enumerate([(120, 180, 900, 186), (120, 700, 900, 706),
                                          (120, 180, 126, 700), (894, 180, 900, 700)]):
        site_lines.append(PdfLineObservation(
            element_id=f"site:{i}", start_pt=(x0, y0), end_pt=(x1, y1),
            primitive_family="polyline", source_layers=("C-SITE",),
        ))
    site = PdfPageObservation(
        page_number=1, width_pt=FIXTURE["page_width_pt"], height_pt=FIXTURE["page_height_pt"],
        texts=(_text("site-title", "SITE PLAN", 1040, 50),
               _text("site-scale", FIXTURE["scale_text"], 120, 150)),
        lines=tuple(site_lines),
    )
    plan = _retitle(_plan(), "title", "A-110 CONSTRUCTION PLAN", 1040, 50)
    document = _two_page_document((site, plan), "arch")
    model = import_observations(document)
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    resolved = [r for r in regions if r["status"] == "resolved"]
    assert resolved, f"floor region must resolve; regions={regions}"
    # The resolved region is the FLOOR plan's bbox, not the site sheet's.
    bbox = resolved[0]["source_bbox_pt"]
    assert bbox[0] >= 100 and bbox[2] <= 470, f"site sheet displaced the floor: {bbox}"


def test_probe_key_plan_first_does_not_displace_floor_plan() -> None:
    site_lines = [
        PdfLineObservation(element_id=f"key:{i}", start_pt=(x0, y0), end_pt=(x1, y1),
                           primitive_family="polyline", source_layers=("A-KEY",))
        for i, (x0, y0, x1, y1) in enumerate([(120, 180, 700, 186), (120, 560, 700, 566),
                                              (120, 180, 126, 566), (694, 180, 700, 566)])
    ]
    key = PdfPageObservation(
        page_number=1, width_pt=FIXTURE["page_width_pt"], height_pt=FIXTURE["page_height_pt"],
        texts=(_text("key-title", "KEY PLAN", 1040, 50),
               _text("key-scale", FIXTURE["scale_text"], 120, 150)),
        lines=tuple(site_lines),
    )
    plan = _plan()
    document = _two_page_document((key, plan), "arch")
    model = import_observations(document)
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    resolved = [r for r in regions if r["status"] == "resolved"]
    assert resolved, f"floor region must resolve; regions={regions}"
    bbox = resolved[0]["source_bbox_pt"]
    assert bbox[0] >= 100 and bbox[2] <= 470


def test_probe_symbols_inside_plan_area_stay_out_of_wall_evidence() -> None:
    from dataclasses import replace as dc_replace
    page = _plan()
    # Four small filled squares (device symbols) inside the plan region.
    symbol_lines = []
    n = 0
    for (x, y) in [(200, 240), (300, 300), (250, 420), (350, 470)]:
        symbol_lines.extend(_strip(f"symbol:{n}", [x, y, x + 8, y + 8]))
        n += 1
    page = dc_replace(page, lines=(*page.lines, *symbol_lines))
    evidence = ft.sheet_wall_evidence(page, meters_per_point=MPP)
    bbox, segments = evidence.drawings[0]
    ids = {segment_id for segment_id, *_ in [(s,) for s in segments]} if segments else set()
    # Region bbox unchanged versus the no-symbol baseline.
    baseline = ft.sheet_wall_evidence(_plan(), meters_per_point=MPP)
    assert evidence.drawings[0][0] == baseline.drawings[0][0]
    assert len(evidence.drawings) == len(baseline.drawings)


def test_probe_missing_scale_has_precise_reason_code() -> None:
    from dataclasses import replace as dc_replace
    page = _plan()
    texts = tuple(t for t in page.texts if t.element_id != "scale")
    page = dc_replace(page, texts=texts)
    model = import_observations(_document(page, "arch"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    unresolved = [r for r in regions if r["status"] != "resolved"]
    assert unresolved, "no-scale plan must not resolve"
    codes = unresolved[0]["reason_codes"]
    assert codes and any("scale" in code for code in codes), f"codes={codes}"


def test_probe_different_scales_per_region_each_resolve() -> None:
    from dataclasses import replace as dc_replace
    page = _plan(second=True, excluded=False)
    texts = []
    for t in page.texts:
        if t.element_id == "scale-b":
            texts.append(_text("scale-b", 'SCALE: 1/8" = 1\'-0"', 770, 150))
        else:
            texts.append(t)
    page = dc_replace(page, texts=tuple(texts))
    model = import_observations(_document(page, "arch"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    assert len(regions) == 2
    statuses = [r["status"] for r in regions]
    assert all(status == "resolved" for status in statuses), f"statuses={statuses} regions={regions}"


def test_probe_hatch_and_dimensions_stay_out_of_evidence() -> None:
    from dataclasses import replace as dc_replace
    page = _plan()
    extra = []
    # Hatch-only strokes (a parallel-stroke hatch patch, unfilled) inside plan.
    for i in range(6):
        y = 240 + i * 6
        extra.append(PdfLineObservation(
            element_id=f"hatch:{i}", start_pt=(360, y), end_pt=(430, y + 10),
            primitive_family="polyline", filled=False, stroke_present=True,
            source_layers=("A-WALL-PATT",),
        ))
    # Dimension lines with tick text crossing the plan.
    extra.append(PdfLineObservation(
        element_id="dim:1", start_pt=(100, 170), end_pt=(460, 170),
        primitive_family="polyline", source_layers=("A-DIMS",),
    ))
    extra.extend(_strip("dim:tick-l", [96, 166, 104, 174], filled=False))
    extra.extend(_strip("dim:tick-r", [456, 166, 464, 174], filled=False))
    page = dc_replace(page, lines=(*page.lines, *extra))
    evidence = ft.sheet_wall_evidence(page, meters_per_point=MPP)
    baseline = ft.sheet_wall_evidence(_plan(), meters_per_point=MPP)
    assert len(evidence.drawings) == len(baseline.drawings)
    assert evidence.drawings[0][0] == baseline.drawings[0][0]


def test_probe_same_scale_two_regions_still_split() -> None:
    page = _plan(second=True, excluded=False)
    model = import_observations(_document(page, "arch"))
    regions = model.attributes["pdf_architecture"]["drawing_regions"]
    assert len(regions) == 2
