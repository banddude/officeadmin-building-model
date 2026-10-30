import copy
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from PIL import Image,ImageChops

from oabm.drawings.native_vectors import native_view_asset,render_native_vector,NativeViewAsset
from oabm.drawings.native_sheet_set import create_native_sheet_set,preflight_native_sheet_set
from oabm.drawings.company_sheets import rebuild_company_sheet_set

SVG=b'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:ifc="http://www.ifcopenshell.org/ns" width="100mm" height="50mm" viewBox="0 0 100 50" data-scale="1:48"><style>
text {font-family: DejaVu Sans; font-size:3px; fill:black;stroke:none}
.geometry path {fill:#222222;stroke:#000000;stroke-width:0.2;fill-rule:evenodd}
.geometry .dash {fill:none;stroke:red;stroke-dasharray:2,1}
</style><g class="geometry" ifc:guid="0CdQj1T1r4oAcDn5L0mPqU"><path d="M5,5 L30,5 L30,30 L5,30 Z M10,10 L10,25 L25,25 L25,10 Z"/><path class="dash" d="M40,5 C60,5 45,30 70,30 A5,5 0 0 1 80,35"/></g><text x="5" y="43">NATIVE <tspan font-weight="700">VIEW</tspan></text></svg>'''


def native(tmp_path,svg=SVG):
    root=tmp_path/'native';root.mkdir(exist_ok=True);(root/'view.svg').write_bytes(svg)
    receipt={'status':'review_only_success','views':{'plan':{'svg':'view.svg','native_svg_mm':[100.,50.],'scale':'1/48'}},'files':{'view.svg':hashlib.sha256(svg).hexdigest()},'input_sha256':{},'canonical_identity':{}}
    (root/'receipt.json').write_text(json.dumps(receipt));return root


def prepared(tmp_path,svg=SVG):
    source=native(tmp_path,svg)
    profile={'version':'company-sheet-profile-v1','fields':{'company':'SYNTHETIC COMPANY'},'template':{'width_mm':200,'height_mm':100,'margin_mm':5,'rail_width_mm':60,'rail_inset_mm':5}}
    job={'version':'company-sheet-job-v1','set_id':'native-review','date':'2026-01-01','revision':'R1','formats':['svg','pdf'],'fields':{'project':'SYNTHETIC','scale':'AS NOTED'},'sheets':[{'id':'A1','title':'NATIVE VIEW'}]}
    request={'version':'native-review-sheet-set-v1','profile':'profile.json','job':'job.json','native_delivery':'native','sheets':[{'sheet_id':'A1','views':[{'view_id':'plan','origin_mm':[10,20]}],'body':None}]}
    for name,data in [('profile.json',profile),('job.json',job),('request.json',request)]: (tmp_path/name).write_text(json.dumps(data))
    return tmp_path/'request.json'


def test_native_shapes_and_compound_holes_remain_exact_and_styles_isolated(tmp_path):
    root=native(tmp_path);asset=native_view_asset(root,'plan',origin_mm=(10,20));component,receipt=render_native_vector(asset,(5,5,150,95))
    original=ET.fromstring(SVG)
    assert [n.get('d') for n in component.iter() if n.tag.endswith('}path')]==[n.get('d') for n in original.iter() if n.tag.endswith('}path')]
    assert not list(component.iter('{http://www.w3.org/2000/svg}style'))
    assert receipt['bounds_mm']==[10,20,110.,70.]
    assert receipt['source_guids']==['0CdQj1T1r4oAcDn5L0mPqU']
    assert any('stroke-dasharray:2,1' in n.get('style','') for n in component.iter())


def test_shared_svg_pdf_matches_native_pixels_and_rebuilds(tmp_path):
    cairosvg=pytest.importorskip('cairosvg')
    request=prepared(tmp_path);assert preflight_native_sheet_set(request)['status']=='presentation_preflight_passed'
    delivery=create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    shared=delivery/'company/delivery';svg=next((shared/'sheets').glob('*.svg')).read_bytes()
    def rgb(raw):
        image=Image.open(io.BytesIO(raw)).convert('RGBA');bg=Image.new('RGBA',image.size,'white');bg.alpha_composite(image);return bg.convert('RGB')
    original=rgb(cairosvg.svg2png(bytestring=SVG,output_width=400,output_height=200))
    sheet=rgb(cairosvg.svg2png(bytestring=svg,output_width=800,output_height=400))
    actual=sheet.crop((40,80,440,280));assert ImageChops.difference(original,actual).getbbox() is None
    assert (shared/'sheet-set.pdf').read_bytes().startswith(b'%PDF')
    rebuilt=rebuild_company_sheet_set(shared,output_directory=tmp_path/'rebuild')
    assert next((rebuilt/'sheets').glob('*.svg')).read_bytes()==svg
    rerun=create_native_sheet_set(delivery/'inputs/portable.json',output_directory=tmp_path/'portable')
    assert next((rerun/'company/delivery/sheets').glob('*.svg')).read_bytes()==svg


@pytest.mark.parametrize('old,new',[
    (b'<g class="geometry"',b'<script'),
    (b'<g class="geometry"',b'<g onload="bad" class="geometry"'),
    (b'fill:#222222',b'fill:url(https://example.invalid/resource)'),
    (b'text {font-family',b'@import "https://example.invalid/style";text {font-family'),
    (b'M5,5',b'MNaN,5'),
    (b'<g class="geometry"',b'<image href="https://example.invalid/image" class="geometry"'),
])
def test_active_external_or_unsupported_native_svg_refuses(tmp_path,old,new):
    source=native(tmp_path,SVG.replace(old,new))
    with pytest.raises((ValueError,ET.ParseError)):native_view_asset(source,'plan',origin_mm=(0,0))


def test_native_receipts_bounds_and_scale_fail_before_attempt(tmp_path):
    request=prepared(tmp_path)
    changed=json.loads(request.read_text());changed['sheets'][0]['views'][0]['origin_mm']=[100,20];request.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='does not fit'):create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    assert not(tmp_path/'attempt').exists()
    changed['sheets'][0]['views'][0]['origin_mm']=[10,20];request.write_text(json.dumps(changed))
    job=json.loads((tmp_path/'job.json').read_text());job['fields']['scale']='1:50';(tmp_path/'job.json').write_text(json.dumps(job))
    with pytest.raises(ValueError,match='titleblock scale'):preflight_native_sheet_set(request)
    job['fields']['scale']='AS NOTED';(tmp_path/'job.json').write_text(json.dumps(job))
    (tmp_path/'native/view.svg').write_bytes(SVG+b' ')
    with pytest.raises(ValueError,match='receipt mismatch'):preflight_native_sheet_set(request)


@pytest.mark.parametrize('old,new',[
    (b'width="100mm"',b'transform="translate(100,0)" width="100mm"'),
    (b'font-size:3px',b'font-size:2em'),
    (b'<text x="5"',b'<text font-size="2em" x="5"'),
    (b'font-family: DejaVu Sans',b'font-family: inherit'),
    (b'<g class="geometry"',b'<g fill="red;opacity:0" class="geometry"'),
    (b'<g class="geometry"',b'<g fill="red!important" class="geometry"'),
])
def test_transformed_viewports_and_relative_font_cascades_refuse(tmp_path,old,new):
    source=native(tmp_path,SVG.replace(old,new))
    with pytest.raises(ValueError):native_view_asset(source,'plan',origin_mm=(10,20))


def test_changed_shared_inputs_never_publish_a_completed_native_set(tmp_path,monkeypatch):
    from oabm.drawings import native_sheet_set as module
    request=prepared(tmp_path);original=module.create_company_sheet_set
    def changed(profile,job,**kwargs):
        data=json.loads(Path(job).read_text());data['fields']['scale']='1:50';Path(job).write_text(json.dumps(data))
        return original(profile,job,**kwargs)
    monkeypatch.setattr(module,'create_company_sheet_set',changed)
    with pytest.raises(ValueError,match='company inputs changed'):module.create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    assert not(tmp_path/'attempt/delivery').exists()


@pytest.mark.parametrize('value',['red/*','red*/','red/**/',"'red",'rgb(1,2,3','red[','rgb(1,2,3))'])
def test_presentation_values_cannot_cross_css_value_boundaries(tmp_path,value):
    svg=SVG.replace(b'<g class="geometry"',f'<g fill="{value}" class="geometry"'.encode())
    source=native(tmp_path,svg)
    with pytest.raises(ValueError,match='CSS'):
        native_view_asset(source,'plan',origin_mm=(10,20))


def test_stylesheet_rule_and_declaration_comments_remain_supported(tmp_path):
    svg=SVG.replace(b'text {font-family',b'/* synthetic rule comment */ text {/* declaration comment */ font-family')
    svg=svg.replace(b'font-family: DejaVu Sans',b'font-family: "DejaVu Sans", sans-serif')
    svg=svg.replace(b'fill:#222222',b'fill:rgb(34,34,34)')
    source=native(tmp_path,svg)
    asset=native_view_asset(source,'plan',origin_mm=(10,20))
    component,_=render_native_vector(asset,(5,5,150,95))
    assert not list(component.iter('{http://www.w3.org/2000/svg}style'))
    assert any('NATIVE' in (node.text or '') for node in component.iter())


@pytest.mark.parametrize('tail,error',[('&#x202E;HIDDEN','control'),('&#x10FFFD;','glyph')])
@pytest.mark.parametrize('nested',[False,True])
def test_mixed_text_tails_use_the_parent_font_and_text_validation(tmp_path,tail,error,nested):
    content=f'<tspan>OK</tspan>{tail}'
    if nested:content=f'<tspan>{content}</tspan>'
    svg=SVG.replace(b'NATIVE <tspan font-weight="700">VIEW</tspan>',content.encode())
    with pytest.raises(ValueError,match=error):
        native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))


def test_nested_mixed_text_preserves_pixels_and_accounts_for_tail_only_fonts(tmp_path):
    cairosvg=pytest.importorskip('cairosvg')
    from oabm.drawings.company_sheets import _used_font_faces
    content=b'<tspan font-weight="700">OUT<tspan>INNER</tspan>SPAN</tspan>TAIL'
    svg=SVG.replace(b'NATIVE <tspan font-weight="700">VIEW</tspan>',content)
    request=prepared(tmp_path,svg)
    asset=native_view_asset(tmp_path/'native','plan',origin_mm=(10,20))
    component,receipt=render_native_vector(asset,(5,5,150,95))
    assert sum(face['owned_text_characters'] for face in receipt['font_faces'])==16
    assert {face['source_weight'] for face in receipt['font_faces']}=={400,700}
    assert {(face['svg_family'],face['weight']) for face in _used_font_faces([ET.tostring(component)])}=={
        (face['resolved_family'],face['resolved_weight']) for face in receipt['font_faces']}
    delivery=create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    shared=delivery/'company/delivery'
    actual_svg=next((shared/'sheets').glob('*.svg')).read_bytes()
    def rgb(raw):
        image=Image.open(io.BytesIO(raw)).convert('RGBA');bg=Image.new('RGBA',image.size,'white');bg.alpha_composite(image);return bg.convert('RGB')
    expected=rgb(cairosvg.svg2png(bytestring=svg,output_width=400,output_height=200))
    actual=rgb(cairosvg.svg2png(bytestring=actual_svg,output_width=800,output_height=400)).crop((40,80,440,280))
    assert ImageChops.difference(expected,actual).getbbox() is None
    manifest=json.loads((shared/'MANIFEST.json').read_text())
    assert {face['postscript_name'] for face in receipt['font_faces']}<={face['postscript_name'] for face in manifest['font_faces']}
    assert {face['postscript_name'] for face in receipt['font_faces']}<=set(manifest['pdf']['pages'][0]['embedded_fonts'])


def test_shared_font_inventory_includes_tail_only_parent_face():
    from oabm.drawings.company_sheets import _used_font_faces
    svg='<svg xmlns="http://www.w3.org/2000/svg"><text font-family="Liberation Sans" font-weight="400"><tspan font-family="DejaVu Sans" font-weight="700">SPAN</tspan>TAIL</text></svg>'
    assert {(face['svg_family'],face['weight']) for face in _used_font_faces([svg])}=={('Liberation Sans',400),('DejaVu Sans',700)}


@pytest.mark.parametrize('tail_in_parent',[False,True])
def test_tail_coverage_uses_parent_face_instead_of_child_face(tmp_path,monkeypatch,tail_in_parent):
    from types import SimpleNamespace
    from oabm.drawings import native_vectors,company_sheets
    monkeypatch.setattr(native_vectors,'_native_face',lambda family,weight:(family,weight,family))
    def face(family,weight):
        characters='OK'+('X' if (family=='Liberation Sans')==tail_in_parent else '')
        return SimpleNamespace(_oabm_codepoints=set(map(ord,characters))),family,None,family
    monkeypatch.setattr(company_sheets,'_font',face)
    content=b'<tspan style="font-family:DejaVu Sans">OK</tspan>X'
    svg=SVG.replace(b'<text x="5"',b'<text style="font-family:Liberation Sans" x="5"')
    svg=svg.replace(b'NATIVE <tspan font-weight="700">VIEW</tspan>',content)
    if tail_in_parent:
        native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))
    else:
        with pytest.raises(ValueError,match='glyph'):
            native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))


def test_implicit_native_text_font_refuses_instead_of_substituting(tmp_path):
    svg=SVG.replace(b'font-family: DejaVu Sans;',b'')
    with pytest.raises(ValueError,match='explicit inherited font-family'):
        native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))


@pytest.mark.parametrize('child_only',[False,True])
def test_explicit_inherited_or_child_only_native_fonts_work(tmp_path,child_only):
    svg=SVG.replace(b'font-family: DejaVu Sans;',b'')
    if child_only:
        svg=svg.replace(b'NATIVE <tspan font-weight="700">VIEW</tspan>',b'<tspan font-family="DejaVu Sans">CHILD</tspan>')
    else:
        svg=svg.replace(b'<svg xmlns=',b'<svg font-family="DejaVu Sans" xmlns=')
    asset=native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))
    component,receipt=render_native_vector(asset,(5,5,150,95))
    assert receipt['font_faces']
    assert receipt['source_guids']==['0CdQj1T1r4oAcDn5L0mPqU']


@pytest.mark.parametrize('key,value',[
    ('opacity','0.5'),('display','inline'),('filter','url(#f)'),('clip-path','none'),
    ('flood-color','red'),('flood-opacity','0.2'),('text-decoration','underline'),
    ('dominant-baseline','central'),('alignment-baseline','middle'),
])
def test_noninherited_presentation_values_stay_on_the_original_owner(tmp_path,key,value):
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50" data-scale="1:48" {key}="{value}"><defs><filter id="f"><feFlood/></filter></defs><g id="child"><rect width="20" height="20"/></g></svg>'.encode()
    asset=native_view_asset(native(tmp_path,svg),'plan',origin_mm=(10,20))
    component,_=render_native_vector(asset,(5,5,150,95))
    assert key+':' in component.get('style','')
    assert all(key+':' not in node.get('style','') for node in component.iter() if node is not component)


@pytest.mark.parametrize('local_values',[False,True])
def test_flood_defaults_and_local_values_match_original_standard_renderer_pixels(tmp_path,local_values):
    renderer=shutil.which('rsvg-convert')
    if renderer is None:pytest.skip('installed librsvg converter required for filter-reference pixels; no install')
    local=' flood-color="blue" flood-opacity="0.5"' if local_values else ''
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50" data-scale="1:48" flood-color="red" flood-opacity="0.2"><defs><filter id="f"><feFlood{local}/></filter></defs><rect x="5" y="5" width="20" height="20" filter="url(#f)"/></svg>'.encode()
    request=prepared(tmp_path,svg)
    delivery=create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    shared=next((delivery/'company/delivery/sheets').glob('*.svg'))
    def raster(path,width,height):
        raw=subprocess.run([renderer,'--width',str(width),'--height',str(height),str(path)],check=True,capture_output=True,timeout=30).stdout
        image=Image.open(io.BytesIO(raw)).convert('RGBA');bg=Image.new('RGBA',image.size,'white');bg.alpha_composite(image);return bg.convert('RGB')
    original=raster(tmp_path/'native/view.svg',400,200)
    actual=raster(shared,800,400).crop((40,80,440,280))
    if not local_values:assert original.getpixel((50,50))==(0,0,0)
    assert ImageChops.difference(original,actual).getbbox() is None


def test_inherited_fill_stroke_and_font_preserve_original_pixels(tmp_path):
    cairosvg=pytest.importorskip('cairosvg')
    svg=b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50" data-scale="1:48" fill="blue" stroke="red" stroke-width="0.3" font-family="DejaVu Sans" font-size="3px"><g><rect x="5" y="5" width="20" height="20"/><text x="5" y="40">INHERITED <tspan>TEXT</tspan></text></g></svg>'
    request=prepared(tmp_path,svg)
    asset=native_view_asset(tmp_path/'native','plan',origin_mm=(10,20))
    component,_=render_native_vector(asset,(5,5,150,95))
    rect=next(component.iter('{http://www.w3.org/2000/svg}rect'))
    assert 'fill:blue' in rect.get('style') and 'stroke:red' in rect.get('style')
    delivery=create_native_sheet_set(request,output_directory=tmp_path/'attempt')
    shared=next((delivery/'company/delivery/sheets').glob('*.svg')).read_bytes()
    def raster(raw,width,height):
        image=Image.open(io.BytesIO(cairosvg.svg2png(bytestring=raw,output_width=width,output_height=height))).convert('RGBA')
        bg=Image.new('RGBA',image.size,'white');bg.alpha_composite(image);return bg.convert('RGB')
    difference=ImageChops.difference(raster(svg,400,200),raster(shared,800,400).crop((40,80,440,280)))
    # Stroked glyphs can incur one 8-bit channel level of nested-viewport
    # antialias rounding. Geometry, inherited colors and source face stay fixed.
    assert max(high for low,high in difference.getextrema())<=1
