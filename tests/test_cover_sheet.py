import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from oabm.drawings.cover_sheet import CoverCapacityError, compose_cover_body, create_cover_sheet, validate_cover


@pytest.fixture
def config():
    return json.loads((Path(__file__).parents[1] / 'examples/cover-sheet/input.json').read_text())


def test_layout_is_deterministic_and_bounded(config):
    a = compose_cover_body(config, (10, 10, 800, 480))
    assert a == compose_cover_body(config, (10, 10, 800, 480))
    for panel in a.layout['panels']:
        x, y, right, bottom = panel['bounds_mm']
        assert 10 <= x < right <= 800
        assert 10 <= y < bottom <= 480
    assert [p['title'] for p in a.layout['panels']] == ['Project', 'Scope', 'Sheet index', 'Contacts', 'Job notes']
    assert 'Location map' not in a.svg_elements


@pytest.mark.parametrize('field', ['scope', 'sheets', 'contacts'])
def test_many_rows_are_refused_without_truncation(config, field):
    config[field] *= 300
    if field == 'sheets':
        for i, row in enumerate(config[field]):
            config[field][i] = {**row, 'id': config['id'] if i == 0 else f'S{i}'}
    with pytest.raises(CoverCapacityError, match='needs'):
        compose_cover_body(config, (10, 10, 800, 480))


def test_long_text_wraps_and_xml_is_escaped(config):
    text = '<tag>&' + 'longword' * 30
    config['scope'] = [text]
    body = compose_cover_body(config, (10, 10, 800, 480))
    scope = next(p for p in body.layout['panels'] if p['title'] == 'Scope')
    assert ''.join(scope['lines']) == text
    ET.fromstring('<g>' + body.svg_elements + '</g>')


@pytest.mark.parametrize('field', ['name', 'location'])
def test_required_project_facts(config, field):
    del config['project'][field]
    with pytest.raises(ValueError, match='explicit name and location'):
        validate_cover(config)


def test_map_is_only_supplied_geometry(config):
    config['location_map'] = {'caption': 'Supplied diagram', 'width': 100, 'height': 60, 'segments': [[0, 0, 100, 60]]}
    body = compose_cover_body(config, (10, 10, 800, 480))
    assert body.layout['panels'][-1]['title'] == 'Location map'
    config['location_map']['segments'][0][2] = 101
    with pytest.raises(ValueError, match='outside'):
        validate_cover(config)


def test_dependency_failure_publishes_nothing(config, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(config))
    def resolve(*args):
        return SimpleNamespace(sheets=[SimpleNamespace(**row, drawing_bounds_mm=(10,10,800,480)) for row in config['sheets']])
    def fail(*args, **kwargs):
        raise ValueError('PDF dependency unavailable')
    monkeypatch.setitem(sys.modules, 'oabm.drawings.company_sheets', SimpleNamespace(
        resolve_company_sheet_set=resolve, create_company_sheet_set=fail))
    with pytest.raises(ValueError, match='PDF dependency'):
        create_cover_sheet(path, profile_path='private-profile', job_path='job', output_directory=tmp_path/'output')
    assert not (tmp_path/'output'/'delivery').exists()


def test_shared_boundary_order_body_and_atomic_receipts(config, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import hashlib
    calls = []
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(config))
    def resolve(profile, job):
        calls.append(('resolve', profile, job))
        return SimpleNamespace(sheets=[SimpleNamespace(**row, drawing_bounds_mm=(20,20,780,460)) for row in config['sheets']])
    def create(profile, job, *, output_directory, bodies):
        assert set(bodies) == {config['id']}
        group = ET.fromstring(bodies[config['id']])
        assert group.tag == '{http://www.w3.org/2000/svg}g'
        assert 'Synthetic Workshop' in ''.join(group.itertext())
        delivery = output_directory / 'delivery'
        delivery.mkdir(parents=True)
        (delivery/'shared-sentinel.txt').write_text('Shared layer owns stationery and output ordering')
        return delivery
    monkeypatch.setitem(sys.modules, 'oabm.drawings.company_sheets', SimpleNamespace(
        resolve_company_sheet_set=resolve, create_company_sheet_set=create))
    outputs = [create_cover_sheet(path, profile_path='profile', job_path='job', output_directory=tmp_path/f'o{i}') for i in range(2)]
    assert calls == [('resolve', 'profile', 'job')] * 2
    assert (outputs[0]/'COVER-MANIFEST.json').read_bytes() == (outputs[1]/'COVER-MANIFEST.json').read_bytes()
    manifest = json.loads((outputs[0]/'COVER-MANIFEST.json').read_text())
    for row in manifest['files']:
        assert hashlib.sha256((outputs[0]/row['path']).read_bytes()).hexdigest() == row['sha256']
    assert json.loads((outputs[0]/'cover-input.json').read_text()) == config
    with pytest.raises(FileExistsError):
        create_cover_sheet(path, profile_path='profile', job_path='job', output_directory=tmp_path/'o0')


@pytest.mark.parametrize('glyph', ['\u2031', '\u6f22', '\U0001f600', '\uff37', '\u0301'])
def test_unsupported_wide_glyphs_refused_without_source_loss(config, glyph, tmp_path):
    supplied = glyph * 128
    config['scope'] = [supplied]
    path = tmp_path/'wide.json'
    raw = json.dumps(config, ensure_ascii=False).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='unsupported glyph U\\+'):
        compose_cover_body(config, (10,10,800,480))
    with pytest.raises(ValueError, match='unsupported glyph U\\+'):
        create_cover_sheet(path, profile_path='unused', job_path='unused', output_directory=tmp_path/'output')
    assert config['scope'] == [supplied]
    assert path.read_bytes() == raw
    assert not (tmp_path/'output').exists()


@pytest.mark.parametrize('glyph', ['W', '@', 'M', '~'])
def test_supported_wide_ascii_wraps_with_conservative_envelope(config, glyph):
    from oabm.drawings.company_sheets import measure_sheet_text
    supplied = glyph * 256
    config['scope'] = [supplied]
    body = compose_cover_body(config, (10,10,800,480))
    panel = next(p for p in body.layout['panels'] if p['title'] == 'Scope')
    assert ''.join(panel['lines']) == supplied
    for line in panel['lines']:
        assert measure_sheet_text(line, 3, font_family='DejaVu Sans') <= panel['bounds_mm'][2] - panel['bounds_mm'][0] - 8


@pytest.mark.parametrize('where', ['project_key', 'project_value', 'sheet', 'contact', 'note', 'caption'])
def test_glyph_boundary_applies_to_every_body_text_source(config, where):
    glyph = '\u2031'
    if where == 'project_key':
        config['project'][glyph] = 'supplied'
    elif where == 'project_value':
        config['project']['name'] = glyph
    elif where == 'sheet':
        config['sheets'][0]['title'] = glyph
    elif where == 'contact':
        config['contacts'][0]['name'] = glyph
    elif where == 'note':
        config['notes'] = [glyph]
    else:
        config['location_map'] = {'caption': glyph, 'width': 1, 'height': 1, 'segments': [[0,0,1,1]]}
    with pytest.raises(ValueError, match='unsupported glyph'):
        validate_cover(config)


def test_unbounded_font_substitution_and_heading_overflow_are_refused(config):
    with pytest.raises(ValueError, match='only DejaVu Sans'):
        compose_cover_body(config, (10,10,800,480), font_family='Some Wide Font')
    with pytest.raises(CoverCapacityError, match='insufficient width'):
        compose_cover_body(config, (10,10,55,480))


def test_real_shared_profile_cover_svg_pdf_and_replay(config, tmp_path):
    import io
    from pypdf import PdfReader
    from oabm.drawings.company_sheets import rebuild_company_sheet_set, resolve_company_sheet_set
    repo = Path(__file__).parents[1]
    profile = repo/'fixtures/company_sheets/v1/profile.json'
    job = repo/'examples/cover-sheet/job.json'
    source = repo/'examples/cover-sheet/input.json'
    original = (profile.read_bytes(), job.read_bytes(), source.read_bytes())
    outputs = [create_cover_sheet(source, profile_path=profile, job_path=job, output_directory=tmp_path/f'run-{i}') for i in range(2)]
    first = outputs[0]/'company/delivery'
    second = outputs[1]/'company/delivery'
    assert (first/'sheet-set.pdf').read_bytes() == (second/'sheet-set.pdf').read_bytes()
    manifest = json.loads((first/'MANIFEST.json').read_text())
    assert manifest['sheet_order'] == [row['id'] for row in config['sheets']]
    svg = (first/'sheets/001-G001.svg').read_text()
    assert 'EXAMPLE ELECTRIC' in svg and 'Synthetic Workshop' in svg
    assert 'Scope'.upper() in svg and 'Sheet index'.upper() in svg
    root = ET.fromstring(svg)
    assert root.get('width') == '914.4mm' and root.get('height') == '609.6mm'
    pdf = PdfReader(io.BytesIO((first/'sheet-set.pdf').read_bytes()))
    assert len(pdf.pages) == 3
    for page, sheet in zip(pdf.pages, resolve_company_sheet_set(profile, job).sheets):
        assert abs(float(page.mediabox.width) - sheet.width_mm*72/25.4) < .01
        assert abs(float(page.mediabox.height) - sheet.height_mm*72/25.4) < .01
        assert sheet.id in page.extract_text()
    text = ' '.join(pdf.pages[0].extract_text().split())
    assert 'SCOPE' in text and 'CONTACTS' in text and 'Synthetic Workshop' in text
    for row in config['sheets']:
        assert f"{row['id']} {row['title']}" in text
    assert not list(pdf.pages[0].images)
    replay = rebuild_company_sheet_set(first, output_directory=tmp_path/'replay')
    assert (replay/'sheet-set.pdf').read_bytes() == (first/'sheet-set.pdf').read_bytes()
    assert (profile.read_bytes(), job.read_bytes(), source.read_bytes()) == original


@pytest.mark.parametrize('change', ['missing', 'reordered', 'title', 'extra', 'id'])
def test_real_shared_sheet_index_contradictions_fail_before_output(config, tmp_path, change):
    repo = Path(__file__).parents[1]
    profile = repo/'fixtures/company_sheets/v1/profile.json'
    job = repo/'examples/cover-sheet/job.json'
    if change == 'missing':
        config['sheets'].pop()
    elif change == 'reordered':
        config['sheets'][1:] = reversed(config['sheets'][1:])
    elif change == 'title':
        config['sheets'][1]['title'] = 'Conflicting title'
    elif change == 'extra':
        config['sheets'].append({'id': 'E999', 'title': 'Absent from delivery'})
    else:
        config['sheets'][1]['id'] = 'E999'
    source = tmp_path/'contradictory-index.json'
    raw = json.dumps(config).encode()
    source.write_bytes(raw)
    with pytest.raises(ValueError, match='exactly match.*IDs and titles in order'):
        create_cover_sheet(source, profile_path=profile, job_path=job, output_directory=tmp_path/'output')
    assert source.read_bytes() == raw
    assert not (tmp_path/'output').exists()


def test_measured_wrapping_preserves_supplied_spaces_in_shared_svg(config):
    from oabm.drawings.company_sheets import resolve_company_sheet_set, render_company_sheet_svg
    supplied = '  ' + ('W  M @ ' * 45) + '   '
    config['scope'] = [supplied]
    repo = Path(__file__).parents[1]
    sheet = resolve_company_sheet_set(repo/'fixtures/company_sheets/v1/profile.json', repo/'examples/cover-sheet/job.json').sheets[0]
    body = compose_cover_body(config, sheet.drawing_bounds_mm)
    scope = next(p for p in body.layout['panels'] if p['title'] == 'Scope')
    assert ''.join(scope['lines']) == supplied
    svg = render_company_sheet_svg(sheet, body_svg='<g xmlns="http://www.w3.org/2000/svg">'+body.svg_elements+'</g>')
    texts = list(ET.fromstring(svg).iter('{http://www.w3.org/2000/svg}text'))
    for line in scope['lines']:
        assert any(node.text == line and node.get('{http://www.w3.org/XML/1998/namespace}space') == 'preserve' for node in texts)
