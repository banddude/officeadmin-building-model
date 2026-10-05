# Experimental bounded image-registration proposals

Related: #93 (reuse), #98 (bounded proposals), #224 (source target recovery).

This module is not wired into PDF import or canonical placement. It does not complete any of these issues. An eligible proposal is still unaccepted and must pass region, level, source, competing-target and geometry checks. It cannot override a strong existing refusal or place devices in an unresolved frame.

## Reuse decision

Use the existing optional OpenCV dependency for SIFT extraction and brute-force descriptor matching, rather than implement an image feature detector. OpenCV explains SIFT, including multiple descriptors at one position, in its [official tutorial](https://docs.opencv.org/4.x/da/df5/tutorial_py_sift_intro.html). Current OpenCV 4.x is under the [Apache 2.0 license](https://github.com/opencv/opencv/blob/4.x/LICENSE); follow its notices when distributing it.

Existing point_registration solves unpaired point clouds with CPD and RANSAC. It does not preserve descriptor correspondences at a fixed printed scale. Existing mask_registration can offer separate line-mask evidence, but neither module's convergence flag supplies canonical acceptance for this experiment.

## Interface and limits

propose_feature_translation consumes paired PDF coordinates and explicit printed scales. It compares translation hypotheses and diagnostic quarter turns/reflections at those fixed scales. No affine distortion or scale optimization is allowed.

propose_raster_translation takes two grayscale drawing crops plus exact pixel-to-PDF transforms. Rendering and crop selection remain caller-owned. It reads no files, performs no network/model calls and mutates no canonical objects. It uses at most 16 million pixels per crop and requests 15,000 SIFT features; more than 1,500 ratio-filtered correspondence pairs is refused rather than sampled silently.

The proposal records a version, input fingerprint, transform, measured residual, spatial controls and a refusal reason. accepted is always false in returned results. Its fingerprint binds pixels and coordinate transforms for the raster adapter; the caller must additionally retain source-file hashes and region references.

## Evidence scoring

Duplicate pairs and repeated source/target coordinates do not increase support. A deterministic spatial subset requires at least 0.5 m separation in both images. At least eight independent controls, 3 m spans in both axes, and a 1 m second-principal-axis spread are required for a candidate to proceed to geometry validation. A 0.05 m residual bound applies. A competing distinct pose with at least eight controls and 75% of the winning support refuses the proposal. Unsupported winning rotation/reflection is reported rather than applied.

These are conservative experimental screening constants, not building-code rules or a claimed calibrated probability. They require broader benchmark validation before production integration. Large repeated annotation clusters, long single lines, different target regions and unresolved levels must not be mistaken for a complete building registration.

## Required before integration

- Validate all eligible candidate targets together, including unresolved/different-level distractors.
- Preserve strong source geometry, scale and orientation refusals.
- Require source-backed, resolved project-frame and level provenance.
- Independently inspect full-plan overlays and out-of-sample source sets.
- Confirm deterministic results on pinned dependency versions and bounded runtime/memory.
- Prove device-placement correctness end to end; unit tests and descriptor matches alone are insufficient.
