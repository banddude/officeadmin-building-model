"""Only detached bounded PNG assets can enter the shared sheet image channel."""
import io,json,hashlib
from dataclasses import replace
import pytest
from PIL import Image
from oabm.drawings import company_sheets as cs
from test_company_sheets import inputs


def asset():
    stream=io.BytesIO();Image.new('RGB',(16,12),'navy').save(stream,format='PNG')
    return cs.SheetImageAsset('vicinity',stream.getvalue(),(30,30,70,60),'Synthetic map attribution',{'provider':'synthetic','source':'local test fixture'})


def test_image_is_hash_retained_and_byte_identically_rebuilt(tmp_path):
    p,j=inputs(tmp_path);image=asset()
    root=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':(image,)})
    rows=json.loads((root/'resolved-sheets.json').read_bytes());record=rows[0]['images'][0]
    assert record['sha256']==hashlib.sha256(image.png_bytes).hexdigest()
    assert (root/record['path']).read_bytes()==image.png_bytes
    svg=(root/'sheets/001-E0.0.svg').read_text()
    assert 'data:image/png;base64,' in svg
    other=cs.rebuild_company_sheet_set(root,output_directory=tmp_path/'rebuild')
    # Rebuild intentionally replaces original profile/job with retained portable inputs.
    for name in ('resolved-sheets.json','sheets/001-E0.0.svg','sheets/002-E1.0.svg','sheets/003-E2.0.svg',record['path']):
        assert (root/name).read_bytes()==(other/name).read_bytes()


@pytest.mark.parametrize('changes',[{'png_bytes':b'<svg/>'},{'bounds_mm':(0,0,9999,9999)},{'attribution':''},{'provenance':{}},{'provenance':{'bad':'\uffff'}}])
def test_bad_asset_refuses_before_attempt(tmp_path,changes):
    p,j=inputs(tmp_path)
    with pytest.raises(ValueError):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':(replace(asset(),**changes),)})
    assert not (tmp_path/'run').exists()


def test_unknown_sheet_and_arbitrary_body_images_still_refused(tmp_path):
    p,j=inputs(tmp_path)
    with pytest.raises(ValueError,match='unknown sheet'):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'bad',images={'UNKNOWN':(asset(),)})
    with pytest.raises(ValueError):cs.create_company_sheet_set(p,j,output_directory=tmp_path/'bad-body',bodies={'E0.0':'<g><image href="https://example.invalid/map.png"/></g>'})


def test_overlap_and_duplicate_identity_refuse_before_attempt(tmp_path):
    p,j=inputs(tmp_path);image=asset()
    for values in ((image,image),(image,replace(image,id='second'))):
        with pytest.raises(ValueError,match='duplicate|overlap'):
            cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':values})
    assert not (tmp_path/'run').exists()


def test_validated_image_detaches_provenance_and_rejects_tampered_retained_bytes(tmp_path):
    p,j=inputs(tmp_path);image=asset();sheet=cs.resolve_company_sheet_set(p,j).sheets[0]
    detached=cs._sheet_images((image,),sheet.drawing_bounds_mm)[0]
    image.provenance['provider']='changed'
    assert detached.provenance['provider']=='synthetic'
    root=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':(detached,)})
    row=json.loads((root/'resolved-sheets.json').read_bytes())[0]['images'][0]
    (root/row['path']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='receipt mismatch'):
        cs.rebuild_company_sheet_set(root,output_directory=tmp_path/'rebuild')
    assert not (tmp_path/'rebuild').exists()


def test_crc_valid_but_undecodable_png_refuses_before_publication(tmp_path):
    import struct,zlib
    image=asset();raw=image.png_bytes;bad=raw[:8];offset=8
    while offset<len(raw):
        length=struct.unpack('>I',raw[offset:offset+4])[0]
        kind=raw[offset+4:offset+8];data=raw[offset+8:offset+8+length]
        if kind==b'IDAT':data=b'invalid compressed pixel stream'
        bad+=struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        offset+=12+length
    # Header/CRC validation succeeds, but actual pixels cannot be decoded.
    with Image.open(io.BytesIO(bad)) as opened:opened.verify()
    p,j=inputs(tmp_path)
    with pytest.raises(ValueError,match='invalid sheet image PNG'):
        cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':(replace(image,png_bytes=bad),)})
    assert not (tmp_path/'run').exists()


def rewrite_image_record(root,change):
    path=root/'resolved-sheets.json';rows=json.loads(path.read_bytes());change(rows[0]['images'][0]);path.write_text(json.dumps(rows))
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    receipt=next(row for row in manifest['files'] if row['path']=='resolved-sheets.json')
    receipt.update(bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (root/'MANIFEST.json').write_text(json.dumps(manifest))


@pytest.mark.parametrize('mode',['absolute','traversal','unreceipted','stale-sha','symlink'])
def test_rebuild_image_paths_cannot_read_external_or_unbound_files(tmp_path,mode):
    p,j=inputs(tmp_path);image=asset()
    root=cs.create_company_sheet_set(p,j,output_directory=tmp_path/'run',images={'E0.0':(image,)})
    external=tmp_path/'external.png';external.write_bytes(image.png_bytes)
    if mode=='absolute':rewrite_image_record(root,lambda row:row.update(path=str(external)))
    elif mode=='traversal':rewrite_image_record(root,lambda row:row.update(path='../../external.png'))
    elif mode=='unreceipted':
        digest='0'*64;name=f'inputs/assets/{digest}.png';(root/name).write_bytes(image.png_bytes)
        rewrite_image_record(root,lambda row:row.update(path=name,sha256=digest))
    elif mode=='stale-sha':rewrite_image_record(root,lambda row:row.update(sha256='0'*64))
    else:
        row=json.loads((root/'resolved-sheets.json').read_bytes())[0]['images'][0]
        target=root/row['path'];target.unlink();target.symlink_to(external)
    with pytest.raises(ValueError):cs.rebuild_company_sheet_set(root,output_directory=tmp_path/'rebuild')
    assert not (tmp_path/'rebuild').exists()
