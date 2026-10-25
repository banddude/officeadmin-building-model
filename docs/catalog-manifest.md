# Offline electrical catalog manifests

`oabm.electrical.catalog_manifest.CatalogManifest` loads a bounded, immutable JSON
byte snapshot and searches exact product records. It adapts eligible entries to
the existing `BoxCatalogEntry`, `RacewayCatalogEntry`, and `SupportCatalogEntry`
inputs. It performs no network calls, source-document verification, automatic
product selection, geometry import, compatibility inference or model mutation.
This is a catalog input, not a competing building model or a code checker.

```python
from pathlib import Path
from oabm.electrical.catalog_manifest import CatalogManifest

catalog = CatalogManifest(Path("my-catalog.json").read_bytes())
matches = catalog.search("cable", kind="raceway")
entry = catalog.selector_entry("explicit-product-key")
# Pass entry to the existing selector with explicit intents and rules.
```

Offline search is also available as:

```sh
python -m oabm.electrical.catalog_manifest my-catalog.json --query cable
```

The CLI prints JSON and can require `--expected-sha256` for an existing trusted
manifest pin. No file is written. Search is deterministic, case-insensitive AND
word matching over manufacturer, part number and description, with exact kind
and manufacturer filters. It does not rank substitutes or choose the first hit.

For an index-first, on-demand workflow, `asset_candidates(product_id,
format="STEP")` returns only the exact chosen product's recorded model links,
source hashes and inspection/rights metadata. It never fetches or caches a model,
uses credentials, scrapes a catalog, substitutes a PDF for a model, or treats a
rights label as verified acquisition permission. Missing links yield an empty
tuple. The acquisition layer must obtain any necessary provider/API permission
and verify returned bytes/identity separately before project use. This allows a
small index without mirroring a vendor's entire model library.

## Version 1 shape

The top-level fields are exactly `version` (`electrical-catalog-v1`), `catalog_id`,
`catalog_revision`, `sources` and `entries`. Every product uses the existing
three-part `CatalogProduct` identity. A caller must retain trusted manifest bytes
and must not replace a catalog revision's contents. `sha256` identifies the exact
input bytes; `to_json()` provides sorted deterministic normalization. Different
formatting still has a different raw hash. This module is not a global revision
registry and does not authenticate a manufacturer's statements.

Each source has exactly `id`, HTTPS `url`, `title`, `revision` (string or null),
`locator` (page/section/measurement description), `retrieved_at` (recorded date
such as `2026-01-01`, or exact UTC timestamp `2026-01-01T12:30:00Z`) and
retrieved-byte `sha256`. Date-only precision is retained, not converted into an
invented time of day. URLs are references,
never executable download instructions. Sources and hashes are caller declarations;
the loader does not claim it retrieved or independently inspected those bytes.

Each entry has exactly:

- `product_id`, `manufacturer`, `part_number`, `description`;
- `kind`: `box`, `raceway`, `support`, or `reference`;
- `geometry_basis`: `dimension_derived_envelope` when a `size` is supplied,
  otherwise `metadata_only`;
- `fields`, `evidence`, `assets` as described below.

The exact field sets mirror existing selector inputs:

- box: `box_type`, `size`, `listed_volume_m3`;
- raceway: `route_type`, `wiring_method`, `designation_kind`, `designation`,
  `nominal_diameter_m`;
- support: `category`, `component_type`, `size`;
- reference: `category`, `designation`.

`size` is null or `{x,y,z}` in **metres**. Listed interior volume is a separately
sourced cubic-metre fact, never derived from the exterior envelope. Unknown
optional values remain null and existing selectors retain their unresolved
behavior. Whole-route NM-B/MC assemblies use `route_type="cable"`,
`wiring_method="cable"`, `designation_kind="listed_assembly"`; the exact assembly
is identified by SKU/designation. Nominal diameter is not actual cable OD.

Every supplied non-null selector field, plus `product`, requires explicit
evidence. Each record has exactly `field` (top-level field name), `source_id`,
`derivation` (`observed`, `user`, `inferred`) and numeric `confidence` from 0 to 1.
Each becomes its own existing field-scoped `Provenance` record. Evidence is not
widened to other fields. Source locators, hashes, catalog hash, manufacturer and
part number survive through existing canonical selectors and consumers. Sourced
dimensions and caller-chosen identity mappings can have different derivations.
The declaration is not proof that the evidence supports the selected field.

## Native assets are not native output

Each optional asset has exactly `source_id`, `format` (STEP/DXF/DWG/RFA/IFC/GLB/PDF),
`units` (m/mm/in/unknown), `validation` (uninspected/header_only/geometry_inspected),
`origin` (manufacturer_native_cad/manufacturer_reference/derived), and `rights`
(unknown/redistribution_permitted). The corresponding source binds its bytes.
These records describe acquired/reference assets; this module never loads them.
A manufacturer STEP attached to a box whose output is an envelope does **not**
make that envelope manufacturer-native geometry. Unknown units stay unknown.
Rights are recorded assertions, not legal clearance or permission to redistribute.

Connectors/couplings without a reviewed fitting-product contract remain
`reference` entries. They can be found and inspected, but `selector_entry`
explicitly refuses to turn them into supports, boxes, or invented physical
fittings. No nominal size is converted into a bore, insertion depth or thread.
Native part import and exact fitting selection remain separate work under #342.

## Safety, scope and tests

Unknown fields, duplicate keys/identities/evidence, malformed URLs/timestamps,
nonfinite numbers, boolean dimensions and unsupported selector data fail before
any entry is returned. Input is limited to 8 MiB, 1,000 sources and 5,000 entries.
Results are detached copies; caller mutations cannot change the loaded snapshot.
No asset download, path traversal, model writes or paid calls occur in this API.

Tests use synthetic sources and verify malformed inputs, scope preservation,
raw hash pins, deterministic search/serialization, mutation isolation, existing
box selection and its IFC round trip, GLB, schedules and quantity consumers.
Legacy model contracts and default saved pipelines are unchanged. A manifest
loader alone does not constitute saved-pipeline or real-project acceptance.
Private catalogs and manufacturer binaries must stay in their authorized storage;
only generic code and anonymous synthetic fixtures belong in this public repo.
