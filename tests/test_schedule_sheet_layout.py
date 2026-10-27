"""Synthetic vector layout regressions; no engineered choices or customer data."""
from dataclasses import replace
import xml.etree.ElementTree as ET

import pytest

from oabm.drawings.model import DrawingSet, DrawingSchedule, ScheduleColumn, ScheduleRow, SourceReference
from oabm.drawings.schedule_layout import ScheduleLayout, paginate_schedules, wrap_cell
from oabm.drawings.sheets import SheetSpec


def drawing(count=90, value='Supplied value'):
    return DrawingSet('model:synthetic', (), (DrawingSchedule('schedule:test', 'Supplied facts',
        (ScheduleColumn('id', 'Canonical source ID'), ScheduleColumn('fact', 'Supplied value')),
        tuple(ScheduleRow(f'circuit:{i:04d}', (f'circuit:{i:04d}', value)) for i in range(count))),),
        tuple(SourceReference(f'circuit:{i:04d}', None, 1) for i in range(count)))


def spec():
    return SheetSpec('E600', 210, 297, titleblock_height_mm=60, title='Schedules', revision='R1')


def test_many_rows_repeated_headers_references_and_no_omissions():
    source = drawing()
    before = source.to_json()
    pages = paginate_schedules(source, spec())
    assert len(pages) > 1
    assert [(r['row_index'], r['source_id']) for p in pages for r in p.row_map] == [(i, f'circuit:{i:04d}') for i in range(90)]
    assert len({r['source_id'] for p in pages for r in p.row_map}) == 90
    for n, p in enumerate(pages, 1):
        body = p.body_svg(ScheduleLayout())
        ET.fromstring(body)
        assert f'Schedule page {n}/{len(pages)}' in body
        assert p.spec.id == f'E{599+n}'
        headers = [x for x in p.drawing_set.views[0].primitives if ('role', 'repeated_header') in x.metadata and x.kind == 'text']
        assert headers
        for row in p.row_map:
            assert row['bounds_mm'][3] <= spec().height_mm - spec().margin_mm - spec().titleblock_height_mm
            assert [''.join(c) for c in row['wrapped_cells']] == row['cells']
    assert [p.body_svg(ScheduleLayout()) for p in pages] == [p.body_svg(ScheduleLayout()) for p in paginate_schedules(source, spec())]
    assert source.to_json() == before


def test_long_opaque_text_spaces_newlines_xml_and_narrow_columns():
    layout = ScheduleLayout(column_widths_mm={'fact': 25})
    value = '<unknown> & conflict ' + 'X' * 100 + ' supplied text'
    pages = paginate_schedules(drawing(1, value), spec(), layout=layout)
    wrapped = pages[0].row_map[0]['wrapped_cells'][1]
    assert ''.join(wrapped) == value.replace('\n', '')
    from oabm.drawings.company_sheets import measure_sheet_text
    assert all(measure_sheet_text(line, layout.text_height_mm, font_family='DejaVu Sans') <= 22 for line in wrapped)
    assert '&lt;unknown' in pages[0].body_svg(layout)
    with pytest.raises(ValueError, match='too narrow'):
        paginate_schedules(drawing(1), spec(), layout=ScheduleLayout(column_widths_mm={'fact': 4}))


def test_oversized_row_fails_with_source_and_action():
    with pytest.raises(ValueError, match=r'circuit:0000.*enlarge paper'):
        paginate_schedules(drawing(1, 'Z' * 5000), spec())


@pytest.mark.parametrize('value', ['tab\tdata', 'bad\x00data', 3])
def test_invalid_cell_is_explicit(value):
    with pytest.raises(ValueError):
        wrap_cell(value, 30, ScheduleLayout())


def test_invalid_columns_sources_and_duplicate_rows():
    with pytest.raises(ValueError, match='unknown schedule column'):
        paginate_schedules(drawing(1), spec(), layout=ScheduleLayout(column_widths_mm={'oops': 10}))
    with pytest.raises(ValueError, match='exceed'):
        paginate_schedules(drawing(1), spec(), layout=ScheduleLayout(column_widths_mm={'fact': 300}))
    with pytest.raises(ValueError, match='canonical source'):
        paginate_schedules(replace(drawing(1), source_index=()), spec())
    d = drawing(1)
    with pytest.raises(ValueError, match='duplicate row'):
        paginate_schedules(replace(d, schedules=(replace(d.schedules[0], rows=d.schedules[0].rows * 2),)), spec())


def test_shared_body_bounds_are_used_without_branding():
    class SharedSheet:
        id = 'E600'
        title = 'Schedules'
        width_mm = 420
        height_mm = 297
        drawing_bounds_mm = (30, 20, 310, 200)
    pages = paginate_schedules(drawing(5), SharedSheet())
    assert all(r['bounds_mm'][0] == 32 for p in pages for r in p.row_map)
    assert all(r['bounds_mm'][1] >= 22 and r['bounds_mm'][3] <= 198 for p in pages for r in p.row_map)
    assert 'titleblock' not in pages[0].body_svg(ScheduleLayout())


def test_empty_table_retains_visible_note_without_invented_row():
    d = drawing(0)
    d = replace(d, source_index=(SourceReference('equipment:empty', None, 1),))
    pages = paginate_schedules(d, spec(), empty_schedule_sources={'schedule:test': 'equipment:empty'})
    assert len(pages) == 1 and not pages[0].row_map
    assert 'No listed circuits' in pages[0].body_svg(ScheduleLayout())


def test_wrapping_consumes_actual_shared_font_metrics_and_preserves_spaces():
    layout = ScheduleLayout()
    assert len(wrap_cell('i' * 100, 25, layout)) < len(wrap_cell('W' * 100, 25, layout))
    for value in ('A B', 'A  B', ' A B ', '   ', '  long supplied label  ' * 4):
        assert ''.join(wrap_cell(value, 25, layout)) == value
