"""Known-answer source geometry, not rendered visibility or clipping claims."""
from pathlib import Path
import math
import pytest
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          FloatObject, NameObject)
from oabm.importers.pdf_electrical.importer import extract_pdf

IDENTITY=(1,0,0,1,0,0)

def apply(matrix, point):
    a,b,c,d,e,f=matrix; x,y=point
    return a*x+c*y+e,b*x+d*y+f

def display(point, rotation, origin):
    x,y=point[0]-origin[0],point[1]-origin[1]
    return {0:(x,y),90:(y,300-x),180:(300-x,300-y),270:(300-y,x)}[rotation]

def pdf(path, *, caller=(1,0,0,1,100,120), matrix=(1,0,0,1,5,7),
        nested=False, rotation=0, origin=(0,0), repeat=False, local=IDENTITY):
    writer=PdfWriter(); page=writer.add_blank_page(300,300)
    page.mediabox=ArrayObject([FloatObject(x) for x in (*origin,origin[0]+300,origin[1]+300)])
    page.rotate(rotation)
    font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),
      NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
    resources=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
    def form(data, matrix, resources):
        obj=DecodedStreamObject();obj.set_data(data)
        obj.update({NameObject("/Type"):NameObject("/XObject"),NameObject("/Subtype"):NameObject("/Form"),
          NameObject("/BBox"):ArrayObject([FloatObject(x) for x in (0,0,200,200)]),
          NameObject("/Matrix"):ArrayObject([FloatObject(x) for x in matrix]),
          NameObject("/Resources"):resources})
        return writer._add_object(obj)
    inner=form(("q "+" ".join(map(str,local))+" cm 10 10 m 20 20 l S BT /F1 10 Tf 1 0 0 1 12 14 Tm (FORM) Tj ET Q").encode(),matrix,resources)
    if nested:
        resources2=DictionaryObject({NameObject("/XObject"):DictionaryObject({NameObject("/F"):inner})})
        inner=form(b"q 1 0 0 1 3 4 cm /F Do Q",(2,0,0,2,11,13),resources2)
    page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):resources["/Font"],
       NameObject("/XObject"):DictionaryObject({NameObject("/F"):inner})})
    # Parent text and an unfinished caller path bracket the Form.
    # An unfinished caller path must also survive the Form's independent path.
    command=("1 1 m 2 2 l q "+" ".join(map(str,caller))+
       " cm BT /F1 10 Tf 1 0 0 1 6 8 Tm (CALLER) Tj ET /F Do Q S ")
    if repeat: command+="q 1 0 0 1 40 50 cm /F Do Q "
    command+="BT /F1 10 Tf 1 0 0 1 70 80 Tm (AFTER) Tj ET 60 60 m 65 65 l S"
    stream=DecodedStreamObject();stream.set_data(command.encode());page[NameObject("/Contents")]=writer._add_object(stream)
    writer.write(path)

@pytest.mark.parametrize("rotation",[0,90,180,270])
@pytest.mark.parametrize("origin",[(0,0),(30,50),(-30,-50)])
@pytest.mark.parametrize("caller,matrix",[
 ((1,0,0,1,100,120),(1,0,0,1,5,7)),
 ((2,0,0,3,40,50),(0,1,-1,0,25,30)),
 ((1,.25,.5,1,40,50),(1,.5,0,1,5,7)),
])
@pytest.mark.parametrize("nested",[False,True])
def test_form_vectors_text_symbol_width_and_restoration(tmp_path,rotation,origin,caller,matrix,nested):
    path=tmp_path/"form.pdf";pdf(path,caller=caller,matrix=matrix,nested=nested,rotation=rotation,origin=origin)
    doc=extract_pdf(path)
    def world(p):
        p=apply(matrix,p)
        if nested: p=apply((2,0,0,2,11,13),apply((1,0,0,1,3,4),p))
        return apply(caller,p)
    expected=[display(world(p),rotation,origin) for p in [(10,10),(20,20)]]
    assert doc.vectors[0].points_pt == pytest.approx(expected)
    form_text=next(t for t in doc.texts if t.text=="FORM")
    assert (form_text.x_pt,form_text.y_pt)==pytest.approx(display(world((12,14)),rotation,origin))
    axis=(world((0,1))[0]-world((0,0))[0],world((0,1))[1]-world((0,0))[1])
    assert form_text.font_size_pt==pytest.approx(10*math.hypot(*axis))
    caller_text=next(t for t in doc.texts if t.text=="CALLER")
    assert (caller_text.x_pt,caller_text.y_pt)==pytest.approx(display(apply(caller,(6,8)),rotation,origin))
    after=next(t for t in doc.texts if t.text=="AFTER")
    assert (after.x_pt,after.y_pt)==pytest.approx(display((70,80),rotation,origin))
    assert doc.vectors[1].points_pt==pytest.approx([display(p,rotation,origin) for p in [(1,1),(2,2)]])
    assert doc.vectors[2].points_pt==pytest.approx([display(p,rotation,origin) for p in [(60,60),(65,65)]])
    # Same Form name is deliberately used in parent and child resources.
    innermost=doc.symbols[-1]
    assert (innermost.x_pt,innermost.y_pt)==pytest.approx(display(world((0,0)),rotation,origin))

def test_repeated_form_invocations_keep_distinct_stable_ids(tmp_path):
    path=tmp_path/"reuse.pdf";pdf(path,repeat=True)
    first=extract_pdf(path);second=extract_pdf(path)
    assert first==second
    texts=[t for t in first.texts if t.text=="FORM"]
    assert [(t.x_pt,t.y_pt) for t in texts]==[(117,141),(57,71)]
    assert len({t.element_id for t in texts})==2
    assert first.vectors[0].points_pt==((115,137),(125,147))
    assert first.vectors[2].points_pt==((55,67),(65,77))


@pytest.mark.parametrize("local",[(2,0,0,3,4,5),(0,1,-1,0,30,40),(1,.5,.25,1,4,5)])
def test_local_stream_transform_composes_inside_form(tmp_path,local):
    path=tmp_path/"local.pdf"; pdf(path,local=local)
    doc=extract_pdf(path)
    def expected(p): return apply((1,0,0,1,100,120),apply((1,0,0,1,5,7),apply(local,p)))
    assert doc.vectors[0].points_pt==pytest.approx([expected((10,10)),expected((20,20))])
    text=next(t for t in doc.texts if t.text=="FORM")
    assert (text.x_pt,text.y_pt)==pytest.approx(expected((12,14)))
    # A local cm changes painted geometry, not the invoked Form origin.
    assert (doc.symbols[0].x_pt,doc.symbols[0].y_pt)==(105,127)


def test_uniform_form_scale_corrects_width_and_restores_caller(tmp_path):
    path=tmp_path/"width.pdf"; pdf(path,caller=(2,0,0,2,100,120),matrix=(3,0,0,3,5,7))
    doc=extract_pdf(path)
    assert doc.vectors[0].metadata["line_width_pt"]==6
    assert doc.vectors[1].metadata["line_width_pt"]==1
    assert doc.vectors[2].metadata["line_width_pt"]==1


@pytest.mark.parametrize("installed",["5.9.0","6.19.0","6.20.9","6.21rc1","6.22rc1","6.22.dev1","7.0","7.0rc1","invalid"])
def test_unsupported_parser_versions_refuse_before_parsing(tmp_path,monkeypatch,installed):
    import importlib.metadata
    import pypdf
    from oabm.importers.pdf_electrical.importer import ElectricalPdfError
    path=tmp_path/"version.pdf";pdf(path)
    monkeypatch.setattr(importlib.metadata,"version",lambda name:installed)
    def unexpected(*args,**kwargs): pytest.fail("unsupported parser must not be invoked")
    monkeypatch.setattr(pypdf,"PdfReader",unexpected)
    with pytest.raises(ElectricalPdfError,match="pypdf.*6.21"):
        extract_pdf(path)

@pytest.mark.parametrize("installed",["6.21","6.21.0","6.21.3","6.21.3.post1","6.21.3+local"])
def test_supported_version_boundary_parsing_only(monkeypatch,installed):
    import importlib.metadata
    from oabm.importers.pdf_electrical.importer import _require_supported_pypdf
    monkeypatch.setattr(importlib.metadata,"version",lambda name:installed)
    _require_supported_pypdf()

def test_unverifiable_parser_metadata_refuses(monkeypatch):
    import importlib.metadata
    from oabm.importers.pdf_electrical.importer import _require_supported_pypdf,ElectricalPdfError
    def missing(name): raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata,"version",missing)
    with pytest.raises(ElectricalPdfError,match="cannot verify"):
        _require_supported_pypdf()

@pytest.mark.parametrize("nested",[False,True])
@pytest.mark.parametrize("bad",["short","null","boolean","string"])
def test_malformed_form_matrix_refuses_whole_extraction(tmp_path,nested,bad):
    from pypdf import PdfReader
    from pypdf.generic import NullObject,BooleanObject,TextStringObject
    from oabm.importers.pdf_electrical.importer import ElectricalPdfError
    path=tmp_path/"bad.pdf";pdf(path,nested=nested,repeat=True)
    reader=PdfReader(path);obj=reader.pages[0]["/Resources"]["/XObject"]["/F"]
    if nested: obj=obj["/Resources"]["/XObject"]["/F"]
    values={"short":ArrayObject([FloatObject(1)]),"null":NullObject(),
            "boolean":ArrayObject([BooleanObject(True),*[FloatObject(v) for v in (0,0,1,0,0)]]),
            "string":ArrayObject([TextStringObject("1"),*[FloatObject(v) for v in (0,0,1,0,0)]])}
    obj[NameObject("/Matrix")]=values[bad]
    writer=PdfWriter();writer.append(reader);writer.write(path)
    with pytest.raises(ElectricalPdfError,match="Form /Matrix"):
        extract_pdf(path)
