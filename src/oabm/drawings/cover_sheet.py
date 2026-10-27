"""Supplied project facts to a bounded cover body on shared company stationery."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path

from .blank_set import _decode, _json, _text as _single_line, _identifier
from .sheet_svg import _xml, _fmt
from .company_sheets import measure_sheet_text, validate_sheet_text

VERSION = 'cover-sheet-v1'
SUPPORTED_FONT = 'DejaVu Sans'
def _text(value, label):
    _single_line(value, label)
    try:
        return validate_sheet_text(value)
    except ValueError as exc:
        for index, char in enumerate(value):
            if not 32 <= ord(char) <= 126:
                raise ValueError(f'{label}: unsupported glyph U+{ord(char):04X} at character {index}; '
                                 f'{exc}; source text was not changed') from exc
        raise ValueError(f'{label}: {exc}; source text was not changed') from exc


def _wrap_text(value, width_mm, size_mm, family):
    """Wrap with the shared measured ink/advance contract, preserving all text."""
    def fits(text):
        return measure_sheet_text(text, size_mm, font_family=family) <= width_mm
    lines = []
    while value:
        if not fits(value[0]):
            raise CoverCapacityError('text: one supported glyph exceeds panel width')
        low, high = 1, 2
        while high < len(value) and fits(value[:high]):
            low, high = high, high * 2
        high = min(high, len(value))
        while low < high:
            middle = (low + high + 1) // 2
            if fits(value[:middle]):
                low = middle
            else:
                high = middle - 1
        count = low
        if count < len(value):
            split = value.rfind(' ', 0, count)
            if split > 0:
                count = split + 1
        lines.append(value[:count])
        value = value[count:]
    return lines


class CoverCapacityError(ValueError):
    """All content is retained in the request; capacity failures publish nothing."""


def validate_cover(config):
    required = {'version', 'id', 'project', 'scope', 'sheets', 'contacts'}
    optional = {'notes', 'location_map'}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - optional:
        raise ValueError('cover: required version, id, project, scope, sheets, contacts; optional notes/location_map')
    if config['version'] != VERSION:
        raise ValueError('unsupported cover version')
    _identifier(config['id'], 'id')
    project = config['project']
    if not isinstance(project, dict) or not {'name', 'location'} <= project.keys():
        raise ValueError('project: explicit name and location required')
    for key, value in project.items():
        _text(key, 'project field')
        _text(value, 'project.' + key)
    for key in ('scope', 'sheets', 'contacts'):
        if not isinstance(config[key], list) or not config[key]:
            raise ValueError(key + ': supply a nonempty ordered list')
    for i, value in enumerate(config['scope']):
        _text(value, f'scope[{i}]')
    seen = set()
    for row in config['sheets']:
        if not isinstance(row, dict) or set(row) != {'id', 'title'}:
            raise ValueError('sheets: each row needs id and title')
        _identifier(row['id'], 'sheet id')
        _text(row['title'], 'sheet title')
        if row['id'].casefold() in seen:
            raise ValueError('duplicate sheet id')
        seen.add(row['id'].casefold())
    if config['id'].casefold() not in seen:
        raise ValueError('sheet index must include the cover id')
    for row in config['contacts']:
        if not isinstance(row, dict) or not {'role', 'name'} <= row.keys() or row.keys() - {'role', 'name', 'phone', 'email', 'address'}:
            raise ValueError('contacts: role/name required; phone/email/address optional')
        for key, value in row.items():
            _text(value, 'contact.' + key)
    if 'notes' in config:
        if not isinstance(config['notes'], list):
            raise ValueError('notes: expected list')
        for value in config['notes']:
            _text(value, 'note')
    if 'location_map' in config:
        m = config['location_map']
        if not isinstance(m, dict) or set(m) != {'caption', 'width', 'height', 'segments'}:
            raise ValueError('location_map: caption, width, height, segments required')
        _text(m['caption'], 'map caption')
        for key in ('width', 'height'):
            if type(m[key]) not in (int, float) or not math.isfinite(m[key]) or m[key] <= 0:
                raise ValueError('map dimensions must be finite positive numbers')
        if not isinstance(m['segments'], list) or not m['segments']:
            raise ValueError('map: supply explicit vector segments')
        for segment in m['segments']:
            if not isinstance(segment, list) or len(segment) != 4:
                raise ValueError('map segments: [x1,y1,x2,y2] required')
            for i, value in enumerate(segment):
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= m['width' if i % 2 == 0 else 'height']:
                    raise ValueError('map segment outside supplied extent')
    return json.loads(_json(config))


@dataclass(frozen=True)
class CoverBody:
    svg_elements: str
    layout: dict


def compose_cover_body(config, bounds_mm, *, font_family='DejaVu Sans', text_height_mm=3.0):
    """Lay out facts inside shared template body bounds, in physical mm.

    Uses shared printable-ASCII validation and measured font widths, failing with panel diagnostics
    instead of shrinking, clipping or dropping supplied facts.
    """
    config = validate_cover(config)
    if len(bounds_mm) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds_mm):
        raise ValueError('body bounds require four finite millimetre coordinates')
    left, top, right, bottom = bounds_mm
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError('body bounds must be a positive rectangle')
    if type(text_height_mm) not in (int, float) or not math.isfinite(text_height_mm) or text_height_mm < 2.5:
        raise ValueError('text height must be at least 2.5 mm')
    if font_family != SUPPORTED_FONT:
        raise ValueError('cover body supports only DejaVu Sans with its bounded printable-ASCII text layout')
    # Keep panel strokes inside the shared body's permitted physical rectangle.
    left, top, right, bottom = left + .25, top + .25, right - .25, bottom - .25
    gap, pad = 6.0, 4.0
    width = (right - left - gap) / 2
    columns = [top, top]
    elements, panels = [], []
    sections = [('Project', [f'{k}: {v}' for k, v in config['project'].items()], 0),
                ('Scope', config['scope'], 0),
                ('Sheet index', [f"{r['id']}  {r['title']}" for r in config['sheets']], 1),
                ('Contacts', [' | '.join(f'{k}: {v}' for k, v in r.items()) for r in config['contacts']], 1)]
    if config.get('notes'):
        sections.append(('Job notes', config['notes'], 0))
    line_height = text_height_mm * 1.6
    text_width = width - 2 * pad
    if any(measure_sheet_text(title.upper(), text_height_mm, font_family=font_family) > text_width
           for title, _, _ in sections):
        raise CoverCapacityError('body: insufficient width for cover panels')
    def text(value, x, y, size):
        return f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-family="{_xml(font_family)}" font-size="{_fmt(size)}" fill="#000000" xml:space="preserve">{_xml(value)}</text>'
    for title, facts, column in sections:
        lines = []
        for fact in facts:
            lines.extend(_wrap_text(fact, text_width, text_height_mm, font_family))
        panel_height = 2 * pad + line_height * (len(lines) + 2)
        y = columns[column]
        if y + panel_height > bottom:
            raise CoverCapacityError(f'{title}: needs {panel_height:.2f} mm; {bottom-y:.2f} mm available; enlarge paper or simplify supplied content')
        x = left + column * (width + gap)
        elements.append(f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(width)}" height="{_fmt(panel_height)}" fill="none" stroke="#000000" stroke-width="0.25"/>')
        elements.append(text(title.upper(), x + pad, y + pad + text_height_mm, text_height_mm))
        for i, line in enumerate(lines, 2):
            elements.append(text(line, x + pad, y + pad + i * line_height, text_height_mm))
        panels.append({'title': title, 'bounds_mm': [x, y, x+width, y+panel_height], 'lines': lines})
        columns[column] += panel_height + gap
    if 'location_map' in config:
        m = config['location_map']
        x, y = left, columns[0]
        available = bottom - y - 2 * pad - 2 * line_height
        scale = min((width - 2 * pad) / m['width'], available / m['height'])
        if available <= 0 or scale <= 0:
            raise CoverCapacityError('Location map: insufficient remaining body capacity')
        elements.append(text(m['caption'], x + pad, y + text_height_mm, text_height_mm))
        if measure_sheet_text(m['caption'], text_height_mm, font_family=font_family) > text_width:
            raise CoverCapacityError('Location map: caption exceeds panel width')
        for x1, y1, x2, y2 in m['segments']:
            elements.append(f'<line x1="{_fmt(x+pad+x1*scale)}" y1="{_fmt(y+2*line_height+y1*scale)}" x2="{_fmt(x+pad+x2*scale)}" y2="{_fmt(y+2*line_height+y2*scale)}" stroke="#000000" stroke-width="0.25"/>')
        panels.append({'title': 'Location map', 'bounds_mm': [x, y, x+width, bottom], 'scale': scale})
    return CoverBody('\n'.join(elements), {'version': VERSION, 'bounds_mm': list(bounds_mm),
                     'text_support': {'font_family': SUPPORTED_FONT, 'glyphs': 'U+0020-U+007E',
                                      'measurement': 'shared occupied width including ink bearings and 1% margin',
                                      'text_width_mm': text_width},
                     'panels': panels})


def create_cover_sheet(config_path, *, profile_path, job_path, output_directory):
    """Produce the cover through the shared company profile and sheet tool."""
    raw = Path(config_path).read_bytes()
    config = validate_cover(_decode(raw))
    from .company_sheets import resolve_company_sheet_set, create_company_sheet_set

    resolved = resolve_company_sheet_set(profile_path, job_path)
    actual_index = [{'id': sheet.id, 'title': sheet.title} for sheet in resolved.sheets]
    if config['sheets'] != actual_index:
        raise ValueError('cover sheet index must exactly match the shared delivery sheet IDs and titles in order; '
                         'use the complete resolved company sheet list')
    # The shared set is authoritative for the visible index and produced pages.
    config['sheets'] = actual_index
    matches = [sheet for sheet in resolved.sheets if sheet.id == config['id']]
    if len(matches) != 1:
        raise ValueError('cover id must identify exactly one shared company sheet')
    sheet = matches[0]
    body = compose_cover_body(config, sheet.drawing_bounds_mm)
    subtree = '<g xmlns="http://www.w3.org/2000/svg">' + body.svg_elements + '</g>'
    target = Path(output_directory).absolute()
    target.mkdir(parents=True)
    pending = target / '.pending'
    pending.mkdir()
    shared = create_company_sheet_set(profile_path, job_path, output_directory=pending / 'company',
                                      bodies={sheet.id: subtree})
    if not Path(shared).is_relative_to(pending):
        raise ValueError('shared delivery must remain inside the pending cover attempt')
    (pending / 'cover-input.json').write_bytes(raw)
    (pending / 'cover-layout.json').write_bytes(_json(body.layout))
    files = []
    for path in sorted(pending.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            files.append({'path': path.relative_to(pending).as_posix(),
                          'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    (pending / 'COVER-MANIFEST.json').write_bytes(_json({'version': VERSION, 'id': config['id'], 'files': files}))
    os.rename(pending, target / 'delivery')
    return target / 'delivery'


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cover', type=Path)
    parser.add_argument('profile', type=Path)
    parser.add_argument('job', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    try:
        delivery = create_cover_sheet(args.cover, profile_path=args.profile, job_path=args.job,
                                      output_directory=args.output)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(delivery)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
