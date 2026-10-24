"""Public synthetic physical-sheet saved-delivery integration."""
from dataclasses import replace
import io
import json
from pathlib import Path
import zipfile

import pytest

from oabm.model import ContractError
from oabm.drawings.model import Point2
from oabm.drawings.sheets import SheetSpec, ViewPlacement
from oabm.pipeline.review_drawings import BANNER, generate_review_drawings, read_review_drawing_config
from oabm.pipeline.review_sheet_delivery import (VERSION, ReviewSheet, ReviewGlyph,
    ReviewSheetDeliveryConfig, _lower_glyphs)
from oabm.pipeline import saved_electrical as saved
from oabm.pipeline.saved_revisions import _load
from test_review_drawings_integration import (source, combined, drawing_config, read, write, sha, rehash)
from test_saved_job import setup, run, approve, revise, tree_bytes


def config(*, formats=("svg", "dxf"), revision="A"):
    drawings = drawing_config(scale=100.)
    # Explicit synthetic marks, not a standard electrical symbol library.
    glyphs = tuple(ReviewGlyph(token=token, segments_mm=((Point2(-1., 0.), Point2(1., 0.)),
        (Point2(0., -1.), Point2(0., 1.)))) for token in (
        "equipment:panelboard", "device:luminaire", "device:receptacle", "device:evse",
        "device:dimmer", "device:junction_box", "opening:door", "fitting:elbow-90"))
    return ReviewSheetDeliveryConfig(version=VERSION, drawings=drawings,
        sheets=(ReviewSheet(paper=SheetSpec(id="SYNTHETIC-01", width_mm=420, height_mm=297,
            title=BANNER, revision=revision, titleblock_height_mm=30),
            placements=(ViewPlacement(id="plan", view_id=drawings.plans[0].id, x_mm=25, y_mm=25),),
            formats=formats),), glyphs=glyphs)


def persist(source, tmp_path, cfg=None, name="a"):
    return saved.persist_saved_electrical(combined(source, tmp_path/(name+".json"), cfg or config()),
        output_root=tmp_path/"out", run_id=name)


def test_config_roundtrip_and_exact_versions():
    cfg=config()
    assert read_review_drawing_config(cfg.to_dict()) == cfg
    for mutate in (lambda d:d.update(version="guessed"), lambda d:d.update(extra=True),
                   lambda d:d["sheets"][0]["paper"].update(title="Ready for construction"),
                   lambda d:d["sheets"][0]["paper"].update(titleblock_height_mm=0),
                   lambda d:d["sheets"][0].update(formats=[]),
                   lambda d:d["sheets"][0]["placements"][0].update(view_id="missing")):
        doc=cfg.to_dict();mutate(doc)
        with pytest.raises((ContractError, ValueError)):read_review_drawing_config(doc)


def test_saved_sheet_archive_replays_and_keeps_full_sources(source,tmp_path):
    final=persist(source,tmp_path)
    model,report,_,metadata=_load(final)
    package=final/"generated-sheet-sources.zip"
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert all(i.date_time == (1980,1,1,0,0,0) for i in archive.infolist())
        names=archive.namelist()
        assert names == sorted(names)
        assert not any(n.endswith((".pdf", ".bin", ".png")) or "/" in n for n in names)
        receipt=json.loads(archive.read("sheet-delivery.json"))
        assert receipt["source_originals_included"] is False and receipt["permit_ready"] is False
        assert receipt["canonical_model_sha256"] == report["canonical_output_sha256"]
        assert receipt["review"]["source_proposals_not_admitted"]
        for row in receipt["files"]:
            data=archive.read(row["path"])
            assert row["sha256"]==sha(data) and row["bytes"]==len(data)
        drawing=json.loads(archive.read("drawing-set.json"))
        owners={e["id"]:e for rows in model.to_dict().values() if isinstance(rows,list)
            for e in rows if isinstance(e,dict) and "id" in e}
        assert all(e["provenance"]==owners[e["canonical_id"]].get("provenance",[]) for e in drawing["drawing_set"]["source_index"])
        assert receipt["symbol_lowering"]
        assert BANNER in archive.read("physical-sheet-001.svg").decode()
        import ezdxf
        cad=ezdxf.read(io.StringIO(archive.read("physical-sheet-001.dxf").decode()))
        assert not list(cad.modelspace()) and not cad.audit().has_errors
        assert any(BANNER in e.dxf.text for layout in cad.layouts if layout.name!="Model" for e in layout.query("TEXT"))
    second=persist(source,tmp_path,name="b")
    assert package.read_bytes()==(second/package.name).read_bytes()


@pytest.mark.parametrize("target",["physical-sheet-001.svg", "physical-sheet-001.dxf", "physical-sheet-001.svg.json", "generated-sheet-sources.zip", "sheet-delivery.json"])
def test_rehashed_tampered_exports_refused(source,tmp_path,target):
    final=persist(source,tmp_path)
    p=final/target;p.write_bytes(p.read_bytes()+b"stale")
    report=read(final/"completion-report.json")
    for row in report["artifacts"].values():
        if row["reference"]==str(p):row.update(sha256=sha(p.read_bytes()),bytes=len(p.read_bytes()))
    write(final/"completion-report.json",report);rehash(final)
    with pytest.raises(ContractError,match="drawing/model replay mismatch"):_load(final)


def test_missing_glyph_and_partial_write_do_not_publish(source,tmp_path,monkeypatch):
    cfg=replace(config(),glyphs=())
    with pytest.raises(saved.SavedDeliveryError):persist(source,tmp_path,cfg,name="missing")
    assert not (tmp_path/"out/missing/delivery").exists()
    original=saved._write
    def fail(path,data):
        if path.suffix==".zip":raise OSError("synthetic archive write error")
        return original(path,data)
    with monkeypatch.context() as m:
        m.setattr(saved,"_write",fail)
        with pytest.raises(saved.SavedDeliveryError):persist(source,tmp_path,name="partial")
    assert not (tmp_path/"out/partial/delivery").exists()
    frozen=tree_bytes(tmp_path/"out/partial")
    _load(persist(source,tmp_path,name="retry"))
    assert tree_bytes(tmp_path/"out/partial")==frozen


def test_saved_job_regenerates_sheet_only_configuration_without_dispatch(tmp_path):
    path,source,calls,adapter=setup(tmp_path)
    upstream=Path(read(path)["upstream"]["path"]);doc=read(upstream)
    doc["version"]=saved.DRAWING_VERSION;doc.pop("circuit_design")
    p=upstream.parent/"sheet-config.json";write(p,config().to_dict())
    doc["review_drawings"]={"path":str(p),"sha256":sha(p.read_bytes())}
    write(upstream,doc);job=read(path);job["upstream"]["sha256"]=sha(upstream.read_bytes());write(path,job)
    approve(path,source,run(path,adapter))
    first=run(path,adapter)
    assert first["status"]=="delivery_persisted", first
    again=run(path,adapter)
    assert again["export_reused"] and len(calls)==1
    final=Path(first["delivery"]);frozen=tree_bytes(final)
    write(p,config(revision="B").to_dict());doc["review_drawings"]["sha256"]=sha(p.read_bytes());write(upstream,doc)
    revise(path,lambda d:d["upstream"].update(sha256=sha(upstream.read_bytes())))
    changed=run(path,adapter)
    assert changed["status"]=="delivery_persisted" and changed["proposal_reused"] and len(calls)==1
    assert changed["export_attempt_id"]!=first["export_attempt_id"]
    target=Path(changed["delivery"])
    assert (target/"model.json").read_bytes()==(final/"model.json").read_bytes()
    assert (target/"generated-sheet-sources.zip").read_bytes()!=(final/"generated-sheet-sources.zip").read_bytes()
    assert tree_bytes(final)==frozen


@pytest.mark.parametrize("scale",[25.,100.])
@pytest.mark.parametrize("fmt",["svg","dxf"])
def test_explicit_glyph_paper_axes_scale_and_source_joins(scale,fmt):
    from test_physical_sheets import drawing
    from oabm.drawings.model import DrawingPrimitive
    from oabm.drawings.sheet_svg import render_sheet_svg
    from oabm.drawings.sheet_dxf import sheet_to_dxf
    source=drawing();view=source.views[0]
    primitive=DrawingPrimitive("glyph", "symbol", "symbols:electrical", ("wall",),
        (Point2(2,2),), symbol="synthetic:asymmetric")
    source=replace(source,views=(replace(view,id="review:floor",scale=scale,primitives=(primitive,),dimensions=()),))
    glyph=ReviewGlyph(token=primitive.symbol,segments_mm=((Point2(0,0),Point2(2,1)),))
    cfg=replace(config(formats=(fmt,)),glyphs=(glyph,))
    before=source.to_json();lowered,joins=_lower_glyphs(source,cfg)
    sheet=cfg.sheets[0]
    renderer=render_sheet_svg if fmt=="svg" else sheet_to_dxf
    result=renderer(lowered,sheet.paper,sheet.placements)
    manifest=result.manifest()
    from oabm.drawings.sheets import compose_sheet
    composition=compose_sheet(lowered,sheet.paper,sheet.placements).to_dict()
    p=composition["views"][0]["primitives"][0]
    a,b=p["points_mm"]
    assert b["x"]-a["x"]==pytest.approx(2) and b["y"]-a["y"]==pytest.approx(1)
    assert a["x"]==pytest.approx(25+2*1000/scale)
    assert a["y"]==pytest.approx(25+3*1000/scale)
    assert p["source_ids"]==["wall"] and joins[0]["line_ids"]==[p["id"]]
    if fmt == "svg":
        import xml.etree.ElementTree as ET
        item=next(i for i in manifest["items"] if i["source_item_id"]==p["id"])
        element=next(e for e in ET.fromstring(result.svg).iter() if e.get("id")==item["svg_id"])
        vertices=[tuple(map(float, value.split(","))) for value in element.get("points").split()]
        assert vertices[0]==pytest.approx((a["x"],a["y"]))
        assert vertices[1]==pytest.approx((b["x"],b["y"]))
    else:
        import ezdxf
        cad=ezdxf.read(io.StringIO(result.dxf.decode()))
        item=next(i for i in manifest["entities"] if i.get("primitive_id")==p["id"])
        entity=cad.entitydb[item["handle"]]
        assert entity.dxf.start.x==pytest.approx(a["x"])
        assert entity.dxf.end.y==pytest.approx(sheet.paper.height_mm-b["y"])
    assert source.to_json()==before
    assert _lower_glyphs(source,cfg)==(lowered,joins)


@pytest.mark.parametrize("fmt",["svg","dxf"])
def test_symbol_bounds_and_unplaced_text_fail(fmt):
    from test_physical_sheets import drawing
    from oabm.drawings.model import DrawingPrimitive
    from oabm.drawings.sheet_svg import render_sheet_svg
    from oabm.drawings.sheet_dxf import sheet_to_dxf
    source=drawing();view=source.views[0]
    primitive=DrawingPrimitive("glyph", "symbol", "symbols:electrical", ("wall",),
        (Point2(0,0),), symbol="synthetic:edge")
    source=replace(source,views=(replace(view,id="review:floor",primitives=(primitive,),dimensions=()),))
    cfg=replace(config(formats=(fmt,)),glyphs=(ReviewGlyph(token=primitive.symbol,
        segments_mm=((Point2(-2,0),Point2(2,0)),)),))
    lowered,_=_lower_glyphs(source,cfg);sheet=cfg.sheets[0]
    with pytest.raises(ValueError):
        (render_sheet_svg if fmt=="svg" else sheet_to_dxf)(lowered,sheet.paper,sheet.placements)
    source=replace(source,views=(replace(source.views[0],primitives=(replace(primitive,text="unplaced"),)),))
    with pytest.raises(ContractError,match="unplaced symbol text"):_lower_glyphs(source,cfg)


def test_mixed_config_and_missing_marker_refused(source,tmp_path):
    a=persist(source,tmp_path,name="a")
    b=persist(source,tmp_path,config(revision="B"),name="b")
    p=a/"generated-sheet-sources.zip";p.write_bytes((b/p.name).read_bytes())
    report=read(a/"completion-report.json");report["artifacts"]["sheet_package"].update(sha256=sha(p.read_bytes()),bytes=len(p.read_bytes()))
    write(a/"completion-report.json",report);rehash(a)
    with pytest.raises(ContractError,match="drawing/model replay mismatch"):_load(a)
    (b/"COMPLETED.json").unlink()
    with pytest.raises((ContractError,FileNotFoundError)):_load(b)


def test_requested_dxf_failure_never_degrades_to_svg_success(source,tmp_path,monkeypatch):
    import oabm.drawings.sheet_dxf as dxf
    def fail(*args,**kwargs):raise RuntimeError("synthetic unavailable requested CAD exporter")
    monkeypatch.setattr(dxf,"sheet_to_dxf",fail)
    with pytest.raises(saved.SavedDeliveryError):persist(source,tmp_path,name="dxf-failed")
    assert not (tmp_path/"out/dxf-failed/delivery").exists()
    assert read(tmp_path/"out/dxf-failed/failure.json")["runner_completion"]["delivery_ready"] is False
