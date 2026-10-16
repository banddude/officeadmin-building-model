# Primary AI electrical-symbol proposals

`oabm.proposals.symbols` is the provider-neutral primary extraction boundary for
electrical source symbols. It extends the source/render contracts from
`oabm.proposals.takeoff` and the existing `Endpoint` / `RoutingConfig`.
It does not require candidates from a deterministic recognizer. The caller
supplies verified source images, immutable PDF bytes and a registered transport
callable. The `pdf_electrical` package owns orchestration, review decisions,
registration and canonical materialization.

## Inputs and transport

`SymbolRequest` contains one or more target crops plus floor context and every
applicable legend, schedule and notes image. All three context roles must be
declared `supplied`, `missing` or `inapplicable`, with a rationale. Supplied
context IDs must enumerate every image of that role. These declarations record
the caller's supplied context; they do not prove that the caller discovered the
entire document's applicable context. Missing roles remain visible in each
exported proposal's unknowns.

Supported categories are `luminaire`, `switch`, `dimmer`, `detector`, `fan`,
`receptacle`, `equipment`, `linear_symbol` and `unknown`. Type vocabulary is
caller supplied. A model must acknowledge every target crop in
`scanned_image_ids` and describe coverage limitations in `coverage_notes`.

Register a callable under the configured endpoint's `adapter_id`:

```python
def transport(request_document, *, images, provider_id, model_alias, prompt_version):
    # Send exactly the serialized request and actual supplied image bytes.
    # Capture provider metadata from the transport, never from model JSON.
    return SymbolAdapterReply(
        output_json=raw_domain_json,
        resolved_model_id=reported_model_id_or_none,
        provider_request_id=reported_request_id_or_none,
    )
```

The model JSON is only `SymbolOutput`; it cannot choose routing identity, circuits,
canonical entities or review acceptance. Missing transport-reported identity
stays unknown. A configured alias, CLI session ID or a model's own assertion is
not a resolved provider model ID.

No SDK, network client, credential access or model downloads are included here.
The consuming application owns its existing authenticated transport and timeout
policy. Synthetic callable tests establish contract behavior, not live model
capability.

## Explicit transport resource limits

Each request carries a typed `SymbolResourceLimits`, serialized into its
fingerprint. Defaults bound the sum of every target and context crop to 64 million
pixels and all supplied encoded images to 64 MB. Callers may lower the pixel
ceiling to match transport capacity; its hard ceiling matches the existing
64-million-pixel verifier limit. The encoded-byte limit is configurable up to
256 MB, alongside the existing per-image and PDF verification bounds.

Pixel totals are checked while building the request. Encoded-byte totals are
checked before decoding, rerendering or calling a provider, and again on saved
proposal export. Boundary failures report the configured limit and require an
explicit split or transport batch. Nothing automatically truncates images,
resizes them, drops context or retries with a smaller prompt.

The 24-image/12-target single-request bounds describe one transport batch, not a
semantic context selection rule. The importer owns the full stage context
inventory and any multi-call orchestration: retain every applicable legend,
schedule and note, attach the relevant complete context to each target task, and
preserve request/response receipts for each batch. If the chosen transport cannot
carry that context together, surface the blocked batch or use an explicitly
recorded staged context workflow. Removing relevant context to make a batch fit
is not an acceptable resource-limit response. This module neither claims nor
implements a multi-batch stage planner.

## Strong baseline and explicit escalation

`SymbolRoutingPolicy(config=existing_routing_config)` defaults to
`strong_baseline`, selecting the configured `vision_sol` endpoint first.
This explicit symbol-stage policy does not change existing text/ranking dispatch.

Only typed `AdapterAvailabilityError("unavailable" | "rate_limited")` can select
an explicitly designated, distinct `strong_availability_fallback`. Invalid strong
output, identity mismatch and generic adapter errors stay failed. Astra runs
only in explicit `diagnostic` mode and yields no integrable proposals.

`qualified_cheaper` is currently disabled and fails before provider dispatch.
The error exposes insufficient task/category/context/target-composition evidence
and directs callers to `strong_baseline`. A one-case paired experiment cannot
authorize a harder or differently composed task. Saved policies cannot opt back
into the disabled mode.

`measure_symbol_qualification` and its portable records remain available only
to preserve explicit paired experiments and their raw receipts. Reference labels
are never sent to adapters. Neither a passing experiment nor a stored
`SymbolQualification` grants cheaper routing. A future separately reviewed gate
would need adequate independent coverage and explicit composition scope.
Strong low-confidence marks remain pending with visible unknowns.

## Durable execution and review boundary

Create a fresh private journal path for each run:

```python
with AttemptJournal.create(private_attempt_path) as journal:
    run = dispatch_symbols(
        request, policy, adapters=adapters,
        images=image_bytes_by_id, documents=pdf_bytes_by_sha256,
        journal=journal,
    )

run.validate_against(request, policy, images=image_bytes_by_id,
                     documents=pdf_bytes_by_sha256, journal_path=private_attempt_path)
proposals = run.proposals(images=image_bytes_by_id, documents=pdf_bytes_by_sha256,
                          journal_path=private_attempt_path)
duplicates = run.duplicate_candidates(
    images=image_bytes_by_id, documents=pdf_bytes_by_sha256,
    journal_path=private_attempt_path)
```

The journal is exclusive-create, mode 0600, append-only JSONL with an integrity
chain. The parent directory entry and each receipt are fsynced. A durable start
precedes each provider call; a durable finish precedes returning the run. Raw
output, endpoint, reported provider metadata, routing reason and request/policy
fingerprints are preserved for successful and rejected domain outputs. A crash
may leave a start without a finish; the receipt is not a success. The hash chain
detects edits, not an adversary able to rewrite an entire file. Keep journals and
source-bearing runs private.

`SymbolRun` round-trips through the existing strict portable contract. Its
`status` is `completed`, `failed` or `diagnostic`. `accepted` remains false and
`review_state` remains pending. Dispatch and later proposal export both verify
the supplied PDF digest, page, MediaBox/rotation and rendered RGB pixels using
the existing render manifest. Saved runs must also match the caller's current
request and policy.

Portable `SymbolRun.from_json()` data and its `output` are unverified model data.
Trusted `proposals()`, `validate_against()` and `duplicate_candidates()` require
an explicit `journal_path=` from the caller's trusted local storage. They verify
the journal chain and compare every stored start/finish receipt, including exact
raw output and provider metadata, before export. Editing an otherwise valid
category or source geometry in the portable run cannot match the unchanged
journal. Each run uses its own journal; extra or missing receipts also fail.
Do not source the trusted path or journal from model-controlled output. The
journal is a local trust anchor, not a signature protecting against a party
that can rewrite both the run and the trusted journal.

Each `SymbolProposal` carries document SHA-256, source/page/image identifiers,
source mark evidence, type/category proposal, confidence, unknowns and inferred
provenance. `fingerprint` binds the complete proposal for importer-owned review.
`request_fingerprint`, `result_fingerprint` and `attempt_id` link it to raw
attempt evidence. No canonical model objects are created.

## Coordinates and duplicates

Raw `bounds`, point `anchor` and ordered `polyline` use normalized target-crop
coordinates, origin top left, x right, y down. Bounds are positive-area
`(left, top, right, bottom)`. A mark has exactly one point anchor or a polyline;
point device categories cannot acquire linear geometry. Linear luminaires and
unknown lines can preserve ordered source paths.

Exported `bounds_px` and `bounds_normalized` use the whole displayed raster with
the same top-left convention. `bounds_pt`, `anchor_pt` and `polyline_pt` use
displayed PDF points, origin bottom left, x right, y up. Conversion uses the
verified crop affine over PDFium's ceiled raster extent, including fractional
dimensions and rotation. These remain source observations, not registered BIM
geometry.

`source_mark_id` is derived from source identity and observed source geometry,
independent of tile, classification and attempt. It is not a physical-object
identity. Overlapping same-page observations and conflicting classifications
produce pending duplicate candidates while retaining every raw proposal.
Across pages only an explicit matching proposed `instance_label` produces a
review cue. Repeated fixture `type_tag` values or coincident page coordinates do
not establish equivalence. No suppression, cross-page merge or count
deduplication happens here.

## Private evaluation integration

The public regression corpus uses independently authored synthetic PDF images.
Private evaluation orchestration should save the exact request, source/render
manifests, context inventory, raw run and attempt journal before any reviewed
labels. Derive item lists and overlays from exported proposals, preserving
proposal/source IDs and both coordinate conventions. Independent missed/duplicate
review belongs in a separate label artifact. Do not copy customer pages, model
outputs or benchmark labels into this repository, and do not rewrite a private
benchmark repository from this adapter.

The prior private floor experiment predates this generalized contract and has
no resolved provider-model receipt. It must remain a historical raw experiment;
these tests do not retroactively certify it as a controlled model comparison.
