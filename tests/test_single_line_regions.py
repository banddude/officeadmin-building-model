from types import SimpleNamespace
from copy import deepcopy
import pytest
from oabm.drawings.single_line.demo_drafting import make_model
from oabm.drawings.single_line import project_topology, plan_pages
from oabm.drawings.single_line.production import build_single_line_bodies

BOUNDS=(5,5,900,650)
REGIONS={'diagram':(5,5,650,500)}

def prepared():
    model=make_model()
    plan=plan_pages(project_topology(model),BOUNDS,first_sheet_id='SL-020',regions=REGIONS)
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in plan['pages']]
    return model,plan,sheets

def test_precomputed_regions_and_external_index_are_preserved():
    model,plan,sheets=prepared()
    original=deepcopy(plan)
    index=[p['id'] for p in plan['pages']]+['PN-101']
    bodies,doc=build_single_line_bodies(model,sheets,layout=plan,sheet_index=index)
    assert plan==original
    assert list(bodies)==index[:-1]
    assert doc['layout']['external_sheet_ids']==['PN-101']
    assert {key:tuple(value) for key,value in doc['layout']['effective_regions'].items()}==REGIONS
    assert len(doc['layout']['delegated_feeder_connection_ids'])==7
    assert all(label['bounds_mm'][2]<=650 for sheet in doc['sheets'] for label in sheet['text_bounds'])

def test_precomputed_mutation_and_resolved_bounds_are_rejected():
    model,plan,sheets=prepared()
    broken=deepcopy(plan);broken['pages'][0]['id']='OTHER'
    with pytest.raises(ValueError,match='content changed'):
        build_single_line_bodies(model,sheets,layout=broken)
    with pytest.raises(ValueError,match='drawing bounds differ'):
        build_single_line_bodies(model,[SimpleNamespace(id=s.id,drawing_bounds_mm=(5,5,901,650)) for s in sheets],layout=plan)

@pytest.mark.parametrize('regions',[{'diagram':(0,0,30,30)}, {'diagram':(5,5,650,500),'notes':(600,400,700,550)}])
def test_invalid_component_regions_fail(regions):
    with pytest.raises(ValueError,match='regions'):
        plan_pages(project_topology(make_model()),BOUNDS,regions=regions)


def test_every_primitive_ink_is_inside_a_declared_component():
    import xml.etree.ElementTree as ET
    from oabm.drawings.company_sheets import _body_group
    model=make_model()
    plan=plan_pages(project_topology(model),BOUNDS,first_sheet_id='SL-020',regions={
        'diagram':(5,5,650,500),'notes':(660,5,895,640)})
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in plan['pages']]
    bodies,doc=build_single_line_bodies(model,sheets,layout=plan)
    for page in doc['layout']['pages']:
        for child in ET.fromstring(bodies[page['id']]):
            group=ET.Element('g');group.append(child)
            raw=ET.tostring(group,encoding='unicode')
            fits=False
            for region in page['effective_regions'].values():
                try:_body_group(raw,region);fits=True;break
                except ValueError:pass
            assert fits,raw
