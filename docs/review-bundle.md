# Review bundle bridge

The module oabm.exports.review_bundle builds a deterministic, read-only package for a later review UI. It joins the existing canonical confidence review with verified GLB nodes and caller-authorized source references. It does not read or change files, fetch evidence, upload artifacts, sign links, or call a service.

## Inputs and validation

Call build_review_bundle with:

- a BuildingModel;
- the exact GLB bytes produced for that model;
- caller-provided references for the model snapshot, GLB, confidence review, and source-reference mapping;
- the existing ConfidenceReviewManifest (or its strict JSON representation);
- a mapping from (source_kind, source_id) to caller-provided source paths or IDs.

The builder fails closed when the GLB model ID or snapshot digest does not match, the review manifest cannot be reproduced from the current model and its bound crop references, a canonical entity ID is missing or duplicated across any typed model entity collection, a GLB canonical ID is missing/unknown/duplicated, a wire node names an unknown route, or a review provenance source lacks a reference. Unused source-map entries fail too, which catches typos. Entities that the current exporter does not draw remain in the bundle with an empty rendered_node_indices array.

Conductor wires are the one valid one-to-many node mapping: each visual wire retains the conductor's canonical ID and a verified route ID. Every other GLB canonical ID maps to one node.

Review rows are joined from the canonical model. The entities field carries each matched entity's actual confidence, serialized provenance records, resolved source references, and GLB node indices. This includes quantity-warning items whose confidence-review provenance summary may be empty: the bridge resolves their entity IDs back to current canonical entities. It never derives source confidence or review facts from summarized GLB extras.

## Version 1.0.0 identity contract

The bundle has kind oabm.review-bundle and version 1.0.0. Its artifacts object carries:

| Entry | Reference | SHA-256 content identity |
| --- | --- | --- |
| canonical_model | Caller reference and canonical model_id | Compact sorted-key UTF-8 model.to_json(indent=None) |
| glb | Caller reference | Exact raw GLB bytes |
| confidence_review | Caller reference | Compact sorted-key UTF-8 ConfidenceReviewManifest.to_json(indent=None) |
| source_reference_mapping | Caller reference and sorted mapping entries | Compact sorted-key UTF-8 JSON for the sorted {source_kind, source_id, reference} array |

The references are labels or paths supplied by the caller. The bridge does not resolve them. SHA-256 identifies content; it does not authenticate who supplied it or prove that an external source exists. The caller is responsible for authorization and for providing durable, non-secret references. Do not put credentials, signed URLs, or private source content in public fixtures.

Each bundle item retains the complete existing confidence-review item and adds canonical entity joins:

- canonical_id, canonical collection, and the entity's current canonical confidence;
- full provenance records from the canonical serialized entity, each with its resolved source_reference;
- rendered_node_indices, empty when the entity is not represented by a GLB node.

ReviewBundle.to_json() is stable across repeated builds. ReviewBundle.from_dict() validates and round-trips the versioned portable document.

## Display thickness

min_display_wall_thickness_m remains a labeled GLB drawing option. Re-exporting with a different value changes only the derived GLB vertices and disclosure metadata; it leaves canonical model serialization, IFC output, and quantities unchanged. A future viewer thickness control must request a re-export from the canonical model. It must not pretend to change a saved mesh or mutate canonical dimensions.

Public tests use a synthetic model and caller-owned fixture paths only.
