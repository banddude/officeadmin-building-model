# Exact small-path glyph candidate index

Glyph clustering registers every small path's entire bounding box in a spatial
index. Each later path queries every cell intersecting its box expanded by the
existing gap tolerance. Two boxes within that gap must overlap this queried
region on both axes. Finer cells therefore remove unrelated candidate pairs;
they do not decide whether a pair connects.

The index cell width is four times the existing gap tolerance (currently 10 pt),
rather than maximum path extent plus gap (56.5 pt). The existing maximum extent,
Euclidean box-gap predicate, full-box registration, sorted candidate order,
union-find decisions, oversized-component rejection, source IDs, features and
final ordering are unchanged. No geometry is sampled, capped or truncated.

The tradeoff is more index entries for wide paths: a maximum-width path can
span more fine cells. This is intended to reduce dense buckets of short paths,
not guarantee faster execution on every possible drawing.

Independent tests generate source PDFs with 3,000 separated two-stroke glyphs,
full-width paths, boundary/diagonal contacts, negative origins, curves, fills,
multiple pages and deterministic mixed-width observations. They compare complete
old-cell/new-cell cluster dataclasses and an independent all-pairs component
reference. The stress case retains every authored source ID while requiring at
least six times fewer bounding-box union calls. Existing importer byte-identity
fixtures and the existing dense-page runtime assertion remain unchanged.

Private profiling artifacts and customer geometry are excluded from this change.
Clustering equivalence and faster candidate lookup do not establish complete
symbol recognition or private-project acceptance.
