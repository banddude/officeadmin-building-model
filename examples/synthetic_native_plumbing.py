"""Entirely synthetic IFC extension proof, not a plumbing design or product model.

Run: python examples/synthetic_native_plumbing.py /tmp/new-synthetic-plumbing
The intentionally simple solids are supplied synthetic envelopes. The open waste
outlet stops above the receptor: no false port connection bridges the air gap.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.placement
import numpy as np

from oabm.ifc import canonical_id_to_ifc_guid, to_ifc
from oabm.model import BuildingModel

ROOT = Path(__file__).resolve().parents[1]


def build_fixture(destination: Path) -> tuple[Path, Path, Path]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    model_path = destination / 'model.json'
    native_sink_guid = canonical_id_to_ifc_guid('synthetic-plumbing:stable-sink')
    model_text = (ROOT / 'examples/synthetic-native-bonsai-model.json').read_text()
    model_path.write_text(model_text)
    model = BuildingModel.load(model_path)
    f = to_ifc(model)
    original_ids = {item.id() for item in f.by_type('IfcRoot')}
    body = next(c for c in f.by_type('IfcGeometricRepresentationSubContext') if c.ContextIdentifier == 'Body')
    level = f.by_guid(canonical_id_to_ifc_guid('level:ground'))

    def product(cls, name, kind, xyz, size):
        p = ifcopenshell.api.run('root.create_entity', f, ifc_class=cls, name=name, predefined_type=kind)
        matrix = np.eye(4)
        matrix[:3, 3] = xyz
        ifcopenshell.api.run('geometry.edit_object_placement', f, product=p, matrix=matrix)
        ifcopenshell.api.run('spatial.assign_container', f, products=[p], relating_structure=level)
        sx, sy, sz = size
        profile = f.create_entity('IfcRectangleProfileDef', ProfileType='AREA', XDim=sx, YDim=sy)
        solid = f.create_entity('IfcExtrudedAreaSolid', SweptArea=profile,
            Position=f.create_entity('IfcAxis2Placement3D', Location=f.create_entity('IfcCartesianPoint', Coordinates=(0., 0., 0.))),
            ExtrudedDirection=f.create_entity('IfcDirection', DirectionRatios=(0., 0., 1.)), Depth=sz)
        representation = f.create_entity('IfcShapeRepresentation', ContextOfItems=body,
            RepresentationIdentifier='Body', RepresentationType='SweptSolid', Items=[solid])
        p.Representation = f.create_entity('IfcProductDefinitionShape', Representations=[representation])
        ps = ifcopenshell.api.run('pset.add_pset', f, product=p, name='Synthetic_Plumbing_Evidence')
        ifcopenshell.api.run('pset.edit_pset', f, pset=ps, properties={
            'Source': 'Public synthetic fixture only', 'GeometryBasis': 'Supplied synthetic envelope',
            'EngineeringSize': 'UNKNOWN - not a selected product or design',
            'FieldLocation': 'UNKNOWN - synthetic placement only'})
        return p

    sink = product('IfcSanitaryTerminal', 'Synthetic sink envelope', 'SINK', (1., 1., .8), (.8, .5, .2))
    receptor = product('IfcWasteTerminal', 'Synthetic indirect receptor envelope', 'FLOORTRAP', (2.3, 1., .02), (.4, .4, .12))
    # Pipe bodies are supplied synthetic circular extrusions, not engineering sizes.
    pipes = {}
    for name, system, xyz, length in [
        ('cold', 'DOMESTICCOLDWATER', (.8, 1., .2), .6),
        ('hot', 'DOMESTICHOTWATER', (1.2, 1., .2), .6),
        ('waste', 'DRAINAGE', (2.3, 1., .3), .5),
    ]:
        pipe = product('IfcPipeSegment', 'Synthetic ' + name, 'RIGIDSEGMENT', xyz, (.03, .03, length))
        solid = pipe.Representation.Representations[0].Items[0]
        old = solid.SweptArea
        solid.SweptArea = f.create_entity('IfcCircleProfileDef', ProfileType='AREA', Radius=.015)
        if name == 'waste':
            # A supplied diagonal synthetic span reaches the sink instead of
            # inventing a connection at a point outside its envelope.
            solid.Position.Axis = f.create_entity('IfcDirection', DirectionRatios=(-1.1, 0., .5))
            solid.Depth = math.sqrt(1.1**2 + .5**2)
        f.remove(old)
        group = ifcopenshell.api.run('root.create_entity', f, ifc_class='IfcDistributionSystem', name='Synthetic ' + name + ' system', predefined_type=system)
        ifcopenshell.api.run('group.assign_group', f, products=[pipe, sink] if name != 'waste' else [pipe, sink, receptor], group=group)
        ports = []
        for endpoint, z in [('start', xyz[2]), ('end', xyz[2] + length)]:
            port = ifcopenshell.api.run('system.add_port', f, element=pipe)
            port.Name = 'Synthetic ' + name + ' ' + endpoint
            port.PredefinedType = 'PIPE'
            port.SystemType = system
            port.FlowDirection = 'SOURCEANDSINK'
            matrix = np.eye(4); matrix[:3, 3] = (1.2 if name == 'waste' and endpoint == 'end' else xyz[0], xyz[1], z)
            ifcopenshell.api.run('geometry.edit_object_placement', f, product=port, matrix=matrix)
            ports.append(port)
        sink_port = ifcopenshell.api.run('system.add_port', f, element=sink)
        sink_port.Name = 'Synthetic sink ' + name
        sink_port.PredefinedType = 'PIPE'; sink_port.SystemType = system
        sink_port.FlowDirection = 'SOURCEANDSINK'
        matrix = np.eye(4); matrix[:3, 3] = (1.2 if name == 'waste' else xyz[0], xyz[1], xyz[2] + length)
        ifcopenshell.api.run('geometry.edit_object_placement', f, product=sink_port, matrix=matrix)
        ifcopenshell.api.run('system.connect_port', f, port1=ports[1], port2=sink_port, direction='SINK' if name == 'waste' else 'SOURCE')
        pipes[name] = pipe
    # The receptor has an explicitly unconnected receiving port. A relationship
    # crossing the open air gap would assert a false physical connection.
    receptor_port = ifcopenshell.api.run('system.add_port', f, element=receptor)
    receptor_port.Name = 'Open indirect receiving point'
    receptor_port.PredefinedType = 'PIPE'; receptor_port.SystemType = 'DRAINAGE'
    receptor_port.FlowDirection = 'SINK'
    matrix = np.eye(4); matrix[:3, 3] = (2.3, 1., .14)
    ifcopenshell.api.run('geometry.edit_object_placement', f, product=receptor_port, matrix=matrix)
    # Deterministic public fixture IDs; no source/customer data is used.
    for index, entity in enumerate(e for e in f.by_type('IfcRoot') if e.id() not in original_ids):
        entity.GlobalId = canonical_id_to_ifc_guid(f'synthetic-plumbing:{entity.is_a()}:{index}')
    sink.GlobalId = native_sink_guid
    # API collection assembly uses sets; make the supplied fixture order explicit.
    for relation in f.by_type('IfcRelContainedInSpatialStructure'):
        relation.RelatedElements = sorted(relation.RelatedElements, key=lambda e: e.GlobalId)
    for class_name in ('IfcRelNests', 'IfcRelAssignsToGroup'):
        for relation in f.by_type(class_name):
            relation.RelatedObjects = sorted(relation.RelatedObjects, key=lambda e: e.GlobalId)
    source_path = destination / 'source.ifc'
    f.write(str(source_path))
    request_text = (ROOT / 'examples/synthetic-native-bonsai-request.json').read_text()
    request = json.loads(request_text)
    request['version'] = 'native-bonsai-ifc-v1'
    request['views'] = [request['views'][0]]
    view = request['views'][0]
    view['id'] = 'plumbing_plan'
    view['annotations'] = []
    view['native_annotations'] = [
        {'source_guid': sink.GlobalId, 'text': 'SINK - SIZE UNVERIFIED', 'label_offset_mm': [4, -20]},
        {'source_guid': receptor.GlobalId, 'text': 'INDIRECT RECEPTOR', 'label_offset_mm': [-2, 10]},
        {'source_guid': pipes['waste'].GlobalId, 'text': 'AIR GAP INTENT', 'label_offset_mm': [-2, -8]},
    ]
    request_path = destination / 'request.json'
    request_path.write_text(json.dumps(request, indent=2) + '\n')
    return model_path, source_path, request_path


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: synthetic_native_plumbing.py NEW_OUTPUT_DIRECTORY')
    for path in build_fixture(Path(sys.argv[1])):
        print(path)
