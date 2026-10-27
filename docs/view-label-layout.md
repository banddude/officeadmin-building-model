# Bounded model-view label placement

`layout_view_labels` is an opt-in presentation pass over an existing
`DrawingView`. It offsets `annotations:labels` text to avoid symbols, route and
other geometry strokes, dimension lines/text and previously placed labels.
The [one-command model delivery](model-delivery.md) applies it automatically
to requested plans. Existing generator/serializer defaults remain unchanged.
This is a local SVG-view improvement, not a permit-sheet layout engine.

```python
from oabm.drawings import generate_plan, layout_view_labels, paper_text_height_m, view_to_svg

view = generate_plan(model, spec, label_overlap="keep")
height = paper_text_height_m(spec.scale)
view = layout_view_labels(view, pixels_per_model_unit=100, text_height_m=height)
svg = view_to_svg(view, pixels_per_model_unit=100, text_height_m=height)
```

Use identical pixel scale and text height for layout and SVG serialization.
The pass matches `view_to_svg`'s fixed four-pixel symbol radius. It does not
know physical-sheet glyph dimensions or external renderer/font overrides.
Generate labels with `label_overlap="keep"` so an earlier suppression pass
cannot remove candidates before they can be relocated. Dimensions may use the
existing independent suppression policy first.

Labels are processed in stable source-ID/primitive-ID order. A clear original
anchor is retained. Otherwise the pass tries at most 24 nearby offsets in a
fixed order and clamps the full estimated text rectangle to the existing view
bounds. Neither canonical geometry nor view bounds change. Full label strings
are never shortened, shrunk or wrapped. Conservative text rectangles reserve
one em per code point, bearings, ascenders in positive model Y and a quarter em
for descenders. This follows SVG's flipped Y projection; it is not the legacy
label-overlap estimate or an exact measurement of font glyphs.

The default two-pixel clearance applies to text rectangles and stroked geometry.
Polygon edges, including the implicit closing edge, are obstacles; polygon
interiors are not treated as filled because the existing SVG uses `fill="none"`.
Displaced labels get a short straight leader referring to the same canonical
source IDs and label ID. Leaders start outside the owner's symbol when one
exists at the original anchor, may connect through their own source geometry, and must clear other
source geometry, placed text and other leaders. Their target is the original
derived label anchor, not a newly inferred survey or fitting connection point.
The pass never moves the symbols/routes to accommodate text.

If no candidate fits, the label is omitted from rendered primitives. The view
metadata retains the complete ID, text, source IDs, original anchor and reason
in `unplaced_labels`; `unplaced_labels_count` and `relocated_labels` summarize
the result. All non-label source geometry remains. The production manifest and
notice mark unplaced labels `needs_review`. A published review output therefore
does not imply that every label could be displayed.

The pass records its version and numerical settings. Reapplying identical
settings returns the same view. To use different settings, regenerate the
original view; a second pass with different settings refuses rather than
moving labels again or losing earlier unplaced text.

## Supported scope and limitations

This pass supports the existing orthographic DrawingView/SVG primitives; tests
and the production integration focus on plan equipment/device labels. It does
not compose physical sheets, place notes in boxes, implement general leader
routing, shape fonts, prove all possible placements exist or certify drawing
readiness. Conservative estimates can leave a label unplaced even where a
human could fit it. Inspect metadata and actual SVG pixels for the final job.
The existing GLB geometry, fittings and straight conduit representation are
unaffected. No automatic engineering approval is added.

## Executable synthetic examples

```sh
PYTHONPATH=src python examples/view_label_layout.py /tmp/view-label-demo
PYTHONPATH=src python examples/model_delivery.py /tmp/model-delivery-with-labels
```

The first produces before/after SVG and drawing JSON from one synthetic
canonical model. Two equipment labels relocate; one deliberately overlong label
is reported with full text. The second exercises the integrated plan/GLB
workflow, verifies file hashes and proves byte-identical complete rebuilds.
Neither command runs a renderer. Inspect the SVG on cloud for visual QA.
