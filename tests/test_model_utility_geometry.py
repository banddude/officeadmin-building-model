"""Synthetic exact-footprint/requirement cases; runtime pending owner release."""
from dataclasses import replace
import hashlib
import math

from oabm.model import (ElectricalEquipment,Pose,Point3,Size3,Quaternion,SiteFeature,SiteRequirement)
from oabm.drawings.model_utility import project_model_site
from test_site_contract_types import geometry
from test_site_model_integration import site_model


def placed_model(tmp_path,*,rotated=False,size=True):
    source=tmp_path/'source';source.write_bytes(b'synthetic geometric evidence')
    digest=hashlib.sha256(source.read_bytes()).hexdigest();model=site_model(digest)
    rotation=Quaternion(z=math.sin(math.pi/8),w=math.cos(math.pi/8)) if rotated else Quaternion()
    owner=ElectricalEquipment(id='equipment:main',equipment_type='meter_main',
        pose=Pose(position=Point3(x=3,y=3,z=2),rotation=rotation),size=Size3(x=2,y=1,z=2) if size else None)
    node=replace(model.service_nodes[1],location_basis='placed_equipment',equipment_id=owner.id,plan_position=None,unresolved_reason=None)
    return replace(model,electrical_equipment=(owner,),service_nodes=(model.service_nodes[0],node,model.service_nodes[2])),{'source:synthetic':source}


DRAWING={'scale':100.,'placement_mm':[15.,20.],'padding_mm':5.,'legend_width_mm':95.}


def test_rotated_known_box_projects_actual_convex_footprint_not_aabb_or_generic_square(tmp_path):
    model,sources=placed_model(tmp_path,rotated=True)
    request,receipt=project_model_site(model,source_files=sources,drawing=DRAWING)
    feature=next(row for row in request['features'] if row['id']=='service:main')
    assert feature['kind']=='equipment_region' and feature['closed'] is True
    points=feature['points'];origin=points[0]
    area=abs(sum((a[0]-origin[0])*(b[1]-origin[1])-(b[0]-origin[0])*(a[1]-origin[1])
        for a,b in zip(points,(*points[1:],points[0]))))/2
    assert abs(area-2)<1e-8
    span=(max(p[0] for p in points)-min(p[0] for p in points))*(max(p[1] for p in points)-min(p[1] for p in points))
    assert span>area*2
    assert receipt['placed_equipment_joins']['service:main']=='equipment:main'


def test_anchor_inside_known_enclosure_has_zero_separation(tmp_path):
    model,sources=placed_model(tmp_path)
    gas=SiteFeature(id='site:gas',kind='gas_location',status='existing',geometry=geometry('point',[(3,3)]),
        extent_basis='source_marker',evidence=model.site_features[0].evidence)
    requirement=SiteRequirement(id='requirement:gas',kind='minimum_horizontal_separation',source_id=gas.id,
        target_id='service:main',reference_basis='anchor_to_boundary',minimum_m=1.)
    model=replace(model,site_features=(*model.site_features,gas),site_requirements=(requirement,))
    _,receipt=project_model_site(model,source_files=sources,drawing=DRAWING)
    measured,=receipt['measurement_requirements']
    assert measured['distance_m']==0 and measured['meets_supplied_horizontal_minimum'] is False
    assert measured['engineering_approval'] is False


def test_unknown_extent_is_unresolved_for_envelope_separation(tmp_path):
    model,sources=placed_model(tmp_path,size=False)
    gas=SiteFeature(id='site:gas',kind='gas_location',status='existing',geometry=geometry('polygon',[(6,2),(7,2),(7,3),(6,3)]),
        extent_basis='confirmed_envelope',evidence=model.site_features[0].evidence)
    requirement=SiteRequirement(id='requirement:gas',kind='minimum_horizontal_separation',source_id=gas.id,
        target_id='service:main',reference_basis='envelope_to_envelope',minimum_m=1.)
    model=replace(model,site_features=(*model.site_features,gas),site_requirements=(requirement,))
    _,receipt=project_model_site(model,source_files=sources,drawing=DRAWING)
    measured,=receipt['measurement_requirements']
    assert measured['distance_m'] is None and measured['status']=='unresolved'
    assert 'meets_supplied_horizontal_minimum' not in measured


def test_working_rectangle_requires_known_front_and_enclosure_interface(tmp_path):
    model,sources=placed_model(tmp_path)
    surface=SiteFeature(id='site:surface',kind='working_surface',status='proposed',owner_id='service:main',
        geometry=geometry('polygon',[(2,1.5),(4,1.5),(4,2.5),(2,2.5)]),evidence=model.site_features[0].evidence)
    requirement=SiteRequirement(id='requirement:work',kind='working_surface_for',source_id=surface.id,
        target_id='service:main',reference_basis='surface_to_equipment',width_m=1.,depth_m=.5)
    model=replace(model,site_features=(*model.site_features,surface),site_requirements=(requirement,))
    _,receipt=project_model_site(model,source_files=sources,drawing=DRAWING)
    measured,=receipt['measurement_requirements']
    assert measured['status']=='unresolved' and len(measured['surface_edges'])==4
    known=replace(model,service_equipment_facts=(replace(model.service_equipment_facts[0],front_normal_radians=-math.pi/2),))
    _,receipt=project_model_site(known,source_files=sources,drawing=DRAWING)
    measured,=receipt['measurement_requirements']
    assert measured['status']=='coordinate_measurement_only'
    assert abs(measured['width_m']-2)<1e-8 and abs(measured['depth_m']-1)<1e-8
    assert measured['engineering_approval'] is False
