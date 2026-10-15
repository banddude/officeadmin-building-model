"""Synthetic dash candidates exercise the dense nearest-endpoint bottleneck."""
import math
import random

import pytest

from oabm.importers.pdf_electrical import importer as engine


def _exhaustive_local_scan(ends):
    # Frozen prechange 3x3 local-cell scan; no grouped or sorted-axis index.
    gap = engine._DASH_ARC_CHAIN_GAP_PT
    grid = {}
    for index, (x, y) in enumerate(ends):
        grid.setdefault((math.floor(x / gap), math.floor(y / gap)), []).append(index)
    nearest = {}
    for index, (x, y) in enumerate(ends):
        cx, cy = math.floor(x / gap), math.floor(y / gap)
        best = None
        for nx in (cx - 1, cx, cx + 1):
            for ny in (cy - 1, cy, cy + 1):
                for other in grid.get((nx, ny), ()):
                    if other // 2 == index // 2:
                        continue
                    distance = math.hypot(ends[other][0] - x, ends[other][1] - y)
                    if distance <= gap and (best is None or (distance, other) < best):
                        best = distance, other
        if best is not None:
            nearest[index] = best[1]
    return nearest


@pytest.mark.parametrize("seed", range(12))
def test_nearest_map_is_exact_for_clustered_and_duplicate_coordinates(seed):
    rng = random.Random(seed)
    ends = []
    for index in range(80):
        x = rng.choice((-60.0, -30.0, 0.0, 30.0)) + rng.choice((0.0, 0.25, 0.5))
        y = rng.choice((-30.0, 0.0, 30.0)) + rng.choice((0.0, 0.25, 0.5))
        ends.extend(((x, y), (x + 2 + index % 20, y)))
    assert engine._nearest_dash_endpoints(ends) == _exhaustive_local_scan(ends)


def test_nearest_map_retains_ties_boundaries_and_own_dash_exclusion():
    gap = engine._DASH_ARC_CHAIN_GAP_PT
    ends = [
        (-60.0, -30.0), (-58.0, -30.0),
        (-30.0, -30.0), (-28.0, -30.0),
        (-60.0, 0.0), (-58.0, 0.0),
        (-60.0, -30.0), (-58.0, -30.0),
        (-60.0, -30.0), (-57.0, -30.0),
        (100.0, 0.0), (100.0 + gap, 0.0),
        (100.0, gap), (100.0 + gap, gap),
        (200.0, 0.0), (202.0, 0.0),
        (math.nextafter(200.0 + gap, math.inf), 0.0), (233.0, 0.0),
    ]
    assert engine._nearest_dash_endpoints(ends) == _exhaustive_local_scan(ends)
    assert engine._nearest_dash_endpoints(ends)[0] == 6
    assert engine._nearest_dash_endpoints(ends)[10] == 12
    assert 14 not in engine._nearest_dash_endpoints(ends)
    assert engine._nearest_dash_endpoints([]) == {}
    assert engine._nearest_dash_endpoints([(0.0, 0.0), (2.0, 0.0)]) == {}


def _dense_dash_vectors():
    # 6,000 eligible 4-point dashes, in dense clusters with exact duplicates.
    # Unlike the older one-point benchmark, every path enters dash matching.
    vectors = []
    for index in range(6000):
        local = index % 100
        if local and local % 7 == 0:
            local -= 1
        x = (index // 100) * 100.0 + (local % 10) * 0.25
        y = (local // 10) * 0.5
        vectors.append(engine.PdfVectorPathObservation(
            f"dense:dash:{index:05d}", 1, ((x, y), (x + 4.0, y)),
        ))
    return tuple(vectors)


def test_dense_dash_candidates_preserve_exact_map_and_reduce_distance_work(monkeypatch):
    vectors = _dense_dash_vectors()
    assert all(engine._dash_segment(vector) is not None for vector in vectors)
    ends = [point for vector in vectors for point in (vector.points_pt[0], vector.points_pt[-1])]
    original_hypot = math.hypot
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original_hypot(*args)

    with monkeypatch.context() as patch:
        patch.setattr(engine.math, "hypot", counted)
        expected = _exhaustive_local_scan(ends)
        exhaustive_calls = calls
        calls = 0
        actual = engine._nearest_dash_endpoints(ends)
        indexed_calls = calls
    assert actual == expected
    assert indexed_calls < exhaustive_calls / 4


def test_arc_train_output_matches_exhaustive_scan_with_dense_candidates(monkeypatch):
    radius = 60.0
    arc = tuple(
        engine.PdfVectorPathObservation(
            f"arc:{index}", 1,
            tuple((radius * math.cos(angle), -100.0 + radius * math.sin(angle))
                  for angle in (index * 0.35, index * 0.35 + 0.25)),
        )
        for index in range(6)
    )
    vectors = (*arc, *_dense_dash_vectors()[:300])
    actual = engine._page_dashed_arc_train_vector_ids(vectors)
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_nearest_dash_endpoints", _exhaustive_local_scan)
        expected = engine._page_dashed_arc_train_vector_ids(vectors)
    assert actual == expected == {vector.element_id for vector in arc}


def test_random_floating_endpoints_match_all_pairs_oracle():
    for seed in range(64):
        rng = random.Random(seed + 1000)
        ends = []
        for _ in range(25):
            x, y = rng.uniform(-100, 100), rng.uniform(-100, 100)
            if ends and rng.random() < 0.25:
                x, y = rng.choice(ends)
            angle = rng.uniform(-math.pi, math.pi)
            length = rng.uniform(2, 34)
            ends.extend(((x, y), (x + length * math.cos(angle), y + length * math.sin(angle))))
        expected = {}
        for index, (x, y) in enumerate(ends):
            candidates = [
                (distance, other)
                for other, point in enumerate(ends)
                if other // 2 != index // 2
                and (distance := math.hypot(point[0] - x, point[1] - y)) <= engine._DASH_ARC_CHAIN_GAP_PT
            ]
            if candidates:
                expected[index] = min(candidates)[1]
        assert engine._nearest_dash_endpoints(ends) == expected
