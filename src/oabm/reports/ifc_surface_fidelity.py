"""Read-only IFC surface declaration comparisons; not visual or optical QA."""
from __future__ import annotations

import math

_FIELDS = ('surface_rgb', 'diffuse_rgb', 'metallic', 'roughness', 'alpha', 'transmission', 'ior')


def _expected_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{field}: finite number required')
    if not (0 < value <= 10 if field == 'ior' else 0 <= value <= 1):
        raise ValueError(f'{field}: out of supported range')
    return float(value)


def _validate(expected):
    if not isinstance(expected, list) or len(expected) > 1000:
        raise ValueError('expectations must be a list of at most 1000 entries')
    seen = set()
    for entry in expected:
        if not isinstance(entry, dict) or set(entry) != {'product_guid', 'style_name', 'values'}:
            raise ValueError('expectation requires product_guid, style_name, values')
        key = (entry['product_guid'], entry['style_name'])
        if any(not isinstance(x, str) or not x or len(x) > 255 for x in key) or key in seen:
            raise ValueError('unique nonempty product_guid/style_name pair required')
        seen.add(key)
        values = entry['values']
        if not isinstance(values, dict) or set(values) != set(_FIELDS):
            raise ValueError('all expected fields must be supplied; null means unknown')
        for field, val in values.items():
            if val is None:
                continue
            if field.endswith('_rgb'):
                if not isinstance(val, list) or len(val) != 3:
                    raise ValueError(f'{field}: RGB triple required')
                for component in val:
                    _expected_number(component, field)
            else:
                _expected_number(val, field)


def _scalar(value, kind, transform=lambda x: x):
    if value is None:
        return {'status': 'missing', 'value': None}
    if not hasattr(value, 'is_a') or not value.is_a(kind):
        return {'status': 'unsupported', 'value': None, 'ifc_type': value.is_a() if hasattr(value, 'is_a') else type(value).__name__}
    value = transform(float(value.wrappedValue))
    if not math.isfinite(value) or not 0 <= value <= 1:
        return {'status': 'unsupported', 'value': None, 'ifc_type': kind}
    return {'status': 'present', 'value': value}


def _colour(value):
    if value is None:
        return {'status': 'missing', 'value': None}
    if not value.is_a('IfcColourRgb'):
        return {'status': 'unsupported', 'value': None, 'ifc_type': value.is_a()}
    values = [float(value.Red), float(value.Green), float(value.Blue)]
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
        return {'status': 'unsupported', 'value': None, 'ifc_type': 'IfcColourRgb'}
    return {'status': 'present', 'value': values}


def _actual(style):
    rendering = [s for s in style.Styles if s.is_a('IfcSurfaceStyleRendering')]
    refr = [s for s in style.Styles if s.is_a('IfcSurfaceStyleRefraction')]
    if len(rendering) > 1 or len(refr) > 1:
        return {key: {'status': 'ambiguous', 'value': None} for key in _FIELDS}
    r = rendering[0] if rendering else None
    ref = refr[0] if refr else None
    result = {key: {'status': 'missing', 'value': None} for key in _FIELDS}
    if r:
        result['surface_rgb'] = _colour(r.SurfaceColour)
        result['diffuse_rgb'] = _colour(r.DiffuseColour)
        result['metallic'] = _scalar(r.SpecularColour, 'IfcNormalisedRatioMeasure')
        result['roughness'] = _scalar(r.SpecularHighlight, 'IfcSpecularRoughness')
        # IFC Transparency is an optional raw real, not a defined-type wrapper.
        if r.Transparency is not None:
            value = float(r.Transparency)
            result['alpha'] = ({'status': 'present', 'value': 1-value} if math.isfinite(value) and 0 <= value <= 1
                               else {'status': 'unsupported', 'value': None})
        result['transmission'] = _scalar(r.TransmissionColour, 'IfcNormalisedRatioMeasure')
    if ref and ref.RefractionIndex is not None:
        value = float(ref.RefractionIndex)
        result['ior'] = ({'status': 'present', 'value': value} if math.isfinite(value) and value > 0
                         else {'status': 'unsupported', 'value': None})
    return result


def ifc_surface_fidelity(ifc, expectations, *, convention, tolerance=1e-6):
    """Compare selected attached IFC declarations with caller-supplied values.

    No files are opened and neither input is mutated. `convention` must explicitly
    acknowledge the producer's factor-to-metallic/transmission interpretation.
    A report cannot prove rendered appearance, completeness, geometry or optics.
    """
    if convention != 'ifc-factor-as-metallic-and-transmission-v1':
        raise ValueError('explicit supported producer convention required')
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or not 0 <= tolerance <= .01:
        raise ValueError('tolerance must be finite in [0, 0.01]')
    _validate(expectations)
    rows = []
    for entry in sorted(expectations, key=lambda x: (x['product_guid'], x['style_name'])):
        try:
            product = ifc.by_guid(entry['product_guid'])
        except (RuntimeError, ValueError):
            product = None
        styles = {}
        body_items = 0
        matching_items = 0
        unsupported_assignments = False
        mapped_items = 0
        competing_style_items = 0
        if product is not None and product.is_a('IfcProduct'):
            representation = getattr(product, 'Representation', None)
            for rep in getattr(representation, 'Representations', ()) or ():
                if rep.RepresentationIdentifier != 'Body':
                    continue
                for item in rep.Items:
                    body_items += 1
                    mapped_items += int(item.is_a('IfcMappedItem'))
                    item_matches = False
                    direct_style_ids = set()
                    for styled in item.StyledByItem or ():
                        for style in styled.Styles:
                            if not style.is_a('IfcSurfaceStyle'):
                                unsupported_assignments = True
                            else:
                                direct_style_ids.add(style.id())
                                if style.Name == entry['style_name']:
                                    styles[style.id()] = style
                                    item_matches = True
                    matching_items += int(item_matches)
                    competing_style_items += int(item_matches and len(direct_style_ids) > 1)
        selector = 'product_missing' if product is None else 'unsupported_product' if not product.is_a('IfcProduct') else ('unsupported_assignment' if unsupported_assignments else 'style_missing') if not styles else 'ambiguous_style' if len(styles) != 1 else 'found'
        selected_style = next(iter(styles.values())) if selector == 'found' else None
        unsupported_style_elements = sorted({s.is_a() for s in selected_style.Styles if not s.is_a('IfcSurfaceStyleRendering') and not s.is_a('IfcSurfaceStyleRefraction')}) if selected_style else []
        actual = _actual(selected_style) if selector == 'found' else {k: {'status': selector, 'value': None} for k in _FIELDS}
        fields = {}
        for field in _FIELDS:
            expected = entry['values'][field]
            a = actual[field]
            if expected is None:
                status = 'expected_unknown'
            elif a['status'] != 'present':
                status = a['status']
            else:
                xs = expected if isinstance(expected, list) else [expected]
                ys = a['value'] if isinstance(a['value'], list) else [a['value']]
                status = 'match' if all(abs(float(x)-float(y)) <= tolerance for x,y in zip(xs,ys)) else 'mismatch'
            fields[field] = {'expected': list(expected) if isinstance(expected, list) else expected, 'actual': a, 'comparison': status}
        rows.append({'product_guid': entry['product_guid'], 'style_name': entry['style_name'], 'selector_status': selector,
                     'body_item_count': body_items, 'matching_body_item_count': matching_items,
                     'unsupported_assignment_present': unsupported_assignments, 'mapped_body_item_count': mapped_items,
                     'matching_items_with_competing_styles': competing_style_items,
                     'unsupported_style_elements': unsupported_style_elements, 'fields': fields})
    return {'version': '1.0.0', 'convention': convention, 'tolerance': tolerance, 'rows': rows,
            'limitations': ['Only explicitly selected direct Body item styles are checked; selection and coverage are caller responsibilities.',
                            'Declared IFC factors are interpreted under caller-specified convention, not measured optics.',
                            'No claim of viewer fidelity, geometry, source completeness, or engineering acceptance.']}
