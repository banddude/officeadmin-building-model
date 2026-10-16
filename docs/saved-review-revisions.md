# Comparing completed saved reviews

`oabm.pipeline.saved_revisions.compare_saved_reviews(base_directory,
target_directory)` is an opt-in read-only consumer of two explicitly selected
completed local deliveries from the saved electrical entrypoint. It calls the
existing `oabm.qa.build_revision_diff` unchanged and returns
`SavedRevisionComparison(diff_json, context_json)`.

`result.diff_json` is exactly the existing diff engine's `to_json()` output,
without reference paths, persistence metadata, pricing or extra entity changes.
`result.diff` decodes a fresh strict `RevisionDiff`. `result.context` separately
retains both input deliveries' source coverage, physical completeness, scope/bid
limits, unresolved findings and claims, along with the verified local artifact
identities. Editing either decoded copy does not modify the result or inputs.

The executable module prints an envelope containing the unchanged diff document
and its context:

```sh
PYTHONPATH=src python -m oabm.pipeline.saved_revisions \
  /private/reviews/first/delivery /private/reviews/second/delivery
```

Failure exits nonzero without a partial comparison. The Python API refuses with
`ContractError`. No input file is changed, deleted, repaired or reaccepted; the
consumer writes only the explicitly invoked CLI's stdout/stderr.

## Completed-artifact checks

Both arguments must name published `delivery` directories. `.pending`, missing
markers, failed/durability-unknown attempts, symlink paths, unsafe/duplicate
manifest names, unlisted files and unsupported formats are refused. The
completion marker's run and delivery identities must match its reservation and
runner report. Every listed file's exact size and SHA-256 is checked, and every
published file must be covered. Marker and payload bytes are checked again
before validation finishes. Concurrent modification is unsupported and causes a
refusal when observed; this reader does not lock or mutate input directories.

The consumer requires the merged saved-review runner format and successful
requested stages, explicit readiness and the existing incomplete-source claims.
Final reference paths and exact hashes must match actual local artifacts.
Copied or relocated sets whose recorded artifact references still point to
another directory are refused; this interface does not silently relabel them.
The held optional drawing, scope, branch-ownership and code-profile extensions
are not dependencies or accepted alternate formats.

Canonical JSON is strictly decoded and bound to the report digest. Saved symbol
and placement reviews, dispositions/counts and journal snapshots are checked
against the model history and receipt records. Quantities and scope are
regenerated from the canonical snapshot and recorded grouping/policy;
confidence review is regenerated with its supplied threshold. The existing
review-bundle builder checks source-map joins and regenerates trusted GLB bytes
from the model/export options before comparing the full bundle. Optional IFC
must match an exact fresh canonical-model export, then reload with matching
model/schema/frame, entity identities and retained attributes. The existing IFC
roundtrip can differ in the least significant quaternion bits; exact re-export
bytes establish binding without introducing a tolerance into the canonical diff.
That check requires the existing IFC runtime dependency when IFC was requested. No dependency installation occurs.

Hashes and internal consistency are not signatures, reviewer authentication,
power-loss testing or an independent acceptance of physical placement. Original
PDF/image pixels and external reviewer authority are not revalidated by this
consumer; recorded source identities and committed local artifacts are checked.
Those distinctions remain explicit in the comparison context.

## Narrow comparability

This slice compares review changes over one saved base, not arbitrary plan-set
revisions. Both deliveries must have:

- the same canonical `model_id`, complete coordinate-system declaration/units,
  and schema version;
- the same recorded base-model canonical digest;
- the same complete set of source aliases, exact PDF SHA-256 identities and
  one-based pages in their retained saved symbol-run histories.

A changed PDF is explicitly refused as **identity reconciliation unavailable**.
Reviewed symbol IDs include the PDF digest, so passing different PDF bytes to
an ordinary stable-ID diff could misleadingly report removals/additions. There
is no nearest-geometry match, alias guessing, PDF registration, frame conversion,
migration, source-ID reconciliation or all-added/all-removed fallback here.
Different review base geometry/architecture also requires a broader future
comparability contract. An added/removed entity within a comparable review pair
still uses the unchanged canonical diff's existing semantics.

Freshly reviewed elevation changes retain the canonical device ID and appear as
pose/provenance changes, with no quantity delta when the quantity identity is
unchanged. Identical models produce an empty diff even when their output paths,
markers and bundle references differ. The consumer adds no price, demand/load
calculation, code/compliance assertion, production OA action or provider call.
Source coverage remains unavailable, buildability unverified and permit readiness
false. An empty canonical diff is not proof that an incompletely recognized
source drawing is unchanged. The broader #235 issue remains open.

Tests create deliveries through the actual saved entrypoint using the existing
independent synthetic fixtures. Source changes are fresh rendered PDFs with new
saved stub receipts; reviewed pose changes use newly captured valid assertions.
Intentionally corrupted snapshots are used only for negative rejection tests.
