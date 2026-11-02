"""Actual analytic curves survive ordinary view/sheet/CAD/PDF consumers."""
from dataclasses import replace
import io
import math
import pytest
from oabm.model import (Level, compute_physical_source_sha256, compute_physical_request_sha256,
    LayerDefinition, LayerSelectionConfig, SavedPreset, resolve_preset, ContractError)
from oabm.drawings import PlanSpec, VisibilityPolicy, Bounds2, generate_drawing_set, view_to_svg, SheetSpec, ViewPlacement
from oabm.drawings.sheet_svg import render_sheet_svg
from oabm.drawings.sheet_dxf import sheet_to_dxf
from oabm.drawings.sheet_pdf import render_drawing_sheet_pdf
from oabm.exports.layer_presentation import apply_layer_selection
from test_physical_route_schedule import measured_model


def drawing_inputs():
    model=measured_model()
    source=replace(model,routes=(),name='Synthetic curved drawing',levels=(Level(id='level:main',elevation_m=0,height_m=3),))
    route=model.routes[0]
    context=replace(route.context,source_model_sha256=compute_physical_source_sha256(source))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,route.requirements))
    model=replace(source,routes=(replace(route,context=context),))
    plan=PlanSpec(id='plan:curve',level_id='level:main',scale=50,
        visibility=VisibilityPolicy(electrical=False,labels=False,dimensions=False))
    drawing=generate_drawing_set(model,plans=(plan,),physical_geometry='xy-centerline')
    sheet=SheetSpec('C-1',300,220,titleblock_height_mm=35,title='Analytic curve',revision='A')
    placements=(ViewPlacement('main',plan.id,10,10),)
    return model,plan,drawing,sheet,placements


def test_ordinary_plan_sheet_svg_and_dxf_retain_true_quarter_arc():
    ezdxf=pytest.importorskip('ezdxf')
    model,plan,drawing,sheet,placements=drawing_inputs()
    before=model.to_json()
    arcs=[p for p in drawing.views[0].primitives if p.kind=='quarter_arc']
    assert len(arcs)==1 and len(drawing.views[0].primitives)==3
    assert drawing.output_version=='1.1.0'
    assert dict(drawing.views[0].metadata)['physical_routes_unknown_level']==model.routes[0].id
    assert set(arcs[0].source_ids)<={row.canonical_id for row in drawing.source_index}
    assert ' A ' in view_to_svg(drawing.views[0])
    assert ' A ' in render_sheet_svg(drawing,sheet,placements).svg
    artifact=sheet_to_dxf(drawing,sheet,placements)
    native=ezdxf.read(io.StringIO(artifact.dxf.decode()))
    cad=list(native.layout().query('ARC'))
    assert len(cad)==1
    assert cad[0].dxf.radius==pytest.approx(.25*1000/plan.scale)
    assert (cad[0].dxf.end_angle-cad[0].dxf.start_angle)%360==pytest.approx(90)
    assert not native.audit().errors
    assert model.to_json()==before


def test_saved_resolver_hide_ghost_reset_applies_to_actual_arc():
    model,plan,drawing,sheet,placements=drawing_inputs()
    config=LayerSelectionConfig(layers=(LayerDefinition(id='layer:route',name='Route',member_ids=(model.routes[0].id,)),),
        presets=(SavedPreset(id='preset:hide',name='Hide',hidden_layer_ids=('layer:route',)),
                 SavedPreset(id='preset:ghost',name='Ghost',ghosted_layer_ids=('layer:route',)),
                 SavedPreset(id='preset:all',name='All',visible_layer_ids=('layer:route',))))
    for name,count in (('hide',0),('ghost',3),('all',3)):
        resolution=resolve_preset(config,model,preset=f'preset:{name}',unassigned='visible')
        view=apply_layer_selection(drawing.views[0],model,resolution)
        assert len(view.primitives)==count
        selected=replace(drawing,views=(view,))
        svg=render_sheet_svg(selected,sheet,placements).svg
        assert (' A ' in svg)==(count>0)
        if name=='ghost':assert all(p.style.stroke=='ghost' for p in view.primitives)
        if name=='all':assert all(p.style.stroke=='route' for p in view.primitives)


def test_pdf_retains_vector_curves_with_actual_page_readback():
    model,plan,drawing,sheet,placements=drawing_inputs()
    result=render_drawing_sheet_pdf(drawing,sheet,placements)
    import pypdf
    reader=pypdf.PdfReader(io.BytesIO(result.pdf),strict=True)
    assert len(reader.pages)==1 and not list(reader.pages[0].images)
    content=reader.pages[0].get_contents().get_data()
    assert b' c' in content  # PDF cubic curve operator, not tessellated lines.
    assert result.manifest()['drawing_output_version']=='1.1.0'


def test_admission_and_crop_refuse_instead_of_flattening_or_omitting():
    model,plan,drawing,sheet,placements=drawing_inputs()
    with pytest.raises(ContractError,match='does not support physical'):
        generate_drawing_set(model,plans=(plan,))
    with pytest.raises(ContractError,match='crop physical geometry'):
        generate_drawing_set(model,plans=(replace(plan,bounds=Bounds2(.1,-.1,1,1)),),physical_geometry='xy-centerline')
    hidden=replace(plan,visibility=replace(plan.visibility,routes=False))
    assert not generate_drawing_set(model,plans=(hidden,),physical_geometry='xy-centerline').views[0].primitives


def test_actual_pdf_titleblock_glyphs_remain_visible(tmp_path):
    import shutil,subprocess
    from PIL import Image
    renderer=shutil.which('pdftoppm')
    if renderer is None:pytest.skip('Poppler unavailable; actual glyph raster acceptance requires pdftoppm')
    model,plan,drawing,sheet,placements=drawing_inputs()
    result=render_drawing_sheet_pdf(drawing,sheet,placements)
    path=tmp_path/'sheet.pdf';path.write_bytes(result.pdf)
    prefix=tmp_path/'page'
    subprocess.run([renderer,'-singlefile','-r','96','-png',str(path),str(prefix)],check=True,capture_output=True)
    image=Image.open(prefix.with_suffix('.png')).convert('L')
    factor=96/25.4
    # Interior titleblock text region excludes all frame/divider strokes.
    crop=image.crop(tuple(round(value*factor) for value in (12,178,110,189)))
    assert sum(value<150 for value in crop.tobytes())>50
