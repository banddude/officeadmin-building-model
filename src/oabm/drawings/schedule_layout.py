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
                meta = dict(primitive.metadata)
                size = float(meta.get('font_size_mm', layout.text_height_mm))
                weight = int(meta.get('font_weight', '400'))
                fragments.append(f'<text {attrs} x="{x:.9f}" y="{y:.9f}" '
                    f'font-family="DejaVu Sans" font-size="{size:.9f}" font-weight="{weight}" '
                    f'fill="#000000" xml:space="preserve">{_xml(primitive.text)}</text>')
            else:
                vertices = ' '.join(f'{x:.9f},{y:.9f}' for x, y in points)
                fragments.append(f'<polyline {attrs} points="{vertices}" fill="none" '
                                 f'stroke="#000000" stroke-width="{dict(thin=.13, normal=.25, heavy=.5)[primitive.style.weight]}"/>')
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


def draft_panel_schedule_selection(report, *, panel_ids=None, include_empty_panels=True, include_unassigned=True):
    """Audit a read-only card selection over the complete validated report.

    Panel selection never changes report contents or classifies unsupported
    owners as panels. Unassigned inclusion is independent of panel_ids, matching
    the legacy all-groups default. IDs and output order follow the report.
    """
    from oabm.model import stable_id
    report.to_dict()
    if type(include_empty_panels) is not bool or type(include_unassigned) is not bool:
        raise ValueError('schedule selection flags must be booleans')
    available = {group.panel.id for group in report.panels}
    if panel_ids is None:
        requested = available
    else:
        if not isinstance(panel_ids, (list, tuple, set, frozenset)) or any(not isinstance(v, str) or not v for v in panel_ids):
            raise ValueError('panel_ids: supply explicit canonical panel IDs, or None for all')
        requested = set(panel_ids)
        if len(requested) != len(panel_ids):
            raise ValueError('panel_ids: duplicate canonical panel ID')
        if requested - available:
            raise ValueError('panel_ids: unknown report panel IDs: ' + ', '.join(sorted(requested - available)))
    groups = []
    for group in report.panels:
        reason = ('panel_not_requested' if group.panel.id not in requested else
                  'empty_panel_excluded' if not group.circuits and not include_empty_panels else 'selected')
        groups.append({'panel_id': group.panel.id, 'selected': reason == 'selected', 'reason': reason,
            'schedule_ids': [stable_id('schedule', f'panel-facts:{group.panel.id}'),
                             stable_id('schedule', f'panel-fact-summary:{group.panel.id}')],
            'circuit_ids': [fact.circuit.id for fact in group.circuits]})
    if report.unassigned_circuits:
        groups.append({'panel_id': None, 'selected': include_unassigned,
            'reason': 'selected' if include_unassigned else 'unassigned_excluded',
            'schedule_ids': [stable_id('schedule', f'unassigned-panel-facts:{report.source_model_id}')],
            'circuit_ids': [fact.circuit.id for fact in report.unassigned_circuits]})
    return {'source_model_id': report.source_model_id, 'groups': groups,
        'selected_schedule_ids': [sid for group in groups if group['selected'] for sid in group['schedule_ids']],
        'excluded_schedule_ids': [sid for group in groups if not group['selected'] for sid in group['schedule_ids']]}


def draft_panel_schedule_pages(report, drawing_set, sheet, *, layout=None, region_bounds_mm=None, table_width_mm=420.0,
                               panel_ids=None, include_empty_panels=True, include_unassigned=True):
    """Compose plan-style paired panel cards from the retained fact report.

    Optional panel_ids/include_empty_panels/include_unassigned filter cards only;
    defaults preserve all legacy groups. An empty selection returns (). Use
    draft_panel_schedule_selection for selected/excluded identity audit.
    The caller can reserve a paper-mm region beside a one-line. Presentation
    attributes are read only from canonical owner attributes.panel_schedule:
    circuit description/ocpd (literal text), side/row (explicit table positions),
    and phase_va (explicit A/B/C connected values). Equipment may supply literal
    main/bus/aic/source. Missing facts print TBD; no odd/even pairing, slots,
    breakers, demand or phase allocation is inferred. Supplied rows are printed
    under ROW / CKT; omitted ranges are annotated, never filled as spares.
    Unpositioned circuits stay
    in a clearly labelled unassigned-position list, one actual circuit per row.
    """
    import math
    from oabm.model import stable_id
    from oabm.drawings.schedule_ratings import panel_ratings, circuit_ratings
    layout = layout or ScheduleLayout(packing="compact")
    if layout.column_widths_mm:
        raise ValueError('drafted panel schedules do not support column_widths_mm; use table_width_mm or ledger mode')
    selection = draft_panel_schedule_selection(report, panel_ids=panel_ids,
        include_empty_panels=include_empty_panels, include_unassigned=include_unassigned)
    if not selection['selected_schedule_ids']:
        return ()
    selected_panels = {group['panel_id'] for group in selection['groups'] if group['selected']}
    if hasattr(sheet, 'drawing_bounds_mm'):
        paper_bounds = tuple(sheet.drawing_bounds_mm)
        template = SheetSpec(sheet.id, sheet.width_mm, sheet.height_mm, title=sheet.title)
    else:
        paper_bounds = (sheet.margin_mm, sheet.margin_mm, sheet.width_mm - sheet.margin_mm,
                        sheet.height_mm - sheet.margin_mm - sheet.titleblock_height_mm)
        template = sheet
    region = region_bounds_mm or paper_bounds
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise ValueError('region_bounds_mm requires left, top, right, bottom paper mm')
    l, t, r, b = tuple(_finite(v, 'region_bounds_mm') for v in region)
    if l >= r or t >= b or l < paper_bounds[0] or t < paper_bounds[1] or r > paper_bounds[2] or b > paper_bounds[3]:
        raise ValueError('drafted region must fit the shared sheet drawing_bounds_mm')
    inset, gutter = 3.0, 8.0
    region_width, region_height = r - l - 2 * inset, b - t - 2 * inset
    table_width_mm = _finite(table_width_mm, 'table_width_mm')
    if table_width_mm <= 0:
        raise ValueError('table_width_mm must be positive')
    card_width = min(table_width_mm, region_width)
    if card_width < 240:
        raise ValueError('drafted panel table needs at least 240 mm width at configured readable text size')
    columns = max(1, math.floor((region_width + gutter) / (card_width + gutter)))
    if columns > 1:
        card_width = (region_width - gutter * (columns - 1)) / columns
    font, line = layout.text_height_mm, layout.line_height_mm
    pad = layout.padding_mm
    heading = max(font * 1.4, font)
    paired_widths = [card_width * n / 102 for n in (5, 24, 3, 6, 7, 4, 4, 4, 7, 6, 3, 24, 5)]
    # Drawing source rows remain the report adapter's full, lossless facts.
    source_schedules = {s.id: s for s in drawing_set.schedules}
    row_lookup = {(s.id, row.source_id): (i, row) for s in drawing_set.schedules for i, row in enumerate(s.rows)}
    cards = []

    def supplied(entity, allowed):
        value = entity.get('attributes', {}).get('panel_schedule', {})
        if not isinstance(value, dict) or set(value) - set(allowed):
            raise ValueError(f"{entity['id']}: panel_schedule accepts only {sorted(allowed)}")
        for key in ('description', 'ocpd', 'main', 'bus', 'aic', 'source'):
            if key in value:
                validate_sheet_text(value[key])
        return value

    def wrapped(value, width, *, size=None, weight=400):
        size = font if size is None else size
        value = str(value)
        validate_sheet_text(value)
        available = width - 2 * pad
        if available <= 0:
            raise ValueError('drafted column has no text capacity; enlarge table or reduce configured padding')
        result = []
        while value:
            low, high = 0, len(value)
            while low < high:
                n = (low + high + 1) // 2
                if measure_sheet_text(value[:n], size, font_family='DejaVu Sans', font_weight=weight) <= available:
                    low = n
                else:
                    high = n - 1
            if not low:
                raise ValueError('drafted column too narrow for a supplied glyph; enlarge table or reduce text size')
            result.append(value[:low]); value = value[low:]
        return tuple(result or [''])

    def value(number):
        return 'TBD' if number is None else str(number)

    # Separate unknown positions from actual paired positions, rather than
    # giving an unknown-position circuit the appearance of a left-hand slot.
    groups = []
    for group in report.panels:
        if group.panel.id not in selected_panels:
            continue
        def has_position(fact):
            fields = fact.circuit.entity.get('attributes', {}).get('panel_schedule', {})
            return isinstance(fields, dict) and ('side' in fields or 'row' in fields)
        positioned = tuple(f for f in group.circuits if has_position(f))
        unpositioned = tuple(f for f in group.circuits if f not in positioned)
        if positioned and unpositioned:
            groups.extend(((group.panel, positioned, group, False), (group.panel, unpositioned, group, True)))
        else:
            groups.append((group.panel, group.circuits, group, True))
    if report.unassigned_circuits and include_unassigned:
        def unsupported_position(fact):
            fields = fact.circuit.entity.get('attributes', {}).get('panel_schedule', {})
            return isinstance(fields, dict) and ('side' in fields or 'row' in fields)
        positioned = tuple(f for f in report.unassigned_circuits if unsupported_position(f))
        unpositioned = tuple(f for f in report.unassigned_circuits if not unsupported_position(f))
        if positioned and unpositioned:
            groups.extend(((None, positioned, None, False), (None, unpositioned, None, False)))
        else:
            groups.append((None, report.unassigned_circuits, None, False))
    for ordinal, (panel, facts, group, include_summary) in enumerate(groups, 1):
        panel_id = panel.id if panel is not None else report.source_model_id
        entity = panel.entity if panel is not None else {}
        panel_name = entity.get('name') if panel else None
        panel_identity = panel_id if not panel_name or panel_name == panel_id else f'{panel_id} | {panel_name}'
        title = f'{ordinal}  PANEL {panel_identity} - CIRCUIT SCHEDULE' if panel else f'{ordinal}  UNSUPPORTED SOURCE OWNERS - CIRCUIT FACTS'
        panel_fields = panel_ratings(entity, supplied(entity, {'main', 'bus', 'aic', 'source'})) if panel else {}
        circuit_sid = stable_id('schedule', f'panel-facts:{panel_id}') if panel else stable_id('schedule', f'unassigned-panel-facts:{report.source_model_id}')
        summary_sid = stable_id('schedule', f'panel-fact-summary:{panel_id}') if panel else None
        label_conflicts = {cid: conflict.circuit_ids for conflict in group.label_conflicts for cid in conflict.circuit_ids} if group else {}
        positions, unassigned = {}, []
        for fact in facts:
            circuit = fact.circuit.entity
            fields = circuit_ratings(circuit, supplied(circuit, {'description', 'ocpd', 'side', 'row', 'phase_va'}))
            if ('side' in fields) != ('row' in fields):
                raise ValueError(f'{fact.circuit.id}: supply both panel_schedule.side and row, or neither')
            if 'side' in fields:
                if not isinstance(fields['side'], str) or fields['side'] not in {'left', 'right'} or type(fields['row']) is not int or fields['row'] < 1:
                    raise ValueError(f'{fact.circuit.id}: explicit side left/right and positive integer row required')
                key = (fields['row'], fields['side'])
                if key in positions:
                    raise ValueError(f'{fact.circuit.id}: duplicate explicit panel schedule position {key}')
                positions[key] = (fact, fields)
            else:
                unassigned.append((fact, fields))
            phase = fields.get('phase_va', {})
            if not isinstance(phase, dict) or set(phase) - {'A', 'B', 'C'}:
                raise ValueError(f'{fact.circuit.id}: phase_va accepts explicit A/B/C supplied values only')
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in phase.values()):
                raise ValueError(f'{fact.circuit.id}: phase_va requires finite nonnegative supplied VA')
        unpositioned_card = bool(facts) and not positions
        widths = ([card_width * n / 100 for n in (8, 38, 4, 8, 12, 10, 10, 10)]
                  if unpositioned_card else paired_widths)
        xedges = [0.0]
        for width in widths:
            xedges.append(xedges[-1] + width)
        physical_rows = []
        previous_row = 0
        for n in sorted({n for n, side in positions}):
            gap_before = (previous_row + 1, n - 1) if n > previous_row + 1 else None
            physical_rows.append((positions.get((n, 'left')), positions.get((n, 'right')), n, gap_before))
            previous_row = n
        physical_rows.extend((item, None, None, None) for item in unassigned)
        if not physical_rows:
            physical_rows = [(None, None, None, None)]
        intro = [f'SYSTEM: {entity.get("system") or "TBD"}   VOLTAGE: {value(entity.get("rated_voltage_v"))} V',
                 f'MAIN: {panel_fields.get("main", "TBD")}   BUS: {panel_fields.get("bus", "TBD")}   AIC: {panel_fields.get("aic", "TBD")}',
                 f'SOURCE: {panel_fields.get("source", "NOT SUPPLIED")}' if panel else 'Source owner classification is unsupported; no panel assignment inferred.']
        if unpositioned_card:
            title += ' - UNPOSITIONED CIRCUITS'
        title_lines = wrapped(title, card_width, size=heading, weight=600)
        intro_lines = tuple(part for text in intro for part in wrapped(text, card_width))
        head_height = len(title_lines) * heading * 1.4 + len(intro_lines) * line + 4 * pad
        headers = ('ROW / CKT', 'LOAD / DESCRIPTION', 'P', 'OCPD', 'VA', 'A', 'B', 'C', 'VA', 'OCPD', 'P', 'LOAD / DESCRIPTION', 'ROW / CKT')
        if unpositioned_card:headers = ('CKT', 'LOAD / DESCRIPTION', 'P', 'OCPD', 'VA', 'A', 'B', 'C')
        header_cells = tuple(wrapped(v, w, weight=600) for v, w in zip(headers, widths))
        header_height = max(map(len, header_cells)) * line + 2 * pad
        notes = ('TBD = not supplied. Unassigned side is not a spare or an available circuit.',
                 'A/B/C: supplied per-phase VA only; no phase allocation or demand inferred.')
        note_lines = tuple(part for text in notes for part in wrapped(text, card_width))
        tail_height = len(note_lines) * line + 2 * pad + 2 * line
        summary_height = 8 * line + 4 * pad if panel and include_summary else 0
        position_text = ('CIRCUIT POSITIONS NOT SUPPLIED. Listed rows do not identify panel slots.' if unpositioned_card else
                         'Positions shown only where explicitly supplied; remaining rows are display order.')
        position_height = len(wrapped(position_text, card_width)) * line + 3 * pad
        available = region_height - head_height - header_height - tail_height - summary_height - position_height
        if available <= 0:
            raise ValueError('drafted panel heading/summary leave no circuit rows in supplied region')
        rows = []
        for left_item, right_item, supplied_row, gap_before in physical_rows:
            explicit_position = supplied_row is not None
            cells = []
            def side(item):
                if item is None:
                    return (f'{value(supplied_row)} / TBD', 'UNASSIGNED', 'TBD', 'TBD', 'TBD')
                fact, fields = item
                c = fact.circuit.entity
                description = fields.get('description', c.get('name') or 'DESCRIPTION NOT SUPPLIED')
                if c.get('voltage_v') is not None:
                    description += ' | ' + str(c['voltage_v']) + ' V'
                if c.get('phase') is not None:
                    description += ' | phase ' + str(c['phase'])
                if fact.circuit.id in label_conflicts:
                    description += f' [LABEL CONFLICT; CIRCUIT ID: {fact.circuit.id}]'
                opaque_label = c.get('circuit_number')
                if opaque_label is None or not opaque_label.strip():
                    description += f' [LABEL NOT SUPPLIED; CIRCUIT ID: {fact.circuit.id}]' if panel is not None else ' [LABEL NOT SUPPLIED]'
                if panel is None:
                    description += f' | CIRCUIT ID: {fact.circuit.id} | SOURCE PORT: {fact.source_port.id} | SOURCE OWNER: {fact.source_owner.id}'
                if 'phase_va' in fields and c.get('load_va') is not None and not math.isclose(math.fsum(fields['phase_va'].values()), c['load_va']):
                    description += ' [PHASE/LOAD CONFLICT]'
                label = 'TBD' if opaque_label is None or not opaque_label.strip() else opaque_label
                identity = label if unpositioned_card else f'{value(fields.get("row"))} / ' + label
                return (identity, description, value(c.get('poles')), fields.get('ocpd', 'TBD'), value(c.get('load_va')))
            left_values, right_values = side(left_item), side(right_item)
            assigned = [item for item in (left_item, right_item) if item is not None]
            phases = []
            for phase in ('A', 'B', 'C'):
                if assigned and all(phase in fields.get('phase_va', {}) for fact, fields in assigned):
                    total = math.fsum(fields['phase_va'][phase] for fact, fields in assigned)
                    if not math.isfinite(total):
                        raise ValueError('supplied phase VA total exceeds finite output range')
                    phases.append(str(total))
                else:
                    phases.append('TBD')
            values = (*left_values, *phases) if unpositioned_card else (*left_values, *phases, *reversed(right_values))
            cells = tuple(wrapped(v, w) for v, w in zip(values, widths))
            row_height = max(map(len, cells)) * line + 2 * pad
            gap_lines = wrapped(f'ROWS {gap_before[0]}-{gap_before[1]} NOT SUPPLIED - NO SPARE CAPACITY INFERRED', card_width, weight=600) if gap_before else ()
            gap_height = len(gap_lines) * line + 2 * pad if gap_lines else 0
            if row_height + gap_height > available:
                raise ValueError(f'panel {panel_id}: circuit row and position marker exceed drafted region; enlarge region/table width')
            rows.append((left_item, right_item, explicit_position, values, cells, row_height, gap_before, gap_lines, row_height + gap_height))
        chunks, used = [[]], 0
        for row in rows:
            if chunks[-1] and used + row[-1] > available:
                chunks.append([]); used = 0
            chunks[-1].append(row); used += row[-1]
        for part, chunk in enumerate(chunks, 1):
            primitives, maps = [], []
            y = 0.0
            def add(kind, points, sources, *, text=None, weight='thin', size=None, bold=False, role=''):
                identity = f'drafted-card:{len(cards):03d}:item:{len(primitives):06d}'
                meta = [('role', role)]
                if kind == 'text':
                    meta.extend((('font_size_mm', str(font if size is None else size)), ('font_weight', '600' if bold else '400')))
                primitives.append(DrawingPrimitive(identity, kind, 'schedule', tuple(sources),
                    tuple(Point2(x / 1000, (region_height - yy) / 1000) for x, yy in points),
                    text=text, style=LineStyle(weight=weight), metadata=tuple(meta)))
                return identity
            anchor_sources = (panel_id,) if panel else (facts[0].circuit.id,)
            def text_lines(parts, x, yy, sources, *, size=None, bold=False, role=''):
                size = font if size is None else size
                step = line if size == font else size * 1.4
                return [add('text', [(x + pad, yy + pad + size + i * step)], sources,
                            text=v, size=size, bold=bold, role=role) for i, v in enumerate(parts)]
            def rule(yy, weight='normal'):
                add('line', [(0.3, yy), (card_width - .3, yy)], anchor_sources, weight=weight, role='table_rule')
            text_lines(title_lines, 0, y, anchor_sources, size=heading, bold=True, role='schedule_title')
            y += len(title_lines) * heading * 1.4 + 2 * pad
            text_lines(intro_lines, 0, y, anchor_sources, role='panel_metadata')
            y = head_height
            rule(y, 'heavy')
            for i, parts in enumerate(header_cells):
                text_lines(parts, xedges[i], y, anchor_sources, bold=True, role='repeated_header')
            header_top = y
            y += header_height
            rule(y, 'normal')
            grid_segments, grid_start, position_gaps = [], header_top, []
            for left_item, right_item, explicit_position, values, cells, row_height, gap_before, gap_lines, total_height in chunk:
                if gap_before:
                    grid_segments.append((grid_start, y))
                    rule(y, 'normal')
                    text_lines(gap_lines, 0, y, anchor_sources, bold=True, role='position_gap')
                    y += len(gap_lines) * line + 2 * pad
                    rule(y, 'normal')
                    grid_start = y
                    position_gaps.append({'first_row': gap_before[0], 'last_row': gap_before[1],
                                          'status': 'not_supplied_no_spare_capacity_inferred'})
                def item_sources(item):
                    if item is None:return ()
                    fact=item[0]
                    return ((fact.circuit.id, fact.source_port.id, fact.source_owner.id) if panel is None else (fact.circuit.id,))
                left_sources, right_sources = item_sources(left_item), item_sources(right_item)
                row_sources = tuple(dict.fromkeys((*left_sources,*right_sources))) or anchor_sources
                cell_ids = []
                for i, parts in enumerate(cells):
                    sources = (left_sources or anchor_sources) if unpositioned_card or i < 5 else row_sources if i < 8 else (right_sources or anchor_sources)
                    cell_ids.append(text_lines(parts, xedges[i], y, sources, role='schedule_row'))
                for item, side, indices in ((left_item, 'left', range(0, 8)), (right_item, 'right', range(5, 13))):
                    if item is None:
                        continue
                    fact, fields = item
                    row_index, original = row_lookup[(circuit_sid, fact.circuit.id)]
                    maps.append({'schedule_id': circuit_sid, 'row_index': row_index, 'source_id': fact.circuit.id,
                        'cells': list(original.cells), 'wrapped_cells': [[c] for c in original.cells],
                        'display_cells': list(values[:5] if side == 'left' else values[8:]),
                        'phase_cells': list(values[5:8]),
                        'phase_basis': 'sum of explicitly supplied per-circuit phase_va; missing remains TBD',
                        'source_port_id': fact.source_port.id, 'source_owner_id': fact.source_owner.id,
                        'label_conflict_circuit_ids': list(label_conflicts.get(fact.circuit.id, ())),
                        'position_status': 'supplied' if explicit_position else 'not_supplied_display_order_only',
                        'supplied_side': fields.get('side'), 'supplied_row': fields.get('row'), 'display_side': 'unpositioned' if unpositioned_card else side,
                        'primitive_ids': [identity for i in indices for identity in cell_ids[i]],
                        'bounds_mm': [0 if side == 'left' else xedges[5], y,
                                      xedges[8] if side == 'left' else card_width, y + row_height]})
                y += row_height
                rule(y, 'thin')
            if not facts:
                text_lines(wrapped('NO LISTED CIRCUITS. No circuit slots or spares inferred.', card_width), 0, y, anchor_sources, role='empty_table')
                y += line + 2 * pad
            grid_segments.append((grid_start, y))
            for start, end in grid_segments:
                for edge in xedges:
                    add('line', [(max(.3, min(card_width - .3, edge)), start),
                                 (max(.3, min(card_width - .3, edge)), end)], anchor_sources,
                        weight='normal' if edge in (xedges[0], xedges[5], xedges[8], xedges[-1]) else 'thin', role='column_rule')
            text_lines(wrapped(position_text, card_width), 0, y + pad, anchor_sources, role='position_status')
            y += len(wrapped(position_text, card_width)) * line + 3 * pad
            if panel and include_summary and part == len(chunks):
                summary_top = y
                rule(y, 'heavy')
                text_lines(('SUPPLIED CONNECTED-LOAD SUMMARY',), 0, y, anchor_sources, bold=True, role='load_summary_title')
                y += line + 2 * pad
                summary_rows = [('KNOWN CONNECTED SUBTOTAL', str(group.known_connected_va) + ' VA'),
                                ('LISTED CIRCUITS / UNKNOWN LOADS', f'{len(group.circuits)} / {len(group.unknown_load_circuit_ids)}'),
                                ('LISTED LOAD COMPLETENESS', 'ALL LISTED LOADS KNOWN' if group.all_listed_loads_known else 'INCOMPLETE - UNKNOWN LOADS'),
                                ('DEMAND / SERVICE CAPACITY', 'NOT ESTABLISHED')]
                summary_ids = []
                for label, fact_value in summary_rows:
                    a = wrapped(label, card_width * .48)
                    c = wrapped(fact_value, card_width * .52)
                    rh = max(len(a), len(c)) * line + 2 * pad
                    summary_ids += text_lines(a, 0, y, anchor_sources, role='load_summary')
                    summary_ids += text_lines(c, card_width * .48, y, anchor_sources, role='load_summary')
                    y += rh; rule(y, 'thin')
                source_row = source_schedules[summary_sid].rows[0]
                maps.append({'schedule_id': summary_sid, 'row_index': 0, 'source_id': panel_id,
                    'cells': list(source_row.cells), 'wrapped_cells': [[c] for c in source_row.cells],
                    'primitive_ids': summary_ids, 'bounds_mm': [0, summary_top, card_width, y]})
                y += pad
            text_lines(note_lines, 0, y, anchor_sources, role='schedule_note')
            y += len(note_lines) * line + 2 * pad
            text_lines((f'PANEL SCHEDULE {part}/{len(chunks)}',), 0, y, anchor_sources, role='page_reference')
            y += line + 2 * pad
            if y > region_height:
                raise ValueError('drafted table exceeds reserved paper region; enlarge region or reduce supplied text length')
            cards.append({'primitives': primitives, 'rows': maps, 'height': y, 'schedule_id': circuit_sid,
                          'schedule_page': part, 'schedule_pages': len(chunks), 'panel_id': panel_id,
                          'included_schedule_ids': [circuit_sid] + ([summary_sid] if panel and include_summary and part == len(chunks) else []),
                          'position_gaps': position_gaps})
    # A mixed-position schedule may contain two visual sections; retain one
    # consistent continuation sequence across both, not two independent 1/1s.
    by_schedule = {}
    for card in cards:
        by_schedule.setdefault(card['schedule_id'], []).append(card)
    for schedule_cards in by_schedule.values():
        for page_number, card in enumerate(schedule_cards, 1):
            card['schedule_page'] = page_number
            card['schedule_pages'] = len(schedule_cards)
            card['primitives'] = [
                replace(p, text=f'PANEL SCHEDULE {page_number}/{len(schedule_cards)}')
                if dict(p.metadata).get('role') == 'page_reference' else p
                for p in card['primitives']
            ]
    # Column-first placement leaves a genuine region for a caller's one-line.
    placed, column, cursor = [[]], 0, 0.0
    for card in cards:
        if layout.packing == 'one_table_per_sheet' and placed[-1]:
            placed.append([]); column = 0; cursor = 0.0
        if cursor and cursor + card['height'] > region_height:
            column += 1; cursor = 0.0
        if column >= columns:
            placed.append([]); column = 0; cursor = 0.0
        placed[-1].append((card, column * (card_width + gutter), cursor))
        cursor += card['height'] + gutter
    result = []
    for number, items in enumerate(placed, 1):
        match = re.fullmatch(r'(.*?)([0-9]+)', sheet.id)
        sid = (match[1] + str(int(match[2]) + number - 1).zfill(len(match[2]))) if match else f'{sheet.id}{number:03d}'
        view_id = f'drafted-panel-page:{number:03d}'
        primitives, row_map, sections = [], [], []
        for card, xx, yy in items:
            identities = {}
            for p in card['primitives']:
                identity = f'{view_id}:item:{len(primitives):06d}'; identities[p.id] = identity
                primitives.append(replace(p, id=identity, points=tuple(Point2(pt.x + xx / 1000, pt.y - yy / 1000) for pt in p.points)))
            for row in card['rows']:
                box = row['bounds_mm']
                row_map.append({**row, 'sheet_id': sid, 'page': number,
                    'primitive_ids': [identities[i] for i in row['primitive_ids']],
                    'bounds_mm': [l + inset + xx + box[0], t + inset + yy + box[1],
                                  l + inset + xx + box[2], t + inset + yy + box[3]]})
            sections.append({k: card[k] for k in ('schedule_id', 'schedule_page', 'schedule_pages', 'panel_id', 'included_schedule_ids', 'position_gaps')})
            sections[-1]['bounds_mm'] = [l + inset + xx, t + inset + yy, l + inset + xx + card_width, t + inset + yy + card['height']]
        # Pairing changes printed order only; logical source coverage stays canonical.
        order = {s.id: i for i, s in enumerate(drawing_set.schedules)}
        row_map.sort(key=lambda row: (order[row['schedule_id']], row['row_index']))
        view = DrawingView(view_id, 'plan', template.title, Bounds2(0, 0, region_width / 1000, region_height / 1000),
                           1, tuple(primitives), metadata=(('presentation', 'drafted paired panel schedule'),))
        result.append(SchedulePage(replace(template, id=sid), DrawingSet(drawing_set.model_id, (view,), (), drawing_set.source_index),
            ViewPlacement(view_id, view_id, l + inset, t + inset), tuple(row_map), None, None, None, tuple(sections)))
    return tuple(result)
