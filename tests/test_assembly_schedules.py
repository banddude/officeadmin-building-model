"""Physical records in schedules alongside actual assembly cuts, no duplicate takeoff."""
from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
from oabm.drawings import generate_schedules, generate_drawing_set, PlanSpec, VisibilityPolicy
from oabm.quantities import extract_quantities


def test_completed_room_schedules_include_every_parent_and_physical_child_once():
    model = synthetic_enclosed_room_assembly()
    before = model.to_json()
    quantities = extract_quantities(model).to_json()
    schedules = {s.id: s for s in generate_schedules(model)}
    parents = schedules['schedule:building-assemblies']
    children = schedules['schedule:assembly-components']
    assert {row.source_id for row in parents.rows} == {a.id for a in model.building_assemblies}
    assert len(parents.rows) == len(model.building_assemblies)
    assert {row.source_id for row in children.rows} == {c.id for c in model.assembly_components}
    assert len(children.rows) == len(model.assembly_components)
    assert all(row.cells[-2:] == ('unknown', 'unknown') for row in children.rows)
    assert 'not takeoff' in parents.title
    assert model.to_json() == before and extract_quantities(model).to_json() == quantities


def test_cut_drawing_set_keeps_schedules_and_their_source_identities():
    model = synthetic_enclosed_room_assembly()
    drawing = generate_drawing_set(model, assembly_geometry='cut', plans=(PlanSpec(
        id='plan:room', level_id=model.levels[0].id, visibility=VisibilityPolicy(labels=False, dimensions=False)),))
    assert drawing.views and drawing.views[0].primitives
    indexed = {source.canonical_id for source in drawing.source_index}
    rows = [row for schedule in drawing.schedules for row in schedule.rows]
    assert rows and all(row.source_id in indexed for row in rows)
    assert any(s.id == 'schedule:assembly-components' for s in drawing.schedules)
    assert any(p.layer == 'architecture:assembly-cuts' for p in drawing.views[0].primitives)
