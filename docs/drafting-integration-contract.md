# Reference-style drafting integration contract

This is the shared boundary for the reference-style drafting repair after the
2026-10-27 current-state audit. Existing candidate output is a mechanical
checkpoint, not professional drafting acceptance. Reference documents and company
assets remain private; public examples and regressions use synthetic facts.

## Component-owner boundary

Component owners retain their existing body producers and local schemas. The
integration owner owns `production_set.py`, `company_sheets.py`, shared interfaces
and combined evidence. Coordinate any shared-file change before editing it.
There is no new parallel sheet renderer or canonical model.

The following current APIs remain stable while component drafting is repaired:

- `resolve_company_sheet_set(profile_path, job_path)` returns immutable resolved
  sheets with IDs, titles, physical paper dimensions, effective job fields,
  detached logo bytes and `drawing_bounds_mm=(left,top,right,bottom)`.
- Body coordinates are physical millimetres, page origin at upper left. Supply
  one SVG `g` subtree within those bounds. Do not include an outer page/title
  block, mutate defaults, or reread mutable assets during rendering.
- `validate_sheet_text(text, *, font_family=None, font_weight=400)` rejects
  XML-illegal/control characters and, when a family is selected, absent glyphs.
  `measure_sheet_text(text, font_size_mm, *, font_family='Liberation Sans',
  font_weight=400)` measures the actual resolved font. Consumers wrap their
  content; final shared validation remains mandatory.
- Supported body primitives: groups, rectangles, lines, circles, polylines,
  polygons and plain text. Use explicit solid hex colours and millimetre stroke
  widths. Supported joined strokes are round; the renderer emits that policy.
  Preserve meaningful whitespace. No external resources, scripts, CSS, path
  programs, transforms or arbitrary XML are admitted. Electrical symbols can be
  composed from existing primitives. A genuinely missing primitive requires a
  reviewed narrow shared extension, not bypassing bounds validation.
- `create_company_sheet_set(profile_path, job_path, *, output_directory,
  bodies=None)` accepts bodies keyed by exact sheet ID and publishes the complete
  ordered SVG/PDF packet, retained input/assets and hash receipts. Fresh output,
  exclusive ownership, used-font hash binding and PDF substitution checks remain.

Component pagination must return final page IDs/titles, body geometry and
canonical source/connection/row identity maps. Do not hide facts to make a page
fit. The coordinator reconciles final page order and the cover index after all
pagination. Every source identity must be accounted for as drawn, continued or
explicitly unresolved; sheet density is not a substitute for legibility.

## Reference-style acceptance

The first single-line target is one representative sheet with electrical bus
bars, branch/breaker symbols, transformer/ground symbols and composed annotations
from explicit facts. A rectangular device flowchart is not an accepted substitute.
Missing ratings, conductor facts or grounding facts remain visibly unknown; no
invented engineering annotations. Expand to residential and larger branching
cases only after independent inspection of actual cloud-rendered pages.

Schedules must use recognizable electrical panel conventions without inventing
breakers, phases, spare positions, demand or service sizing. Cover/site/details
must be composed as drawing sheets with usable hierarchy, notes and view/detail
identification at explicit scales. The shared company rail remains common.

Each component supplies exact source SHA, synthetic inputs, generated SVG/PDF,
identity/coverage manifest, focused tests and explicit gaps. Source review and
cloud reference-style pixel review are distinct gates. A combined packet then
proves the same project fields, final index, font binding and facts across tools.
No merge follows from a mechanical layout test alone.

## Assembly-consumer implementation plan

Current `assembly_compat.require_no_populated_building_assemblies` intentionally
refuses populated schema-1.6 assemblies in ordinary drawings and IFC. Removing
that guard alone would silently drop physical children and is prohibited.

The existing GLB path `exports.gltf._assembly_component_mesh` already supports
canonical Box3D, ProfileExtrusion, PanelGeometry and WireSweep, sharing extrusion
and wire kernels from `assembly_geometry.py`. Reuse/extract that deterministic
geometry contract into a neutral module rather than having drawings or IFC import
the GLB writer. Preserve original component IDs, parent IDs, units, poses and
source-qualified provenance.

1. Prove a neutral component geometry adapter for all four canonical shapes with
   existing synthetic wall/opening/ceiling fixtures. Keep GLB bytes/quantities and
   canonical serialization unchanged. No new schema is currently justified.
2. Add opt-in drawing assembly projection/section support using actual physical
   child geometry, selected level/view/cut semantics and canonical source IDs.
   Do not draw only aggregate host envelopes, and do not draw hidden back edges
   as cut lines. Keep unsupported view/geometry combinations explicit.
3. Add IFC physical child products with stable IDs, parent aggregation and
   native geometry using established IfcOpenShell helpers where suitable.
   Keep strict canonical metadata/roundtrip and editable supported shapes;
   triangulated fallback, if needed, must be explicit rather than claiming
   parametric editability. Verify reimport and source identity before relaxing
   the populated-assembly guards for supported cases.
4. Independently review each consumer delta, then exercise the same synthetic
   physical model through quantity, GLB, drawing and IFC exports. Unknown
   attachment/manufacturer/structural fit remains unknown.

This plan does not authorize unsupported geometry loss, automatic penetrations,
engineering approval or a canonical contract workaround. A real contract gap
must be raised under AGENTS.md before implementation.
