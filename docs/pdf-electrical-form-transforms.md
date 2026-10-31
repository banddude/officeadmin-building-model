# Electrical PDF Form coordinate correction

Recursive Form observations compose the invoking page/Form CTM, the Form's own
`/Matrix`, and the local stream CTM before page display normalization. This
corrects vector geometry and paint width, text anchors and rendered font sizes,
and emitted Form-symbol origins. Form paths are isolated from unfinished caller
paths and restored after return. Nested/repeated invocations retain existing
source IDs. Non-Form page extraction remains unchanged.

This does not claim Form clipping, visibility, optional-content handling, or
complete rendering support. Existing symbol selection and unsupported source
evidence policies are not broadened.

### Parser compatibility and migration

Electrical extraction now requires `pypdf>=6.21,<7` and checks the installed
version before parsing. Older, prerelease, invalid, or unverifiable versions
raise an explicit `ElectricalPdfError`. Existing environments below this minimum must be
updated by their owner before using this path. No automatic installation or
fallback parser is performed. `packaging` supplies PEP 440 version checking.

The [pypdf 6.21.0 source](https://github.com/py-pdf/pypdf/blob/6.21.0/pypdf/_page.py)
removes the duplicate caller-coordinate Form summary callback (upstream #4079)
that is still present in the
[6.19.0 source](https://github.com/py-pdf/pypdf/blob/6.19.0/pypdf/_page.py).
The coordinate callback scope depends on this corrected behavior. Runtime
verification here used installed pypdf 6.21.3, not every allowed future version.
Version-bound tests simulate metadata only; they are not multi-version parser runs.

Invalid Form matrices are validated before linked visitor stacks are committed.
A recorded matrix error rejects the whole extraction even when pypdf suppresses
a nested callback exception; partial observations are not returned. This does
not claim general malformed-PDF or renderer completeness.
