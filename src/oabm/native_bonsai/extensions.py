"""Fail-closed preservation boundary for local, self-contained native additions.

This is not a plumbing importer or a second canonical model. The native source
is retained as the authoritative extension graph; the canonical projection is
used only to check the separately supplied canonical document.
"""
from __future__ import annotations

import re
import math

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.unit

from oabm.ifc import round_trip_differences, to_ifc
from oabm.ifc.adapter import CANONICAL_PSET, _from_ifc_canonical_subset


PLUMBING_CLASSES = frozenset({
    'IfcPipeSegment', 'IfcPipeFitting', 'IfcSanitaryTerminal', 'IfcWasteTerminal',
})
_PLUMBING_TYPES = frozenset(name + 'Type' for name in PLUMBING_CLASSES)
_SYSTEM_TYPES = frozenset({
    'DOMESTICCOLDWATER', 'DOMESTICHOTWATER', 'DRAINAGE', 'SEWAGE',
    'WASTEWATER', 'WATERSUPPLY', 'RAINWATER', 'STORMWATER', 'NOTDEFINED',
})
_GUID = re.compile(r'[0-3][0-9A-Za-z_$]{21}')


class SourceSnapshot(dict):
    """Exact entity serialization plus original project context references."""
    def __init__(self, native):
        super().__init__((entity.id(), entity.to_string()) for entity in native)
        self.project_contexts = {project.id(): tuple(context.id() for context in project.RepresentationContexts or ())
                                 for project in native.by_type('IfcProject')}


def source_snapshot(native: ifcopenshell.file) -> dict[int, str]:
    """Snapshot every existing STEP entity, not merely product counts or GUIDs.

IFC entity IDs are deliberately part of this conservative preservation check.
A runtime which renumbers or changes any existing entity is unsupported, even
if its output might be geometrically equivalent. New presentation entities are
allowed. Original placements, properties, classes, IDs and all relationships
must remain exactly intact, including non-root geometry/resource entities.
"""
    return SourceSnapshot(native)


def verify_source_snapshot(snapshot: dict[int, str], saved: ifcopenshell.file,
                           *, allow_drawing_context_additions: bool = False) -> None:
    current = source_snapshot(saved)
    changed = [key for key, value in snapshot.items() if current.get(key) != value]
    if changed and allow_drawing_context_additions:
        projects = getattr(snapshot, 'project_contexts', {})
        for key in tuple(changed):
            if key not in projects or key not in current:
                continue
            project = saved.by_id(key)
            if project.is_a() != 'IfcProject':
                continue
            original_ids = projects[key]
            contexts = tuple(project.RepresentationContexts or ())
            ids = tuple(context.id() for context in contexts)
            if ids[:len(original_ids)] != original_ids or len(ids) <= len(original_ids) or len(set(ids)) != len(ids):
                continue
            additions = contexts[len(original_ids):]
            if not all(context.id() not in snapshot and context.is_a() == 'IfcGeometricRepresentationContext'
                       and context.ContextType == 'Plan' and context.ContextIdentifier is None
                       and context.CoordinateSpaceDimension == 2
                       and isinstance(context.Precision, (int, float)) and math.isfinite(context.Precision)
                       and context.Precision > 0
                       and all(inverse.id() not in snapshot or inverse == project for inverse in saved.get_inverse(context))
                       for context in additions):
                continue
            # Compare every other project attribute without mutating the saved
            # file. Only an append-only new Plan context association is exempt.
            check = ifcopenshell.file.from_string(saved.to_string())
            check.by_id(key).RepresentationContexts = tuple(check.by_id(cid) for cid in original_ids)
            if check.by_id(key).to_string() == snapshot[key]:
                changed.remove(key)
    if changed:
        # Do not echo entity contents, paths, names or private property values.
        raise ValueError('native source preservation failed: existing IFC entities changed or missing')


def _reject_resources(native: ifcopenshell.file) -> None:
    """Reject executable/presentation/resource-bearing input, never sanitize it.

The worker may create its own reviewed presentation resources after this gate.
Existing drawings, text templates, URLs, image textures and external libraries
are not granted access merely because the caller supplied a local IFC file.
"""
    forbidden = ('IfcAnnotation', 'IfcExternalReference', 'IfcExternalInformation',
                 'IfcSurfaceTexture', 'IfcTextLiteral', 'IfcCoordinateOperation',
                 'IfcProjectedCRS', 'IfcProjectLibrary')
    for entity in native:
        if any(entity.is_a(kind) for kind in forbidden):
            raise ValueError('native source contains unsupported presentation, external resource or georeferencing entities')
        if entity.is_a('IfcPropertySet'):
            name = (entity.Name or '').casefold()
            if name.startswith(('epset_', 'bbim')):
                raise ValueError('native source contains unsupported presentation or runtime property sets')
        for value in entity:
            if isinstance(value, ifcopenshell.entity_instance) and value.is_a('IfcURIReference'):
                raise ValueError('native source contains an unsupported URI reference')


def validate_extension_source(native: ifcopenshell.file, model) -> list[dict]:
    """Validate a self-contained OABM IFC with a bounded native plumbing lane.

No native addition is converted to an electrical entity or canonical route.
The returned diagnostics explicitly distinguish preserved native facts from
unsupported canonical semantics. The caller must retain the source and verify
``source_snapshot`` after the native save/reopen, before publishing success.
"""
    if native.schema != 'IFC4':
        raise ValueError('native extension source requires IFC4')
    if len(list(native)) > 100_000:
        raise ValueError('native extension source entity budget exceeded')
    _reject_resources(native)
    roots = native.by_type('IfcRoot')
    guids = [root.GlobalId for root in roots]
    if any(not isinstance(guid, str) or not _GUID.fullmatch(guid) for guid in guids) or len(set(guids)) != len(guids):
        raise ValueError('native extension source requires unique valid IFC GlobalIds')
    units = [unit for assignment in native.by_type('IfcUnitAssignment')
             for unit in assignment.Units if getattr(unit, 'UnitType', None) == 'LENGTHUNIT']
    if len(units) != 1 or not units[0].is_a('IfcSIUnit') or units[0].Name != 'METRE' or units[0].Prefix is not None:
        raise ValueError('native extension source requires one explicit metre length unit')
    if ifcopenshell.util.unit.calculate_unit_scale(native) != 1.0:
        raise ValueError('native extension source must use metres')

    expected = to_ifc(model)
    expected_objects = {entity.GlobalId: entity.is_a() for entity in expected.by_type('IfcObjectDefinition')}
    diagnostics = []
    added_products = set()
    for entity in native.by_type('IfcObjectDefinition'):
        kind = entity.is_a()
        expected_kind = expected_objects.get(entity.GlobalId)
        if expected_kind is not None:
            if kind != expected_kind:
                raise ValueError('native source changes a canonical/adapter IFC class')
            continue
        if kind in PLUMBING_CLASSES or kind in _PLUMBING_TYPES or kind == 'IfcDistributionSystem':
            if ifcopenshell.util.element.get_pset(entity, CANONICAL_PSET) is not None:
                raise ValueError('native plumbing must not masquerade as canonical metadata')
            if kind == 'IfcDistributionSystem' and entity.PredefinedType not in _SYSTEM_TYPES:
                raise ValueError('unsupported native distribution system type')
            if kind in PLUMBING_CLASSES:
                added_products.add(entity.id())
                if entity.ObjectPlacement is None:
                    raise ValueError('native plumbing requires explicit placement')
                if entity.Representation is None and kind not in {'IfcSanitaryTerminal', 'IfcWasteTerminal'}:
                    raise ValueError('native pipe/fitting requires explicit geometry')
                representations = entity.Representation.Representations if entity.Representation is not None else ()
                if entity.Representation is not None and (not representations or any(not representation.Items for representation in representations)):
                    raise ValueError('native plumbing requires nonempty geometry representations')
                if not any(representation.RepresentationIdentifier == 'Body' for representation in representations):
                    for representation in representations:
                        if representation.RepresentationType != 'Curve3D':
                            raise ValueError('body-less plumbing supports only explicit Curve3D polylines')
                        for item in representation.Items:
                            if item.is_a() != 'IfcPolyline' or len(item.Points) < 2:
                                raise ValueError('body-less plumbing supports only nonempty 3D polylines')
                            if any(len(point.Coordinates) != 3 or any(not math.isfinite(value) for value in point.Coordinates)
                                   for point in item.Points):
                                raise ValueError('body-less plumbing requires finite explicit 3D points')
                    diagnostics.append({'ifc_guid': entity.GlobalId, 'ifc_class': kind,
                                        'status': 'native_body_omitted',
                                        'reason': ('Native terminal has no geometry; only its explicit placement and source facts are preserved, not a visible body or physical-size claim.'
                                                   if entity.Representation is None else
                                                   'Native source contains no Body representation; supplied non-solid representation is preserved, not a physical-size claim.')})
            diagnostics.append({'ifc_guid': entity.GlobalId, 'ifc_class': kind,
                                'status': 'preserved_native_only',
                                'reason': 'Canonical plumbing semantics, sizing and code compliance are unsupported; native facts are retained without inference.'})
        elif kind != 'IfcDistributionPort':
            raise ValueError('unsupported noncanonical IFC object in extension source')
    # Extra ports must have exactly one explicit plumbing owner; orphan and
    # cross-domain attachments are not silently interpreted as plumbing.
    for port in native.by_type('IfcDistributionPort'):
        if port.GlobalId in expected_objects:
            continue
        owners = {rel.RelatingObject.id() for rel in native.by_type('IfcRelNests') if port in rel.RelatedObjects}
        owners.update(rel.RelatedElement.id() for rel in native.by_type('IfcRelConnectsPortToElement') if rel.RelatingPort == port)
        if len(owners) != 1 or not owners.issubset(added_products):
            raise ValueError('native plumbing port requires exactly one supported plumbing owner')
        if ifcopenshell.util.element.get_pset(port, CANONICAL_PSET) is not None:
            raise ValueError('native plumbing port must not masquerade as canonical metadata')

    projected = _from_ifc_canonical_subset(native)
    if round_trip_differences(model, projected):
        raise ValueError('native source canonical subset does not match the supplied model')
    return sorted(diagnostics, key=lambda item: (item['ifc_class'], item['ifc_guid']))


def prepare_render_source(native: ifcopenshell.file, model):
    """Isolate old drawing/document trees without ever loading their resources.

    The returned renderer copy is disposable. The original source remains opaque
    and unchanged, including old resource paths; only reviewed generated additions
    may subsequently be grafted into an original-source clone. This is not a
    sanitizing rewrite of the user's deliverable.
    """
    render = ifcopenshell.file.from_string(native.to_string())
    annotations = render.by_type('IfcAnnotation')
    for annotation in annotations:
        if ifcopenshell.util.element.get_pset(annotation, CANONICAL_PSET) is not None:
            raise ValueError('canonical annotation cannot be isolated as opaque presentation')
    seeds = set(annotations)
    seeds.update(render.by_type('IfcDocumentReference'))
    seeds.update(render.by_type('IfcDocumentInformation'))
    seeds.update(render.by_type('IfcTextLiteral'))
    for group in render.by_type('IfcGroup'):
        if group.ObjectType == 'DRAWING':
            members = [member for rel in render.by_type('IfcRelAssignsToGroup')
                       if rel.RelatingGroup == group for member in rel.RelatedObjects]
            if any(not member.is_a('IfcAnnotation') for member in members):
                raise ValueError('opaque drawing group includes a physical object')
            seeds.add(group)
    for pset in render.by_type('IfcPropertySet'):
        name = (pset.Name or '').casefold()
        if name.startswith('bbim'):
            raise ValueError('native source contains unsupported runtime property sets')
        if name.startswith('epset_'):
            seeds.add(pset)

    # Resource entities are removable only if they belong to an old annotation
    # subtree. A texture/library affecting a physical Body remains a refusal.
    presentation_descendants = {item for annotation in annotations for item in render.traverse(annotation)}
    for entity in presentation_descendants:
        if entity.is_a('IfcExternalReference') or entity.is_a('IfcExternalInformation') or entity.is_a('IfcSurfaceTexture'):
            seeds.add(entity)
    doomed = set(seeds)
    queue = list(seeds)
    while queue:
        entity = queue.pop()
        for inverse in render.get_inverse(entity):
            if inverse in doomed:
                continue
            if inverse.is_a('IfcRelContainedInSpatialStructure'):
                # file.remove removes only the annotation references, retaining
                # unrelated physical containment on the renderer copy.
                continue
            if inverse.is_a('IfcObjectDefinition'):
                raise ValueError('opaque presentation resource is referenced by a physical object')
            doomed.add(inverse)
            queue.append(inverse)
    removed = sorted({entity.is_a() for entity in doomed})
    for entity in sorted(doomed, key=lambda item: item.id(), reverse=True):
        render.remove(entity)
    for relation in tuple(render.by_type('IfcRelContainedInSpatialStructure')):
        if not relation.RelatedElements:
            render.remove(relation)
    diagnostics = validate_extension_source(render, model)
    if doomed:
        diagnostics.append({'status': 'opaque_presentation_preserved_not_rendered',
                            'entity_count': len(doomed), 'ifc_classes': removed,
                            'reason': 'Existing drawing/document instructions are retained only in the final source clone; they are never loaded or evaluated by the renderer.'})
    return render, source_snapshot(render), diagnostics


def merge_render_additions(original: ifcopenshell.file, render_baseline: dict[int, str],
                           rendered: ifcopenshell.file) -> ifcopenshell.file:
    """Graft trusted new drawing entities onto an untouched original clone.

    All renderer baseline entities must remain unchanged except the explicitly
    checked append-only drawing context association. New references back into
    the baseline map to original entities by retained STEP identity, not name or
    geometric matching. Removed original presentation trees are never consulted.
    """
    verify_source_snapshot(render_baseline, rendered, allow_drawing_context_additions=True)
    _validate_drawing_delta(original, render_baseline, rendered)
    final = ifcopenshell.file.from_string(original.to_string())
    copied = {}

    def clone_value(value):
        if isinstance(value, tuple):
            return tuple(clone_value(item) for item in value)
        if not isinstance(value, ifcopenshell.entity_instance):
            return value
        if value.id() in render_baseline:
            return final.by_id(value.id())
        if not value.id():
            return final.create_entity(value.is_a(), value.wrappedValue)
        if value.id() in copied:
            return copied[value.id()]
        result = final.create_entity(value.is_a())
        copied[value.id()] = result
        for index, item in enumerate(value):
            result[index] = clone_value(item)
        return result

    for entity in rendered:
        if entity.id() not in render_baseline:
            clone_value(entity)
    for project in rendered.by_type('IfcProject'):
        before = getattr(render_baseline, 'project_contexts', {}).get(project.id())
        if before is None:
            raise ValueError('renderer created an unsupported project')
        additions = tuple(project.RepresentationContexts or ())[len(before):]
        if additions:
            target = final.by_id(project.id())
            target.RepresentationContexts = (*target.RepresentationContexts, *(clone_value(context) for context in additions))
    verify_source_snapshot(source_snapshot(original), final, allow_drawing_context_additions=True)
    return final


def _validate_drawing_delta(original, baseline, rendered):
    """New inverse relationships must not alter preexisting physical meaning."""
    original_guids = {entity.GlobalId for entity in original.by_type('IfcRoot')}
    new = {entity.id(): entity for entity in rendered if entity.id() not in baseline}
    new_annotations = {key for key, entity in new.items() if entity.is_a() == 'IfcAnnotation'}
    new_groups = {key for key, entity in new.items() if entity.is_a() == 'IfcGroup' and entity.ObjectType == 'DRAWING'}
    seen = set()

    def references(value):
        if isinstance(value, tuple):
            for item in value:
                yield from references(item)
        elif isinstance(value, ifcopenshell.entity_instance) and value.id():
            yield value

    for entity in new.values():
        # Inverse IFC semantics are not restricted to IfcRelationship. A new
        # IfcStyledItem can restyle an old Body without changing that Body's
        # serialized attributes, for example. Every old/new graph edge needs
        # an explicit presentation-only meaning, not merely a class whitelist.
        for index, value in enumerate(entity):
            attribute = entity.attribute_name(index)
            for reference in references(value):
                if reference.id() not in baseline:
                    continue
                permitted = (
                    entity.is_a() == 'IfcShapeRepresentation' and attribute == 'ContextOfItems'
                    and reference.is_a('IfcGeometricRepresentationContext')
                ) or (
                    entity.is_a() == 'IfcGeometricRepresentationSubContext' and attribute == 'ParentContext'
                    and reference.is_a() == 'IfcGeometricRepresentationContext'
                ) or (
                    entity.is_a() == 'IfcRelAssociatesDocument' and attribute == 'RelatedObjects'
                    and reference.is_a() == 'IfcProject'
                ) or (
                    entity.is_a() == 'IfcRelAssignsToProduct' and attribute == 'RelatingProduct'
                    and reference.is_a('IfcProduct')
                )
                if not permitted:
                    raise ValueError('generated drawing delta contains an unsupported reference into the source graph')
        if entity.is_a('IfcRoot'):
            guid = entity.GlobalId
            if not isinstance(guid, str) or not _GUID.fullmatch(guid) or guid in original_guids or guid in seen:
                raise ValueError('generated drawing delta has invalid or conflicting identity')
            seen.add(guid)
        if entity.is_a('IfcObjectDefinition') and entity.id() not in new_annotations | new_groups:
            raise ValueError('generated drawing delta contains a nonpresentation object')
        if not entity.is_a('IfcRelationship'):
            continue
        kind = entity.is_a()
        related = {item.id() for item in getattr(entity, 'RelatedObjects', ())}
        if kind == 'IfcRelDefinesByProperties':
            valid = bool(related) and related.issubset(new_annotations) and entity.RelatingPropertyDefinition.id() in new
        elif kind == 'IfcRelAssignsToGroup':
            valid = bool(related) and related.issubset(new_annotations) and entity.RelatingGroup.id() in new_groups
        elif kind == 'IfcRelAssignsToProduct':
            valid = bool(related) and related.issubset(new_annotations) and entity.RelatingProduct.id() in baseline
        elif kind == 'IfcRelAssociatesDocument':
            allowed = new_annotations | {project.id() for project in rendered.by_type('IfcProject') if project.id() in baseline}
            valid = bool(related) and related.issubset(allowed) and entity.RelatingDocument.id() in new
        else:
            valid = False
        if not valid:
            raise ValueError('generated drawing delta contains an unsupported semantic relationship')
