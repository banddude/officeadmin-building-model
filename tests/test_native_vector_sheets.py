import copy
import hashlib
import io
import json
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
