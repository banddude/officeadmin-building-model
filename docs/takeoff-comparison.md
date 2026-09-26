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

## Comparison by fixture-tag label

Many takeoffs tally the fixture-tag LABELS printed beside a fixture run rather
than device symbols. Comparing symbol counts with a label tally is not
like-for-like, so `oabm.qa.takeoff_comparison` also compares label tallies by
tag:

- `normalize_tag(tag)` normalizes one tag spelling: unicode dashes to ASCII
  hyphens, collapsed whitespace, upper case, no spaces around hyphens, and a
  leading `N` or `(N)` new-work prefix stripped. A bare `N` or `(N)` is left
  alone, so normalization never invents a tag.
- `count_label_occurrences(observations, tag_pattern=..., exclude_boxes=...)`
  counts tag occurrences in raw text observations (mappings or objects with
  `text`, `x_pt`, `y_pt` and optional `page`, like the extractor's
  `PdfTextObservation`). The default pattern matches `LF-12`/`LF-12A`-style
  fixture tags and is configurable. Every match in an observation's text
  counts once. `exclude_boxes` maps a page number to `(x0, y0, x1, y1)`
  rectangles: an observation anchored inside a rectangle on its page is
  skipped, so the legend's or schedule's own tag list is not tallied. An
  observation without a usable page is never excluded.
- `compare_tag_labels(label_counts_by_sheet, reference_by_tag, base_sheets=...,
  alternate_sheets=...)` compares per-sheet label tallies with a reference by
  tag. Both sides are normalized before comparison, so case, whitespace,
  hyphen variants and a new-work prefix never split one tag into two. Each
  scored tag reports its reference count, base-sheet and alternate-sheet
  label counts, their sum, the signed delta, and exact matches on base and
  allowing alternates. The exact numerators and denominators are always
  reported; a tag neither side counts is listed under `not_scored`, because
  two zeros are not evidence of a match. Sheets declared in both groups
  raise; undeclared sheets and declared sheets without observations are
  reported so no evidence is silently dropped, and undeclared counts never
  enter the scored sums.

All three helpers are pure, deterministic and JSON-serializable.

Private pilot references and their label mappings stay outside this repository.
Only aggregate counts, match-rate numerators and denominators, reason codes and
non-identifying sheet numbers are reported publicly.
