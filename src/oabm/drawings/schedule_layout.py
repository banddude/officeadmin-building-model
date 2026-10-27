"""Paper-space schedule layout over the existing derived drawing contract."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import re

from .model import (Bounds2, DrawingPrimitive, DrawingSet, DrawingView, LineStyle,
                    Point2, _finite)
from .sheets import SheetSpec, ViewPlacement
from .sheet_svg import _xml
from .company_sheets import validate_sheet_text, measure_sheet_text


@dataclass(frozen=True)
class ScheduleLayout:
    text_height_mm: float = 2.5
    line_height_mm: float = 4.0
    padding_mm: float = 1.5
    column_widths_mm: dict[str, float] = field(default_factory=dict)
    packing: str = 'one_table_per_sheet'

    def __post_init__(self):
        if self.packing not in {'one_table_per_sheet', 'compact'}:
            raise ValueError('packing: use one_table_per_sheet or compact')
        for key in ('text_height_mm', 'line_height_mm', 'padding_mm'):
            value = _finite(getattr(self, key), key)
            if value <= 0:
                raise ValueError(f'{key}: supply a positive finite millimetre value')
        if self.line_height_mm < self.text_height_mm * 1.5:
            raise ValueError('line_height_mm must be at least 1.5 times text_height_mm')
        if not isinstance(self.column_widths_mm, dict):
            raise ValueError('column_widths_mm must be an object keyed by schedule column key')
        widths = {}
        for key, value in self.column_widths_mm.items():
            if not isinstance(key, str) or not key:
                raise ValueError('column width keys must be nonempty strings')
            widths[key] = _finite(value, f'column_widths_mm.{key}')
            if widths[key] <= 0:
                raise ValueError(f'column_widths_mm.{key}: must be positive')
        object.__setattr__(self, 'column_widths_mm', widths)


def wrap_cell(value: str, width_mm: float, layout: ScheduleLayout) -> tuple[str, ...]:
    """Wrap losslessly using the shared installed-font ink/advance measurement."""
    if not isinstance(value, str):
        raise ValueError('schedule cell must be a string')
    available = width_mm - 2 * layout.padding_mm
    if available <= 0:
        raise ValueError('column too narrow for one character; increase column width or reduce text_height_mm')
    lines = []
    for line in value.split('\n'):
        validate_sheet_text(line)
        if not line:
            lines.append('')
            continue
        while line:
            low, high = 0, len(line)
            while low < high:
                middle = (low + high + 1) // 2
                if measure_sheet_text(line[:middle], layout.text_height_mm, font_family='DejaVu Sans') <= available:
                    low = middle
                else:
                    high = middle - 1
            if not low:
                raise ValueError('column too narrow for one character; increase column width or reduce text_height_mm')
            lines.append(line[:low])
            line = line[low:]
    return tuple(lines)


@dataclass(frozen=True)
class SchedulePage:
    spec: SheetSpec
    drawing_set: DrawingSet
    placement: ViewPlacement
    row_map: tuple[dict, ...]
    schedule_id: str | None
    schedule_page: int | None
    schedule_pages: int | None
    sections: tuple[dict, ...] = ()

    def body_svg(self, layout: ScheduleLayout) -> str:
        """Bounded paper-mm subtree for the shared company sheet renderer."""
        view = self.drawing_set.views[0]
        fragments = ['<g xml:space="preserve">']
        for primitive in view.primitives:
            points = [(self.placement.x_mm + p.x * 1000,
                       self.placement.y_mm + (view.bounds.max_y - p.y) * 1000)
                      for p in primitive.points]
            attrs = ''  # Canonical identity is retained in the row/drawing manifest.
            if primitive.kind == 'text':
                x, y = points[0]
                fragments.append(f'<text {attrs} x="{x:.9f}" y="{y:.9f}" '
                    f'font-family="DejaVu Sans" font-size="{layout.text_height_mm:.9f}" '
                    f'fill="#000000" xml:space="preserve">{_xml(primitive.text)}</text>')
            else:
                vertices = ' '.join(f'{x:.9f},{y:.9f}' for x, y in points)
                fragments.append(f'<polyline {attrs} points="{vertices}" fill="none" '
                                 'stroke="#000000" stroke-width="0.13"/>')
        fragments.append('</g>')
        return '\n'.join(fragments)


def paginate_schedules(drawing_set: DrawingSet, sheet, *, layout=None, empty_schedule_sources=None) -> tuple[SchedulePage, ...]:
    """Deterministic row-preserving pagination with optional ordered packing.

    The template's titleblock is owned by the shared sheet renderer. Body bounds
    derive only from SheetSpec. Rows never split: an oversized row is actionable
    failure, never a partially successful packet. Original schedules remain in
    the retained drawing input; page drawings contain their vector projection.
    """
    layout = layout or ScheduleLayout()
    schedules = drawing_set.schedules
    if not schedules or len({s.id for s in schedules}) != len(schedules):
        raise ValueError('supply nonempty schedules with unique schedule IDs')
    sources = {s.canonical_id for s in drawing_set.source_index}
    all_keys = {c.key for s in schedules for c in s.columns}
    if set(layout.column_widths_mm) - all_keys:
        raise ValueError('column_widths_mm contains unknown schedule column keys')
    # Inset admits line stroke widths and separates body from the shared frame.
    inset = 2.0
    if hasattr(sheet, 'drawing_bounds_mm'):
        left, top, right, bottom = sheet.drawing_bounds_mm
        template = SheetSpec(sheet.id, sheet.width_mm, sheet.height_mm, title=sheet.title)
    else:
        left, top = sheet.margin_mm, sheet.margin_mm
        right = sheet.width_mm - sheet.margin_mm
        bottom = sheet.height_mm - sheet.margin_mm - sheet.titleblock_height_mm
        template = sheet
    width = right - left - 2 * inset
    height = bottom - top - 2 * inset
    pages = []
    empty_schedule_sources = empty_schedule_sources or {}
    for schedule in schedules:
        if not schedule.id or not schedule.columns:
            raise ValueError('schedule requires an ID and at least one column')
        if len({r.source_id for r in schedule.rows}) != len(schedule.rows):
            raise ValueError(f'schedule {schedule.id}: duplicate row source ID')
        if any(r.source_id not in sources for r in schedule.rows):
            raise ValueError(f'schedule {schedule.id}: row has no canonical source reference')
        if not schedule.rows and empty_schedule_sources.get(schedule.id) not in sources:
            raise ValueError(f'schedule {schedule.id}: empty table needs an explicit canonical empty_schedule_sources binding')
        fixed = sum(layout.column_widths_mm.get(c.key, 0) for c in schedule.columns)
        automatic = sum(c.key not in layout.column_widths_mm for c in schedule.columns)
        if fixed > width or automatic and fixed >= width:
            raise ValueError(f'schedule {schedule.id}: configured columns exceed available body width')
        default = (width - fixed) / automatic if automatic else 0
        widths = tuple(layout.column_widths_mm.get(c.key, default) for c in schedule.columns)
        title_lines = wrap_cell(schedule.title, width, layout)
        headers = tuple(wrap_cell(c.title + (f' ({c.unit})' if c.unit else ''), w, layout)
                        for c, w in zip(schedule.columns, widths))
        title_height = len(title_lines) * layout.line_height_mm + 2 * layout.padding_mm
        header_height = max(map(len, headers)) * layout.line_height_mm + 2 * layout.padding_mm
        footer_height = 2 * layout.line_height_mm + 2 * layout.padding_mm
        available = height - title_height - header_height - footer_height
        if available <= 0:
            raise ValueError(f'schedule {schedule.id}: title/headers leave no row area; enlarge paper or columns')
        chunks = [[]]
        consumed = 0
        for ordinal, row in enumerate(schedule.rows):
            cells = tuple(wrap_cell(cell, w, layout) for cell, w in zip(row.cells, widths))
            row_height = max(map(len, cells)) * layout.line_height_mm + 2 * layout.padding_mm
            if row_height > available:
                raise ValueError(f'schedule {schedule.id} row {ordinal + 1} ({row.source_id}): needs {row_height:g} mm; '
                                 f'only {available:g} mm available; enlarge paper/column widths or reduce text size')
            if chunks[-1] and consumed + row_height > available:
                chunks.append([])
                consumed = 0
            chunks[-1].append((ordinal, row, cells, row_height))
            consumed += row_height
        for part, chunk in enumerate(chunks, 1):
            number = len(pages) + 1
            match = re.fullmatch(r'(.*?)([0-9]+)', sheet.id)
            page_id = (match[1] + str(int(match[2]) + number - 1).zfill(len(match[2]))) if match else f'{sheet.id}{number:03d}'
            spec = replace(template, id=page_id)
            view_id = f'schedule-page:{number:03d}'
            primitives = []
            row_map = []
            def point(x, y):
                return Point2(x / 1000, (height - y) / 1000)
            def add(kind, points, source_id, *, text=None, metadata=()):
                identity = f'{view_id}:item:{len(primitives):06d}'
                primitives.append(DrawingPrimitive(identity, kind, 'schedule', (source_id,),
                    tuple(point(x, y) for x, y in points), text=text, metadata=metadata,
                    style=LineStyle(weight='thin')))
                return identity
            first_source = chunk[0][1].source_id if chunk else empty_schedule_sources[schedule.id]
            def lines(values, x, y, source, metadata=()):
                ids = []
                for i, value in enumerate(values):
                    ids.append(add('text', [(x + layout.padding_mm,
                        y + layout.padding_mm + layout.text_height_mm + i * layout.line_height_mm)],
                        source, text=value, metadata=metadata))
                return ids
            lines(title_lines, 0, 0, first_source, (('role', 'schedule_title'),))
            y = title_height
            def cells_box(cells, row_height, source, metadata):
                x = 0
                ids = []
                for cell, w in zip(cells, widths):
                    # Stroke lies strictly inside the view, including outer borders.
                    add('polyline', [(x + .1, y + .1), (x + w - .1, y + .1),
                        (x + w - .1, y + row_height - .1), (x + .1, y + row_height - .1),
                        (x + .1, y + .1)], source, metadata=metadata)
                    ids.extend(lines(cell, x, y, source, metadata))
                    x += w
                return ids
            cells_box(headers, header_height, first_source, (('role', 'repeated_header'), ('schedule_id', schedule.id)))
            y += header_height
            if not chunk:
                empty_lines = wrap_cell('No listed circuits; no slots or spares inferred.', width, layout)
                if len(empty_lines) * layout.line_height_mm + 2 * layout.padding_mm > available:
                    raise ValueError('empty-table note exceeds available body; enlarge paper')
                lines(empty_lines, 0, y, first_source, (('role', 'empty_table'),))
            for ordinal, row, cells, row_height in chunk:
                meta = (('role', 'schedule_row'), ('schedule_id', schedule.id), ('row_index', str(ordinal)))
                ids = cells_box(cells, row_height, row.source_id, meta)
                row_map.append({'schedule_id': schedule.id, 'row_index': ordinal, 'source_id': row.source_id,
                    'sheet_id': spec.id, 'page': number, 'primitive_ids': ids,
                    'bounds_mm': [left + inset, top + inset + y,
                                  left + inset + sum(widths), top + inset + y + row_height],
                    'cells': list(row.cells), 'wrapped_cells': [list(v) for v in cells]})
                y += row_height
            footer = f'Schedule page {part}/{len(chunks)} | Sheet {spec.id}'
            footer_lines = wrap_cell(footer, width, layout)
            if len(footer_lines) > 2:
                raise ValueError('page reference exceeds footer capacity; shorten sheet ID or enlarge paper')
            lines(footer_lines, 0, height - footer_height, first_source, (('role', 'page_reference'),))
            view = DrawingView(view_id, 'plan', schedule.title, Bounds2(0, 0, width / 1000, height / 1000),
                1, tuple(primitives), metadata=(('presentation', 'schedule table; no geometric scale'),))
            projected = DrawingSet(drawing_set.model_id, (view,), (), drawing_set.source_index)
            pages.append(SchedulePage(spec, projected, ViewPlacement(view_id, view_id,
                left + inset, top + inset), tuple(row_map), schedule.id, part, len(chunks)))
    return _pack_sections(tuple(pages), sheet.id, layout) if layout.packing == 'compact' else tuple(pages)


def _pack_sections(pages, first_sheet_id, layout):
    """Pack complete pre-paginated sections in source order without shrinking.

    A bounded second pass: never split/reorder a section or alter cell wrapping.
    Full continuation sections retain repeated headers. Every packed section
    retains its own panel title and schedule N/M reference. Final physical IDs,
    primitive references, row bounds and section bounds are rebuilt together.
    """
    first = pages[0]
    bounds = first.drawing_set.views[0].bounds
    height, width = bounds.height * 1000, bounds.width * 1000
    footer_height = 2 * layout.line_height_mm + 2 * layout.padding_mm
    gap = layout.line_height_mm + 2 * layout.padding_mm
    groups = [[]]
    used = 0.0
    for page in pages:
        primitives = tuple(p for p in page.drawing_set.views[0].primitives if ('role', 'page_reference') not in p.metadata)
        if page.row_map:
            table_bottom = max(r['bounds_mm'][3] for r in page.row_map) - page.placement.y_mm
        else:
            table_bottom = max(height - point.y * 1000 + (layout.text_height_mm * .3 if p.kind == 'text' else 0)
                               for p in primitives for point in p.points) + layout.padding_mm
        section_height = table_bottom + footer_height
        if section_height > height + 1e-7:
            raise ValueError('compact section exceeds available body height; enlarge paper')
        offset = used + (gap if groups[-1] else 0)
        if groups[-1] and offset + section_height > height + 1e-7:
            groups.append([])
            offset = 0.0
        groups[-1].append((page, primitives, table_bottom, section_height, offset))
        used = offset + section_height
    packed = []
    for number, group in enumerate(groups, 1):
        match = re.fullmatch(r'(.*?)([0-9]+)', first_sheet_id)
        sid = (match[1] + str(int(match[2]) + number - 1).zfill(len(match[2]))) if match else f'{first_sheet_id}{number:03d}'
        spec = replace(first.spec, id=sid)
        view_id = f'schedule-packed-page:{number:03d}'
        primitives, row_map, sections = [], [], []
        for section_index, (page, content, table_bottom, section_height, offset) in enumerate(group):
            identities = {}
            for p in content:
                identity = f'{view_id}:item:{len(primitives):06d}'
                identities[p.id] = identity
                primitives.append(replace(p, id=identity,
                    points=tuple(Point2(pt.x, pt.y - offset / 1000) for pt in p.points),
                    metadata=(*p.metadata, ('section_index', str(section_index)))))
            for row in page.row_map:
                box = list(row['bounds_mm'])
                box[1] += offset
                box[3] += offset
                row_map.append({**row, 'sheet_id': sid, 'page': number,
                    'primitive_ids': [identities[i] for i in row['primitive_ids']], 'bounds_mm': box})
            sections.append({'schedule_id': page.schedule_id, 'schedule_page': page.schedule_page,
                'schedule_pages': page.schedule_pages, 'bounds_mm': [page.placement.x_mm,
                page.placement.y_mm + offset, page.placement.x_mm + width,
                page.placement.y_mm + offset + section_height]})
            footer = f'Schedule page {page.schedule_page}/{page.schedule_pages} | Sheet {sid}'
            footer_lines = wrap_cell(footer, width, layout)
            if len(footer_lines) > 2:
                raise ValueError('packed page reference exceeds footer capacity; shorten sheet ID or enlarge paper')
            source = content[0].source_ids
            for line, value in enumerate(footer_lines):
                identity = f'{view_id}:item:{len(primitives):06d}'
                y = offset + table_bottom + layout.padding_mm + layout.text_height_mm + line * layout.line_height_mm
                primitives.append(DrawingPrimitive(identity, 'text', 'schedule', source,
                    (Point2(layout.padding_mm / 1000, (height - y) / 1000),), text=value,
                    metadata=(('role', 'page_reference'), ('section_index', str(section_index)))))
        view = DrawingView(view_id, 'plan', spec.title, bounds, 1, tuple(primitives),
            metadata=(('presentation', 'packed schedule sections; no geometric scale'),))
        drawing = DrawingSet(first.drawing_set.model_id, (view,), (), first.drawing_set.source_index)
        only = sections[0] if len(sections) == 1 else {}
        packed.append(SchedulePage(spec, drawing, ViewPlacement(view_id, view_id,
            first.placement.x_mm, first.placement.y_mm), tuple(row_map), only.get('schedule_id'),
            only.get('schedule_page'), only.get('schedule_pages'), tuple(sections)))
    return tuple(packed)
