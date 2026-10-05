"""A fill must not erase independently observed stroke evidence."""
from dataclasses import replace

from oabm.importers.pdf_architecture.importer import _Transform2D, _hatch_evidence_ids
from oabm.importers.pdf_architecture.types import PdfLineObservation, PdfPageObservation

TRANSFORM = _Transform2D(.02, 0, 0, 0, 'synthetic-scale', 1.)


def _line(key, y, stroke):
    return PdfLineObservation(key, (30, y), (90, y), primitive_family='polyline',
                              filled=True, fill_grays=(.4,), stroke_present=stroke)


def test_short_filled_boundary_keeps_an_independent_visible_stroke():
    lines = [_line('wall-face-a', 20, True), _line('wall-face-b', 25, True)]
    page = PdfPageObservation(1, 200, 100, lines=tuple(lines))
    assert _hatch_evidence_ids(page, lines, TRANSFORM) == set()
    pure_fill = [replace(line, stroke_present=False) for line in lines]
    assert _hatch_evidence_ids(replace(page, lines=tuple(pure_fill)), pure_fill, TRANSFORM) == {line.element_id for line in lines}


def test_independent_strokes_do_not_bypass_periodic_hatch_recognition():
    lines = [_line(f'hatch-{i}', 20 + 5*i, True) for i in range(6)]
    page = PdfPageObservation(1, 200, 100, lines=tuple(lines))
    assert _hatch_evidence_ids(page, lines, TRANSFORM) == {line.element_id for line in lines}
