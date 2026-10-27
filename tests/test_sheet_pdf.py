"""Real local conversion and bounded PDF publication, with synthetic titleblocks."""
import hashlib
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from oabm.drawings import sheet_pdf
from oabm.drawings.blank_set import BlankSheetSetError, create_blank_sheet_set, resolve_blank_sheet_set
from oabm.drawings.sheet_svg import RenderedSheetSvg

FIXTURE=Path(__file__).parents[1]/'fixtures/blank_sheets/v1/config.json'


def request(tmp_path,formats=None):
    cfg=json.loads(FIXTURE.read_bytes());cfg['formats']=formats or ['svg','pdf']
    path=tmp_path/'config.json';path.write_text(json.dumps(cfg))
    return path,cfg


@pytest.fixture
def runtime():
    try:return sheet_pdf._dependencies()
    except ImportError as exc:pytest.skip(str(exc))


def specs():
    return tuple(r['spec'] for r in resolve_blank_sheet_set(json.loads(FIXTURE.read_bytes())))


def test_real_mixed_size_pages_order_text_vectors_and_source_binding(tmp_path,runtime):
    path,cfg=request(tmp_path)
    root=create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    assert manifest['pdf']['status']=='generated' and manifest['pdf']['page_count']==3
    packet=(root/'sheet-set.pdf').read_bytes()
    reader=PdfReader(io.BytesIO(packet),strict=True)
    assert len(reader.pages)==3
    for page,spec in zip(reader.pages,specs()):
        assert float(page.mediabox.width)==pytest.approx(spec.width_mm*72/25.4,abs=.01)
        assert float(page.mediabox.height)==pytest.approx(spec.height_mm*72/25.4,abs=.01)
        text=page.extract_text()
        assert spec.id in text and spec.title in text
        assert 'Project: Synthetic Workshop' in text
        assert f'Revision: {spec.revision}' in text
        assert not list(page.images)
        assert b'BT' in page.get_contents().get_data()  # Text operators, not a page screenshot.
    assert 'Discipline: Power' in reader.pages[1].extract_text()
    assert 'Date: 2026-02-01' in reader.pages[2].extract_text()
    mapping=json.loads((root/'sheet-set.pdf.map.json').read_bytes())
    assert mapping['page_order']==['E0.0','E1.0','E2.0']
    assert mapping['pdf_sha256']==hashlib.sha256(packet).hexdigest()
    for number,row in enumerate(mapping['pages'],1):
        svg=root/f"sheets/{number:03d}-{row['sheet_id']}.svg"
        assert row['svg_sha256']==hashlib.sha256(svg.read_bytes()).hexdigest()
        assert row['font_names']
    assert reader.metadata.title==cfg['set_id']
    assert '/CreationDate' not in reader.metadata and '/ModDate' not in reader.metadata
    for row in manifest['files']:
        data=(root/row['path']).read_bytes()
        assert len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256']


def test_real_rebuild_does_not_preserve_cairo_clock_metadata(runtime):
    a=sheet_pdf.render_blank_sheet_set_pdf(specs(),title='Synthetic packet')
    time.sleep(1.1)  # Cross Cairo's second-resolution creation timestamp.
    b=sheet_pdf.render_blank_sheet_set_pdf(specs(),title='Synthetic packet')
    assert a.pdf==b.pdf
    assert a.manifest_json==b.manifest_json


def test_missing_dependency_fails_requested_pdf_without_publishing(tmp_path,monkeypatch):
    path,_=request(tmp_path)
    def missing():raise ImportError(sheet_pdf.DEPENDENCY_MESSAGE)
    monkeypatch.setattr(sheet_pdf,'_dependencies',missing)
    with pytest.raises(BlankSheetSetError,match='combined PDF.*optional extra') as exc:
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert exc.value.stage=='combined PDF'
    assert (tmp_path/'attempt/.pending/sheets/001-E0.0.svg').exists()
    assert not (tmp_path/'attempt/delivery').exists()
    assert not (tmp_path/'attempt/.pending/sheet-set.pdf').exists()


def test_svg_only_never_loads_optional_pdf_runtime(tmp_path,monkeypatch):
    path,_=request(tmp_path,['svg'])
    monkeypatch.setattr(sheet_pdf,'_dependencies',lambda:pytest.fail('unrequested converter'))
    root=create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert not (root/'sheet-set.pdf').exists()
    assert json.loads((root/'MANIFEST.json').read_bytes())['pdf']['status']=='not_requested'


@pytest.mark.parametrize('payload',[
    '<image href="https://example.invalid/private.png"/>',
    '<text style="fill:url(https://example.invalid/paint)">bad</text>',
    '<script>bad()</script>',
    r'<text fill="u\72l(https://example.invalid/paint)">bad</text>',
    '<style>@import "https://example.invalid/style.css";</style>',
])
def test_external_or_unsupported_content_refused_before_converter(payload,monkeypatch):
    import pypdf
    fake=SimpleNamespace(svg2pdf=lambda **k:pytest.fail('converter must not see resource input'),__version__='fake')
    monkeypatch.setattr(sheet_pdf,'_dependencies',lambda:(fake,SimpleNamespace(),pypdf))
    spec=specs()[0]
    xml=f'<svg xmlns="http://www.w3.org/2000/svg" width="609.6mm" height="457.2mm">{payload}</svg>'
    monkeypatch.setattr(sheet_pdf,'render_blank_sheet_svg',lambda s:RenderedSheetSvg(xml,'{}'))
    with pytest.raises(ValueError,match='unsupported'):
        sheet_pdf.render_blank_sheet_set_pdf((spec,),title='Synthetic')


def pdf_bytes(width,height,count=1):
    writer=PdfWriter()
    for _ in range(count):writer.add_blank_page(width=width,height=height)
    out=io.BytesIO();writer.write(out);return out.getvalue()


@pytest.mark.parametrize('problem',['dimensions','text','page_count'])
def test_bad_converter_output_is_rejected(problem,runtime,monkeypatch):
    cairo,cffi,pypdf=runtime
    spec=specs()[0]
    wrong=pdf_bytes(10,20) if problem=='dimensions' else pdf_bytes(spec.width_mm*72/25.4,spec.height_mm*72/25.4,2 if problem=='page_count' else 1)
    monkeypatch.setattr(cairo,'svg2pdf',lambda **k:wrong)
    with pytest.raises(ValueError,match={'dimensions':'dimensions','text':'text verification','page_count':'exactly one'}[problem]):
        sheet_pdf.render_blank_sheet_set_pdf((spec,),title='Synthetic')


def test_late_conversion_failure_keeps_all_sheets_pending(tmp_path,runtime,monkeypatch):
    path,_=request(tmp_path)
    cairo,_,_=runtime
    original=cairo.svg2pdf
    calls=[]
    def fail(**kwargs):
        calls.append(kwargs['bytestring'])
        if len(calls)==2:raise RuntimeError('synthetic page2 failure')
        return original(**kwargs)
    monkeypatch.setattr(cairo,'svg2pdf',fail)
    with pytest.raises(BlankSheetSetError,match='sheet E1.0.*page2 failure'):
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert (tmp_path/'attempt/.pending/sheets/003-E2.0.svg').exists()
    assert not (tmp_path/'attempt/delivery').exists()
    assert not (tmp_path/'attempt/.pending/sheet-set.pdf').exists()


def test_pdf_cannot_package_a_different_retained_svg(tmp_path,runtime,monkeypatch):
    from oabm.drawings import blank_set
    path,_=request(tmp_path)
    original=blank_set.render_blank_sheet_svg
    def altered(spec):
        result=original(spec)
        return RenderedSheetSvg(result.svg.replace('Synthetic Workshop','Different Workshop'),result.manifest_json)
    monkeypatch.setattr(blank_set,'render_blank_sheet_svg',altered)
    with pytest.raises(BlankSheetSetError,match='source differs'):
        create_blank_sheet_set(path,output_directory=tmp_path/'attempt')
    assert not (tmp_path/'attempt/delivery').exists()


def test_explicit_literal_url_text_is_not_a_fetchable_asset(runtime):
    from dataclasses import replace
    spec=replace(specs()[0],titleblock_fields=(('Project','https://example.invalid/literal'),))
    result=sheet_pdf.render_blank_sheet_set_pdf((spec,),title='Synthetic')
    assert 'https://example.invalid/literal' in PdfReader(io.BytesIO(result.pdf)).pages[0].extract_text()
