# Local saved-review delivery

Run an explicit saved-input package through the existing reviewed electrical
runner and persist its outputs locally:

```sh
PYTHONPATH=src python -m oabm.pipeline.saved_electrical inputs.json \
  --output-root /private/local/deliveries --run-id review-v001
```

This module follows the repository's executable-module CLI pattern; it adds no
installed command, shared CLI change or runtime dependency. The Python API is
`persist_saved_electrical(manifest_path, output_root=..., run_id=...)`. Success
returns the published delivery directory; CLI success prints that path and exits
zero. Failure exits nonzero. The API raises `SavedDeliveryError` with
`attempt_directory`, `stage` and the original exception as its cause. Setup
failures are included: after reservation the module attempts a failure receipt;
before reservation the reported attempt path may not exist, and no receipt is
written into a path this invocation does not own. An already
reserved run ID raises `FileExistsError` before any files in that attempt change.

## Explicit manifest

The manifest is a JSON object with exactly the following keys. Duplicate JSON
keys, nonfinite numbers, extra keys and unknown versions fail.

| Key | Value |
| --- | --- |
| version | `saved-electrical-input-v1` |
| delivery_id | Explicit stable caller delivery label |
| model | File reference to canonical BuildingModel JSON |
| review | File reference to saved SymbolReview JSON |
| runs | File reference to JSON array of SymbolRun records |
| decisions | File reference to JSON array of SymbolDecision records |
| registrations | File reference to JSON array of SymbolRegistration records |
| assertion_review | File reference to PlacementAssertionReview JSON |
| scope_policy | File reference to ScopePolicy JSON |
| review_config | File reference to ReviewConfig JSON |
| documents | PDF digest to file-reference object mapping |
| images | Source image ID to file-reference object mapping |
| evidence_documents | Evidence digest to file-reference object mapping |
| journals | Request fingerprint to saved journal file-reference object mapping |
| proposal_groups | Explicit inventory group to proposal ID array mapping |
| canonical_groups | Explicit inventory group to existing canonical ID array mapping |
| source_references | Array of `{source_kind, source_id, reference}` rows |
| glb_options | Explicit exporter options object; `{}` selects existing defaults |
| include_ifc | Explicit boolean; true requires the existing IFC dependency |
| coverage_report | Explicit `null`; no asserted coverage object is accepted |

A file reference has exactly `path` and `sha256`. Paths may be absolute or
relative to the manifest's directory. They are local filesystem paths, never
URLs, shell expressions or fetched references. SHA-256 is lowercase hex over the
exact saved file bytes. For example, construct one reference using:

```python
from hashlib import sha256
from pathlib import Path
path = Path("base-model.json")
reference = {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
```

Use existing typed records' `to_dict()` for JSON records, with arrays for runs,
decisions and registrations. Empty collections must still be supplied explicitly.
All existing source pixel, journal, fingerprint, placement and canonical contract
validators run unchanged. The manifest supplies no reviewer authentication;
the local caller must select authorized reviews and private storage.

Source-reference rows are caller-authorized labels passed to the review bundle,
not destinations or files to fetch. Duplicate source identities fail. Output
references are generated from the chosen run's final local directory; callers
cannot override filenames, route outputs outside that directory or substitute a
partial artifact set. Versions intentionally have different reference-bearing
report/bundle bytes; canonical content is unchanged for identical saved inputs.

## Atomic publication and retries

The output root is created if needed. After exclusive attempt reservation, the
output root and every containing ancestor through the filesystem root are
synced before input loading. This persists each new directory link and also
covers a root left by an earlier interrupted or failed setup. `run_id` is a fresh 1-80 character label
using ASCII letters, digits, underscores and hyphens, starting with a letter or
digit. The module exclusively creates its run directory. Concurrent callers
using the same label cannot both reserve it. It never overwrites or deletes an
existing attempt, source file or prior delivery.

During computation, `run_id/attempt.json` records the reservation. Files are
written exclusively under `run_id/.pending`, with mode 0600 inside newly created
0700 directories. The bytes of every referenced input are checked against the
manifest before use; journals are copied into `.pending/journals` so the existing
validator's later reads use those exact saved bytes. Input snapshots are not
source edits, provider attestations or fresh recognition runs.

The full payload set, input hash receipt and runner completion report are written
and synced before `COMPLETED.json` is created. That marker lists the exact bytes,
hashes and relative paths of every other payload file, including copied journals.
The entire `.pending` directory is then renamed to `delivery` within the same
reserved parent, and the parent directory is synced. Local filesystems supporting
atomic directory rename and directory fsync are required. Network filesystem
transaction guarantees are outside this interface.

Only **`run_id/delivery/COMPLETED.json`**, with matching listed file hashes, denotes
a published set. A marker under `.pending` is uncommitted. Do not treat
`attempt.json`, a runner `delivery_ready` flag, individual model files or a
`.pending` directory as completed persistence. `failure.json`, when writable,
records failures and retains an underlying runner completion report if available.
No payload directory is published when computation or pre-publication writes
fail. A parent-fsync failure after the rename is separately reported as
`durability_unknown` with `directory_committed=true`; investigate that attempt
rather than claiming durable success or overwriting it.

If a process is interrupted, the reservation and any partial data remain. Retry
with a **new** run ID and the same original inputs. There is no implicit resume,
cleanup, deletion, replacement of prior bytes or mutation of source journals.
A full disk may prevent writing the failure receipt too; the missing published
directory still prevents a partial attempt from appearing complete.

## Meaning of the persisted outputs

The delivery contains model JSON, model-derived quantities, scope ledger,
confidence review, source map, GLB, review bundle and optional IFC, plus
`completion-report.json`, `input-receipt.json`, copied journals and the persistence
marker. The existing runner remains the source of every modeling decision.

Persistence success means files were computed and published locally. Consult the
runner report for deferred proposals, source failures, physical unknowns and
scope/bid limits. Source coverage remains unavailable, physical completeness
unverified and base-bid eligibility unestablished. Complete takeoff, buildability,
code compliance and production integration claims stay false. Empty canonical
inventory after deferral does not establish an empty source drawing.

No provider calls, engineering calculations, routing, network destinations,
uploads, credentials, OfficeAdmin writes or HTML transfer are added. Keep real
source manifests and output roots private. Tests generate independent synthetic
source documents and saved stub receipts and use the actual runner consumers.

Prepare placement assertion reviews against the exact canonical snapshot loaded
from the saved model. The existing assertion fingerprint is strict about JSON
numeric representation: an in-memory integer elevation can load as a float.
This entry point does not recapture a review or rewrite its fingerprint to make
an old review pass; an incompatible saved binding fails and retains the runner's
failure report. A reviewer must explicitly supply the correctly bound review.
