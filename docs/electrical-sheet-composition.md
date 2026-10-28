# Electrical sheet composition

`production-sheet-set-v2` can give one ordered sheet slot to a composed
single-line/feeder-register/panel consumer. This reuses canonical topology,
`single_line.plan_pages`, the existing shared feeder-grid adapter,
`draft_panel_schedule_pages` and the company profile renderer. It does not
introduce another electrical engine or splice completed PDFs.

```sh
PYTHONPATH=src python examples/electrical_composition.py /tmp/electrical-proof-new
# Prepared editable inputs can use the same installed production command:
oabm-production-sheets /tmp/electrical-proof-new/inputs/production.json /tmp/electrical-other-attempt
```

The representative public-safe model has eight equipment objects, seven explicit
connections and no listed outgoing circuits on its two panelboards. Its default
composition prints the actual feeder register across a wide bottom band, with a
schematic main area and notes at right. It does not manufacture empty panel
branch tables or duplicate unsupported source-owner feeder facts into them.
A model-qualified caption states the omitted empty-panel/unsupported-group scope;
the complete canonical model and panel report remain retained in the delivery.
This example and mechanical tests do not establish professional visual acceptance.

## Explicit composition settings

An outer `single_line` request adds `composition` with these exact keys:

- `version`: `electrical-sheet-regions-v2`.
- `single_line_regions`: global paper-mm rectangles named `diagram`, `notes` and
  `feeder_register`. All three are required so supplied facts are not delegated
  away from the delivered sheet. Reserve space for every header/stroke/footer.
- `panel_policy`: `listed_only` for supported panel owners with actual listed
  circuits; `include_empty` explicitly requests empty panel registers too;
  `all_reported` explicitly requests the full report-ledger presentation,
  including unsupported source owners. Empty/unassigned output is not a spare
  circuit or a designed branch assignment.
- `panel_placement`: `continuation` for independently titled full-width panel
  pages, or `shared` for explicit nonoverlapping paper slots.
- `panel_region_mm`: null for full-width continuations, otherwise the explicit
  shared panel rectangle.
- `panel_width_mm`: readable card width, passed to the existing schedule engine.

Set outer `schedules` to null when this consumer owns the panel output, avoiding
accidental duplicate tables. Existing region-v1 prototypes retain their explicit
full-report/shared-region behavior. Existing production v1 requests and individual
consumer commands remain available.

Listed/empty panel selection uses the schedule owner's shared
`draft_panel_schedule_selection` and `draft_panel_schedule_pages` APIs over the
complete validated source report. Selected/excluded identities and reasons are
retained; the coordinator does not construct incomplete source reports.

## Binding and geometry boundaries

The plan seals the canonical model digest, single-line layout, panel report,
source schedules, rendered panel bodies, row maps, selected/omitted schedule IDs
and final page identities. Rendering refuses a changed model or plan, and consumes
the precomputed SLD layout without replanning. Final index entries may include
panel-only continuations; they receive no fabricated single-line geometry.

`compose_body_regions` checks all ink against the shared company validator and
one explicitly declared region, including half-strokes and real-font text bounds.
Separate regions cannot overlap or violate the requested gutter. A body may occupy
several disjoint rectangles, leaving real holes for another producer. The helper
does not shrink, clip, shorten text or change canonical facts to make output fit.
Per-part hashes and detached source bindings enter the composition manifest.

Panel-only pages use existing typed paper placements. They retain exact table
scale/cells/identities and align at the top. Headers, source qualifications,
continuations and actual cloud pixels still need independent drafting review.
No demand/service sizing, survey, manufacturer-fit or engineering approval is
asserted.

## Adaptive selection (opt-in)

`electrical-sheet-regions-v3` retains the v2 fields, sets `panel_placement` to
`auto` and `panel_region_mm` to null, and adds `layout_candidates`. The original
`single_line_regions` define the full-size continuation baseline. Each of one to
eight candidates supplies a unique `id`, its own `single_line_regions`, and an
explicit `panel_region_mm`. Rectangles use the same paper-mm convention.

The tool first validates the baseline, then plans and renders each compact
candidate through the existing single-line, schedule and shared ink validators.
It chooses a valid candidate only when it produces fewer total electrical sheets;
ties keep the baseline or earlier candidate. Invalid preset syntax, nonfinite
coordinates, overlaps and out-of-bounds regions are errors. Content that cannot
fit a syntactically valid preset is recorded as rejected, with the baseline
retained. Fonts, circuit facts, scales and canonical objects are never changed
to win the comparison. This is bounded preset selection, not a universal layout
optimizer or an engineering check.

The coordinator retains `derived/electrical-layout-selection.json`, including
the attempts, selected preset and sealed plan fingerprint, and incorporates it
in the normal hash manifest. The final cover index still follows actual output
pages. v1/v2 behavior and company defaults are unchanged.

The existing full-set demonstration can exercise this mode:

```sh
PYTHONPATH=src python examples/reference_production_set.py /tmp/residential-new \
  --variant residential --adaptive-layout
```

The same preset is evaluated for reference and commercial variants. It is not
selected by job name or by assuming how many panel rows a job should have.
