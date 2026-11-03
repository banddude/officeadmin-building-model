# Sizing comparison preview

`preview(model, proposal)` checks that a proposal is still bound to a validated model snapshot and that its request payload and target mapping agree with the source. It replays the sizing boundary's input and response checks without rerunning or authenticating the provider. It never edits a model or approves a design.

The `sizing-preview-v1` output compares existing and proposed **line and neutral size/material**, and compares the supplied ground size to the existing ground size. It reports count and route membership, excluded conductors, unknowns, and explicit differences. Missing roles, count mismatches, unrecognized sizes/materials, unrepresented neutrals or grounds, and unknown route membership remain `unresolved`. `matches_in_scope` means only that the explicitly compared fields match; it does not imply the installation is compliant.

The provider's output is editable and unauthenticated. Request and model hashes detect mismatched inputs or stale source, not a forged or incorrect result. The preview does not verify ampacity, conduit fill, grounding adequacy, insulation equivalence, equipment protection, existing conduit equivalence, code edition or field conditions. A separate qualified review is required before any design decision.

Preview outputs repeat source evidence and provenance from the request. Treat them as potentially sensitive project data; do not commit or share them publicly without an appropriate review.
