"""Native lines preserve endpoints; bounding boxes cannot encode slope."""
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, RectangleObject
from oabm.importers.pdf_architecture.extract import extract_pdf


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
@pytest.mark.parametrize('offset', [False, True])
def test_crossing_native_lines_remain_distinct_at_every_display_rotation(tmp_path: Path, rotation, offset):
    writer = PdfWriter(); page = writer.add_blank_page(200, 100)
    translation = '1 0 0 1 50 70 cm ' if offset else ''
    if offset:
        page.mediabox = RectangleObject([50, 70, 250, 170])
        page.cropbox = RectangleObject([50, 70, 250, 170])
    if rotation:
        page.rotate(rotation)
    stream = DecodedStreamObject()
    stream.set_data((translation+'20 20 m 170 80 l S 20 80 m 170 20 l S').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    source = tmp_path/'synthetic-crossing-lines.pdf';writer.write(source)
    observed = extract_pdf(source).pages[0]
    def display(p):
        x, y = p
        return {0: (x,y), 90: (y,200-x), 180: (200-x,100-y), 270: (100-y,x)}[rotation]
    expected = {tuple(sorted(map(display, segment))) for segment in [((20,20),(170,80)),((20,80),(170,20))]}
    actual = {tuple(sorted((line.start_pt,line.end_pt))) for line in observed.lines}
    assert len(observed.lines) == 2
    assert actual == expected
    assert len({line.element_id for line in observed.lines}) == 2
    assert extract_pdf(source).pages[0] == observed


def test_missing_endpoints_do_not_guess_a_diagonal_direction():
    from oabm.importers.pdf_architecture.extract import _native_line_segments
    diagonal = {'x0': 10, 'y0': 10, 'x1': 20, 'y1': 20}
    vertical = {**diagonal, 'x1': 10}
    assert _native_line_segments([diagonal, vertical, {}], 100) == (vertical,)


@pytest.mark.parametrize('points', [[(10, 10), (float('nan'), 20)], [(10, 10)], [('bad', 20), (10, 30)]])
def test_malformed_source_endpoints_are_not_replaced_with_bbox_guesses(points):
    from oabm.importers.pdf_architecture.extract import _native_line_segments
    assert not _native_line_segments([{'pts': points, 'x0': 10, 'y0': 10, 'x1': 10, 'y1': 20}], 100)
