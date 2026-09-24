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
