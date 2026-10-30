"""Synthetic extension preservation: graph facts, real reopen, and native PLAN_VIEW."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import ifcopenshell.util.placement
import pytest

from oabm.ifc import IfcAdapterError, from_ifc, round_trip_differences
from oabm.ifc.adapter import _from_ifc_canonical_subset
from oabm.model import BuildingModel
from oabm.native_bonsai import NativeRuntime, export_native_ifc, validate_ifc_request

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('synthetic_native_plumbing', ROOT / 'examples/synthetic_native_plumbing.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
build_fixture = _fixture.build_fixture


def plumbing_facts(f):
    """Independent numerical/semantic oracle, not just object counts."""
    settings = ifcopenshell.geom.settings()
    settings.set('use-world-coords', True)
    def vertices(product):
        # IfcOpenShell's geometry wrapper borrows storage owned by shape.
        shape = ifcopenshell.geom.create_shape(settings, product)
        return list(shape.geometry.verts)

    products = f.by_type('IfcPipeSegment') + f.by_type('IfcSanitaryTerminal') + f.by_type('IfcWasteTerminal')
    return {
        'products': {p.GlobalId: {
            'class': p.is_a(), 'type': p.PredefinedType,
            'placement': ifcopenshell.util.placement.get_local_placement(p.ObjectPlacement).tolist(),
            'vertices': vertices(p),
            'properties': {k: v for k, v in ifcopenshell.util.element.get_pset(p, 'Synthetic_Plumbing_Evidence').items() if k != 'id'},
        } for p in products},
        'ports': {p.GlobalId: (p.Name, p.PredefinedType, p.SystemType, p.FlowDirection,
            ifcopenshell.util.placement.get_local_placement(p.ObjectPlacement).tolist())
            for p in f.by_type('IfcDistributionPort') if p.PredefinedType == 'PIPE'},
        'links': sorted((r.GlobalId, r.RelatingPort.GlobalId, r.RelatedPort.GlobalId) for r in f.by_type('IfcRelConnectsPorts')),
        'systems': {s.GlobalId: (s.PredefinedType, sorted(p.GlobalId for rel in s.IsGroupedBy for p in rel.RelatedObjects))
            for s in f.by_type('IfcDistributionSystem') if s.PredefinedType in ('DOMESTICCOLDWATER', 'DOMESTICHOTWATER', 'DRAINAGE')},
    }


def test_synthetic_plumbing_source_is_explicit_and_canonical_conversion_refuses(tmp_path):
    model_path, source, request = build_fixture(tmp_path / 'fixture')
    f = ifcopenshell.open(str(source))
    original = source.read_bytes()
    facts = plumbing_facts(f)
    assert len(facts['products']) == 5
    assert len(facts['links']) == 3
    assert {v[0] for v in facts['systems'].values()} == {'DOMESTICCOLDWATER', 'DOMESTICHOTWATER', 'DRAINAGE'}
    assert all(v['properties']['EngineeringSize'].startswith('UNKNOWN') for v in facts['products'].values())
    open_port = next(p for p in f.by_type('IfcDistributionPort') if p.Name == 'Open indirect receiving point')
    assert not open_port.ConnectedTo and not open_port.ConnectedFrom
    with pytest.raises(IfcAdapterError):
        from_ifc(f)
    assert source.read_bytes() == original
    assert round_trip_differences(BuildingModel.load(model_path), _from_ifc_canonical_subset(f)) == ()
    validate_ifc_request(json.loads(request.read_text()), BuildingModel.load(model_path), f)


@pytest.mark.parametrize('mutation', [
    lambda r: r['views'][0]['native_annotations'][0].update(text='{unsafe}'),
    lambda r: r['views'][0]['native_annotations'][0].update(source_guid='missing'),
    lambda r: r['views'][0]['native_annotations'][0].update(script='not data'),
    lambda r: r['views'][0].update(target_view='REFLECTED_PLAN_VIEW'),
    lambda r: r['views'][0]['native_annotations'].append(copy.deepcopy(r['views'][0]['native_annotations'][0])),
])
def test_native_annotations_are_strict(tmp_path, mutation):
    model_path, source, request = build_fixture(tmp_path / 'fixture')
    data = json.loads(request.read_text())
    mutation(data)
    with pytest.raises(ValueError):
        validate_ifc_request(data, BuildingModel.load(model_path), ifcopenshell.open(str(source)))


def test_actual_native_plumbing_plan_preserves_geometry_semantics_and_links(tmp_path):
    blender, bonsai = os.environ.get('OABM_TEST_BLENDER'), os.environ.get('OABM_TEST_BONSAI_PYTHON')
    if not blender or not bonsai:
        pytest.skip('explicit installed Blender/Bonsai runtime not supplied; native proof not executed')
    model_path, source, request_path = build_fixture(tmp_path / 'fixture')
    before = plumbing_facts(ifcopenshell.open(str(source)))
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = export_native_ifc(model_path, source, request_path, tmp_path / 'delivery', NativeRuntime(Path(blender), Path(bonsai)))
    receipt = json.loads((result / 'receipt.json').read_text())
    assert receipt['native_source_preservation']['verified'] is True
    assert receipt['canonical_roundtrip']['numerical_differences'] == []
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert receipt['input_sha256']['source.ifc'] == source_digest
    reopened = ifcopenshell.open(str(result / 'model.ifc'))
    assert plumbing_facts(reopened) == before
    assert round_trip_differences(BuildingModel.load(model_path), _from_ifc_canonical_subset(reopened)) == ()
    view = receipt['views']['plumbing_plan']
    svg = ET.parse(result / view['svg']).getroot()
    assert svg.attrib['data-scale'] == '1:48'
    svg_text = ' '.join(svg.itertext())
    assert 'AIR GAP INTENT' in svg_text
    assert 'INDIRECT RECEPTOR' in svg_text
    requested = json.loads(request_path.read_text())['views'][0]['native_annotations']
    for ann in requested:
        matching = [a for a in view['annotations'] if a['product_guid'] == ann['source_guid'] and a['text'] == ann['text']]
        assert len(matching) == 1
        native_annotation = reopened.by_guid(matching[0]['annotation_guid'])
        assert any(rel.RelatingProduct.GlobalId == ann['source_guid'] for rel in native_annotation.HasAssignments if rel.is_a('IfcRelAssignsToProduct'))
    # A supported extension delivery is intentionally not a canonical-only export.
    with pytest.raises(IfcAdapterError):
        from_ifc(reopened)


def test_synthetic_source_is_repeatable_with_an_explicit_unconnected_air_gap(tmp_path):
    first = build_fixture(tmp_path / 'first')
    second = build_fixture(tmp_path / 'second')
    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_bytes() == second[1].read_bytes()
    assert first[2].read_bytes() == second[2].read_bytes()
    f = ifcopenshell.open(str(first[1]))
    out = next(p for p in f.by_type('IfcDistributionPort') if p.Name == 'Synthetic waste start')
    receiving = next(p for p in f.by_type('IfcDistributionPort') if p.Name == 'Open indirect receiving point')
    out_xyz = ifcopenshell.util.placement.get_local_placement(out.ObjectPlacement)[:3, 3]
    receiving_xyz = ifcopenshell.util.placement.get_local_placement(receiving.ObjectPlacement)[:3, 3]
    assert out_xyz[:2] == pytest.approx(receiving_xyz[:2])
    assert out_xyz[2] - receiving_xyz[2] == pytest.approx(.16)
    assert not out.ConnectedTo and not out.ConnectedFrom
    assert not receiving.ConnectedTo and not receiving.ConnectedFrom



def test_native_source_rejects_malformed_guid_that_could_collide_with_canonical_id(tmp_path):
    model_path, source, request_path = build_fixture(tmp_path / 'fixture')
    f = ifcopenshell.open(str(source))
    f.by_type('IfcSanitaryTerminal')[0].GlobalId = 'NativePlumbingConflict'
    from oabm.native_bonsai.extensions import validate_extension_source
    with pytest.raises(ValueError, match='GlobalId|GUID'):
        validate_extension_source(f, BuildingModel.load(model_path))


def test_curve_only_pipe_requires_exact_native_body_omission_acknowledgment(tmp_path):
    model_path, source, request_path = build_fixture(tmp_path / 'fixture')
    f = ifcopenshell.open(str(source))
    pipe = f.by_type('IfcPipeSegment')[0]
    context = next(c for c in f.by_type('IfcGeometricRepresentationSubContext') if c.ContextIdentifier == 'Axis')
    curve = f.create_entity('IfcPolyline', Points=[
        f.create_entity('IfcCartesianPoint', Coordinates=(0., 0., 0.)),
        f.create_entity('IfcCartesianPoint', Coordinates=(0., 0., .6)),
    ])
    axis = f.create_entity('IfcShapeRepresentation', ContextOfItems=context,
        RepresentationIdentifier='Axis', RepresentationType='Curve3D', Items=[curve])
    pipe.Representation = f.create_entity('IfcProductDefinitionShape', Representations=[axis])
    model = BuildingModel.load(model_path)
    request = json.loads(request_path.read_text())
    before = f.to_string()
    with pytest.raises(ValueError, match='acknowledged_native_body_omissions'):
        validate_ifc_request(request, model, f)
    request['acknowledged_native_body_omissions'] = [pipe.GlobalId]
    validate_ifc_request(request, model, f)
    assert f.to_string() == before
    from oabm.native_bonsai.extensions import validate_extension_source
    diagnostics = validate_extension_source(f, model)
    assert any(d.get('ifc_guid', d.get('guid')) == pipe.GlobalId and 'body' in str(d).lower() for d in diagnostics)
    for incorrect in [[], [pipe.GlobalId, pipe.GlobalId], [f.by_type('IfcSanitaryTerminal')[0].GlobalId]]:
        wrong = copy.deepcopy(request)
        wrong['acknowledged_native_body_omissions'] = incorrect
        with pytest.raises(ValueError, match='acknowledged_native_body_omissions'):
            validate_ifc_request(wrong, model, f)
