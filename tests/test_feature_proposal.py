"""Synthetic correspondence controls; no private drawing-derived fixtures."""
import numpy as np
import pytest
from oabm.importers.pdf_convergence.feature_proposal import propose_feature_translation


def grid():
    return np.array([(0., 0.), (2., 0.), (4., 0.), (0., 2.), (2., 2.),
                     (4., 2.), (0., 4.), (2., 4.), (4., 4.), (1., 3.)])


def run(a, b, **kw):
    return propose_feature_translation(a, b, source_meters_per_point=1.,
                                      target_meters_per_point=1., **kw)


def test_translation_is_only_a_proposal_and_keeps_printed_scale():
    a=grid();r=run(a,a+[13.,-7.])
    assert r.eligible_for_geometry_validation and not r.accepted
    assert r.translation_target_pt == (13.,-7.)
    assert r.independent_controls == 10
    assert r.rms_residual_m == 0.


def test_source_and_target_printed_scales_are_applied_before_comparison():
    a=grid();r=propose_feature_translation(a,a*2+[26.,-14.],source_meters_per_point=1.,target_meters_per_point=.5)
    assert r.eligible_for_geometry_validation
    assert r.translation_target_pt == (26.,-14.)


def test_permutation_and_duplicate_pairs_do_not_change_result_or_hash():
    a=grid();b=a+[13.,-7.];base=run(a,b)
    assert run(a[::-1],b[::-1]) == base
    assert run(np.vstack([a,a]),np.vstack([b,b])) == base


def test_many_repeated_clustered_features_do_not_outvote_spread_controls():
    a=grid();cluster=np.column_stack((np.arange(100)*.001,np.zeros(100)))+[20.,20.]
    r=run(np.vstack([a,cluster]),np.vstack([a+[13.,-7.],cluster+[80.,30.]]))
    assert r.eligible_for_geometry_validation
    assert r.translation_target_pt == (13.,-7.)


def test_diagonal_collinear_controls_do_not_prove_two_dimensions():
    a=np.column_stack((np.arange(10)*2,np.arange(10)*2))
    assert run(a,a+[13.,-7.]).reason == 'insufficient_two_dimensional_spread'


def test_two_supported_placements_remain_ambiguous():
    a=grid();second=a+[30.,0.]
    r=run(np.vstack([a,second]),np.vstack([a+[13.,-7.],second+[80.,30.]]))
    assert not r.eligible_for_geometry_validation
    assert r.reason == 'competing_feature_placements'


@pytest.mark.parametrize('mirror,quarter',[(True,0),(False,1),(False,2),(False,3)])
def test_unsupported_orientation_is_reported_not_silently_applied(mirror,quarter):
    a=grid();b=a.copy()
    if mirror:b[:,0]*=-1
    for _ in range(quarter):b=np.column_stack((-b[:,1],b[:,0]))
    r=run(a,b+[13.,-7.])
    assert r.reason == 'unsupported_orientation' and not r.accepted


def test_one_target_pixel_cannot_count_as_many_correspondences():
    a=grid();r=run(a,np.zeros_like(a))
    assert not r.eligible_for_geometry_validation


@pytest.mark.parametrize('scale',[0.,-1.,float('nan'),float('inf'),True])
def test_invalid_printed_scales_raise(scale):
    with pytest.raises(ValueError):
        propose_feature_translation(grid(),grid(),source_meters_per_point=scale,target_meters_per_point=1.)


def test_budget_is_enforced_instead_of_silent_sampling():
    with pytest.raises(ValueError,match='budget'):
        run(np.zeros((1501,2)),np.zeros((1501,2)))


def test_nonfinite_points_are_refused():
    a=grid();a[0,0]=np.nan
    with pytest.raises(ValueError,match='finite'):run(a,grid())


def test_overflowing_scaled_coordinates_are_refused():
    with pytest.raises(ValueError,match='scaled coordinates'):
        propose_feature_translation(grid(),grid(),source_meters_per_point=1e308,target_meters_per_point=1.)


def test_raster_adapter_reads_pixels_without_model_mutation():
    cv2=pytest.importorskip('cv2')
    from oabm.importers.pdf_convergence.feature_proposal import propose_raster_translation
    a=np.full((640,640),255,np.uint8)
    rng=np.random.default_rng(13)
    for y in (90,220,350,480):
        for x in (90,220,350,480):
            for _ in range(7):
                dx,dy,ex,ey=rng.integers(-25,26,4)
                cv2.line(a,(int(x+dx),int(y+dy)),(int(x+ex),int(y+ey)),0,2)
    b=np.full((700,720),255,np.uint8);b[30:670,40:680]=a
    before=a.copy()
    r=propose_raster_translation(a,b,source_pixel_origin_pt=(0.,640.),target_pixel_origin_pt=(0.,700.),
        source_points_per_pixel=1.,target_points_per_pixel=1.,source_meters_per_point=.01,target_meters_per_point=.01)
    assert r.eligible_for_geometry_validation and not r.accepted
    assert r.translation_target_pt == pytest.approx((40.,30.),abs=.1)
    assert np.array_equal(a,before)


def test_blank_raster_has_no_proposal_and_fingerprint_covers_pixels():
    pytest.importorskip('cv2')
    from oabm.importers.pdf_convergence.feature_proposal import propose_raster_translation
    args=dict(source_pixel_origin_pt=(0.,100.),target_pixel_origin_pt=(0.,100.),
        source_points_per_pixel=1.,target_points_per_pixel=1.,source_meters_per_point=.01,target_meters_per_point=.01)
    a=np.full((100,100),255,np.uint8)
    r=propose_raster_translation(a,a,**args)
    s=propose_raster_translation(a,a-1,**args)
    assert not r.eligible_for_geometry_validation
    assert r.input_sha256 != s.input_sha256


def test_unrelated_correspondences_do_not_make_a_translation():
    rng=np.random.default_rng(38)
    r=run(rng.uniform(0,30,(100,2)),rng.uniform(0,30,(100,2)))
    assert not r.eligible_for_geometry_validation


def test_seven_well_spread_controls_are_still_insufficient():
    a=grid()[:7]
    assert run(a,a+[13.,-7.]).reason == 'insufficient_correspondences'


def test_raster_input_contract_rejects_non_grayscale_and_bad_origins():
    from oabm.importers.pdf_convergence.feature_proposal import propose_raster_translation
    args=dict(source_pixel_origin_pt=(0.,100.),target_pixel_origin_pt=(0.,100.),
        source_points_per_pixel=1.,target_points_per_pixel=1.,source_meters_per_point=.01,target_meters_per_point=.01)
    with pytest.raises(ValueError,match='grayscale'):
        propose_raster_translation(np.zeros((2,2,3),dtype=np.uint8),np.zeros((2,2),dtype=np.uint8),**args)
    args['source_pixel_origin_pt']=(float('nan'),0.)
    with pytest.raises(ValueError,match='origins'):
        propose_raster_translation(np.zeros((2,2),dtype=np.uint8),np.zeros((2,2),dtype=np.uint8),**args)
