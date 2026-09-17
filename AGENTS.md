# Agent operating rules

This repository is designed for parallel agent development.

## Prime directive

Do not invent a private representation inside a workstream. Exchange data only through the canonical contracts owned by `src/oabm/model` and `contracts/`.

## Boundaries

- Do not modify `officeadmin-books` from work in this repo.
- Do not touch OfficeAdmin production services, databases, credentials, billing, payroll, or customer data.
- Do not commit secrets, tokens, customer documents, private plan sets, or personal information.
- Public fixtures must be synthetic or explicitly cleared for public use.

## Git workflow

- One issue per branch and worktree.
- Read the issue and every listed dependency before coding.
- If blocked by another issue, do not duplicate that dependency's work.
- Keep commits inside the owning workstream unless the issue explicitly spans a convergence gate.
- Open a PR with tests and a concise verification note.
- Do not merge around failing CI.

## Contract discipline

- Stable IDs, coordinate conventions, units, provenance, serialization, and route shapes are contract decisions.
- Contract changes require tests and migration notes.
- Importers produce canonical objects.
- Routers consume canonical geometry and emit canonical routes.
- IFC code materializes and reads canonical objects.
- Quantities and drawings consume canonical objects and routes.

## Testing

Every workstream adds deterministic fixtures and unit tests. Prefer tiny known-answer buildings over giant opaque samples.

The first integrated proof is a synthetic garage with a room, panel, EVSE, obstacle, routed EMT, fittings, quantities, and IFC round trip.
