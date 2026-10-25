"""Independent source-PDF stress and boundary evidence for exact candidate lookup."""
import math
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from oabm.importers.pdf_electrical import importer as api


def _pdf(path,pages):
    writer=PdfWriter()
    for commands in pages:
        page=writer.add_blank_page(width=800,height=800)
        stream=DecodedStreamObject();stream.set_data(('\n'.join(commands)).encode('ascii'))
        page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(path)
    assert not path.with_suffix('.expected.json').exists()
    return api.extract_pdf(path)


def _old_index(vectors,monkeypatch):
    # Restore the exact pre-change candidate cell width; predicates and ordering
    # are unchanged. This produces the full old clusters, not just count totals.
    with monkeypatch.context() as m:
        m.setattr(api,'_GLYPH_CLUSTER_INDEX_CELL_PT',api._GLYPH_PATH_MAX_EXTENT_PT+api._GLYPH_CLUSTER_GAP_PT)
        return api._cluster_small_vector_glyphs(vectors)


def _independent_components(vectors):
    boxes={v.element_id:(min(x for x,y in v.points_pt),min(y for x,y in v.points_pt),
                          max(x for x,y in v.points_pt),max(y for x,y in v.points_pt)) for v in vectors}
    observations={v.element_id:v for v in vectors if boxes[v.element_id][2]-boxes[v.element_id][0]<=54
                  and boxes[v.element_id][3]-boxes[v.element_id][1]<=54}
    adjacent={key:set() for key in observations}
    keys=sorted(observations)
    for n,a in enumerate(keys):
        for b in keys[n+1:]:
            if observations[a].page!=observations[b].page:continue
            first,second=boxes[a],boxes[b]
            width=max(first[2],second[2])-min(first[0],second[0])
            height=max(first[3],second[3])-min(first[1],second[1])
            dx=max(first[0]-second[2],second[0]-first[2],0)
            dy=max(first[1]-second[3],second[1]-first[3],0)
            if width<=54 and height<=54 and math.hypot(dx,dy)<=2.5:
                adjacent[a].add(b);adjacent[b].add(a)
    groups=[];unseen=set(keys)
    while unseen:
        pending=[min(unseen)];group=set()
        while pending:
            key=pending.pop()
            if key in group:continue
            group.add(key);unseen.discard(key);pending.extend(adjacent[key]-group)
        union=(min(boxes[k][0] for k in group),min(boxes[k][1] for k in group),
               max(boxes[k][2] for k in group),max(boxes[k][3] for k in group))
        if union[2]-union[0]<=54 and union[3]-union[1]<=54:groups.append(tuple(sorted(group)))
    return sorted(groups)


def test_boundaries_wide_boxes_chains_and_pages_match_old_and_pairwise_reference(tmp_path,monkeypatch):
    commands=['-12 -3 m -10 -3 l S','-7.5 -3 m -6 -3 l S',
        '9 20 m 10 20 l S','12.5 20 m 14 20 l S',
        '30 40 m 31 40 l S','32.5 42 m 33 42 l S',
        '50 60 m 51 60 l S','53.5000001 60 m 54.5 60 l S',
        '80 100 m 134 100 l S','80 102.5 m 134 102.5 l S',
        '180 200 m 233.999999 200 l S','234 200 m 235 200 l S',
        '300 300 m 350 300 l S','352 300 m 402 300 l S',
        '450 450 8 8 re f','458 450 8 8 re S',
        '500 500 m 505 510 515 510 520 500 c S',
        '560 560 m 560.0000001 560.0000001 l S']
    document=_pdf(tmp_path/'boundaries.pdf',[commands,commands])
    current=api._cluster_small_vector_glyphs(document.vectors)
    assert current==_old_index(document.vectors,monkeypatch)
    assert current==api._cluster_small_vector_glyphs(tuple(reversed(document.vectors)))
    assert sorted(tuple(sorted(c.source_element_ids)) for c in current)==_independent_components(document.vectors)


def test_dense_independent_glyphs_keep_every_cluster_with_fewer_pair_checks(tmp_path,monkeypatch):
    commands=[]
    for row in range(30):
        for col in range(100):
            x,y=10+6*col,10+6*row
            commands += [f'{x-.25} {y} m {x+.25} {y} l S',f'{x} {y-.25} m {x} {y+.25} l S']
    document=_pdf(tmp_path/'dense.pdf',[commands]);original=api._bbox_union;calls=[]
    def counted(a,b):
        calls.append(None);return original(a,b)
    monkeypatch.setattr(api,'_bbox_union',counted)
    old=_old_index(document.vectors,monkeypatch);old_calls=len(calls);calls.clear()
    current=api._cluster_small_vector_glyphs(document.vectors)
    assert current==old and len(current)==3000
    assert all(len(cluster.source_element_ids)==2 for cluster in current)
    assert {key for c in current for key in c.source_element_ids}=={v.element_id for v in document.vectors}
    assert len(calls)*6<old_calls


def test_deterministic_mixed_width_sources_match_full_old_clusters(tmp_path,monkeypatch):
    import random
    for seed in (17,29,83):
        rng=random.Random(seed);commands=[]
        for index in range(100):
            x=10*rng.randrange(-3,55)+rng.choice((0.,.0000001,2.5,9.9999999))
            y=10*rng.randrange(-3,55)+rng.choice((0.,2.5,9.9999999))
            width=rng.choice((0.,.5,2.5,10.,53.9999999,54.,54.0000001))
            height=rng.choice((0.,.5,2.5,10.,54.))
            commands.append(f'{x:.9f} {y:.9f} m {x+width:.9f} {y+height:.9f} l S')
        document=_pdf(tmp_path/f'mixed-{seed}.pdf',[commands,commands])
        current=api._cluster_small_vector_glyphs(document.vectors)
        assert current==_old_index(document.vectors,monkeypatch)
        assert sorted(tuple(sorted(c.source_element_ids)) for c in current)==_independent_components(document.vectors)
