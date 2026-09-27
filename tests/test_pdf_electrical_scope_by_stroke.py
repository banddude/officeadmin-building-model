"""Parameterized new/existing scope from symbol stroke gray (#208).

The stroke-gray scope rule is a read-only helper over an already-extracted
document: it classifies queried symbol positions by the length-weighted
stroke grays of the stroked paths around them and never changes what the
importer extracts. Every PDF here is generated in the test from synthetic
content; the centres, sizes, and layout are invented for these tests.

The default drawing colour in a fresh PDF content stream is DeviceGray black,
so stroked paths read ``stroke_gray`` 0.0 unless the test sets another gray.
"""

import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_electrical import PdfElectricalDocument, PdfVectorPathObservation, extract_pdf
from oabm.importers.pdf_electrical.scope_by_stroke import (
    SCOPE_FRACTION_THRESHOLD,
    StrokeScopeRule,
    scope_by_stroke,
)

RULE = StrokeScopeRule()

# Nine-point symbol circles need a query radius that reaches their stroke;
# symbol centres sit well apart so one query never sees a neighbour.
QUERY_RADIUS_PT = 12.0


def _write_pdf(path: Path, commands: list[str]) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612.0, height=792.0)
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _circle(center: tuple[float, float], radius: float, gray: float | None = None) -> list[str]:
    """A stroked circle approximated by four cubic Bezier arcs, closed with ``h``."""

    kappa = 0.5522847498307936
    off = radius * kappa
    cx, cy = center
    colour = [] if gray is None else [f"{gray:.3f} G"]
    return [
        *colour,
        f"{cx:.3f} {cy + radius:.3f} m",
        f"{cx + off:.3f} {cy + radius:.3f} {cx + radius:.3f} {cy + off:.3f} {cx + radius:.3f} {cy:.3f} c",
        f"{cx + radius:.3f} {cy - off:.3f} {cx + off:.3f} {cy - radius:.3f} {cx:.3f} {cy - radius:.3f} c",
        f"{cx - off:.3f} {cy - radius:.3f} {cx - radius:.3f} {cy - off:.3f} {cx - radius:.3f} {cy:.3f} c",
        f"{cx - radius:.3f} {cy + off:.3f} {cx - off:.3f} {cy + radius:.3f} {cx:.3f} {cy + radius:.3f} c",
        "h S",
    ]


def _extract(tmp_path: Path, name: str, commands: list[str]) -> PdfElectricalDocument:
    pdf = tmp_path / name
    _write_pdf(pdf, commands)
    return extract_pdf(pdf, source_id="test:scope-by-stroke")


def test_five_black_and_gray_symbols_classify_in_query_order(tmp_path: Path) -> None:
    # Three black symbol circles, two gray symbol circles, and a black wall
    # line far from every query point.
    black_centres = [(140.0, 430.0), (300.0, 470.0), (460.0, 430.0)]
    gray_centres = [(210.0, 560.0), (390.0, 590.0)]
    commands = [
        "0 40 m 572 40 l S",
        *(
            command
            for center in black_centres
            for command in _circle(center, 9.0)
        ),
        *(
            command
            for center in gray_centres
            for command in _circle(center, 9.0, 0.3)
        ),
    ]
    document = _extract(tmp_path, "five-symbols.pdf", commands)
    assert len(document.vectors) == 6

    verdicts = scope_by_stroke(
        document,
        [
            (1, *black_centres[0]),
            (1, *black_centres[1]),
            (1, *black_centres[2]),
            (1, *gray_centres[0]),
            (1, *gray_centres[1]),
        ],
        RULE,
        radius_pt=QUERY_RADIUS_PT,
    )
    assert [verdict.scope for verdict in verdicts] == [
        "new",
        "new",
        "new",
        "existing",
        "existing",
    ]
    for verdict in verdicts[:3]:
        assert verdict.fraction_new == pytest.approx(1.0)
        assert verdict.fraction_existing == 0.0
        assert verdict.path_count == 1
    for verdict in verdicts[3:]:
        assert verdict.fraction_existing == pytest.approx(1.0)
        assert verdict.fraction_new == 0.0
        assert verdict.path_count == 1


def test_black_circle_over_gray_circle_is_ambiguous_with_fractions(tmp_path: Path) -> None:
    centre = (250.0, 400.0)
    document = _extract(
        tmp_path,
        "stacked-circles.pdf",
        [*_circle(centre, 9.0, 0.3), *_circle(centre, 9.0, 0.0)],
    )
    (verdict,) = scope_by_stroke(
        document, [(1, *centre)], RULE, radius_pt=QUERY_RADIUS_PT
    )
    assert verdict.scope == "ambiguous"
    assert verdict.fraction_new == pytest.approx(0.5)
    assert verdict.fraction_existing == pytest.approx(0.5)
    assert verdict.path_count == 2


def test_length_weighting_lets_a_long_stroke_dominate(tmp_path: Path) -> None:
    # A 90 pt black stroke and a 30 pt gray stroke both pass within the
    # query radius: 75% new is short of the threshold, so the pair stays
    # ambiguous even though black wins on length.
    document = _extract(
        tmp_path,
        "weighted-strokes.pdf",
        [
            "100 300 m 190 300 l S",
            "0.3 G 145 270 m 145 300 l S",
        ],
    )
    (verdict,) = scope_by_stroke(document, [(1, 145.0, 304.0)], RULE)
    assert verdict.scope == "ambiguous"
    assert verdict.fraction_new == pytest.approx(0.75)
    assert verdict.fraction_existing == pytest.approx(0.25)
    assert verdict.path_count == 2


def test_gray_between_the_ranges_counts_toward_neither_scope(tmp_path: Path) -> None:
    document = _extract(
        tmp_path,
        "gap-gray.pdf",
        ["0.15 G 100 200 m 160 200 l S"],
    )
    (verdict,) = scope_by_stroke(document, [(1, 130.0, 204.0)], RULE)
    assert verdict.scope == "ambiguous"
    assert verdict.fraction_new == 0.0
    assert verdict.fraction_existing == 0.0
    assert verdict.path_count == 1


def test_filled_path_alone_carries_no_scope_evidence(tmp_path: Path) -> None:
    # Scope comes from strokes: a gray-filled shape under the query point is
    # not scope evidence, so the verdict stays unknown.
    document = _extract(
        tmp_path,
        "fill-only.pdf",
        ["0.3 g 120 380 30 30 re f"],
    )
    (verdict,) = scope_by_stroke(document, [(1, 135.0, 395.0)], RULE)
    assert verdict.scope == "unknown"
    assert verdict.fraction_new == 0.0
    assert verdict.fraction_existing == 0.0
    assert verdict.path_count == 0


def test_far_query_point_and_other_page_read_unknown(tmp_path: Path) -> None:
    document = _extract(
        tmp_path,
        "far-query.pdf",
        [*_circle((300.0, 400.0), 9.0), *_circle((300.0, 650.0), 9.0, 0.3)],
    )
    verdicts = scope_by_stroke(
        document,
        [(1, 300.0, 60.0), (2, 300.0, 400.0)],
        RULE,
        radius_pt=QUERY_RADIUS_PT,
    )
    assert [verdict.scope for verdict in verdicts] == ["unknown", "unknown"]
    assert all(verdict.path_count == 0 for verdict in verdicts)
    assert all(verdict.fraction_new == 0.0 for verdict in verdicts)
    assert all(verdict.fraction_existing == 0.0 for verdict in verdicts)


def test_default_radius_bounds_which_strokes_are_nearby(tmp_path: Path) -> None:
    document = _extract(tmp_path, "radius.pdf", ["100 100 m 200 100 l S"])
    inside, outside = scope_by_stroke(
        document, [(1, 150.0, 104.0), (1, 150.0, 110.0)], RULE
    )
    assert inside.scope == "new"
    assert inside.path_count == 1
    assert outside.scope == "unknown"
    assert outside.path_count == 0


def test_hand_built_paths_without_usable_gray_stay_unknown() -> None:
    # A stroked path can omit stroke_gray (pattern colour) or carry a
    # non-numeric value from hand-built input; neither may raise, and
    # neither is scope evidence.
    document = PdfElectricalDocument(
        source_id="test:scope-by-stroke",
        page_count=1,
        vectors=(
            PdfVectorPathObservation(
                "p1:vector:00001",
                1,
                ((50.0, 50.0), (110.0, 50.0)),
                metadata={"paint_operator": "S"},
            ),
            PdfVectorPathObservation(
                "p1:vector:00002",
                1,
                ((50.0, 70.0), (110.0, 70.0)),
                metadata={"paint_operator": "S", "stroke_gray": "bright"},
            ),
        ),
    )
    verdicts = scope_by_stroke(document, [(1, 80.0, 53.0), (1, 80.0, 72.0)], RULE)
    assert [verdict.scope for verdict in verdicts] == ["unknown", "unknown"]
    assert [verdict.path_count for verdict in verdicts] == [1, 1]


def test_rule_rejects_overlapping_or_out_of_range_ranges() -> None:
    for kwargs in (
        {"new_gray_max": 0.3, "existing_gray_min": 0.2},
        {"new_gray_max": 0.2, "existing_gray_min": 0.2},
        {"existing_gray_min": 0.5, "existing_gray_max": 0.4},
        {"new_gray_max": -0.1},
        {"existing_gray_max": 1.1},
        {"new_gray_max": float("nan")},
    ):
        with pytest.raises(ValueError):
            StrokeScopeRule(**kwargs)


def test_rule_defaults_match_the_issue_parameters() -> None:
    rule = StrokeScopeRule()
    assert rule.new_gray_max == 0.1
    assert rule.existing_gray_min == 0.2
    assert rule.existing_gray_max == 0.5


def test_verdicts_are_deterministic_across_runs(tmp_path: Path) -> None:
    document = _extract(
        tmp_path,
        "deterministic.pdf",
        [
            *_circle((180.0, 300.0), 9.0),
            *_circle((360.0, 300.0), 9.0, 0.3),
            *_circle((360.0, 300.0), 9.0, 0.0),
        ],
    )
    queries = [(1, 180.0, 300.0), (1, 360.0, 300.0), (1, 60.0, 60.0)]
    first = scope_by_stroke(document, queries, RULE, radius_pt=QUERY_RADIUS_PT)
    second = scope_by_stroke(document, queries, RULE, radius_pt=QUERY_RADIUS_PT)
    assert first == second
    assert [verdict.scope for verdict in first] == ["new", "ambiguous", "unknown"]
    assert SCOPE_FRACTION_THRESHOLD == 0.8
    assert math.isclose(first[1].fraction_new, 0.5)
