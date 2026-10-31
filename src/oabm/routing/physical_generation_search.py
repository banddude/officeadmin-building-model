"""Deterministic bounded control walks, never sharp-polyline collision tests."""
from dataclasses import dataclass
import heapq
import itertools
from fractions import Fraction
from .physical_generation_math import direction, dot, sub, realize
from .physical_verify_types import VerificationStop

SEARCH_PROFILE = 'physical-cardinal-control-walk-v1'
# Fixed implementation retention bound, separately disclosed from work limits.
STATE_RETENTION_BYTES = 64 * 1024 * 1024
STATE_RESERVED_BYTES = 1024
MAX_BENDS = 511

@dataclass(frozen=True, slots=True)
class State:
    point: tuple[int, int, int]
    parent: int
    incoming: tuple[int, int, int] | None
    depth: int
    walked: int

class CandidateWalks:
    """Stream realizations. Caller verifies each before requesting the next.

    Priority is a deterministic CONTROL-POLYGON heuristic, not an admissible
    physical-cost bound. No global or finite-family optimality is asserted.
    Full parent history is retained; no position/direction dominance pruning.
    """
    def __init__(self, axes, start, end, radius, start_direction, final_direction,
                 start_min, end_min, between_min, bend_penalty, limits, ledger):
        self.axes=axes; self.start=start; self.end=end; self.radius=radius
        self.start_direction=start_direction; self.final_direction=final_direction
        self.start_min=start_min; self.end_min=end_min; self.between_min=between_min
        self.bend_penalty=bend_penalty; self.limits=limits; self.ledger=ledger
        self.pushed=0; self.expanded=0; self.attempted=0; self.candidates=0
        self.exhausted=False; self.seen=set(); self.signature_bytes=0; self.realization_attempts=0

    def _path(self, states, index, target):
        result=[target]
        while index>=0:
            self.ledger.set_cursor('search_history_step', exact=(('state_index_dimensionless',Fraction(index)),))
            self.ledger.debit('search_history_step')
            state=states[index];result.append(state.point);index=state.parent
        result.reverse()
        return tuple(result)

    def _realize(self,path):
        ledger=self.ledger
        ledger.set_cursor('candidate_realization',exact=(('candidate_ordinal_dimensionless',Fraction(self.candidates)),))
        ledger.debit('candidate_signature_check',len(path))
        if path in self.seen:return None
        ledger.debit('candidate_realization',len(path));self.realization_attempts+=1
        try:
            result=realize(path,self.radius,self.start_direction,self.final_direction,
                           self.start_min,self.end_min,self.between_min)
        except ValueError:return None
        if self.candidates==self.limits.candidate_routes:
            raise VerificationStop('resource_limit','candidate_route_limit')
        reserved=len(path)*256
        if self.signature_bytes+reserved>16*1024*1024:
            raise VerificationStop('resource_limit','candidate_signature_retention_limit')
        self.signature_bytes+=reserved;self.seen.add(path);self.candidates+=1
        return result

    def _priority_paths(self):
        # Every proposed corner must belong to the same declared Cartesian
        # family. This prefix changes examination order, never proof criteria.
        yield (self.start,self.end)
        for order in itertools.permutations(range(3)):
            points=[self.start];p=self.start
            for axis in order:
                if p[axis]!=self.end[axis]:
                    p=tuple(self.end[i] if i==axis else p[i] for i in range(3));points.append(p)
            yield tuple(points)
        a=tuple(self.start[i]+self.start_direction[i]*(self.radius+max(1,self.start_min)) for i in range(3))
        b=tuple(self.end[i]-self.final_direction[i]*(self.radius+max(1,self.end_min)) for i in range(3))
        if any(a[i] not in self.axes[i] or b[i] not in self.axes[i] for i in range(3)):return
        for order in itertools.permutations(range(3)):
            points=[self.start,a];p=a
            for axis in order:
                if p[axis]!=b[axis]:
                    p=tuple(b[i] if i==axis else p[i] for i in range(3));points.append(p)
            points.append(self.end)
            yield self._coalesce(points)
        if self.start_direction!=self.final_direction:return
        run_axis=next(i for i in range(3) if self.start_direction[i])
        if any(self.start[i]!=self.end[i] for i in range(3) if i!=run_axis):return
        for axis in range(3):
            if axis==run_axis:continue
            # Tuple ordering implements exact distance, then coordinate tie.
            from .physical_verify_types import reserved_sort
            offsets=reserved_sort(self.axes[axis],self.ledger,key=lambda v:(abs(v-self.start[axis]),v))
            for value in offsets:
                if value==self.start[axis]:continue
                p=tuple(value if i==axis else a[i] for i in range(3))
                q=tuple(value if i==axis else b[i] for i in range(3))
                yield (self.start,a,p,q,b,self.end)

    @staticmethod
    def _coalesce(points):
        out=[]
        for point in points:
            if out and point==out[-1]:continue
            if len(out)>1 and direction(out[-2],out[-1])==direction(out[-1],point):out[-1]=point
            else:out.append(point)
        return tuple(out)

    def __iter__(self):
        ledger=self.ledger
        ledger.set_cursor('priority_candidate_prefix')
        priority_valid=0
        for path in self._priority_paths():
            ledger.debit('priority_candidate_trial')
            candidate=self._realize(path)
            if candidate is not None:
                priority_valid+=1
                yield candidate
                if priority_valid==16:break
        ledger.set_cursor('candidate_search')
        ledger.debit('search_state_push')
        states=[State(self.start,-1,None,0,0)]
        queue=[(sum(abs(a-b) for a,b in zip(self.start,self.end)),0,0)]
        self.pushed=1
        while queue:
            ledger.set_cursor('candidate_search')
            ledger.debit('search_heap_pop',max(1,len(queue).bit_length()))
            _,_,index=heapq.heappop(queue);state=states[index];self.expanded+=1
            for axis in range(3):
                for coordinate_index,coordinate in enumerate(self.axes[axis]):
                    ledger.set_cursor('search_neighbor_trial',exact=(('state_index_dimensionless',Fraction(index)),('depth_dimensionless',Fraction(state.depth)),('axis_dimensionless',Fraction(axis)),('coordinate_index_dimensionless',Fraction(coordinate_index)),('candidate_ordinal_dimensionless',Fraction(self.candidates))))
                    ledger.debit('search_neighbor_trial');self.attempted+=1
                    if coordinate==state.point[axis]:continue
                    q=tuple(coordinate if i==axis else state.point[i] for i in range(3))
                    d=direction(state.point,q)
                    if state.incoming is None:
                        if d!=self.start_direction:continue
                    elif dot(state.incoming,d)!=0:continue
                    if state.depth>MAX_BENDS:continue
                    edge=abs(coordinate-state.point[axis])
                    if q==self.end and d==self.final_direction:
                        path=self._path(states,index,q)
                        candidate=self._realize(path)
                        if candidate is not None:yield candidate
                        # This coordinate can ALSO be a rounded virtual corner
                        # in a later loop; don't equate it to an occupied endpoint.
                    if state.depth==MAX_BENDS:continue
                    trim=self.radius if state.incoming is None else 2*self.radius
                    minimum=self.start_min if state.incoming is None else self.between_min
                    if edge<trim+max(1,minimum):continue
                    if self.pushed==self.limits.pushed_search_states:
                        raise VerificationStop('resource_limit','pushed_search_state_limit')
                    if (self.pushed+1)*STATE_RESERVED_BYTES>STATE_RETENTION_BYTES:
                        raise VerificationStop('resource_limit','search_retention_limit')
                    ledger.set_cursor('search_state_push',exact=(('parent_state_index_dimensionless',Fraction(index)),('next_state_index_dimensionless',Fraction(self.pushed))))
                    ledger.debit('search_state_push')
                    ledger.debit('search_heap_push',max(1,(len(queue)+1).bit_length()))
                    new=State(q,index,d,state.depth+1,state.walked+edge)
                    priority=new.walked+sum(abs(a-b) for a,b in zip(q,self.end))+self.bend_penalty*state.depth
                    heapq.heappush(queue,(priority,state.depth,self.pushed))
                    states.append(new);self.pushed+=1
        self.exhausted=True
