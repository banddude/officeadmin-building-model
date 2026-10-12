# Bounded native IFC4 intake

`oabm.ifc.import_native_ifc(source, source_id=...)` is an explicit native-source reader. It accepts bytes or a path and returns a `NativeIfcImportResult`. Existing `from_ifc` remains the strict OABM round-trip reader; native IFC without canonical property sets still fails there.

The caller supplies a stable source identity. Canonical IDs derive from that identity and native GlobalIds, never STEP line numbers, geometry or revision hashes. The result records the exact input SHA-256 separately. Call `result.validate_against(current_source, source_id=...)` before reusing saved results: changed bytes, identity or imported content refuse. No project, owner, header or product names are copied into the model.

## Supported geometry

The reader supports IFC4 with one explicit SI length unit (metres or millimetres), one unambiguous three-dimensional model context, and storeys connected to the project. It composes context, local-placement, solid and profile transforms, then converts translations and dimensions to metres once. Storey elevation comes from composed placement; the descriptive native Elevation field remains an observation. Missing level heights and construction remain unknown.

A wall needs one straight Axis and one Body containing exactly `IfcExtrudedAreaSolid`, with an exact rectangular `IfcRectangleProfileDef` or rectangular closed five-point `IfcArbitraryClosedProfileDef` polyline. Tapered, hollow, rounded and voided subclasses refuse. The composed extrusion must be vertical. Axis endpoints must agree with the Body's longitudinal extent; a genuine edge or reversed Axis is accepted. The canonical centerline is the Body midplane, and this conversion is explicitly inferred.

An opening needs one imported wall host and one exact rectangular extrusion whose composed box aligns with that host. Vertical and host-normal extrusion, including equivalent rotations distributed between product and solid placement, are supported. The opening must cross the wall and stay within its length and height. Recesses, oblique boxes and ambiguous hosts refuse. Door/window filling relationships preserve type and identity, but their frame/glazing geometry is not imported and produces a visible partial diagnostic.

Native observations, original unit/placement/profile/Axis values and source GUIDs remain under `attributes.native_ifc`. Observed provenance qualifies only those observations. Separate inferred records qualify converted canonical geometry, dimensions and relationships; observed evidence does not silently become a measured canonical centerline.

## Refusals and limitations

Global ambiguity raises a reason-only `NativeIfcImportError`; paths and IFC entity text are not included. Unsupported physical products produce GUID/type/reason diagnostics. Status is `complete`, `partial`, or `no_supported_geometry`; the last has no model. Callers must inspect status and diagnostics. Georeferencing, conflicting containment, placement cycles, tilted world/storey frames, absent required placement fields and unsupported units refuse rather than guess.

Input budgets are 2,000,000 bytes, 10,000 entities, 256 physical products, placement depth 32 and 2,048 aggregate polyline points. Normal extraction does not tessellate or invoke external models. Independent tests use IfcOpenShell native geometry as an oracle.

Existing quantities report gross wall volume; opening deductions remain explicitly unresolved. A 3 × 0.3 × 2 metre fixture therefore reports 1.8 m³ gross while native subtraction yields 1.5 m³. Existing GLB output does not cut opening voids. Neither quantity nor rendering behavior is changed by this reader. The existing IFC exporter requires a project name; unnamed intake remains unnamed and export refuses until the caller explicitly supplies one.

The fixtures are independently authored anonymous synthetic IFC, not copies of the reference source discussed in #239. This bounded slice does not complete general IFC import or the parent workflows.
