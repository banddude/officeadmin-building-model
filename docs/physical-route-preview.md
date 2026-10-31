# Physical-route 3D review preview

Opt in with `build_glb_bytes(model, physical_routes=True, include_obstacles=True)` or the same keyword options on `to_glb`. The canonical model remains the source of truth. These options default off, preserving existing bytes and unsupported-consumer behavior.

Each `PhysicalRoute` becomes one indexed selectable mesh/node carrying its canonical ID, actual outside diameter, centreline bend radius, clearance, segment/bend counts, analytical length and provenance. Quarter arcs use exact canonical segment facts rather than caller-rounded legacy centreline previews. A transported analytic frame shares tangent seam rings through successive 3D bends. Smooth outward side normals and flat terminal caps are exported; there are no internal seam caps or hemispherical end approximations.

The mesh uses a local origin at the first joint. Adaptive bounded angular samples target at most 0.25 mm radial and longitudinal chord deviation separately; these are not a claimed combined surface error bound. Actual float32 vertex error is measured and disclosed. Collapsed or inverted triangles and excessive packing error refuse before any destination write. At most 500,000 physical mesh vertices and 4,096 supplied obstacle boxes are supported. A canonical route can be geometrically valid yet too fine for this display profile.

`include_obstacles` draws identity-pose canonical Box3D Obstacle records, with their IDs and supplied clearance. Existing supported structural members continue to render. Other constraint volumes are metadata, not silently represented as boxes. This preview is not a complete occupied-building survey or a clearance certificate. Unsupported geometry refuses explicitly.

Grouping, hiding, dimming and emphasis operate on the existing canonical IDs. Nonphysical legacy routes keep their prior drawing path. For models with physical routes, reference planes and caller bend-radius overrides are currently refused. Conductors attached to physical routes are refused instead of drawing invented wire geometry. These limitations do not change canonical route data.

For OA review, call `build_confidence_review(..., physical_routes=True)` and pass the same true GLB options to `build_review_bundle`. The bundle still verifies exact canonical identity, confidence output, GLB re-export bytes and source mappings. Its physical confidence item explicitly states that the entire model quantity pass was not evaluated, including legacy objects. No filtered or fabricated takeoff is substituted. Populated building assemblies and other unsupported consumers retain their existing refusals.

GLB node extras include `geometry_kind=line_arc_v1`, `outside_diameter_m`, `centerline_radius_m`, `clearance_m`, `segment_count`, `bend_count`, `length_m`, `geometry_disclosure`, `clearance_verified_by_export=false` and separate tessellation disclosures. A viewer must display these as review facts, not engineering approval.

Actual acceptance uses a router-generated four-bend detour around a supplied box, exact matching model/GLB/review bundle artifacts, and the real OA viewer for picking and details. A static GLB render establishes visual geometry only; it does not establish deployed OA support.
