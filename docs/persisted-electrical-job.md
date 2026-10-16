# Persisted electrical job runner (local prototype)

The runner composes saved electrical inputs, bounded circuit proposals, explicit circuit review, and the existing saved delivery publisher. It replays saved symbol stages; it does not redispatch symbol extraction or grant review authority.

Call `run_saved_electrical_job(manifest_path, job_root=..., adapters=...)` with adapters registered by the trusted caller. The CLI performs saved replay and waiting-state inspection without loading provider code from the manifest.

## Inputs and revisions

The exact version is `persisted-electrical-job-v1`. A job manifest contains `version`, `job_id`, `parent_revision`, `upstream`, `settings`, `circuit_images`, `circuit_documents`, `policy_documents`, `review`, and `retry_policy`. File references contain a local path and exact raw-byte SHA256. The upstream manifest is the existing saved-electrical contract with no circuit design. Settings may explicitly be null while the job is still at source/placement readiness. A non-null reference serializes `CircuitJobSettings`, including caller-selected routing and review policies, approved profiles/connectors, source ports, image/context declarations, prompt version, and resource limits.

The first revision has no parent. Each later revision names the SHA256 of an existing committed job manifest. Raw inputs are privately snapshotted into immutable content-addressed blobs. A committed revision can resume without consulting changed original inputs. New inputs require a new revision and hash. No arbitrary model attributes are interpreted as filesystem references or executable adapter names.

The private job root holds immutable revision snapshots, execution-key bindings, proposal attempts, export attempts, and per-invocation session results. POSIX advisory locking prevents concurrent callers from advancing the same job. Every newly created directory entry, including ancestors, is synced in its parent before transport. A failed barrier stops execution; resumed creation re-syncs the existing anchor and its parent. This is a single-host runner, not a distributed task queue.

## Waiting, recovery, and retries

With null settings and null circuit review, the runner validates the existing saved source and placement inputs, records source dispositions and eligible-device counts, and returns `waiting_for_placement` or `waiting_for_circuit_configuration`. It reports missing configuration, review authority, selected panel source port/profile, and per-proposal placement or connector inputs. It does not dispatch, create circuit attempts, or export; `job_complete` remains false. Zero eligible devices is a waiting condition, including when valid circuit settings have been supplied. No source observation is promoted into a physical fact by this readiness check.

A later immutable revision may supply downstream settings. Their complete typed routing/review policies and authority documents remain mandatory before provider dispatch; review and compilation keep their existing exact binding requirements. A circuit review reference without circuit settings is rejected. Malformed references and supplied invalid settings are rejected even for partial jobs.

A completed provider attempt is reused across missing-review pauses and export-only revisions when its complete request and task framing remain identical. A durable terminal receipt can reconstruct a missing parsed checkpoint. Missing or unfinished terminal receipts leave the call in doubt; the runner blocks and never automatically repeats that external call.

A missing stage HEAD pointer is treated as new work only when the stage directory is completely empty. Prior artifacts without a pointer block execution and remain unchanged; they never authorize another provider call or export.

Known provider failures require a new immutable revision with `retry_policy.proposal_after` naming the exact prior attempt and a nonempty `proposal_reason`. Dispatch also requires `allow_dispatch=true`. A failed export similarly requires `export_after` and `export_reason`; recovery invokes the existing publisher under a fresh attempt ID and preserves the failed attempt. Export failure does not repeat a completed proposal.

A pending proposal returns `waiting_for_review`. A trusted caller must supply a `CircuitReviewDecision` bound to the exact pending run and configured review authority in a new revision. Compilation uses the existing source, placement, policy, and authority checks. No automatic approval or human-review claim is created.

Completed deliveries are verified using the existing revision reader. Wrapper completion markers are checked against the actual delivery marker. Corrupt receipts, immutable artifacts, or completion bindings stop reuse rather than triggering a replacement provider call.

## Limits

The output preserves structured missing physical facts and never claims source completeness, buildability, or verified code compliance. Runtime and task-contract changes require explicit revision handling. This checkpoint has only synthetic fake-adapter qualification. It does not resolve jurisdiction profiles, contact people, publish customer artifacts, or manage a remote scheduler. Publication and production integration remain outside this local checkpoint.
