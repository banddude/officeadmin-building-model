"""Synthetic supplied envelope and projection contract checks."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from oabm.model import BuildingModel, ElectricalEquipment, Pose, Point3, Vector3, Size3, Quaternion
from oabm.drawings.model import Bounds2
from oabm.drawings.details import (DetailSpec, generate_detail, generate_details,
    detail_body_svg, company_detail_bodies)


def model():
    return BuildingModel(model_id="synthetic:detail", electrical_equipment=(
        ElectricalEquipment(id="equipment:demo", equipment_type="cabinet",
            pose=Pose(position=Point3(x=1,y=0,z=1)), size=Size3(x=.6,y=.2,z=.8)),))


def spec(**kw):
    values=dict(id="D1",number="1",title="Synthetic cabinet",recipe="equipment-mounting-elevation@1",
        source_ids=("equipment:demo",),origin=Point3(x=0,y=0,z=0),direction=Vector3(x=0,y=1,z=0),
        bounds=Bounds2(min_x=-2,min_y=0,max_x=0,max_y=2),scale=10,
        near_m=-1,far_m=1,mounting_spec="Supplied freestanding pose",connection_spec="No connection geometry supplied")
    values.update(kw)
    return DetailSpec(**values)


def test_pose_dimensions_and_source_fidelity():
    m=model(); before=m.to_json(); detail=generate_detail(m,spec())
    points=[p for primitive in detail.view.primitives for p in primitive.points]
    assert min(p.x for p in points)==pytest.approx(-1.3)
    assert max(p.x for p in points)==pytest.approx(-.7)
    assert min(p.y for p in points)==pytest.approx(.6)
    assert max(p.y for p in points)==pytest.approx(1.4)
    assert all(p.source_ids == ("equipment:demo",) for p in detail.view.primitives)
    assert "0.6/0.2/0.8 m" in detail.facts[0]
    assert m.to_json()==before


@pytest.mark.parametrize("changes,match", [
    ({"recipe":"imagined@1"},"recipe"), ({"mounting_spec":" "},"mounting_spec"),
    ({"connection_spec":""},"connection_spec"), ({"scale":float('nan')},"finite"),
    ({"source_ids":("wall:missing",)},"unsupported"),
    ({"bounds":Bounds2(-1,0,0,2)},"clip"), ({"near_m":0,"far_m":1},"clip")])
def test_refusals(changes,match):
    with pytest.raises(ValueError,match=match): generate_detail(model(),spec(**changes))


def test_missing_geometry_refused():
    m=model(); m=replace(m,electrical_equipment=(replace(m.electrical_equipment[0],size=None),))
    with pytest.raises(ValueError,match="dimensions"): generate_detail(m,spec())


def test_rotated_pose_preserved():
    m=model(); e=m.electrical_equipment[0]
    m=replace(m,electrical_equipment=(replace(e,pose=replace(e.pose,rotation=Quaternion(x=0,y=0,z=2**-.5,w=2**-.5))),))
    points=[p for primitive in generate_detail(m,spec()).view.primitives for p in primitive.points]
    assert max(p.x for p in points)-min(p.x for p in points)==pytest.approx(.2)


def test_recipe_and_stable_order():
    a=spec(); b=spec(id="D2",number="2",recipe="connection-close-up@1")
    assert generate_details(model(),(b,a))==generate_details(model(),(a,b))
    section=generate_detail(model(),spec(recipe="supported-section@1"))
    assert section.view.view_type=="section"
    with pytest.raises(ValueError,match="unique"): generate_details(model(),(a,a))


def test_body_determinism_overflow_and_complete_preflight():
    d=generate_detail(model(),spec())
    body=detail_body_svg(d,drawing_bounds_mm=(10,10,400,300))
    assert body==detail_body_svg(d,drawing_bounds_mm=(10,10,400,300))
    assert "equipment:demo" in body and 'Projected W 0.6 m' in body
    with pytest.raises(ValueError,match="exceeds"): detail_body_svg(d,drawing_bounds_mm=(10,10,50,50))
    sheets=(SimpleNamespace(id="D1",drawing_bounds_mm=(10,10,400,300)),)
    assert company_detail_bodies(model(),(spec(),),sheets)=={"D1":body}
    with pytest.raises(ValueError,match="exactly"): company_detail_bodies(model(),(spec(id="other"),),sheets)


def test_building_assembly_guard_unchanged():
    from oabm.building_assemblies.fixtures import synthetic_two_opening_wall_assembly
    assembled = synthetic_two_opening_wall_assembly()
    with pytest.raises(ValueError, match="assembl"):
        generate_detail(assembled, spec())


def test_xml_text_is_escaped():
    d=generate_detail(model(),spec(title='Cabinet <provided> & test'))
    body=detail_body_svg(d,drawing_bounds_mm=(10,10,400,300))
    assert '&lt;provided&gt; &amp; test' in body


def test_blank_request_refused():
    from oabm.drawings.details import detail_specs_from_dict
    with pytest.raises(ValueError): detail_specs_from_dict({'version':'imagined','details':[]})


def test_real_shared_pipeline_and_no_partial_package(tmp_path, monkeypatch):
    import hashlib
    import json
    from pathlib import Path
    shared=pytest.importorskip('oabm.drawings.company_sheets')
    from oabm.drawings.details import create_detail_sheet_set
    root=Path(__file__).resolve().parents[1]/'fixtures'/'detail_sheets'
    def create(destination):
        return create_detail_sheet_set(root/'model.json',root/'details.json',root/'profile.json',root/'job.json',output_directory=destination)
    destination=tmp_path/'result'
    assert create(destination)==destination
    assert (destination/'delivery'/'sheet-set.pdf').is_file()
    manifest=json.loads((destination/'detail-manifest.json').read_text())
    assert manifest['inputs']['detail-model.json']==hashlib.sha256((root/'model.json').read_bytes()).hexdigest()
    assert manifest['source_ids']['D1']==['equipment:demo']
    with pytest.raises(ValueError,match='already exist'): create(destination)
    repeated=tmp_path/'repeated'
    create(repeated)
    for path in (destination/'delivery'/'sheets').glob('*.svg'):
        assert path.read_bytes()==(repeated/'delivery'/'sheets'/path.name).read_bytes()
    assert (destination/'delivery'/'sheet-set.pdf').read_bytes()==(repeated/'delivery'/'sheet-set.pdf').read_bytes()
    failed=tmp_path/'failed'
    def broken(*args,output_directory,**kwargs):
        output_directory.mkdir();(output_directory/'partial.svg').write_text('partial')
        raise ValueError('synthetic shared conversion failure')
    monkeypatch.setattr(shared,'create_company_sheet_set',broken)
    with pytest.raises(ValueError,match='conversion failure'): create(failed)
    assert not failed.exists()
    assert not list(tmp_path.glob('.detail-stage-*'))


def test_wide_ascii_note_refused_by_measured_width():
    from oabm.drawings.company_sheets import measure_sheet_text
    note='W'*230
    drawing_bounds=(7.055555556,7.055555556,515.408333333,450.144444444)
    # This fits the former .72 em estimate but not the actual supported face.
    assert len(note)*2.16 < drawing_bounds[2]-drawing_bounds[0]
    assert measure_sheet_text(note,3) > drawing_bounds[2]-drawing_bounds[0]
    with pytest.raises(ValueError,match='text exceeds'):
        detail_body_svg(generate_detail(model(),spec(notes=(note,))),drawing_bounds_mm=drawing_bounds)


@pytest.mark.parametrize('note',['bad\uffffnote','bad\ud800note','bad\x00note'])
def test_invalid_xml_note_refused_early_with_field(note):
    with pytest.raises(ValueError,match='note'):
        spec(notes=(note,))


def test_note_overflow_does_not_create_partial_package(tmp_path):
    import json
    from pathlib import Path
    from oabm.drawings.details import create_detail_sheet_set
    root=Path(__file__).resolve().parents[1]/'fixtures'/'detail_sheets'
    request=json.loads((root/'details.json').read_text())
    request['details'][1]['notes']=['W'*230]
    request_path=tmp_path/'details.json'
    request_path.write_text(json.dumps(request))
    output=tmp_path/'failed'
    with pytest.raises(ValueError,match='text exceeds'):
        create_detail_sheet_set(root/'model.json',request_path,root/'profile.json',root/'job.json',output_directory=output)
    assert not output.exists()
    assert not list(tmp_path.glob('.detail-stage-*'))
