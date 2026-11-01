# Explicit assembly edits and enclosed synthetic room

`regenerate_building_assemblies(model, requests)` edits existing finite-preset
wall, opening-insert and suspended-ceiling assignments. It returns one fully
validated canonical model. The append-only producer APIs retain their guards;
this is their explicit atomic replacement coordinator, not another model.

Supply a nonempty tuple of **complete typed producer requests**, using each
existing assembly ID and host ID. Omitted assignments and all unrelated records
retain their exact canonical content and collection order. Regeneration removes
only the selected owned parts in a private candidate, executes wall producers
before insert producers, and validates every intermediate/final model. Failure
returns no candidate and never mutates the input. Requests in any order produce
the same output; existing collection slots survive, removed parts disappear,
and genuinely new semantic part IDs append in ID order. Reusing a semantic part
key preserves its canonical component ID even when its geometry changes.

```python
from dataclasses import replace
from oabm.building_assemblies import regenerate_building_assemblies, CeilingParameterOverride
from oabm.building_assemblies.room_fixtures import (
    synthetic_enclosed_room_assembly, synthetic_room_requests,
)
model = synthetic_enclosed_room_assembly()
selection = synthetic_room_requests()[-1]
edit = replace(selection, parameter_overrides=(CeilingParameterOverride(
    parameter_id="ceiling.tile.thickness_m", value_kind="length", value=.025, unit="m",
),))
edited = regenerate_building_assemblies(model, (edit,))
```

These requests edit selected assumptions and explicit recipe inputs, including
opening framing specifications, insert dimensions, ceiling parameters and grid
inputs. They do not rewrite source Wall/Opening/Ceiling/Slab facts or turn an
assumption into an observation. Geometry/fit conflicts refuse. This bounded API
does not offer source-geometry editing, new/deleted assignments, host reassignment,
arbitrary wall systems. Use the existing explicit
producer APIs for initial generation. Complete insert requests must accompany an
edited parent wall, even if their selections are unchanged; this ensures the
opening/wall dependency is explicit. External references that cannot survive the
private candidate's validation refuse with the underlying diagnostic rather
than being rewritten, dropped or guessed. Caller-owned custom catalogs must be supplied explicitly through `wall_catalog`,
`opening_insert_catalog` or `suspended_ceiling_catalog`. Their identity and
revision must match the existing assignment, and the original finite producer
validates/interprets them. Canonical resolved snapshots are not executable
recipes; omitted or mismatched catalogs refuse rather than implicitly migrating
the assignment to built-in defaults. Historical non-executable recipes still
refuse through their producer.

## Routed model boundary

Any existing `Route` or `PhysicalRoute` causes `requires_pre_routing_model`.
The operation never leaves a stale verified route, silently deletes routes, or
rewrites electrical selections to pretend they remain valid. Retain the canonical
pre-routing model and complete assembly selections as the editable checkpoint:

1. Apply the explicit assembly edit to that checkpoint.
2. Derive fresh assembly obstacles and allowed plenum domains from the result.
3. Run the existing router again with explicit endpoints/options.
4. Rebuild downstream selections and exports from that new routed result.

The old routed result remains an immutable revision for comparison. A previously
derived ceiling domain is source-bound and refuses on changed generated inputs;
it must be derived again. No automatic rerouting or broad dependency invalidation
is claimed. Routes with external origins require their corresponding upstream
checkpoint; removing routes by hand is not this API's workflow.

## Actual enclosed-room proof

```sh
PYTHONPATH=src python examples/assembly_room_regeneration.py FRESH_OUTPUT --render
```

Use the existing runtime with IFC and the optional CairoSVG adapter for PNGs.
Without `--render`, SVG, GLB, IFC, quantities, canonical models, freshly routed
models, report and hash manifest are still emitted. The example does not install
or change any runtime.

The source has four face-contacting, butt-ended rectangular wall envelopes,
a closed interior Space, a supplied door void and window void, a suspended
ceiling with an explicitly selected 1.2192 m illustrative module and exactly one
existing Slab deck. The larger module keeps this room demo within the existing
router's finite candidate-grid budget; dense layouts can refuse that budget
without relaxing obstacles or route checks. It is an enclosed synthetic room,
not the historical one-wall `combined_fixtures` region. Independent wall
presets model each wall; corner connections, wall-to-deck connections, ceiling
perimeter support, tee connectors and anchors remain unknown/unmodeled. Face
contact does not establish structural continuity or construction adequacy.

The edit changes header depth from .1397 to .18415 m, door frame face width
from .0508 to .0635 m and tile thickness from .01905 to .025 m. Before and after
use the same source facts and component IDs. The example exports and round-trips
the actual physical IFC, generates GLB and quantities from the same canonical
parts, renders room plan/front-wall/ceiling cuts and an isometric projection of the
shared canonical meshes (translucency and colors are review aids), and runs the existing plenum
router against each revision's generated obstacles and fresh domain. It also
proves refusal of regeneration on routed output and reuse of the old domain.
The report retains actual route geometry checks, changed IDs and limitations.
These are synthetic illustrative dimensions, not manufacturer parts, structural
sizing, installation approval, code certification, other trades or recognition.

Issue #356 acceptance remains with the parent reviewer. The finite presets and
existing deck fulfill the supported composition; joined corner systems, generated
decks, source-geometry editors and general dependency invalidation are outside
this bounded implementation.
