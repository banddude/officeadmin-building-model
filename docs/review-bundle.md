# Review bundle bridge

The module oabm.exports.review_bundle builds a deterministic, read-only package for a later review UI. It joins the existing canonical confidence review with verified GLB nodes and caller-authorized source references. It does not read or change files, fetch evidence, upload artifacts, sign links, or call a service.

## Inputs and validation

Call build_review_bundle with:

- a BuildingModel;
- the exact GLB bytes produced for that model;
- optional `glb_options`, normalized and recorded in the bundle, for any explicit display/export options (including `min_display_wall_thickness_m`);
- caller-provided references for the model snapshot, GLB, confidence review, and source-reference mapping;
- the existing ConfidenceReviewManifest (or its strict JSON representation);
- a mapping from (source_kind, source_id) to caller-provided source paths or IDs.

Before reading any GLB metadata, the builder regenerates the expected GLB bytes from the canonical model using the supplied normalized display/export options and requires an exact byte match. This binds geometry, node presence, canonical IDs, and metadata to the trusted exporter output instead of trusting self-reported GLB extras. It also checks expected rendered coverage for every exporter-supported entity and wire instance; canonical entities outside those rendered collections remain valid with empty node lists. Review bundles require canonical schema 1.3 or newer, whose GLB output includes explicit rendered-node IDs and a model snapshot digest. Earlier schemas continue to export byte-identically and are rejected by the review-bundle builder. The builder fails closed when the GLB model ID or snapshot digest does not match, the review manifest cannot be reproduced from the current model and its bound crop references, a canonical entity ID is missing or duplicated across any typed model entity collection, a GLB canonical ID is missing, unknown, or duplicated; a wire node names an unknown route, maps its conductor to a route it does not carry, or has a mismatched route-bound node name; or a review provenance source lacks a reference. Unused source-map entries fail too, which catches typos. Entities that the current exporter does not draw remain in the bundle with an empty rendered_node_indices array.

Conductor wires are the one valid one-to-many node mapping: each visual wire retains the conductor's canonical ID and a route ID owned by that conductor. Every other rendered canonical ID maps to one node. The exporter-supported coverage check distinguishes missing rendered entities from legitimate non-rendered canonical entities.

Review rows are joined from the canonical model. The entities field carries each matched entity's actual confidence, serialized provenance records, resolved source references, and GLB node indices. This includes quantity-warning items whose confidence-review provenance summary may be empty: the bridge resolves their entity IDs back to current canonical entities. It never derives source confidence or review facts from summarized GLB extras.

## Version 1.0.0 identity contract

The bundle has kind oabm.review-bundle and version 1.0.0. Its artifacts object carries:

| Entry | Reference | SHA-256 content identity |
| --- | --- | --- |
| canonical_model | Caller reference and canonical model_id | Compact sorted-key UTF-8 model.to_json(indent=None) |
| glb | Caller reference and normalized export options | Exact raw GLB bytes |
| confidence_review | Caller reference | Compact sorted-key UTF-8 ConfidenceReviewManifest.to_json(indent=None) |
| source_reference_mapping | Caller reference and sorted mapping entries | Compact sorted-key UTF-8 JSON for the sorted {source_kind, source_id, reference} array |

A future viewer thickness control must request a re-export using the canonical model and recorded export options; it cannot alter a saved mesh. The references are labels or paths supplied by the caller. The bridge does not resolve them. SHA-256 identifies content; it does not authenticate who supplied it or prove that an external source exists. The caller is responsible for authorization and for providing durable, non-secret references. Do not put credentials, signed URLs, or private source content in public fixtures.

Each bundle item retains the complete existing confidence-review item and adds canonical entity joins:

- canonical_id, canonical collection, and the entity's current canonical confidence;
- full provenance records from the canonical serialized entity, each with its resolved source_reference;
- rendered_node_indices, empty when the entity is not represented by a GLB node.

ReviewBundle.to_json() is stable across repeated builds. ReviewBundle.from_dict() validates and round-trips the versioned portable document.

## Display thickness

min_display_wall_thickness_m remains a labeled GLB drawing option. Re-exporting with a different value changes only the derived GLB vertices and disclosure metadata; it leaves canonical model serialization, IFC output, and quantities unchanged. A future viewer thickness control must request a re-export from the canonical model. It must not pretend to change a saved mesh or mutate canonical dimensions.

Public tests use a synthetic model and caller-owned fixture paths only.

## Physical-route viewer handoff

The bounded opt-in uses the existing model and bundle, not a parallel route
collection. Build confidence review with `physical_routes=True`, export the
GLB with `physical_routes=True` and optionally `include_obstacles=True`, then
supply those same flags in `build_review_bundle(..., glb_options=...)`.
The builder reproduces the confidence manifest in that mode and re-exports
the exact GLB before accepting it. Canonical JSON bytes and identity are
unchanged; artifact hashes, source joins, immutable portable facts and stale
evidence refusal remain enforced.

Every physical route has one selectable canonical node. When obstacle export
is enabled, every supported supplied obstacle must also have exactly one
canonical node; missing or unexpected obstacle nodes refuse. Unsupported
obstacle geometry refuses in the exporter rather than disappearing. New true
flags are retained in artifact export options; false flags are omitted from
the portable document so default bundle bytes and legacy round-trips remain
unchanged.

The physical review manifest explicitly records that the **entire model**
quantity pass is unsupported. It never filters out physical routes to produce
a misleading partial takeoff. Populated assemblies remain unsupported; canonical validation still forbids
fitting references to physical routes. Tessellated conduit display and supplied obstacle
meshes are review aids, not a new live clearance proof or engineering approval.
The caller still provides authorized artifact and source references; this
bridge does not upload anything or implement viewer transport.

## Opt-in building assemblies and saved layer sidecar

Populated canonical assemblies can be reviewed with explicit
`building_assemblies=True` on both confidence review and bundle construction.
This admits the existing typed assembly records into the canonical identity and
provenance inventory, validates the full model, preserves actual quantity warnings,
and verifies exact GLB re-export. State-group exports include semantic parent nodes
and every physical child once; ungrouped physical previews omit duplicate aggregate
host bodies. Default assembly refusals remain unchanged.

For complete saved-layer handoff use `build_layer_review` / `read_layer_review`
from `oabm.exports`. They retain the existing review-file schema and pair it with
the existing selection-manifest sidecar, checking the same source/configuration,
request, resolution and exact GLB along with the four original artifacts. See
[layer selections](layer-selections.md) for native IFC/DXF memberships and bounded
JSON admission. This is an engine transport feature, not proof that the current OA
viewer accepts assemblies or implements saved selections.
