# Symbol transport coordinate contract

`oabm.proposals.symbol_transport` supplies deterministic `symbol_output_schema(request)`
and `symbol_transport_prompt(request)` for injected provider transports. It performs
no dispatch, source-pixel verification, canonical mutation, or acceptance.

A hand-built transport schema can omit constraints already enforced by the typed
parser. In particular, unconstrained JSON numbers admit pixel coordinates even
though `SymbolMark` requires normalized coordinates. The shared schema places
`minimum: 0` and `maximum: 1` on each bounds, anchor, and polyline ordinate, and
on confidence. It never divides or repairs returned coordinates. Positive box
area, point/line exclusivity, complete target coverage, evidence relationships,
and all other domain rules remain the typed parser/dispatcher's responsibility.

The prompt identifies each attachment by exact image ID, image/PDF digest,
original page, role, crop rectangle, crop width/height, displayed-page dimensions,
and rotation. Output uses its owning crop's top-left frame: x right, y down,
with pixel locations divided by crop width/height. It explicitly forbids page,
PDF-point, pixel, and canonical-unit output. Repeated source type/work tags are
not unique instance IDs. Source assertions remain pending observations.

A private or application transport must explicitly consume both helpers; merely
installing this module does not change an existing adapter. Record the transport
version, prompt/schema hashes, request fingerprint and exact image hashes with
fresh attempt journals. A changed prompt requires a new request prompt version
and fingerprint. Preserve failed executions without rewriting raw provider data.

`scope_schema=` is an additive optional mark property hook for a separately owned
typed scope contract. It cannot replace coordinate or identity fields; no-scope
historical responses remain schema-readable. The hook caller owns scope-schema
and parser compatibility, including normalized citation bounds. Providers that
require every property in `required` should omit this hook until an explicitly
supported nullable scope contract is available.

The default transport schema omits `uniqueItems`, which strict provider response
formats reject. Duplicate scanned targets remain invalid in `SymbolOutput`;
schema acceptance never replaces domain validation.
