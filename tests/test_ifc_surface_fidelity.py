import copy
import json
from pathlib import Path

import ifcopenshell
import ifcopenshell.api
import pytest

from oabm.ifc import to_ifc, canonical_id_to_ifc_guid
from oabm.model import BuildingModel
from oabm.reports import ifc_surface_fidelity

ROOT = Path(__file__).resolve().parents[1]
CONVENTION = 'ifc-factor-as-metallic-and-transmission-v1'


def fixture_ifc(roughness=True):
    model = BuildingModel.load(ROOT / 'examples/synthetic-native-bonsai-model.json')
    f = to_ifc(model)
    guid = canonical_id_to_ifc_guid('wall:synthetic-south')
    # Pick an actual wall Body without relying on fixture spelling.
    product = next(p for p in f.by_type('IfcWall') if p.Representation)
    guid = product.GlobalId
    colour = {'Name': None, 'Red': .2, 'Green': .3, 'Blue': .4}
    style = ifcopenshell.api.run('style.add_style', f, name='synthetic finish')
    attrs = {'SurfaceColour': colour, 'DiffuseColour': colour, 'Transparency': .2,
             'SpecularColour': .8, 'TransmissionColour': .1, 'ReflectanceMethod': 'NOTDEFINED'}
    if roughness == 'wrong':
        attrs['SpecularHighlight'] = {'SpecularRoughness': .12}
    elif roughness:
        attrs['SpecularHighlight'] = {'IfcSpecularRoughness': .12}
    ifcopenshell.api.run('style.add_surface_style', f, style=style, ifc_class='IfcSurfaceStyleRendering', attributes=attrs)
    style.Styles = tuple(style.Styles) + (f.create_entity('IfcSurfaceStyleRefraction', RefractionIndex=1.5),)
    body = next(r for r in product.Representation.Representations if r.RepresentationIdentifier == 'Body')
    for item in body.Items:
        f.create_entity('IfcStyledItem', Item=item, Styles=[style])
    values = {'surface_rgb': [.2, .3, .4], 'diffuse_rgb': [.2, .3, .4], 'metallic': .8,
              'roughness': .12, 'alpha': .8, 'transmission': .1, 'ior': 1.5}
    return f, [{'product_guid': guid, 'style_name': 'synthetic finish', 'values': values}]


def test_all_explicit_declarations_match_without_mutation():
    f, expected = fixture_ifc()
    before, args = f.to_string(), copy.deepcopy(expected)
    result = ifc_surface_fidelity(f, expected, convention=CONVENTION)
    assert {x['comparison'] for x in result['rows'][0]['fields'].values()} == {'match'}
    assert f.to_string() == before and expected == args
    assert result == ifc_surface_fidelity(f, expected, convention=CONVENTION)
    json.dumps(result, allow_nan=False)


def test_missing_roughness_is_not_a_successful_roundtrip():
    f, expected = fixture_ifc(roughness=False)
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['fields']['roughness']['comparison'] == 'missing'
    assert row['fields']['metallic']['comparison'] == 'match'


def test_mismatch_unknown_and_missing_selector_are_explicit():
    f, expected = fixture_ifc()
    expected[0]['values']['alpha'] = .5
    expected[0]['values']['ior'] = None
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['fields']['alpha']['comparison'] == 'mismatch'
    assert row['fields']['ior']['comparison'] == 'expected_unknown'
    expected[0]['style_name'] = 'absent'
    assert ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]['selector_status'] == 'style_missing'


def test_unsupported_colour_and_shininess_are_not_roughness():
    f, expected = fixture_ifc()
    rendering = f.by_type('IfcSurfaceStyleRendering')[0]
    rendering.DiffuseColour = f.create_entity('IfcNormalisedRatioMeasure', .5)
    rendering.SpecularHighlight = f.create_entity('IfcSpecularExponent', 5.)
    fields = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]['fields']
    assert fields['diffuse_rgb']['comparison'] == 'unsupported'
    assert fields['roughness']['comparison'] == 'unsupported'


def test_invalid_input_and_explicit_convention():
    f, expected = fixture_ifc()
    with pytest.raises(ValueError): ifc_surface_fidelity(f, expected, convention='generic')
    with pytest.raises(ValueError): ifc_surface_fidelity(f, expected + expected, convention=CONVENTION)
    expected[0]['values']['roughness'] = float('nan')
    with pytest.raises(ValueError): ifc_surface_fidelity(f, expected, convention=CONVENTION)


@pytest.mark.parametrize('field,value', [
    ('surface_rgb', [.1, .3, .4]), ('diffuse_rgb', [.2, .2, .4]), ('metallic', .3),
    ('roughness', .6), ('alpha', .2), ('transmission', .7), ('ior', 1.2),
])
def test_every_supported_field_detects_mismatch(field, value):
    f, expected = fixture_ifc()
    expected[0]['values'][field] = value
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['fields'][field]['comparison'] == 'mismatch'


def test_unstyled_body_item_is_not_hidden():
    f, expected = fixture_ifc()
    product = f.by_guid(expected[0]['product_guid'])
    body = next(r for r in product.Representation.Representations if r.RepresentationIdentifier == 'Body')
    new = f.create_entity('IfcBlock', Position=f.create_entity('IfcAxis2Placement3D', Location=f.create_entity('IfcCartesianPoint', (0., 0., 0.))), XLength=1., YLength=1., ZLength=1.)
    body.Items = tuple(body.Items) + (new,)
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['body_item_count'] == 2 and row['matching_body_item_count'] == 1


def test_duplicate_named_styles_are_ambiguous():
    f, expected = fixture_ifc()
    product = f.by_guid(expected[0]['product_guid'])
    body = next(r for r in product.Representation.Representations if r.RepresentationIdentifier == 'Body')
    second = ifcopenshell.api.run('style.add_style', f, name='synthetic finish')
    ifcopenshell.api.run('style.add_surface_style', f, style=second, ifc_class='IfcSurfaceStyleRendering', attributes={'SurfaceColour': {'Red': 0., 'Green': 0., 'Blue': 0.}})
    f.create_entity('IfcStyledItem', Item=body.Items[0], Styles=[second])
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['selector_status'] == 'ambiguous_style'
    assert row['fields']['roughness']['comparison'] == 'ambiguous_style'


def test_texture_is_disclosed_without_fetching():
    f, expected = fixture_ifc()
    style = next(s for s in f.by_type('IfcSurfaceStyle') if s.Name == 'synthetic finish')
    texture = f.create_entity('IfcImageTexture', RepeatS=True, RepeatT=True, URLReference='https://example.invalid/private')
    style.Styles = tuple(style.Styles) + (f.create_entity('IfcSurfaceStyleWithTextures', Textures=[texture]),)
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['unsupported_style_elements'] == ['IfcSurfaceStyleWithTextures']
    assert 'example.invalid' not in json.dumps(row)


def test_mapped_body_is_disclosed_without_traversal():
    f, expected = fixture_ifc()
    product = f.by_guid(expected[0]['product_guid'])
    body = next(r for r in product.Representation.Representations if r.RepresentationIdentifier == 'Body')
    origin = f.create_entity('IfcAxis2Placement3D', Location=f.create_entity('IfcCartesianPoint', (0., 0., 0.)))
    repmap = f.create_entity('IfcRepresentationMap', MappingOrigin=origin, MappedRepresentation=body)
    operator = f.create_entity('IfcCartesianTransformationOperator3D', LocalOrigin=f.create_entity('IfcCartesianPoint', (0., 0., 0.)))
    mapped = f.create_entity('IfcMappedItem', MappingSource=repmap, MappingTarget=operator)
    body.Items = tuple(body.Items) + (mapped,)
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['mapped_body_item_count'] == 1
    assert row['matching_body_item_count'] < row['body_item_count']


def test_actual_wrong_ifcopenshell_key_is_detected():
    f, expected = fixture_ifc(roughness='wrong')
    # Some supported IfcOpenShell releases silently drop this producer key;
    # a future version may accept it. The report must reflect the actual IFC.
    raw = f.by_type('IfcSurfaceStyleRendering')[0].SpecularHighlight
    field = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]['fields']['roughness']
    if raw is None:
        assert field['comparison'] == 'missing'
    elif raw.is_a('IfcSpecularRoughness'):
        assert field['actual']['value'] == pytest.approx(raw.wrappedValue)
        assert field['comparison'] == ('match' if abs(raw.wrappedValue - .12) <= 1e-6 else 'mismatch')
    else:
        assert field['comparison'] == 'unsupported'


def test_report_snapshot_does_not_alias_caller_rgb():
    f, expected = fixture_ifc()
    report = ifc_surface_fidelity(f, expected, convention=CONVENTION)
    expected[0]['values']['surface_rgb'][0] = .9
    field = report['rows'][0]['fields']['surface_rgb']
    assert field['expected'] == [.2, .3, .4] and field['comparison'] == 'match'


def test_competing_direct_styles_are_disclosed():
    f, expected = fixture_ifc()
    product = f.by_guid(expected[0]['product_guid'])
    body = next(r for r in product.Representation.Representations if r.RepresentationIdentifier == 'Body')
    other = ifcopenshell.api.run('style.add_style', f, name='other finish')
    ifcopenshell.api.run('style.add_surface_style', f, style=other, ifc_class='IfcSurfaceStyleRendering', attributes={'SurfaceColour': {'Red': 0., 'Green': 0., 'Blue': 0.}})
    f.create_entity('IfcStyledItem', Item=body.Items[0], Styles=[other])
    row = ifc_surface_fidelity(f, expected, convention=CONVENTION)['rows'][0]
    assert row['matching_items_with_competing_styles'] == 1
    assert row['fields']['roughness']['comparison'] == 'match'
