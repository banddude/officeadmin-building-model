"""Explicit tessellated assembly export and fail-closed native verification.

Physical children retain canonical identity. Tessellation is a derived display
representation, not a parametric manufacturer model. Native shape/placement or
parent edits are refused rather than silently retaining incompatible metadata.
"""
from __future__ import annotations
import numpy as np
import ifcopenshell.util.placement
import ifcopenshell.util.unit
from oabm.assembly_mesh import assembly_component_mesh


def assign_component_body(ifc, product, context, component):
    vertices, indices = assembly_component_mesh(component)
    coords = ifc.create_entity('IfcCartesianPointList3D', CoordList=vertices)
    faces = ifc.create_entity('IfcTriangulatedFaceSet', Coordinates=coords,
        CoordIndex=[tuple(indices[i+j]+1 for j in range(3)) for i in range(0,len(indices),3)],
        Closed=None)
    representation = ifc.create_entity('IfcShapeRepresentation', ContextOfItems=context,
        RepresentationIdentifier='Body', RepresentationType='Tessellation', Items=[faces])
    import ifcopenshell.api.geometry
    ifcopenshell.api.geometry.assign_representation(ifc,product=product,representation=representation)


def validate_native_assemblies(model, canonical_items, error_type):
    """Compare native world geometry and exact parent membership to source facts."""
    for assembly in model.building_assemblies:
        product=canonical_items[assembly.id][0]
        if product.Name != assembly.name:
            raise error_type(f'{assembly.id}: native assembly name edited; update canonical name')
        if not product.is_a('IfcElementAssembly'):
            raise error_type(f'{assembly.id}: expected IfcElementAssembly')
        if product.Representation is not None:
            raise error_type(f'{assembly.id}: aggregate representation is unsupported; physical geometry belongs only to canonical children')
        placement=ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
        if not np.allclose(placement,np.eye(4),atol=1e-8,rtol=0):
            raise error_type(f'{assembly.id}: native aggregate placement edited; regenerate from canonical geometry')
        children=[child for rel in product.IsDecomposedBy for child in rel.RelatedObjects]
        expected={canonical_items[cid][0].id() for cid in assembly.component_ids}
        if len(children)!=len(expected) or {child.id() for child in children}!=expected:
            raise error_type(f'{assembly.id}: native assembly membership differs from canonical component IDs')
    for assembly in model.building_assemblies:
        if assembly.host_representation != "aggregate_host":
            continue
        host = canonical_items[assembly.host_id][0]
        wall_ids = {wall.id for wall in model.walls}
        ceiling_ids = {ceiling.id for ceiling in model.ceilings}
        expected_class = "IfcWall" if assembly.host_id in wall_ids else "IfcCovering" if assembly.host_id in ceiling_ids else None
        if expected_class is None or not host.is_a(expected_class):
            raise error_type(f'{assembly.id}: unsupported aggregate host native class')
        representations = host.Representation.Representations if host.Representation else ()
        # Aggregate hosts retain only their authoritative source Axis. Any Body
        # (or a disguised extra solid/surface representation) duplicates the
        # physical children which explicitly replace the host envelope.
        if (len(representations) != 1 or representations[0].RepresentationIdentifier != "Axis"
            or representations[0].RepresentationType != "Curve3D" or len(representations[0].Items) != 1
            or not representations[0].Items[0].is_a("IfcPolyline")):
            raise error_type(f'{assembly.id}: aggregate host must retain exactly its source Axis; native Body/extra geometry is unsupported')
    for component in model.assembly_components:
        product=canonical_items[component.id][0]
        if product.Name != (component.name or component.part_key):
            raise error_type(f'{component.id}: native component label edited; update canonical name')
        if not product.is_a('IfcBuildingElementProxy'):
            raise error_type(f'{component.id}: expected physical assembly component proxy')
        parents=[rel.RelatingObject for rel in product.Decomposes]
        if len(parents)!=1 or parents[0]!=canonical_items[component.assembly_id][0]:
            raise error_type(f'{component.id}: native assembly parent differs')
        reps=product.Representation.Representations if product.Representation else ()
        if len(reps)!=1 or reps[0].RepresentationIdentifier!='Body' or reps[0].RepresentationType!='Tessellation' or len(reps[0].Items)!=1 or not reps[0].Items[0].is_a('IfcTriangulatedFaceSet'):
            raise error_type(f'{component.id}: expected one canonical tessellated Body')
        face=reps[0].Items[0]
        vertices,indices=assembly_component_mesh(component)
        wanted=[tuple(indices[i+j]+1 for j in range(3)) for i in range(0,len(indices),3)]
        if list(face.CoordIndex)!=wanted or face.PnIndex is not None:
            raise error_type(f'{component.id}: native assembly topology differs')
        coords=np.asarray(face.Coordinates.CoordList,dtype=float)
        if coords.shape!=(len(vertices),3) or not np.isfinite(coords).all():
            raise error_type(f'{component.id}: invalid native assembly coordinates')
        matrix=ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
        world=(matrix@np.column_stack((coords,np.ones(len(coords)))).T).T[:,:3]
        world*=ifcopenshell.util.unit.calculate_unit_scale(product.file)
        if not np.allclose(world,np.asarray(vertices),atol=1e-8,rtol=0):
            raise error_type(f'{component.id}: native assembly geometry/placement edited; update canonical geometry and regenerate')
    _validate_emitted_geometry_graph(model, canonical_items, error_type)


def _same_native_value(actual, expected):
    """ID-free native shape graph equality, with metre/rotation roundoff only."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(
            _same_native_value(actual[key], value) for key, value in expected.items())
    if isinstance(expected, (tuple, list)):
        return isinstance(actual, (tuple, list)) and len(actual) == len(expected) and all(
            _same_native_value(a, b) for a, b in zip(actual, expected))
    if type(expected) is float:
        return type(actual) in (int, float) and np.isfinite(actual) and abs(actual - expected) <= 1e-8
    return type(actual) is type(expected) and actual == expected


def _native_geometry_graph(product, error_type):
    if product.ObjectPlacement is None:
        raise error_type('assembly-related native product requires its exported placement')
    def info(entity):
        def geometry_only(value):
            if isinstance(value,dict):
                return {key:geometry_only(child) for key,child in value.items() if key not in {'Name','Description','ProfileName','TagList'}}
            if isinstance(value,(tuple,list)):return tuple(geometry_only(child) for child in value)
            return value
        return geometry_only(entity.get_info(recursive=True, include_identifier=False)) if entity is not None else None
    relationships = {}
    singular = {'ContainedInStructure': 'RelatingStructure', 'Decomposes': 'RelatingObject',
        'Nests': 'RelatingObject', 'HasOpenings': 'RelatedOpeningElement',
        'VoidsElements': 'RelatingBuildingElement', 'HasFillings': 'RelatedBuildingElement',
        'FillsVoids': 'RelatingOpeningElement'}
    for inverse, attribute in singular.items():
        relationships[inverse] = sorted(str(getattr(getattr(rel, attribute), 'GlobalId', None))
            for rel in getattr(product, inverse, ()))
    for inverse in ('IsDecomposedBy', 'IsNestedBy'):
        relationships[inverse] = sorted(tuple(sorted(str(getattr(child, 'GlobalId', None))
            for child in rel.RelatedObjects)) for rel in getattr(product, inverse, ()))
    return {'class': product.is_a(), 'placement': info(product.ObjectPlacement),
        'representation': info(product.Representation), 'relationships': relationships}


def _validate_emitted_geometry_graph(model, canonical_items, error_type):
    """Audit every native geometry path in the bounded assembly source closure.

    Reuse the existing IFC author, rather than maintaining separate approximated
    readers for wall axes, ceiling axes, opening voids and deck solids. Inverse
    void/fill/containment/decomposition links are checked as well as forward
    geometry. This is deliberately stricter than ordinary legacy Body handling:
    the opt-in physical assembly slice is source-controlled, not parametric edit.
    Presentation styles/groups remain outside canonical geometry.
    """
    from .adapter import to_ifc, canonical_id_to_ifc_guid
    from oabm.model.assembly_validation import assembly_input_source_records
    target_ids = {item.id for item in (*model.building_assemblies, *model.assembly_components)}
    for assembly in model.building_assemblies:
        # Consume the digest's dependency enumerator, not a parallel hand-built
        # list: this includes levels and every wall opening, with or without an
        # opening-insert assignment. Future digest dependencies relay here too.
        target_ids.update(source.id for source in assembly_input_source_records(model,assembly))
        if assembly.deck_id:target_ids.add(assembly.deck_id)
    for component in model.assembly_components:
        if component.hanger_attachment:
            target_ids.update((component.hanger_attachment.top_host_id, component.hanger_attachment.bottom_runner_id))
    native = canonical_items[next(iter(target_ids))][0].file
    units = native.by_type('IfcProject')[0].UnitsInContext
    lengths = [unit for unit in units.Units if unit.UnitType == 'LENGTHUNIT'] if units else []
    if len(lengths) != 1 or not lengths[0].is_a('IfcSIUnit') or lengths[0].Name != 'METRE' or lengths[0].Prefix is not None:
        raise error_type('physical assembly reimport requires the exported metre length unit')
    reference = to_ifc(model, assembly_geometry='tessellated')
    for identity in sorted(target_ids):
        actual = _native_geometry_graph(canonical_items[identity][0], error_type)
        expected = _native_geometry_graph(reference.by_guid(canonical_id_to_ifc_guid(identity)), error_type)
        if not _same_native_value(actual, expected):
            raise error_type(f'{identity}: native assembly/source geometry, placement or structural relationship differs; update canonical facts and regenerate')
