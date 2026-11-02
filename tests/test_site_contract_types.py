"""Synthetic source-only contract cases; no project drawings or coordinates."""
from dataclasses import asdict,replace

import pytest

from oabm.model import ContractError
from oabm.model.assemblies import Point2D
from oabm.model.site import (
    SiteFeature,SitePlanGeometry,SiteRequirement,SiteSourceEvidence,ServiceEquipmentFacts,
)


def geometry(kind,points):
    return SitePlanGeometry(kind=kind,points=tuple(Point2D(x=x,y=y) for x,y in points))


def evidence():
    return SiteSourceEvidence(source_id='source:synthetic',sha256='0'*64,page=1,
        sheet_id='SYN-1',model_frame_id='site',source_units='pdf_point',drawing_units='m',
        scale_denominator=100,region=(0.,0.,1000.,1000.),
        source_to_model=(.0254/72*100,0.,0.,-.0254/72*100,0.,20.))


def test_site_polygon_is_plan_only_and_does_not_repeat_closure_or_invent_elevation():
    polygon=geometry('polygon',[(0,0),(4,0),(4,3),(0,3)])
    feature=SiteFeature(id='site:boundary',kind='property_boundary',geometry=polygon,
        status='existing',evidence=(evidence(),))
    assert all(set(point)=={'x','y'} for point in asdict(feature)['geometry']['points'])
    assert 'z' not in asdict(feature)['geometry']
    assert replace(feature,geometry=geometry('polygon',[(0,0),(5,0),(5,3),(0,3)])).id==feature.id


def test_translated_small_polygon_retains_area_without_global_shoelace_cancellation():
    assert geometry('polygon',[(1e9,1e9),(1e9+1,1e9),(1e9+1,1e9+1),(1e9,1e9+1)]).kind=='polygon'


@pytest.mark.parametrize('transform',[(.01,0.,0.,-.01,0.,1.),(.04,.01,0.,-.04,0.,0.),(.04,0.,0.,-.03,0.,0.)])
def test_metric_source_rejects_inconsistent_scale_shear_and_anisotropic_transform(transform):
    with pytest.raises(ContractError):replace(evidence(),source_to_model=transform)


@pytest.mark.parametrize('kind,points',[
    ('point',[]),('point',[(0,0),(1,1)]),('polyline',[(0,0)]),
    ('polygon',[(0,0),(1,1),(2,2)]),
    ('polygon',[(0,0),(2,2),(0,2),(2,0)]),
    ('polygon',[(0,0),(4,0),(2,0),(2,3),(0,3)]),
    ('polygon',[(0,0),(4,0),(4,3),(0,0)]),
    ('polyline',[(0,0),(2,2),(0,2),(2,0)]),
    ('polyline',[(0,0),(2,0),(1,0)]),
])
def test_non_simple_or_wrong_geometry_refuses(kind,points):
    with pytest.raises(ContractError):geometry(kind,points)


def test_collinear_forward_polyline_is_valid_and_bounded_without_truncation():
    assert len(geometry('polyline',[(0,0),(1,0),(2,0)]).points)==3
    with pytest.raises(ContractError,match='no points truncated'):
        geometry('polyline',[(i,0) for i in range(257)])


@pytest.mark.parametrize('field,value',[
    ('page',0),('page',True),('sha256','invalid'),('source_units','yards'),
    ('scale_denominator',float('inf')),('scale_denominator',True),
    ('region',(0,0,0,1)),('source_to_model',(0,0,0,0,0,0)),
    ('source_to_model',(1,0,0,1,float('nan'),0)),
])
def test_source_evidence_rejects_bad_units_hash_geometry_and_nonfinite_values(field,value):
    with pytest.raises(ContractError):replace(evidence(),**{field:value})


def test_kind_ownership_and_traced_outline_are_explicit():
    point=geometry('point',[(2,3)])
    with pytest.raises(ContractError):
        SiteFeature(id='site:boundary',kind='property_boundary',geometry=point,
            status='existing',evidence=(evidence(),))
    polygon=geometry('polygon',[(0,0),(2,0),(2,2),(0,2)])
    with pytest.raises(ContractError,match='equipment owner'):
        SiteFeature(id='site:work',kind='working_surface',geometry=polygon,
            status='proposed',evidence=(evidence(),))
    traced=SiteFeature(id='site:building',kind='building_outline',geometry=polygon,
        status='existing',evidence=(evidence(),),outline_basis='source_traced')
    assert traced.owner_id is None
    with pytest.raises(ContractError,match='requires an owner'):
        replace(traced,outline_basis='modeled_owner_projection')
    with pytest.raises(ContractError,match='one model frame'):
        replace(traced,evidence=(evidence(),replace(evidence(),model_frame_id='other')))


def test_requirement_reference_basis_does_not_certify_unknown_enclosures():
    requirement=SiteRequirement(id='requirement:separation',kind='minimum_horizontal_separation',
        source_id='site:gas',target_id='equipment:main',reference_basis='anchor_to_boundary',minimum_m=1.)
    assert 'compliant' not in asdict(requirement)
    assert requirement.reference_basis!='envelope_to_envelope'
    with pytest.raises(ContractError):replace(requirement,reference_basis='automatic')
    with pytest.raises(ContractError):replace(requirement,minimum_m=None)


def test_integrated_service_facts_are_one_owner_and_unknown_stays_unknown():
    facts=ServiceEquipmentFacts(id='facts:main',equipment_id='equipment:main',
        equipment_rating_a=200.,continuous_meter_rating_a=160.,integrated_service_disconnect=True)
    assert facts.main_lug_only is None and facts.circuit_positions is None
    with pytest.raises(ContractError):replace(facts,main_lug_only=True)
    with pytest.raises(ContractError):replace(facts,continuous_meter_rating_a=250.)
    with pytest.raises(ContractError):replace(facts,circuit_positions=True)


@pytest.mark.parametrize('field,value',[('minimum_m',1.),('width_m',1.),('depth_m',1.),('orientation_radians',0.),('reference_basis','envelope_to_envelope')])
def test_informational_reference_distance_cannot_carry_clearance_rules(field,value):
    row=SiteRequirement(id='requirement:reference',kind='horizontal_reference_distance',
        source_id='site:gas',target_id='service:main',reference_basis='point_to_point')
    assert row.minimum_m is None
    with pytest.raises(ContractError):replace(row,**{field:value})
