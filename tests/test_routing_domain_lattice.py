"""Prune unreachable coordinate planes without weakening swept containment."""
from dataclasses import replace

import pytest

from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.model import Point3
from oabm.routing import RoutingOptions, RoutingError, derive_suspended_ceiling_route_domain
from oabm.routing import router


def context():
    model=synthetic_wall_inserts_ceiling_assembly()
    options=RoutingOptions(clearance_m=.005)
    domain=derive_suspended_ceiling_route_domain(model,'assembly:ceiling-proof',
                                                nominal_diameter_m=.02,options=options)
    geometry=router._collect_routing_geometry(model,'emt',.01,options,allowed_domain=domain)
    return model,options,geometry


def test_combined_framing_plenum_lattice_excludes_impossible_planes():
    model,options,geometry=context()
    start=Point3(x=.2,y=.3,z=2.5);end=Point3(x=4.6,y=.3,z=2.5)
    axes=router._candidate_coordinates(model,start,end,geometry,options)
    assert len(axes[0])*len(axes[1])*len(axes[2])<=router._MAX_ALLOWED_DOMAIN_GRID_NODES
    assert axes==router._candidate_coordinates(model,start,end,geometry,options)
    for values,lo,hi in zip(axes,('min_x','min_y','min_z'),('max_x','max_y','max_z')):
        assert all(any(getattr(b,lo)-1e-6<=v<=getattr(b,hi)+1e-6 for b in geometry.allowed_domain.volumes)
                   for v in values)
    # The complete checked geometry is retained, including wall framing below
    # the plenum; no obstacle or source identity was removed to meet the cap.
    assert {c.id for c in model.assembly_components}<={r.id for r in geometry.hard_blockers}


def test_in_domain_lattice_cap_still_refuses(monkeypatch):
    model,options,geometry=context()
    monkeypatch.setattr(router,'_MAX_ALLOWED_DOMAIN_GRID_NODES',1)
    with pytest.raises(RoutingError,match='node limit'):
        router._candidate_coordinates(model,Point3(x=.2,y=.3,z=2.5),Point3(x=4.6,y=.3,z=2.5),geometry,options)


def test_unbounded_candidate_generation_is_unchanged_by_pruning():
    model,options,geometry=context()
    unbounded=replace(geometry,allowed_domain=None)
    axes=router._candidate_coordinates(model,Point3(x=.2,y=.3,z=2.5),Point3(x=4.6,y=.3,z=2.5),unbounded,options)
    assert min(axes[2])<0 and max(axes[2])>3


def test_bounded_state_dominance_preserves_cost_bend_tradeoffs():
    dominates=router._dominated_route_state
    assert dominates([(2,3.)],3,4.)
    assert not dominates([(2,3.)],1,4.)
    assert not dominates([(2,3.)],3,2.)
    assert not dominates([(2,3.)],2,3.,strict=True)
    assert dominates([(2,3.)],3,3.,strict=True)
    assert not dominates([(2,3.+1e-13)],3,3.)
