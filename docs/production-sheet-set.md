# Shared production chain

`python -m oabm.drawings.production_set REQUEST.json FRESH_OUTPUT` composes a
cover, supplied site drawing, optional canonical single-line, paginated panel facts and explicit
canonical detail recipes into one shared-profile SVG/PDF packet. Individual
consumer commands remain available. The executable public-safe proof is:

```sh
PYTHONPATH=src python examples/production_sheet_set.py /fresh/synthetic-chain
```

This first produces blank drawing sheets with populated title rails, then a
populated set from the same company/job inputs. Schedule pagination expands its
explicit template slot. The final cover index derives from that expanded list;
all pages are exported together by `create_company_sheet_set`, with one PDF
conversion/assembly path. There is no post-export manual PDF splicing.

`fixtures/production_set/v1/production.json` names the profile, job, canonical
model, cover, supplied site request, schedule layout settings and detail recipes.
Paths resolve beside that request. Site and schedule entries explicitly name
their sheet/template IDs. Every template sheet must have exactly one content
owner. The cover source index must match the initial ordered job list; the
coordinator retains it and emits a derived index after schedule pagination.
Continuation IDs must not collide with other sheets. Common project name,
location/address and project number must agree between cover and titleblock;
site titleblock scale must agree with the explicit site drawing denominator.
Per-sheet field/logo/template overrides still use the shared profile resolver.

Schedules and details consume the same validated canonical model bytes. The
site tool consumes separately qualified supplied drawing geometry; it does not
promote it into surveyed canonical geometry. Existing report arithmetic,
unknown/conflict reporting, detail guards and body overflow checks remain owned
by their existing tools. Raw inputs, portable expanded profile/job/assets,
cover/site maps, canonical schedule row bindings and projected drawing JSON are
retained. `PRODUCTION-MANIFEST.json` binds them and the nested shared delivery.
Keep private inputs and their resulting deliveries out of the public repo.

`single_line: null` retains the original partial-set mode with no single-line
sheet. An explicit `single_line: {"sheet_id": "E601"}` selects a template slot;
the existing single-line planner expands it into its deterministic page IDs and
the same canonical model supplies its topology, schedules and details. All
single-line sources/layout maps are retained, and its pages enter the same shared
PDF call and final cover index. No connections are inferred from site proximity.
Use `--compact` on the executable example to exercise that model plus explicit
`detail-sheet-set@2` slots placing three unchanged-scale details on one sheet.
The original one-detail-per-sheet mode remains supported. This presentation set
is not a permit-set completeness or engineering approval claim.
No demand/service sizing, installation fit, survey accuracy or engineering
approval is inferred. Content and pixel review remain necessary. Output is a
fresh attempt; failures never publish a completed top-level delivery. Keep
exclusive ownership throughout generation. Full font/PDF runtime requirements
are documented in [company-sheet-profile.md](company-sheet-profile.md).

The compact example can be run with
`PYTHONPATH=src python examples/production_sheet_set.py --compact /fresh/compact-chain`.
Schedule packing follows the schedule layout's explicit mode; no model facts,
view scales or notes are changed to reduce page count.

The demonstration defaults to the compact supplied residential topology: cover,
site, single-line, packed panel facts and three-detail sheet (five sheets total).
`--legacy` reproduces the earlier ten-sheet two-panel/one-table-per-sheet proof.
The generic production API still takes explicit job/model/layout inputs; it does
not infer project scope or silently change view scales.
