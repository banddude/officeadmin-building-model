# Takeoff count comparison (#105)

`oabm.qa.takeoff_comparison` compares canonical device counts with human takeoff
references. A takeoff is a reference, not truth. Two takeoffs of one set may
disagree, and both may differ from the drawings. The module only counts and
compares; it never edits a model or a reference.

- `device_scope_counts(model)` counts electrical devices and equipment by
  canonical type and `scope_status` (see `pdf-electrical-importer.md`). It
  reports per-type and total counts, the in-scope total (`new` plus
  `relocated`), unresolved reasons, and source pages. Unresolved scope is
  counted and reported but never added to the in-scope total.
- `compare_counts(extracted, reference, comparable_types=...)` reports each
  comparable type's extracted count, reference count, signed and absolute delta,
  and a status of `exact`, `over` or `under`.
  - A comparable type missing from the reference counts as a zero reference.
  - Types outside the comparable set are listed under `not_scored`.
  - The exact-match rate is exact types divided by comparable types. The
    numerator and denominator are always reported, and the rate is `None` when
    there are no comparable types.
- `reference_disagreements(references, comparable_types=...)` lists comparable
  types on which independent references give different counts.
- `compare_with_references(model, references, supported_types=...)` counts a
  model once and compares its in-scope counts with every reference.

The caller declares the supported types: the canonical types the extractor can
recognize. For each reference, a type is scored when it is supported **and**
that reference counts it or the extraction found it. A type neither side counts
is not scored, because two zeros are not evidence of a match. Categories the
extractor does not support are never claimed as matches or misses.

Private pilot references and their label mappings stay outside this repository.
Only aggregate counts, match-rate numerators and denominators, reason codes and
non-identifying sheet numbers are reported publicly.

## Grouped takeoff lines (base vs alternates)

Commercial bids carry alternates: alternate-scope devices, their conduit and
their conductors are real modeled work, but they must never be added into
base-bid quantities. `oabm.quantities.extract_quantities(model, groups=...)`
takes an optional `groups` mapping from a caller-chosen group name (for
example `"ALTERNATES"`) to the canonical entity ids in it — devices,
equipment, routes, route fittings, conductors. The caller decides membership;
the quantities lane never decides what an alternate is.

- Devices, equipment and routes belong to the group that names them.
- A route fitting belongs to the group that names it, otherwise to its
  route's group.
- A conductor contribution over one route belongs to the group that names the
  conductor, otherwise to that route's group, so a conductor over routes in
  different groups is split per route contribution.
- Anything unlisted stays in the ungrouped base (`group=None`). Group is part
  of the aggregation key, so the same item type in the base and in a group
  gives separate lines: base lines first, then groups sorted by name, each in
  the existing order.

An id may belong to at most one group (`ValueError` otherwise). Ids that
match nothing are ignored and counted. Each item's `to_dict()` carries
`"group"` only on grouped lines, and the report's `to_dict()` gains
`groups: {name: {entities, items}}` and `unmatched_group_ids`, only when
groups were supplied — without `groups` the takeoff is byte-identical to the
plain one.
