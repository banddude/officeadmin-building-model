"""Deterministic, opt-in SVG output for physical sheets.

No XML from callers is accepted. Symbols are explicit paper-space line segments;
unknown tokens fail rather than becoming plausible-looking substitute symbols.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
from dataclasses import dataclass
from typing import Mapping

from .model import DrawingSet, Point2, _finite
from .sheets import ComposedSheet, SheetSpec, ViewPlacement, compose_blank_sheet, compose_sheet

_STROKES = {
    'object': '#000000', 'cut': '#000000', 'opening': '#000000',
    'electrical': '#000000', 'route': '#000000', 'annotation': '#000000',
    'symbol': '#000000', 'space': '#666666', 'obstacle': '#666666',
    'constraint': '#990000', 'ghost': '#a6adb8',
}
_WEIGHTS = {'normal': 0.25, 'heavy': 0.5, 'thin': 0.13}
_PATTERNS = {'solid': None, 'dash': '3 1.5', 'dot': '0.25 1'}


def _fmt(value: float) -> str:
    value = _finite(value, 'SVG coordinate')
    text = f'{value:.9f}'.rstrip('0').rstrip('.')
    return '0' if text in {'', '-0'} else text


def _xml(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('SVG text must be a string')
    if any(not (c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in value):
        raise ValueError('text contains characters forbidden by XML 1.0')
    return html.escape(value, quote=True)


def _one_line(value: str) -> str:
    _xml(value)
    if any(c in value for c in '\n\r\t'):
        raise ValueError('multiline/tabbed SVG text requires explicit layout')
    return value


@dataclass(frozen=True, slots=True)
class SheetSvgOptions:
    font_family: str = 'DejaVu Sans'
    text_height_mm: float = 2.5
    dimension_text_height_mm: float = 2.5
    title_text_height_mm: float = 4.0
    arrow_length_mm: float = 1.5
    text_halo_mm: float = 0.7

    def __post_init__(self) -> None:
        if not isinstance(self.font_family, str) or not self.font_family.strip():
            raise ValueError('font_family must be non-empty')
        _one_line(self.font_family)
        halo = _finite(self.text_halo_mm, 'text_halo_mm')
        if halo < 0:
            raise ValueError('text_halo_mm must be non-negative')
        object.__setattr__(self, 'text_halo_mm', halo)
        for key in ('text_height_mm', 'dimension_text_height_mm', 'title_text_height_mm', 'arrow_length_mm'):
            value = _finite(getattr(self, key), key)
            if value <= 0 or _fmt(value) == '0':
                raise ValueError(f'{key} must be positive')
            object.__setattr__(self, key, value)


@dataclass(frozen=True, slots=True)
class SheetSymbol:
    """Explicit line segments in mm, relative to the anchor, +Y down."""
    segments: tuple[tuple[Point2, Point2], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, (tuple, list)) or not self.segments:
            raise ValueError('symbol requires explicit line segments')
        segments = []
        for segment in self.segments:
            if not isinstance(segment, (tuple, list)) or len(segment) != 2 or not all(isinstance(p, Point2) for p in segment):
                raise ValueError('each symbol segment needs two Point2 endpoints')
            if segment[0] == segment[1]:
                raise ValueError('symbol segment must have positive length')
            segments.append(tuple(segment))
        object.__setattr__(self, 'segments', tuple(segments))


@dataclass(frozen=True, slots=True)
class RenderedSheetSvg:
    svg: str
    manifest_json: str

    def manifest(self) -> dict:
        return json.loads(self.manifest_json)


def render_sheet_svg(
    drawing_set: DrawingSet,
    spec: SheetSpec,
    placements: tuple[ViewPlacement, ...],
    *,
    options: SheetSvgOptions | None = None,
    symbols: Mapping[str, SheetSymbol] | None = None,
) -> RenderedSheetSvg:
    """Compose and render existing views; neither fetches assets nor writes files.

    Text stays editable. Font availability/glyph extents require downstream
    visual QA; the document does not certify layout, design or permit readiness.
    A titleblock is required so sheet identity/revision are visibly printed.
    """
    return _render_composed_sheet_svg(compose_sheet(drawing_set,spec,placements),spec,
                                      options=options,symbols=symbols,drawing_set=drawing_set)


def render_blank_sheet_svg(spec: SheetSpec, *, options: SheetSvgOptions | None = None) -> RenderedSheetSvg:
    """Render a populated titleblock and blank drawing body, without a model."""
    return _render_composed_sheet_svg(compose_blank_sheet(spec),spec,options=options)


def _render_composed_sheet_svg(composed: ComposedSheet, spec: SheetSpec, *,
        options: SheetSvgOptions | None = None, symbols: Mapping[str, SheetSymbol] | None = None,
        drawing_set: DrawingSet | None = None) -> RenderedSheetSvg:
    options = options or SheetSvgOptions()
    symbols = dict(symbols or {})
    for token, symbol in symbols.items():
        if not isinstance(token, str) or not token.strip() or not isinstance(symbol, SheetSymbol):
            raise ValueError('symbol registry must map non-empty tokens to SheetSymbol')
        _xml(token)
    sheet = composed.to_dict()
    if spec.titleblock_height_mm <= 0:
        raise ValueError('SVG output requires a reserved titleblock for sheet identity')
    items: list[dict] = []
    fragments: list[str] = []
    annotations: list[str] = []
    used_symbols: set[str] = set()

    def identity(parts: tuple[str, ...]) -> str:
        return 'oabm-' + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    def check_points(points: list[dict], bounds: list[float], *, pad: float = 0) -> None:
        for p in points:
            px, py = _finite(p['x'], 'x'), _finite(p['y'], 'y')
            if px - pad < bounds[0] - 1e-8 or py - pad < bounds[1] - 1e-8 or px + pad > bounds[2] + 1e-8 or py + pad > bounds[3] + 1e-8:
                raise ValueError('rendered geometry exceeds its placed view; reserve more source bounds')

    def line(a: dict, b: dict, attrs: str) -> str:
        return f'<line x1="{_fmt(a["x"])}" y1="{_fmt(a["y"])}" x2="{_fmt(b["x"])}" y2="{_fmt(b["y"])}" {attrs}/>'

    def text(value: str, p: dict, size: float, *, anchor: str = 'start', attrs: str = '') -> str:
        _one_line(value)
        halo = '' if options.text_halo_mm == 0 else f'paint-order="stroke fill" stroke="#ffffff" stroke-width="{_fmt(options.text_halo_mm)}" stroke-linejoin="round"'
        return f'<text x="{_fmt(p["x"])}" y="{_fmt(p["y"])}" font-family="{_xml(options.font_family)}" font-size="{_fmt(size)}" text-anchor="{anchor}" fill="#000000" {halo} {attrs}>{_xml(value)}</text>'

    def record(view: dict, kind: str, source_item: dict) -> tuple[str, str]:
        identifier = identity((view['placement_id'], view['view_id'], kind, source_item['id']))
        ids = source_item['source_ids']
        items.append({'svg_id': identifier, 'placement_id': view['placement_id'], 'view_id': view['view_id'],
                      'kind': kind, 'source_item_id': source_item['id'], 'source_ids': ids,
                      'source_item_metadata': source_item.get('metadata', [])})
        attrs = f'id="{identifier}" data-source-ids="{_xml(json.dumps(ids, separators=(",", ":")))}"'
        return identifier, attrs

    for view in sheet['views']:
        bounds = view['bounds_mm']
        for p in view['primitives']:
            _, provenance = record(view, 'primitive', p)
            style = p['style']
            if style['stroke'] not in _STROKES or style['weight'] not in _WEIGHTS or style['pattern'] not in _PATTERNS:
                raise ValueError(f'unsupported semantic line style: {style!r}')
            width = _WEIGHTS[style['weight']]
            attrs = f'stroke="{_STROKES[style["stroke"]]}" stroke-width="{_fmt(width)}" fill="none" stroke-linecap="butt" stroke-linejoin="round"'
            if _PATTERNS[style['pattern']]:
                attrs += f' stroke-dasharray="{_PATTERNS[style["pattern"]]}"'
            kind, points = p['kind'], p['points_mm']
            if kind in {'line', 'polyline', 'polygon'}:
                check_points(points, bounds, pad=width / 2)
                tag = 'polygon' if p['closed'] or kind == 'polygon' else 'polyline'
                vertices = ' '.join(f'{_fmt(pt["x"])},{_fmt(pt["y"])}' for pt in points)
                fragments.append(f'<{tag} points="{vertices}" {attrs} {provenance}/>')
            elif kind == 'text':
                if p['text'] is None:
                    raise ValueError('text primitive is missing text')
                annotations.append(text(p['text'], points[0], options.text_height_mm, attrs=provenance))
            elif kind == 'symbol':
                token = p['symbol']
                if token not in symbols:
                    raise ValueError(f'no explicit geometry for symbol {token!r}')
                if p['text']:
                    raise ValueError('symbol labels require a separate positioned text primitive')
                used_symbols.add(token)
                fragments.append(f'<g {provenance} data-symbol="{_xml(token)}">')
                for a, b in symbols[token].segments:
                    translated = [{'x': points[0]['x'] + q.x, 'y': points[0]['y'] + q.y} for q in (a, b)]
                    check_points(translated, bounds, pad=width / 2)
                    fragments.append(line(*translated, attrs))
                fragments.append('</g>')
            else:
                raise ValueError(f'unsupported primitive kind {kind!r}')
        for dimension in view['dimensions']:
            identifier, provenance = record(view, 'dimension', dimension)
            label_id = identifier + '-label'
            items[-1]['svg_label_id'] = label_id
            a, b = dimension['line_start_mm'], dimension['line_end_mm']
            dx, dy = b['x'] - a['x'], b['y'] - a['y']
            length = math.hypot(dx, dy)
            if length < 2 * options.arrow_length_mm:
                raise ValueError('dimension too short for configured arrows')
            ux, uy = dx / length, dy / length
            arrows = []
            for tip, direction in ((a, 1), (b, -1)):
                along = options.arrow_length_mm * direction
                half = options.arrow_length_mm / 3
                arrows.append([tip, {'x': tip['x'] + along * ux - half * uy, 'y': tip['y'] + along * uy + half * ux},
                                    {'x': tip['x'] + along * ux + half * uy, 'y': tip['y'] + along * uy - half * ux}])
            check_points([a, b, dimension['measured_start_mm'], dimension['measured_end_mm']], bounds, pad=.125)
            for arrow in arrows:
                check_points(arrow, bounds)
            attrs = 'stroke="#000000" stroke-width="0.25" fill="none"'
            fragments.append(f'<g {provenance}>')
            fragments.append(line(a, b, attrs))
            fragments.append(line(dimension['measured_start_mm'], a, attrs))
            fragments.append(line(dimension['measured_end_mm'], b, attrs))
            for arrow in arrows:
                vertices = ' '.join(f'{_fmt(pt["x"])},{_fmt(pt["y"])}' for pt in arrow)
                fragments.append(f'<polygon points="{vertices}" fill="#000000"/>')
            # A separate paper-space gap above the line, never a measured-value change.
            anchor = dict(dimension['text_anchor_mm'])
            anchor['y'] -= .8
            check_points([anchor], bounds)
            label_attrs = f'id="{label_id}" data-dimension-group="{identifier}" data-source-ids="{_xml(json.dumps(dimension["source_ids"], separators=(",", ":")))}"'
            annotations.append(text(dimension['text'], anchor, options.dimension_text_height_mm, anchor='middle', attrs=label_attrs))
            fragments.append('</g>')

    # Deliberately simple full-width titleblock, with bounded row capacity.
    left, top, right, bottom = sheet['titleblock']['bounds_mm']
    pad, row = 3.0, options.text_height_mm * 1.8
    title = spec.title or 'Drawing sheet'
    title_lines = [f'{spec.id} | {title}', f'Revision: {spec.revision or "unspecified"}']
    field_lines = [f'{key}: {value}' for key, value in spec.titleblock_fields]
    required_height = 2 * pad + options.title_text_height_mm + (1 + len(field_lines)) * row
    if required_height > bottom - top:
        raise ValueError('titleblock has insufficient height for configured text rows')
    all_lines = [(title_lines[0], options.title_text_height_mm), (title_lines[1], options.text_height_mm)] + [(v, options.text_height_mm) for v in field_lines]
    for value, size in all_lines:
        _one_line(value)
        # Conservative admission bound, not font-shaping certification.
        if len(value) * size > right - left - 2 * pad:
            raise ValueError('titleblock text exceeds conservative row capacity; shorten it or enlarge paper')
    frame = f'<rect x="{_fmt(spec.margin_mm)}" y="{_fmt(spec.margin_mm)}" width="{_fmt(spec.width_mm - 2 * spec.margin_mm)}" height="{_fmt(spec.height_mm - 2 * spec.margin_mm)}" fill="none" stroke="#000000" stroke-width="0.25"/>'
    divider = line({'x': left, 'y': top}, {'x': right, 'y': top}, 'stroke="#000000" stroke-width="0.25"')
    title_svg = [frame, divider]
    baseline = top + pad + options.title_text_height_mm
    for index, (value, size) in enumerate(all_lines):
        title_svg.append(text(value, {'x': left + pad, 'y': baseline}, size, attrs=f'data-titleblock-row="{index}"'))
        baseline += row
    svg = '\n'.join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(spec.width_mm)}mm" height="{_fmt(spec.height_mm)}mm" viewBox="0 0 {_fmt(spec.width_mm)} {_fmt(spec.height_mm)}">',
        f'<title>{_xml(title)}</title>',
        ('<desc>Blank sheet stationery only. No model, design or approval content.</desc>' if drawing_set is None else
         '<desc>Model-derived presentation only. Review fonts, layout and design before use.</desc>'),
        *fragments, *annotations, *title_svg, '</svg>', '',
    ])
    manifest = {
        'kind': 'oabm.sheet-svg', 'version': '1.0.0', 'sheet_id': spec.id,
        'sheet_sha256': hashlib.sha256(composed.to_json(indent=None).encode()).hexdigest(),
        'drawing_set_sha256': sheet['drawing_set_sha256'], 'model_id': sheet['model_id'],
        'svg_sha256': hashlib.sha256(svg.encode()).hexdigest(),
        'source_index': sheet['source_index'], 'items': items,
        'views': [{key: view[key] for key in ('placement_id', 'view_id', 'title', 'scale', 'bounds_mm', 'metadata')} for view in sheet['views']],
        'unplaced_view_ids': sorted({view.id for view in drawing_set.views} - {view['view_id'] for view in sheet['views']}) if drawing_set is not None else [],
        'unrendered_schedule_ids': sorted(schedule.id for schedule in drawing_set.schedules) if drawing_set is not None else [],
        'render_options': {key: getattr(options, key) for key in options.__dataclass_fields__},
        'symbol_geometry_mm': {token: [[[p.x, p.y] for p in segment] for segment in symbols[token].segments] for token in sorted(used_symbols)},
        'styles': {'stroke_colors': _STROKES, 'lineweights_mm': _WEIGHTS, 'dash_patterns_mm': _PATTERNS},
        'limitations': ['SVG bytes are deterministic; pixel rendering depends on installed fonts and renderer.',
                        'Text anchors are positioned; glyph extents/collisions require visual QA.',
                        'Configured white text halos mask underlying strokes locally for readability.',
                        'View titles and schedules are metadata, not automatically laid-out graphics.',
                        'No canonical writeback, engineering approval or permit-readiness claim.'],
    }
    return RenderedSheetSvg(svg, json.dumps(manifest, sort_keys=True, allow_nan=False))
