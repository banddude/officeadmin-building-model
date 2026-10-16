"""Independent miniature PDF appearances; no customer text or geometry."""
import hashlib
import io
from dataclasses import replace

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject, RectangleObject)

from oabm.model import ContractError
from oabm.proposals.pdf_annotations import source_annotation_inventory
from oabm.proposals.takeoff import (render_source_image, _render_page,
    _crop_affine, _validate_rendered_images, SourceImage, RenderManifest,
    LEGACY_RENDER_POLICY, PRINT_RENDER_POLICY)


def annotated_pdf(*, flags=4, rotation=0, offset=False, appearance=True,
                  subtype='/Stamp', optional_content=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=120, height=80)
    if offset:
        page.mediabox = RectangleObject([-10,-20,110,60])
    page.rotate(rotation)
    # A solid red appearance has an independent expected interior RGB sample.
    stream = DecodedStreamObject()
    stream.set_data(b'1 0 0 rg 0 0 20 20 re f')
    stream.update({NameObject('/Type'):NameObject('/XObject'),
                   NameObject('/Subtype'):NameObject('/Form'),
                   NameObject('/BBox'):RectangleObject([0,0,20,20]),
                   NameObject('/Resources'):DictionaryObject()})
    annot = DictionaryObject({NameObject('/Type'):NameObject('/Annot'),
        NameObject('/Subtype'):NameObject(subtype),NameObject('/F'):NumberObject(flags),
        NameObject('/Rect'):RectangleObject([40,20,60,40])})
    if appearance:
        annot[NameObject('/AP')] = DictionaryObject({NameObject('/N'):writer._add_object(stream)})
    if optional_content:
        annot[NameObject('/OC')] = DictionaryObject()
    page[NameObject('/Annots')] = ArrayObject([writer._add_object(annot)])
    buf = io.BytesIO();writer.write(buf);return buf.getvalue()


def capture(document, rotation=0):
    width,height = (160,240) if rotation in (90,270) else (240,160)
    return render_source_image(document,source_id='synthetic:annotation-colors',page=1,
        scale=2,crop_px=(0,0,width,height),image_id='target',role='target')


def red_pixels(payload):
    with Image.open(io.BytesIO(payload)) as image:
        pixels=image.convert('RGB').tobytes()
        return sum(pixels[i]>240 and pixels[i+1]<10 and pixels[i+2]<10
                   for i in range(0,len(pixels),3))


@pytest.mark.parametrize('rotation',[0,90,180,270])
@pytest.mark.parametrize('offset',[False,True])
def test_printable_appearance_survives_rotation_origin_and_source_binding(rotation,offset):
    document=annotated_pdf(rotation=rotation,offset=offset)
    ref,payload=capture(document,rotation)
    assert ref.render_manifest.policy==PRINT_RENDER_POLICY
    assert red_pixels(payload)>=1500
    # Independently computed image-space centres for the authored source square.
    centres = {False: {0:(50,50),90:(30,50),180:(70,30),270:(50,70)},
               True: {0:(60,30),90:(50,60),180:(60,50),270:(30,60)}}
    x,y=centres[offset][rotation]
    with Image.open(io.BytesIO(payload)) as pixels:
        assert pixels.getpixel((2*x,2*y)) == (255,0,0)
    ref.validate_bytes(payload,document=document)
    inventory=source_annotation_inventory(document,page=1)
    assert inventory['document_sha256']==hashlib.sha256(document).hexdigest()
    row=inventory['annotations'][0]
    assert row['print_flag'] and row['subtype']=='/Stamp'
    assert row['raw_rect_pt']==[40,20,60,40]
    assert row['normal_appearance_sha256']==hashlib.sha256(b'1 0 0 rg 0 0 20 20 re f').hexdigest()


@pytest.mark.parametrize('flags,visible',[(0,False),(4,True),(6,False),(36,True)])
def test_print_flags_hidden_and_no_view_follow_print_policy(flags,visible):
    _,payload=capture(annotated_pdf(flags=flags))
    assert (red_pixels(payload)>0)==visible


@pytest.mark.parametrize('kwargs',[
    {'appearance':False},{'subtype':'/Widget'},{'optional_content':True},
    {'subtype':'/UnknownSynthetic'},
])
def test_unsupported_printable_source_content_refuses_instead_of_vanishing(kwargs):
    with pytest.raises(ContractError,match='printable'):
        capture(annotated_pdf(**kwargs))


def legacy_capture(document):
    source,image,wrapper,engine=_render_page(document,source_id='synthetic:annotation-colors',
        page=1,scale=2,policy=LEGACY_RENDER_POLICY)
    try:
        buf=io.BytesIO();image.save(buf,format='PNG');payload=buf.getvalue()
    finally:
        image.close()
    crop=(0,0,source.width_px,source.height_px)
    ref=SourceImage(id='legacy',source=source,role='target',crop_px=crop,
        sha256=hashlib.sha256(payload).hexdigest(),render_manifest=RenderManifest(
        policy=LEGACY_RENDER_POLICY,renderer_version=wrapper,pdfium_version=engine,
        scale=2,pixel_to_display_pt=_crop_affine(source,crop)))
    return ref,payload


def test_historical_policy_is_verified_without_relabeling_or_cache_reuse():
    document=annotated_pdf();old,old_pixels=legacy_capture(document);new,new_pixels=capture(document)
    assert red_pixels(old_pixels)==0 and red_pixels(new_pixels)>=1500
    before=old.to_json()
    _validate_rendered_images((old,new),{old.id:old_pixels,new.id:new_pixels},
        {new.source.document_sha256:document})
    assert old.to_json()==before
    forged=replace(old,render_manifest=replace(old.render_manifest,policy=PRINT_RENDER_POLICY))
    with pytest.raises(ContractError,match='pixels differ'):
        forged.validate_bytes(old_pixels,document=document)


def test_annotation_crop_is_not_a_page_frame_or_resized_raster():
    document=annotated_pdf()
    ref,payload=render_source_image(document,source_id='synthetic:annotation-colors',page=1,
        scale=2,crop_px=(70,70,130,130),image_id='crop',role='target')
    assert red_pixels(payload)>=1500
    assert ref.render_manifest.pixel_to_display_pt==(.5,0.,0.,-.5,35.,45.)
    ref.validate_bytes(payload,document=document)
