"""Independent tiny exact union/complement and real-solid checks."""
from fractions import Fraction as F
import itertools
import pytest

from oabm.routing.physical_domain import verify_domain, check_box
from oabm.routing.physical_verify import adapt_primitives
from oabm.routing.physical_verify_types import WorkLedger, VerificationStop
from physical_route_fixture import make_physical_route

def box(xlo,xhi,ylo=-1,yhi=1,zlo=-1,zhi=1):
    return tuple((F(a),F(b)) for a,b in ((xlo,xhi),(ylo,yhi),(zlo,zhi)))


def primitives():
    return adapt_primitives(make_physical_route(),WorkLedger(100))


def test_union_internal_touching_seam_is_not_forbidden():
    ledger=WorkLedger(10000)
    verify_domain(primitives(),(box(-1,1),box(1,3)),F(1,100),ledger,max_depth=64)
    assert dict(ledger.counters)["domain_interval_merge"] > 0
    assert "domain_gap_cell" not in dict(ledger.counters)


def test_union_bbox_does_not_fill_actual_gap():
    with pytest.raises(VerificationStop) as error:
        verify_domain(primitives(),(box(-1,F(9,10)),box(F(11,10),3)),F(1,100),WorkLedger(10000),max_depth=64)
    assert error.value.status == "rejected_route"
    assert error.value.reason == "domain_contact_or_overlap"


def test_union_covered_corner_with_overlapping_boxes():
    ledger=WorkLedger(10000)
    verify_domain(primitives(),(box(-1,F(3,2)),box(F(1,2),3)),F(1,100),ledger,max_depth=64)


def test_exterior_uses_flat_cylinder_end_not_capsule():
    # Cap gap .02 meets .01; a capsule would incorrectly consume .01 more.
    verify_domain(primitives(),(box(-F(2,100),F(202,100)),),F(1,100),WorkLedger(10000),max_depth=64)


def test_exact_domain_clearance_equality_is_unresolved():
    with pytest.raises(VerificationStop) as error:
        verify_domain(primitives(),(box(-F(1,100),3),),F(1,100),WorkLedger(10000),max_depth=64)
    assert error.value.status == "numerically_unresolved"


def test_all_covered_scans_charged_even_without_emitted_cells():
    boxes=tuple(box(-1,3,-1+i*F(1,1000),1+i*F(1,1000)) for i in range(20))
    with pytest.raises(VerificationStop) as error:
        verify_domain(primitives(),boxes,F(1,100),WorkLedger(100),max_depth=64)
    assert error.value.status == "resource_limit"


def test_permuted_union_boxes_identical_charged_work_and_trace():
    boxes=(box(-1,1),box(0,2),box(1,3))
    observations=[]
    for ordering in itertools.permutations(boxes):
        ledger=WorkLedger(10000)
        verify_domain(primitives(),ordering,F(1,100),ledger,max_depth=64)
        observations.append((ledger.used,ledger.counters,ledger.trace_sha256))
    assert len(set(observations)) == 1


def test_actual_obstacle_collision_and_safe_flat_terminal_gap():
    primitive,=primitives()
    with pytest.raises(VerificationStop) as error:
        check_box(primitive,box(1,F(11,10),-F(1,100),F(1,100),-F(1,100),F(1,100)),
            F(1,100),WorkLedger(100),ids=("route","obstacle"),max_depth=64,kind="obstacle")
    assert error.value.reason == "obstacle_contact_or_overlap"
    check_box(primitive,box(F(202,100),3),F(1,100),WorkLedger(100),
              ids=("route","obstacle"),max_depth=64,kind="obstacle")


def test_first_precision_evaluation_is_in_root_pair_charge():
    primitive,=primitives()
    ledger=WorkLedger(2)
    check_box(primitive,box(3,4),F(1,100),ledger,
        ids=("segment","box"),max_depth=0,kind="obstacle")
    assert ledger.used == 2 # root pair + trace, no duplicate first refinement
    assert "distance_refinement" not in dict(ledger.counters)
