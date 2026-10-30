"""Explicit source-identity bindings for qualified native review views.

These are presentation receipts, not a second building model or a lossy IFC
importer. Native geometry stays authoritative and is never rewritten to fit a
canonical envelope. Legacy export/round-trip contracts are unchanged.
"""
from __future__ import annotations
import hashlib
import json
import re
import math
from decimal import Decimal
from .api import canonical_index

VERSION = 'native-bonsai-ifc-v2'
_GUID = re.compile(r'[0-3][0-9A-Za-z_$]{21}')
_HASH = re.compile(r'[0-9a-f]{64}')
PRESERVE_CLASSES = frozenset({'IfcBoiler', 'IfcDoor', 'IfcWall'})


def fingerprint(value):
    # Canonical decoding normalizes typed numbers (2 -> 2.0). Bind the same
    # numerical fact across serialization without rounding arbitrary integers
    # in source attributes or conflating booleans with numeric one/zero.
    def typed(v):
        if type(v) in (int, float):
            if isinstance(v, float) and not math.isfinite(v): raise ValueError('nonfinite binding fact')
            text = format(Decimal(str(v)), 'f')
            if '.' in text: text = text.rstrip('0').rstrip('.')
            return ['number', '0' if Decimal(text) == 0 else text]
        if isinstance(v, dict): return ['object', [[k, typed(child)] for k, child in sorted(v.items())]]
        if isinstance(v, (list, tuple)): return ['array', [typed(child) for child in v]]
        return [type(v).__name__, v]
    return hashlib.sha256(json.dumps(typed(value), separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def binding_contract(request, model, native):
    """Admit a complete, one-to-one, hash-bound source presentation ledger."""
    contract = request.get('source_contract')
    if not isinstance(contract, dict) or set(contract) != {'source_sha256', 'bindings', 'preserve_products'}:
        raise ValueError('source_contract: exact source_sha256/bindings/preserve_products required')
    if not isinstance(contract['source_sha256'], str) or not _HASH.fullmatch(contract['source_sha256']):
        raise ValueError('source_contract: SHA256 required')
    known = canonical_index(model)
    rows = contract['bindings']
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError('source bindings: bounded list required')
    collections = {e.id: key for key in ('levels', 'spaces', 'walls', 'slabs', 'ceilings', 'openings',
                   'electrical_equipment', 'electrical_devices', 'obstacles') for e in getattr(model, key)}
    if set(collections) != set(known):
        raise ValueError('bound source views support only explicit spatial/equipment/device/obstacle entities')
    classes = {'levels': {'IfcBuildingStorey'}, 'spaces': {'IfcSpace'}, 'walls': {'IfcWall'},
               'slabs': {'IfcSlab'}, 'ceilings': {'IfcCovering'}, 'openings': {'IfcOpeningElement'},
               'obstacles': {'IfcBuildingElementProxy', 'IfcFurnishingElement'}}
    from oabm.ifc.adapter import _device_ifc_type, _equipment_ifc_type
    from ifcopenshell.util.element import get_pset
    import ifcopenshell.geom
    import ifcopenshell.util.placement
    import numpy as np
    geometry_settings = ifcopenshell.geom.settings()
    geometry_settings.set('use-world-coords', True)
    result, used = {}, set()
    omissions = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'canonical_id', 'source_guid', 'canonical_sha256', 'qualification'}:
            raise ValueError('source binding: exact canonical_id/source_guid/canonical_sha256/qualification required')
        cid, guid = row['canonical_id'], row['source_guid']
        if not isinstance(cid, str) or cid not in known or cid in result:
            raise ValueError('source binding: unique known canonical ID required')
        if not isinstance(guid, str) or not _GUID.fullmatch(guid) or guid in used:
            raise ValueError('source binding: unique valid source GUID required')
        if row['canonical_sha256'] != fingerprint(known[cid]):
            raise ValueError('source binding: canonical fact fingerprint changed')
        qualification = row['qualification']
        if not isinstance(qualification, str) or not 1 <= len(qualification) <= 500 or any(not 32 <= ord(c) <= 126 for c in qualification):
            raise ValueError('source binding: explicit printable qualification required')
        try: product = native.by_guid(guid)
        except RuntimeError as exc: raise ValueError('source binding: missing source product') from exc
        key = collections[cid]
        allowed = classes.get(key)
        if key == 'electrical_devices': allowed = {_device_ifc_type(known[cid]['device_type'])[0]}
        if key == 'electrical_equipment': allowed = {_equipment_ifc_type(known[cid]['equipment_type'])[0]}
        if product.is_a() not in allowed:
            raise ValueError('source binding: incompatible canonical/native class')
        if not product.ObjectPlacement:
            raise ValueError('source binding: explicit source placement required')
        representations = product.Representation.Representations if getattr(product, 'Representation', None) else ()
        bodies = [r for r in representations if r.RepresentationIdentifier == 'Body']
        if bodies:
            if any(not body.Items for body in bodies): raise ValueError('source binding: empty Body is unsupported')
            shape = ifcopenshell.geom.create_shape(geometry_settings, product)
            vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
            if not len(vertices) or not np.isfinite(vertices).all(): raise ValueError('source binding: inspectable finite Body required')
            if 'pose' in known[cid]:
                point = np.array([known[cid]['pose']['position'][k] for k in 'xyz'])
                if np.any(point < vertices.min(axis=0)-.00001) or np.any(point > vertices.max(axis=0)+.00001):
                    raise ValueError('source binding: canonical annotation anchor is outside original native Body')
        elif key != 'levels':
            omissions.append(cid)
            if 'pose' in known[cid]:
                point = np.array([known[cid]['pose']['position'][k] for k in 'xyz'])
                native_point = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)[:3,3]
                if np.max(np.abs(point-native_point)) > .00001:
                    raise ValueError('source binding: bodyless canonical anchor differs from native placement')
        metadata = get_pset(product, 'OABM_Canonical')
        if metadata and json.loads(metadata['CanonicalJson']).get('id') != cid:
            raise ValueError('source binding contradicts existing canonical identity')
        # A binding is a source interpretation and must have source evidence,
        # including for later products lacking the old canonical shadow.
        evidence = known[cid].get('provenance', [])
        if not any(p.get('source_element_id') == guid for p in evidence):
            raise ValueError('source binding: source GUID provenance is required')
        result[cid] = dict(row, ifc_class=product.is_a(), geometry_authority='original_native_source')
        used.add(guid)
    if set(result) != set(known):
        raise ValueError('source bindings must cover every canonical entity exactly once')
    acknowledged = request.get('acknowledged_body_omissions')
    if not isinstance(acknowledged, list) or sorted(acknowledged) != sorted(omissions):
        raise ValueError('source binding: acknowledge exactly every bodyless mapped physical product')
    if any(view.get('space_annotations') for view in request.get('views', []) if isinstance(view, dict)):
        raise ValueError('source-bound views do not imply canonical/native space polygon equivalence')
    preserved = contract['preserve_products']
    if not isinstance(preserved, list) or len(preserved) > 1000:
        raise ValueError('preserve_products: bounded list required')
    for row in preserved:
        if not isinstance(row, dict) or set(row) != {'source_guid', 'ifc_class', 'qualification'}:
            raise ValueError('preserve product: exact source_guid/ifc_class/qualification required')
        guid = row['source_guid']
        if not isinstance(guid, str) or not _GUID.fullmatch(guid) or guid in used:
            raise ValueError('preserve product: unique valid source GUID required')
        if row['ifc_class'] not in PRESERVE_CLASSES:
            raise ValueError('preserve product: unsupported explicit native class')
        q = row['qualification']
        if not isinstance(q, str) or not 1 <= len(q) <= 500 or any(not 32 <= ord(c) <= 126 for c in q):
            raise ValueError('preserve product: explicit printable qualification required')
        try: product = native.by_guid(guid)
        except RuntimeError as exc: raise ValueError('preserve product: missing source product') from exc
        if product.is_a() != row['ifc_class'] or not product.ObjectPlacement:
            raise ValueError('preserve product: class/placement mismatch')
        if not product.Representation or not any(r.RepresentationIdentifier == 'Body' and r.Items for r in product.Representation.Representations):
            raise ValueError('preserve product: inspectable native Body required')
        used.add(guid)
    return result


def verify_source_hash(request, source_bytes):
    if request.get('version') == VERSION and request.get('source_contract', {}).get('source_sha256') != hashlib.sha256(source_bytes).hexdigest():
        raise ValueError('source contract SHA256 does not match immutable IFC input')


def native_label_anchor(product):
    """Source Body bounds center for presentation, otherwise explicit placement.

    Native meshes may have baked world coordinates and a zero placement. This
    does not infer mounting, a clear opening, physical dimensions or connectivity.
    """
    import ifcopenshell.geom
    import ifcopenshell.util.placement
    import numpy as np
    reps=product.Representation.Representations if getattr(product,'Representation',None) else ()
    if any(r.RepresentationIdentifier=='Body' for r in reps):
        settings=ifcopenshell.geom.settings();settings.set('use-world-coords',True)
        shape=ifcopenshell.geom.create_shape(settings,product);vertices=np.asarray(shape.geometry.verts).reshape(-1,3)
        if not len(vertices) or not np.isfinite(vertices).all():raise ValueError('native label requires inspectable finite source Body')
        return (vertices.min(axis=0)+vertices.max(axis=0))/2,'native_body_bounds_center'
    if not product.ObjectPlacement:raise ValueError('bodyless native label needs explicit placement')
    return ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)[:3,3],'native_placement_unknown_geometry'
