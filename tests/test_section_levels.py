"""Floor/ceiling band detection in building sections: synthetic cases.

Inputs are ``PdfPageObservation``-shaped values built in code, so the cases
stay deterministic and fast; no PDF extraction is involved.  The fixture
mimics the shared facts of a three-storey building section at 1/4" = 1'-0"
(18 pt per foot) with all coordinates invented here.
"""

from __future__ import annotations

import pytest

from oabm.importers.pdf_architecture.section_levels import (
    LevelElevation,
    SectionLevels,
    find_section_level_lines,
    level_elevations_from_bands,
)
from oabm.importers.pdf_architecture.types import PdfLineObservation, PdfPageObservation

QUARTER_INCH_MPP = 48 * 0.0254 / 72  # metres per point at 1/4" = 1'-0" (18 pt per ft)
PT_PER_FT = 18.0
PAGE_W, PAGE_H = 936.0, 612.0
SECTION_X0, SECTION_X1 = 100.0, 600.0

# Three slab bands.  Finished floors (band upper lines) sit 0, 9.42 ft, and
# 19.18 ft above the lowest floor; each band's lower line sits one ceiling
# height (8'-10", then 8'-5") above the floor below.
BAND_LINES = (
    (88.0, 100.0),      # band 0: ceiling, floor
    (259.0, 269.56),    # band 1
    (421.06, 445.24),   # band 2
)
EXPECTED_BANDS = (
    (100.0, 88.0, 1000.0),
    (269.56, 259.0, 1000.0),
    (445.24, 421.06, 1000.0),
)


def _line(
    start: tuple[float, float],
    end: tuple[float, float],
    element_id: str,
) -> PdfLineObservation:
    return PdfLineObservation(element_id=element_id, start_pt=start, end_pt=end)


def _horizontal(y: float, index: int, x0: float = SECTION_X0, x1: float = SECTION_X1) -> PdfLineObservation:
    return _line((x0, y), (x1, y), f"section-line-{index:04d}")


def _section_page(*, with_clutter: bool = True) -> PdfPageObservation:
    lines: list[PdfLineObservation] = []
    index = 0
    for ceiling_y, floor_y in BAND_LINES:
        lines.append(_horizontal(ceiling_y, index))
        index += 1
        lines.append(_horizontal(floor_y, index))
        index += 1
    lines.append(_line((SECTION_X0, 88.0), (SECTION_X0, 445.24), "wall-left"))
    lines.append(_line((SECTION_X1, 88.0), (SECTION_X1, 445.24), "wall-right"))
    if with_clutter:
        # Short text underline, hatch strokes, and a medium dimension tail.
        lines.append(_horizontal(300.0, 90, x0=620.0, x1=680.0))
        lines.append(_horizontal(150.0, 91, x0=300.0, x1=308.0))
        lines.append(_horizontal(505.0, 92, x0=100.0, x1=250.0))
    return PdfPageObservation(page_number=1, width_pt=PAGE_W, height_pt=PAGE_H, lines=tuple(lines))


def test_three_storey_elevations():
    result = find_section_level_lines(_section_page())
    assert result.bands == EXPECTED_BANDS
    assert result.warnings == ()

    levels = level_elevations_from_bands(result.bands, scale_m_per_pt=QUARTER_INCH_MPP)
    assert [level.level_index for level in levels] == [0, 1, 2]
    assert [level.warnings for level in levels] == [(), (), ()]
    elevations_ft = [level.elevation_m / 0.3048 for level in levels]
    assert elevations_ft == pytest.approx([0.0, 9.42, 19.18], abs=0.02)

    # Datum can be any band index; levels below it come out negative.
    from_first_storey = level_elevations_from_bands(
        result.bands, scale_m_per_pt=QUARTER_INCH_MPP, datum=1
    )
    assert [level.elevation_m / 0.3048 for level in from_first_storey] == pytest.approx(
        [-9.42, 0.0, 9.76], abs=0.02
    )


def test_ceiling_height_check():
    heights_m = (106 * 0.0254, 101 * 0.0254)  # 8'-10" and 8'-5"
    bands = find_section_level_lines(_section_page()).bands

    matching = level_elevations_from_bands(
        bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=heights_m
    )
    assert [level.warnings for level in matching] == [(), (), ()]

    # A deliberate mismatch on the second storey is flagged on that level only.
    mismatched = level_elevations_from_bands(
        bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=(heights_m[0], 9.0 * 0.3048)
    )
    assert mismatched[1].warnings == ()
    assert len(mismatched[2].warnings) == 1
    assert "ceiling_height_mismatch" in mismatched[2].warnings[0]
    assert "2.565" in mismatched[2].warnings[0] and "2.743" in mismatched[2].warnings[0]

    # Wrong height count is flagged, and the overlapping pairs still checked.
    miscounted = level_elevations_from_bands(
        bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=(heights_m[0],)
    )
    assert all("ceiling_height_count_mismatch" in level.warnings[0] for level in miscounted[1:])
    assert all("ceiling_height_mismatch" not in warning for level in miscounted[1:] for warning in level.warnings)


def test_clutter_is_ignored():
    result = find_section_level_lines(_section_page(with_clutter=True))
    assert result.bands == EXPECTED_BANDS
    assert result.warnings == ()
    assert len(result.lines) == 6
    assert all(line.element_id.startswith("section-line-") for line in result.lines)


def test_single_band_fails_closed():
    page = PdfPageObservation(
        page_number=1,
        width_pt=PAGE_W,
        height_pt=PAGE_H,
        lines=(
            _horizontal(88.0, 0),
            _horizontal(100.0, 1),
        ),
    )
    result = find_section_level_lines(page)
    assert len(result.bands) == 1
    assert len(result.warnings) == 1
    assert "fewer_than_two_bands" in result.warnings[0]

    assert level_elevations_from_bands(result.bands, scale_m_per_pt=QUARTER_INCH_MPP) == []
    assert level_elevations_from_bands(
        result.bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=(2.6924,)
    ) == []


def test_determinism():
    ordered = find_section_level_lines(_section_page(with_clutter=False))
    shuffled_lines = list(_section_page(with_clutter=False).lines)
    shuffled_lines.reverse()
    shuffled = find_section_level_lines(
        PdfPageObservation(page_number=1, width_pt=PAGE_W, height_pt=PAGE_H, lines=tuple(shuffled_lines))
    )
    assert ordered == shuffled
    assert ordered.bands == EXPECTED_BANDS

    bands = ordered.bands
    assert level_elevations_from_bands(
        bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=(2.6924, 2.5654)
    ) == level_elevations_from_bands(
        bands, scale_m_per_pt=QUARTER_INCH_MPP, ceiling_heights_m=(2.6924, 2.5654)
    )
    assert find_section_level_lines(_section_page()) == find_section_level_lines(_section_page())


def test_region_scoping_and_degenerate_region():
    full = find_section_level_lines(_section_page(), region_pt=(50.0, 80.0, 700.0, 480.0))
    assert full.bands == EXPECTED_BANDS

    # A region holding only the top storey finds one band and says so.
    upper = find_section_level_lines(_section_page(), region_pt=(50.0, 300.0, 700.0, 612.0))
    assert len(upper.bands) == 1
    assert any("fewer_than_two_bands" in warning for warning in upper.warnings)

    # A region with no horizontals at all fails closed with a reason.
    empty = find_section_level_lines(_section_page(), region_pt=(620.0, 80.0, 700.0, 290.0))
    assert empty.bands == ()
    assert any("no_horizontal_lines" in warning for warning in empty.warnings)

    # In a narrow region the default minimum length shrinks with the region
    # width, so the 60 pt underline is a lone candidate, not a band.
    narrow = find_section_level_lines(_section_page(), region_pt=(620.0, 290.0, 700.0, 480.0))
    assert narrow.bands == ()
    assert any("fewer_than_two_bands" in warning for warning in narrow.warnings)

    # Degenerate regions never raise.
    degenerate = find_section_level_lines(_section_page(), region_pt=(100.0, 88.0, 100.0, 400.0))
    assert degenerate.bands == ()
    assert any("invalid_region" in warning for warning in degenerate.warnings)

    assert isinstance(full, SectionLevels)
    assert all(isinstance(level, LevelElevation) for level in level_elevations_from_bands(
        full.bands, scale_m_per_pt=QUARTER_INCH_MPP
    ))


def test_unusable_elevation_inputs_fail_closed():
    bands = EXPECTED_BANDS
    assert level_elevations_from_bands(bands, scale_m_per_pt=0.0) == []
    assert level_elevations_from_bands(bands, scale_m_per_pt=-1.0) == []
    assert level_elevations_from_bands([], scale_m_per_pt=QUARTER_INCH_MPP) == []
    # Malformed band entries are dropped; the usable bands still resolve.
    mixed = level_elevations_from_bands(
        ("junk", None, (269.56, 259.0, 1000.0), EXPECTED_BANDS[0], EXPECTED_BANDS[2]),
        scale_m_per_pt=QUARTER_INCH_MPP,
    )
    assert [level.band for level in mixed] == [
        EXPECTED_BANDS[0],
        EXPECTED_BANDS[1],
        EXPECTED_BANDS[2],
    ]

    with pytest.raises(ValueError):
        level_elevations_from_bands(bands, scale_m_per_pt=QUARTER_INCH_MPP, datum=7)
