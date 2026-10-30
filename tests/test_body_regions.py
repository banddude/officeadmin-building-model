"""Shared producer composition keeps real holes and never clips/changes bodies."""
from dataclasses import replace
import pytest
from oabm.drawings.body_regions import SheetBodyPart,compose_body_regions


def part(identity,regions,body):return SheetBodyPart(identity,body,regions,{'canonical_source_ids':[identity]})


def test_several_sld_regions_leave_real_panel_region_and_preserve_exact_sources():
    line='<g><line x1="10" y1="10" x2="90" y2="10" stroke="#000000" stroke-width="0.5"/><line x1="110" y1="10" x2="190" y2="10" stroke="#000000" stroke-width="0.5"/></g>'
    table='<g><rect x="10" y="110" width="80" height="70" fill="none" stroke="#000000" stroke-width="0.25"/></g>'
    a=part('single_line',{'diagram':(0,0,100,100),'notes':(105,0,200,100)},line)
    b=part('panels',{'tables':(0,105,100,200)},table)
    result=compose_body_regions((a,b),drawing_bounds_mm=(0,0,200,200))
    assert line in result.body_svg and table in result.body_svg
    assert [r['part_id'] for r in result.coverage]==['single_line','panels']
    assert result.coverage[0]['primitive_counts']=={'diagram':1,'notes':1}
    assert result.coverage[1]['source_bindings']=={'canonical_source_ids':['panels']}
    a.source_bindings['canonical_source_ids'].append('changed')
    assert result.coverage[0]['source_bindings']=={'canonical_source_ids':['single_line']}

@pytest.mark.parametrize('bad',[
    '<g><line x1="10" y1="10" x2="110" y2="10" stroke="#000000"/></g>',
    '<g><line x1="50" y1="50" x2="50" y2="110" stroke="#000000"/></g>',
    '<g><circle cx="0" cy="10" r="2" stroke="#000000" fill="none"/></g>',
])
def test_full_page_admission_is_not_enough_ink_must_stay_in_allocated_region(bad):
    p=part('single_line',{'diagram':(0,0,100,100)},bad)
    with pytest.raises(ValueError):compose_body_regions([p],drawing_bounds_mm=(-10,-10,200,200))


def test_overlap_gap_bad_bindings_and_resources_refuse():
    body='<g><line x1="10" y1="10" x2="20" y2="10" stroke="#000000"/></g>'
    a=part('a',{'r':(0,0,100,100)},body)
    b=part('b',{'r':(99,0,150,100)},body)
    with pytest.raises(ValueError,match='overlap'):compose_body_regions([a,b],drawing_bounds_mm=(0,0,200,200))
    with pytest.raises(ValueError,match='unique'):compose_body_regions([a,a],drawing_bounds_mm=(0,0,200,200))
    with pytest.raises(ValueError,match='source bindings'):compose_body_regions([replace(a,source_bindings={'bad':float('nan')})],drawing_bounds_mm=(0,0,200,200))
    with pytest.raises(ValueError,match='unsupported'):compose_body_regions([replace(a,body_svg='<g><image href="remote"/></g>')],drawing_bounds_mm=(0,0,200,200))
