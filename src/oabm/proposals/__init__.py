"""Bounded inferred proposals; deterministic code owns source and validation."""
from .contracts import (PlacementOptions, CandidateReference, PlacementContext, ImageCrop,
    RankingRequest, RankedCandidate, RankingOutput, Endpoint, RoutingConfig,
    BenchmarkCase, BenchmarkTrial, VisionBenchmark, ProposalResult)
from .dispatch import AdapterAvailabilityError, dispatch_ranking, measure_luna_benchmark
__all__=['PlacementOptions','CandidateReference','PlacementContext','ImageCrop',
    'RankingRequest','RankedCandidate','RankingOutput','Endpoint','RoutingConfig',
    'BenchmarkCase','BenchmarkTrial','VisionBenchmark','ProposalResult',
    'AdapterAvailabilityError','dispatch_ranking','measure_luna_benchmark']
