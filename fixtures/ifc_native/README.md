# Anonymous native IFC fixtures

These IFC4 files are independently authored by tests/native_ifc_fixtures.py under the repository license. They contain only fixed synthetic GUIDs, anonymous header strings and synthetic geometry. No official reference or customer IFC bytes are embedded.

Each has a 3 × 0.3 × 2 metre wall and a 1 × 0.3 × 1 metre hosted void. The millimetre/nested case also contains a synthetic window whose physical geometry remains explicitly unmodeled by the importer. Other variants exercise reversed/edge Axis and reversed rectangular polyline winding. Native EXPRESS validation and independent native geometry comparisons run in tests/test_native_ifc_import.py.
