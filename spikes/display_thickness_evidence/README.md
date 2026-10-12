# Display-thickness evidence render (#88)

Offline inspection output, outside the OABM runtime package. It produces the
visual acceptance evidence issue #88 asks for: a scanned wall recorded at the
honest 0.001 m default drawn so it **reads as a wall**, while the element
panel states plainly what was drawn and why.

```sh
python spikes/display_thickness_evidence/render_evidence.py out.png
pytest -q tests/test_display_thickness_evidence.py
```

The script builds a small **synthetic** room (three walls with the 0.001 m
scan default and `inferred` provenance, one wall measured at 0.2 m, one slab),
exports it twice with `oabm.exports.gltf.to_glb` — once faithfully, once with
`min_display_wall_thickness_m = 0.1` — parses both GLB files back with the
standard library, and renders four orthographic views plus the element-panel
data read out of the **actual exported node extras**:

- Row 1: the whole room, faithful vs drawn.
- Row 2: a 2.4 m top-down crop at the south-west corner, where a recorded
  0.001 m wall is a hairline (invisible against the slab edge) and the drawn
  wall is a translucent 0.1 m band. The translucency is the exporter's
  inferred-provenance style: the drawing still says "not observed".

`evidence.png` is the committed output of the script, regenerated with the
command above. The render is deterministic: fixed camera, sorted faces, and
the exporter's byte-identical GLB output.

## What this closes, and what it does not

The exporter side of #88 is done: a labeled rendering parameter, disclosure
in the node `extras` a viewer's element panel reads, nothing flowing back
into the model, IFC or quantities. **The OA viewer side is not in this
repository**: the production element panel must surface
`canonical_thickness_m`, `drawn_thickness_m` and `thickness_source` whenever
they are present on a wall node, and offer the parameter as a visible viewer
setting. That work is tracked in the follow-up issue linked from #88; this
spike's panel strip is an inspection output rendered from the GLB data, not
the production viewer.
