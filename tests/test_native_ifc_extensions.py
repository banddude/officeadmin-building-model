"""Synthetic-only native extension boundary and exact graph preservation tests."""
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.geometry
import ifcopenshell.api.pset
import ifcopenshell.api.root
import ifcopenshell.api.system
import ifcopenshell.api.context
import numpy as np
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, to_ifc
from oabm.model import BuildingModel
from oabm.native_bonsai.extensions import (
    source_snapshot, validate_extension_source, verify_source_snapshot,
    prepare_render_source, merge_render_additions,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    model = BuildingModel.load(ROOT / 'examples/synthetic-native-bonsai-model.json')
    native = to_ifc(model)
    context = next(c for c in native.by_type('IfcGeometricRepresentationSubContext')
                   if c.ContextIdentifier == 'Body')
    products = []
    for index, kind in enumerate(('IfcSanitaryTerminal', 'IfcWasteTerminal', 'IfcPipeSegment')):
        product = ifcopenshell.api.root.create_entity(native, ifc_class=kind, name='Synthetic plumbing')
        product.GlobalId = canonical_id_to_ifc_guid('synthetic-native:' + kind)
        matrix = np.eye(4)
        matrix[:3, 3] = (index + .5, .5, .25)
        ifcopenshell.api.geometry.edit_object_placement(native, product=product, matrix=matrix)
        representation = ifcopenshell.api.geometry.add_wall_representation(
            native, context=context, length=.2, height=.3, thickness=.1)
        ifcopenshell.api.geometry.assign_representation(native, product=product, representation=representation)
        products.append(product)
    ports = [ifcopenshell.api.system.add_port(native, element=product) for product in products]
    ifcopenshell.api.system.connect_port(native, port1=ports[0], port2=ports[2])
    system = ifcopenshell.api.root.create_entity(native, ifc_class='IfcDistributionSystem',
                                               predefined_type='DOMESTICCOLDWATER')
    system.GlobalId = canonical_id_to_ifc_guid('synthetic-native:cold')
    ifcopenshell.api.system.assign_system(native, products=products, system=system)
    return model, native, products, ports


def test_native_plumbing_graph_is_retained_on_ifc_reopen(source, tmp_path):
    model, native, products, ports = source
    before = source_snapshot(native)
    diagnostics = validate_extension_source(native, model)
    assert {row['ifc_class'] for row in diagnostics} == {
        'IfcSanitaryTerminal', 'IfcWasteTerminal', 'IfcPipeSegment', 'IfcDistributionSystem'}
    assert all(row['status'] == 'preserved_native_only' for row in diagnostics)
    assert source_snapshot(native) == before
    path = tmp_path / 'synthetic.ifc'
    native.write(str(path))
    reopened = ifcopenshell.open(str(path))
    verify_source_snapshot(before, reopened)
    assert validate_extension_source(reopened, model) == diagnostics
    # The graph check covers identity, placements, Body profiles, system type,
    # relationships, canonical metadata/provenance and all electrical entities.
    assert reopened.by_guid(products[0].GlobalId).is_a() == 'IfcSanitaryTerminal'
    assert reopened.by_guid(ports[0].GlobalId).ConnectedTo[0].RelatedPort.GlobalId == ports[2].GlobalId


@pytest.mark.parametrize('change', ['name', 'placement', 'shape', 'connectivity', 'delete'])
def test_preservation_check_rejects_existing_graph_changes(source, change):
    _, native, products, ports = source
    before = source_snapshot(native)
    if change == 'name':
        products[0].Name = 'Changed'
    elif change == 'placement':
        products[0].ObjectPlacement.RelativePlacement.Location.Coordinates = (8., 9., 10.)
    elif change == 'shape':
        products[0].Representation.Representations[0].Items[0].Depth = 9.
    elif change == 'connectivity':
        native.remove(ports[0].ConnectedTo[0])
    else:
        native.remove(products[0])
    with pytest.raises(ValueError, match='preservation failed'):
        verify_source_snapshot(before, native)


def test_preservation_check_allows_only_new_entities(source):
    _, native, _, _ = source
    before = source_snapshot(native)
    ifcopenshell.api.root.create_entity(native, ifc_class='IfcAnnotation')
    verify_source_snapshot(before, native)


@pytest.mark.parametrize('kind', [
    'IfcAnnotation', 'IfcDocumentReference', 'IfcLibraryInformation',
    'IfcImageTexture', 'IfcPixelTexture', 'IfcBlobTexture',
    'IfcTextLiteral', 'IfcExternallyDefinedSurfaceStyle',
])
def test_external_and_existing_presentation_entities_are_refused(source, kind):
    model, native, _, _ = source
    native.create_entity(kind)
    with pytest.raises(ValueError, match='unsupported presentation'):
        validate_extension_source(native, model)


@pytest.mark.parametrize('name', ['EPset_Drawing', 'epset_annotation', 'BBIM_Array', 'bbim_custom'])
def test_runtime_property_sets_are_refused(source, name):
    model, native, products, _ = source
    ifcopenshell.api.pset.add_pset(native, product=products[0], name=name)
    with pytest.raises(ValueError, match='runtime property sets'):
        validate_extension_source(native, model)


def test_uri_typed_property_is_refused(source):
    model, native, _, _ = source
    native.create_entity('IfcPropertySingleValue', Name='not-a-resource-permission',
                         NominalValue=native.create_entity('IfcURIReference', 'https://example.invalid/a'))
    with pytest.raises(ValueError, match='URI reference'):
        validate_extension_source(native, model)


def test_unmapped_physical_objects_and_orphan_ports_are_refused(source):
    model, native, _, _ = source
    extra = ifcopenshell.api.root.create_entity(native, ifc_class='IfcFurnishingElement')
    with pytest.raises(ValueError, match='unsupported noncanonical'):
        validate_extension_source(native, model)
    native.remove(extra)
    ifcopenshell.api.system.add_port(native)
    with pytest.raises(ValueError, match='exactly one supported plumbing owner'):
        validate_extension_source(native, model)


def test_native_plumbing_cannot_spoof_canonical_metadata(source):
    model, native, products, _ = source
    ifcopenshell.api.pset.add_pset(native, product=products[0], name='OABM_Canonical')
    with pytest.raises(ValueError, match='masquerade'):
        validate_extension_source(native, model)


def test_canonical_disagreement_is_not_repaired_or_replaced(source):
    model, native, _, _ = source
    device = native.by_guid(canonical_id_to_ifc_guid(model.electrical_devices[0].id))
    device.Name = 'different canonical fact'
    with pytest.raises(ValueError, match='does not match'):
        validate_extension_source(native, model)


def test_opaque_presentation_is_never_in_renderer_and_is_retained_in_final(source):
    model, original, _, _ = source
    old = ifcopenshell.api.root.create_entity(original, ifc_class='IfcAnnotation')
    old.ObjectType = 'TEXT'
    pset = ifcopenshell.api.pset.add_pset(original, product=old, name='EPset_Annotation')
    ifcopenshell.api.pset.edit_pset(original, pset=pset, properties={
        'Symbols': '/private/not-authorized.svg', 'Stylesheet': 'https://example.invalid/no-network.css',
        'Literal': '{{untrusted-template}}'})
    doc = original.create_entity('IfcDocumentReference', Location='file:///private/not-authorized.svg')
    before = source_snapshot(original)
    render, baseline, diagnostics = prepare_render_source(original, model)
    assert source_snapshot(original) == before
    assert not render.by_type('IfcAnnotation')
    assert not render.by_type('IfcDocumentReference')
    assert not any((p.Name or '').startswith('EPset_') for p in render.by_type('IfcPropertySet'))
    assert diagnostics[-1]['status'] == 'opaque_presentation_preserved_not_rendered'
    new = ifcopenshell.api.root.create_entity(render, ifc_class='IfcAnnotation')
    new.Name = 'Trusted generated label'
    ifcopenshell.api.context.add_context(render, context_type='Plan')
    final = merge_render_additions(original, baseline, render)
    verify_source_snapshot(before, final, allow_drawing_context_additions=True)
    assert final.by_id(doc.id()).Location == 'file:///private/not-authorized.svg'
    assert final.by_guid(old.GlobalId).ObjectType == 'TEXT'
    assert final.by_guid(new.GlobalId).Name == 'Trusted generated label'
    assert len(final.by_type('IfcAnnotation')) == 2


@pytest.mark.parametrize('change', ['replace', 'remove', 'reorder', 'name', 'wrong_context_type'])
def test_only_append_only_plan_context_association_is_exempt(source, change):
    _, native, _, _ = source
    project = native.by_type('IfcProject')[0]
    ifcopenshell.api.context.add_context(native, context_type='Plan')
    before = source_snapshot(native)
    added = ifcopenshell.api.context.add_context(native, context_type='Plan')
    original = project.RepresentationContexts[:-1]
    if change == 'replace':
        project.RepresentationContexts = (added,)
    elif change == 'remove':
        project.RepresentationContexts = (*original[1:], added)
    elif change == 'reorder':
        project.RepresentationContexts = (*reversed(original), added)
    elif change == 'name':
        project.Name = 'not a presentation context change'
    else:
        added.ContextType = 'Model'
    with pytest.raises(ValueError, match='preservation failed'):
        verify_source_snapshot(before, native, allow_drawing_context_additions=True)


def test_empty_native_geometry_is_refused(source):
    model, native, products, _ = source
    products[0].Representation.Representations = ()
    with pytest.raises(ValueError, match='nonempty geometry'):
        validate_extension_source(native, model)


def test_explicit_curve_only_plumbing_records_body_omission(source):
    model, native, products, _ = source
    representation = products[-1].Representation.Representations[0]
    representation.RepresentationIdentifier = 'Axis'
    representation.RepresentationType = 'Curve3D'
    representation.Items = (native.create_entity('IfcPolyline', Points=(
        native.create_entity('IfcCartesianPoint', Coordinates=(0., 0., 0.)),
        native.create_entity('IfcCartesianPoint', Coordinates=(0., 0., 1.)))),)
    rows = validate_extension_source(native, model)
    assert [row['ifc_guid'] for row in rows if row['status'] == 'native_body_omitted'] == [products[-1].GlobalId]


def test_new_inverse_connectivity_cannot_bypass_existing_entity_snapshot(source):
    model, native, _, ports = source
    render, baseline, _ = prepare_render_source(native, model)
    render.create_entity('IfcRelConnectsPorts', GlobalId=canonical_id_to_ifc_guid('unexpected:new-connection'),
                         RelatingPort=render.by_id(ports[1].id()), RelatedPort=render.by_id(ports[2].id()))
    # Every original serialized entity is unchanged, but this new relationship
    # would change its inverse semantics; graft must still refuse it.
    verify_source_snapshot(baseline, render)
    with pytest.raises(ValueError, match='unsupported reference into the source graph'):
        merge_render_additions(native, baseline, render)


@pytest.mark.parametrize('index', [0, 1])
def test_geometry_absent_terminal_is_explicit_not_invented(source, index):
    model, native, products, _ = source
    products[index].Representation = None
    rows = validate_extension_source(native, model)
    omissions = [row for row in rows if row['status'] == 'native_body_omitted']
    assert len(omissions) == 1
    assert omissions[0]['ifc_guid'] == products[index].GlobalId
    assert 'no geometry' in omissions[0]['reason']
    assert products[index].Representation is None


def test_geometry_absent_pipe_is_refused(source):
    model, native, products, _ = source
    products[2].Representation = None
    with pytest.raises(ValueError, match='pipe/fitting requires explicit geometry'):
        validate_extension_source(native, model)


@pytest.mark.parametrize('kind', ['styled_item', 'material_representation', 'geometry_wrapper'])
def test_new_nonrelationship_inverse_edges_cannot_reinterpret_old_geometry(source, kind):
    model, native, products, _ = source
    render, baseline, _ = prepare_render_source(native, model)
    old_representation = render.by_id(products[0].Representation.Representations[0].id())
    if kind == 'styled_item':
        render.create_entity('IfcStyledItem', Item=old_representation.Items[0], Styles=())
    elif kind == 'material_representation':
        render.create_entity('IfcMaterialDefinitionRepresentation', Representations=(old_representation,),
                             RepresentedMaterial=render.create_entity('IfcMaterial', Name='Synthetic new material'))
    else:
        render.create_entity('IfcShapeRepresentation', ContextOfItems=old_representation.ContextOfItems,
                             RepresentationIdentifier='Body', RepresentationType='SweptSolid',
                             Items=old_representation.Items)
    verify_source_snapshot(baseline, render)
    with pytest.raises(ValueError, match='unsupported reference into the source graph'):
        merge_render_additions(native, baseline, render)
