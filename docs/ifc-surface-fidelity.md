# Opt-in IFC surface-declaration comparison

`oabm.reports.ifc_surface_fidelity` is a read-only, in-memory check of explicit
caller expectations against direct surface styles attached to IFC product Body
items. It does not read files or resources, modify the IFC or canonical model,
change native drawing/export guards, or imply rendered, geometric, optical,
field or engineering fidelity. The caller owns trustworthy IFC loading, source
expectations, selection and completeness. Report output is not an authenticated
receipt or an IFC/model fingerprint.

Each expectation has `product_guid`, exact `style_name`, and `values` containing
all seven keys: `surface_rgb`, `diffuse_rgb`, `metallic`, `roughness`, `alpha`,
`transmission`, and `ior`. RGB is a three-number list; null means explicitly
unknown, not zero. All quantities are finite; normalized values are in [0,1],
and IOR is positive and at most 10. The caller must explicitly pass
`convention='ifc-factor-as-metallic-and-transmission-v1'`: this is a producer
convention interpreting IFC `SpecularColour` and `TransmissionColour` factor
values as these illustrative parameters, **not** a universal IFC equivalence.
Roughness specifically requires `IfcSpecularRoughness`; specular exponent is not
roughness. Alpha is 1 minus explicit IFC Transparency. IOR comes from explicit
`IfcSurfaceStyleRefraction.RefractionIndex`. Missing values remain missing.

For each selected style, the report records expected value, actual declaration,
and match/mismatch/missing/unsupported/ambiguous or expected-unknown status.
Missing products/styles and ambiguous duplicate style names are explicit. It
reports total Body item count and how many items carry the selected style; a
matching style on one item is not proof that all product faces are styled.
Items carrying the selected style alongside other direct surface styles are
counted separately, because effective appearance is ambiguous.
Mapped Body items and unsupported style elements (including texture elements)
are counted/listed, not traversed or fetched. Unsupported indirect style
assignments are disclosed, not followed or fetched.
Numeric comparison uses an explicit bounded absolute tolerance (default 1e-6).
Caller-provided names do not establish identity or source equivalence.

Example:

```python
from oabm.reports import ifc_surface_fidelity
report = ifc_surface_fidelity(ifc, [{
    'product_guid': product.GlobalId, 'style_name': 'synthetic finish',
    'values': {'surface_rgb': [.2, .3, .4], 'diffuse_rgb': [.2, .3, .4],
               'metallic': .8, 'roughness': .12, 'alpha': .8,
               'transmission': .1, 'ior': 1.5},
}], convention='ifc-factor-as-metallic-and-transmission-v1')
```

Results describe IFC declarations only. A viewer may ignore or transform them;
textures, shaders, indirect material styles, lighting and screen appearance are
outside this bounded report and require separate verification.
