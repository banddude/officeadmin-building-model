# Voltage-drop review (opt in)

`summarize_voltage_drop(model, proposal)` exposes the reported voltage drop, the caller's limit, voltage, phase, operating current, conductor size and material, and the supplied or model-centerline route length from an existing v1 sizing proposal. It first runs the existing sizing-preview validation against the current model and never changes the model or contacts a provider. It rejects stale models, invalid bindings and non-partial proposals rather than inventing missing facts.

The provider's result is **not independently recalculated or authenticated**. The comparison is only against the supplied limit, not an electrical-code or installation approval. The v1 contract does not specify the voltage-drop method or power factor, and the one-way length may exclude termination leads and slack. A consistently edited result can pass the correlation checks; review all source facts and calculation assumptions before use.

The output uses the strict `voltage-drop-review-v1` schema. Recompute a proposal and regenerate the report after a model or input change. This is one partial review artifact for #233, not the complete permit document suite or an OfficeAdmin deployment.

Saved proposal notes are preserved under `provider_notes_untrusted`, separate from fixed boundary warnings. They may contain private information and cannot override the structured unverified status.

For a saved report, call `validate_voltage_drop_review(model, proposal, report)` against the current model and proposal before display. Schema validity alone does not prove cross-field consistency. Reconstruction is not provider authentication; consistently forged proposal output remains unverified.
