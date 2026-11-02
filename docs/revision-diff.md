# Canonical revision diff

`oabm.qa.build_revision_diff(base, target)` compares two explicitly
comparable `BuildingModel` snapshots (#274, under #235) and reports what a
plan revision actually changed — so a revision stops looking like a new job
with every object brand new.

## API

```python
from oabm.qa import build_revision_diff

diff = build_revision_diff(base, target)
diff.to_json()                       # portable, deterministic
RevisionDiff.from_dict(document)     # strict decode
```

## Comparability is required, never guessed

The supported scope is two snapshots of the same model lineage: same
`model_id`, same coordinate frame and units, same schema version. Any other
combination is a refusal naming the reason — a different identity, a
different frame, a mismatched version (migrate both snapshots first). There
is no nearest-geometry matching, no ID mapping, and no fallback that reports
everything as added or removed. Cross-source ID reconciliation stays out of
scope under #235.

## What the report carries

- `added` / `removed` records carry the full canonical document of the
  object; `changed` records carry explicit field paths with before/after
  values, classified as `geometry`, `product`, `graph` (electrical
  connectivity and hosts), `provenance`, or `attribute`, so a reviewer can
  tell a moved wall from a swapped product from a rewired circuit.
- Objects match by stable canonical ID and collection. Reordering an entity
  collection is not a change; order inside geometry (a wall centerline) is.
  Provenance record order inside an entity is normalized, values verbatim.
- Signed quantity deltas come from the real `extract_quantities` run on
  both snapshots, matched on complete item identity (category, type,
  variant, unit). A size change that re-keys the variant reports the old
  line removed and the new line added — auditable additions and removals,
  never a silent in-place mutation. Unchanged scope emits no line; one-side
  lines are explicit `added`/`removed` with nulls on the absent side.
  Nothing prices anything.

The takeoff can split an otherwise identical numeric item into several rows
because their field/design provenance differs. Revision quantities sum all
comparable rows for the same category, type, unit and complete variant using a
deterministic finite scalar total. A provenance-only split of two EVSEs into two
one-unit rows therefore remains two EVSEs, with no invented credit. Provenance
changes remain explicit in the entity audit; they are not physical removals.
Product, work-scope, geometry/material and measurement bases carried in variants
remain distinct, as do units. Incompatible bid-group or assembly ownership is
refused rather than silently combined into a portable delta that cannot express
that distinction. The portable report schema is unchanged.

## Completed physical assembly revisions

Comparable 1.6/1.7 snapshots include canonical building-assignment parents and
physical components. Geometry changes (including actual mesh geometry), supplied
product/material/preset basis and ownership changes receive their existing
geometry/product/graph classes. Regenerated room edits retain semantic IDs and
report actual takeoff deltas; a changed variant remains an explicit old removal
and new addition under the existing null-absent-side contract. Parent records do
not add a second quantity. A denser ceiling layout reports real added/removed
components and changed assignment membership. Provenance-record reordering alone
does not invent a revision. Mixed snapshots can also contain physical
`line_arc_v1` routes as described below.

## Mixed physical-route revisions

Physical routes pass the existing bounded detached capture and canonical
request/geometry admission before diffing. Stable route IDs remain the only
matching rule. Actual nested fields use dot-separated paths (for example
`joints.1.position.x` or `requirements.outside_diameter_m`) under the unchanged
revision report schema. Child identities and ownership are `graph`; positions,
ordered primitives, OD and supplied radius are `geometry`. Route type and
supplied physical constraints are `product` **basis**, not inferred catalog
selections. Retained source/request context (including domain/objective/resource
settings), IDs and hashes is
`provenance`; a source change alone is not a physical path change.

Canonical provenance order is normalized inside physical owners. Opaque
`attributes` dictionaries retain exact types and key presence, even for keys
named `confidence`, `id` or `provenance`. Evidence and attribute containers are
reported whole so absent keys remain distinguishable from explicit nulls without
adding fields to the established report contract. Legacy-only output is unchanged.

Whole-model signed quantity deltas use the existing analytic takeoff, including
physical and legacy routes, conductors and actual assembly components. OD/type
variant changes retain separate removed/added lines with null absent sides;
unchanged scope is not counted again. These are net quantity deltas, not gross
replacement, salvage or stock quantities. Equal-length path edits still report
geometry changes without inventing a nonzero quantity delta.

For retained normalized snapshot hashes and exact signed straight-tick/pi terms,
use the existing `generate_physical_route_comparison` alongside this report.
Its independent physical inventory remains deliberately scoped and is not added
to the whole-model takeoff a second time. Neither report authenticates a source,
rebinds stale route context, infers changed-source reconciliation, nor performs
live clearance, engineering, product or pricing approval.

`PYTHONPATH=src:examples python examples/canonical_physical_revisions.py FRESH_OUTPUT`
produces actual before/after mixed enclosed-room snapshots, canonical revision
JSON, the existing source-bound physical comparison, takeoffs and a checked
summary. Supplied room edits, a .5 m endpoint extension and a .05 m bend-radius
increase yield physical net `.1 + .1*pi` m plus .5 m of legacy run. All room and
legacy context is explicitly outside clearance proof scope. The fixture uses
the existing explicit 1.7 upgrade and canonical path producer, not a new schema
or an engineering verification receipt.

## Purity and determinism

Neither snapshot is mutated. Item and delta identifiers are stable
(`revision:changed:<collection>:<id>`,
`revision:quantity:<category>:<type>:<digest>`), records are sorted, and
identical snapshot pairs produce byte-identical JSON regardless of input
order. Strict decoding rejects unknown keys, wrong versions/kinds, counts
that disagree with the records, and non-finite quantities.

## Limits

No PDF registration or cross-source ID reconciliation, no priced change
orders, no OA writes, no production revision viewer, and no claim that the
diff captures intent — it reports canonical object and quantity movement.
#235 and #227 stay open.
