# Shared production chain

`python -m oabm.drawings.production_set REQUEST.json FRESH_OUTPUT` composes a
cover, supplied site drawing, paginated canonical panel facts and explicit
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

The current schema requires `single_line: null` and reports it as pending with
no sheet produced. This partial plan presentation is not a complete permit set.
No demand/service sizing, installation fit, survey accuracy or engineering
approval is inferred. Content and pixel review remain necessary. Output is a
fresh attempt; failures never publish a completed top-level delivery. Keep
exclusive ownership throughout generation. Full font/PDF runtime requirements
are documented in [company-sheet-profile.md](company-sheet-profile.md).
