"""Bounded physical route generation from trusted canonical input geometry."""
from dataclasses import dataclass, replace
from fractions import Fraction
import json
import hashlib

from oabm.model import (BuildingModel, PhysicalRoute, PhysicalRouteRequirements,
    PhysicalRoutingContext, ContractError, PhysicalRoutingAdmissionError)
from oabm.model.model import _entity_collections
from oabm.model.physical_route_context import validate_physical_request_source
from oabm.model.physical_routes import exact_ticks, point_ticks
from .physical_capture import capture, bounded_source_json, SOURCE_BYTES, CANDIDATE_BYTES
from .physical_verify_types import WorkLedger, VerificationStop
from .physical_verify import _PhysicalVerificationSession
from .physical_generation_math import compare_objectives
from .physical_generation_adapter import canonical_candidate
from .physical_generation_search import CandidateWalks, SEARCH_PROFILE
from .physical_generation_grid import candidate_coordinates, GRID_PROFILE

NANO=1_000_000_000

@dataclass(frozen=True)
class PhysicalGenerationResult:
    status: str
    reason: str
    route: PhysicalRoute | None
    report_json: str

    def to_dict(self):return json.loads(self.report_json)
    def to_json(self):return self.report_json


def _reserve_identities(source, context, requirements, ledger):
    wanted=(requirements.route_id,requirements.id,context.allowed_domain.id)
    if len(set(wanted))!=3:raise VerificationStop('invalid_input','request_entity_id_collision')
    existing={source.model_id}
    for collection in _entity_collections(source):
        ledger.debit('request_identity_scan',len(collection))
        existing.update(e.id for e in collection)
    for route in source.routes:
        if type(route) is PhysicalRoute:
            nested=(*route.joints,*route.segments,route.requirements,route.context.allowed_domain)
            ledger.debit('request_identity_scan',len(nested));existing.update(e.id for e in nested)
    if existing.intersection(wanted):raise VerificationStop('invalid_input','request_entity_id_collision')
    return frozenset(existing.union(wanted))


def generate_physical_route(source, context, requirements, *, source_json=None):
    """Return an independently live-verified route, or an explicit failure.

    A returned route is a verified incumbent, never a global-optimality claim.
    The report separately states whether search/ranking finished within limits.
    No caller source or request is mutated; no file or network writes occur.
    """
    ledger=None;walks=None;best=None;best_proof=None;best_objective=None
    outcomes=[];ranking_resolved=True;search_complete=False;uncertain=False
    source_admission=None;stop_status=None;stop_reason=None;family=None
    try:
        if type(source) is not BuildingModel or type(context) is not PhysicalRoutingContext or type(requirements) is not PhysicalRouteRequirements:
            raise VerificationStop('invalid_input','source_request_type')
        raw=bounded_source_json(source_json)
        base=capture(source,byte_limit=SOURCE_BYTES)
        context,requirements=capture((context,requirements),byte_limit=CANDIDATE_BYTES)
        text=base.to_json(indent=None)
        if len(text.encode('utf-8'))>SOURCE_BYTES:raise VerificationStop('resource_limit','source_snapshot_byte_limit')
        base=BuildingModel.from_json(text)
        source_admission=validate_physical_request_source(base,context,requirements,source_json=raw)
        ledger=WorkLedger(context.execution_limits.total_refinement_cells)
        ledger.set_cursor('request_identity_scan')
        reserved_ids=_reserve_identities(base,context,requirements,ledger)
        ledger.debit('request_port_lookup',len(base.ports))
        ports={p.id:p for p in base.ports}
        start=point_ticks(ports[context.start_port_id].pose.position)
        end=point_ticks(ports[context.end_port_id].pose.position)
        direction=lambda p:tuple(int(getattr(p.direction,a)) for a in 'xyz')
        start_d=direction(ports[context.start_port_id]);end_d=tuple(-v for v in direction(ports[context.end_port_id]))
        radius=exact_ticks(requirements.centerline_radius_m,'radius',minimum='.001',maximum='10')
        minima=tuple(exact_ticks(getattr(requirements,n),'straight minimum',minimum='0',maximum='2000') for n in ('start_straight_min_m','end_straight_min_m','between_bends_straight_min_m'))
        penalty=exact_ticks(context.objective.bend_penalty_m,'bend penalty',minimum='0',maximum='1000')
        axes=candidate_coordinates(base,context,requirements,ledger)
        family={'profile':GRID_PROFILE,'axes_ticks':axes,'max_bends':511,
            'ordering':'direct_and_manhattan_and_terminal_bridge_and_parallel_dogleg_prefix_then_control_l1_heap',
            'retained_candidate_signature_budget_bytes':16*1024*1024,
            'identity_policy':'route_scoped_semantic_slots_with_32_collision_probes',
            'priority_prefix_distinct_realizations':16,
            'retained_state_budget_bytes':64*1024*1024,'comparison_max_terms':64}
        family_json=json.dumps(family,sort_keys=True,separators=(',',':'))
        family['manifest_sha256']=hashlib.sha256(family_json.encode('ascii')).hexdigest()
        family['session_sha256']=hashlib.sha256((context.request_sha256+family['manifest_sha256']).encode('ascii')).hexdigest()
        walks=CandidateWalks(axes,start,end,radius,start_d,end_d,*minima,penalty,context.execution_limits,ledger)
        session=_PhysicalVerificationSession(ledger)
        for realization in walks:
            ledger.set_cursor('candidate_canonicalization')
            ledger.debit('candidate_canonicalization',len(realization.pieces))
            ledger.debit('candidate_identity_probe_reservation',32*(2*len(realization.pieces)+1))
            try:
                candidate=canonical_candidate(base,requirements,context,realization,reserved_ids=reserved_ids)
            except (ContractError, ValueError):
                uncertain=True;ranking_resolved=False
                outcomes.append({'status':'numerically_unresolved','reason':'generated_candidate_closure'})
                continue
            proof=session.verify(base,candidate,source_json=raw)
            proof_document=proof.to_dict()
            proof_work=proof_document.get('session_work',{})
            outcomes.append({'status':proof.status,'reason':proof.reason,
                'candidate_sha256':proof_document.get('authority',{}).get('candidate_sha256'),
                'charged_at_start':proof_work.get('charged_at_candidate_start'),
                'candidate_charged':proof_work.get('candidate_charged'),
                'charged_cumulative':proof_work.get('charged_cumulative')})
            if proof.status=='verified_route':
                objective=realization.objective(penalty)
                if best is None:order=-1
                else:
                    old_ranking=ranking_resolved;ranking_resolved=False
                    ledger.debit('objective_comparison_reservation',512)
                    order=compare_objectives(objective,best_objective)
                    ranking_resolved=old_ranking and order is not None
                if order is None:
                    ranking_resolved=False;uncertain=True
                if order==0:
                    # Canonical geometry tie-break, independent of source order.
                    signature=tuple((p.kind,p.start,p.end,p.normal or (),p.turn) for p in realization.pieces)
                    old_signature=best_signature
                    order=( (realization.bends,signature) > (best_bends,old_signature) )-( (realization.bends,signature) < (best_bends,old_signature) )
                if order==-1:
                    best=candidate;best_proof=proof;best_objective=objective
                    best_bends=realization.bends
                    best_signature=tuple((p.kind,p.start,p.end,p.normal or (),p.turn) for p in realization.pieces)
            elif proof.status in ('resource_limit','invalid_input','unsupported_input'):
                raise VerificationStop(proof.status,proof.reason)
            elif proof.status=='numerically_unresolved':uncertain=True;ranking_resolved=False
        search_complete=walks.exhausted
    except PhysicalRoutingAdmissionError as exc:
        stop_status,stop_reason=exc.category,exc.reason
    except VerificationStop as exc:
        stop_status,stop_reason=exc.status,exc.reason
    except (ContractError, AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        stop_status,stop_reason='invalid_input','malformed_source_or_request'
    if best is not None:
        status='verified_route';reason='verified_incumbent'
    else:
        status=stop_status or ('numerically_unresolved' if uncertain else 'search_exhausted')
        reason=stop_reason or ('unresolved_candidates' if uncertain else 'finite_family_exhausted')
    report={'algorithm_profile':SEARCH_PROFILE,'status':status,'reason':reason,
        'source_admission':source_admission,'search_complete':search_complete,
        'authority':{} if source_admission is None else {'source_model_sha256':context.source_model_sha256,'request_sha256':context.request_sha256},
        'search_stop_status':stop_status,'search_stop_reason':stop_reason,
        'unfinished_cursor':None if search_complete or ledger is None else ledger.cursor.to_dict(),
        'remaining_count':0 if search_complete else 'unknown',
        'ranking_resolved':ranking_resolved,'optimality_claim':'none',
        'selection_guarantee':'comparison_unresolved' if not ranking_resolved else 'best_verified_among_examined',
        'family':family,
        'objective':None if best_objective is None else {'unit':'nanometres','rational_term':str(best_objective[0]),'pi_coefficient':str(best_objective[1])},
        'candidates':outcomes,'work':None if ledger is None else {'limit':ledger.limit,'charged':ledger.used,'charged_by_kind':dict(ledger.counters)},
        'search':None if walks is None else {'pushed_states':walks.pushed,'expanded_states':walks.expanded,'neighbor_trials':walks.attempted,'candidate_count':walks.candidates,'realization_attempts':walks.realization_attempts},
        'verification':None if best_proof is None else best_proof.to_dict()}
    text=json.dumps(report,sort_keys=True,separators=(',',':'))
    if len(text)>8*1024*1024:
        return PhysicalGenerationResult('resource_limit','generation_report_byte_limit',None,
            '{"reason":"generation_report_byte_limit","status":"resource_limit"}')
    return PhysicalGenerationResult(status,reason,best,text)
