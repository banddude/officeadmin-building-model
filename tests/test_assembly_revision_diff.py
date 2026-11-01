"""Actual regenerated room revisions retain physical IDs and measured deltas."""
from dataclasses import replace
import json
import pytest
from oabm.building_assemblies import regenerate_building_assemblies
from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly, synthetic_room_edit_requests
from oabm.qa.revision_diff import build_revision_diff, RevisionDiff


def test_regenerated_room_diff_carries_geometry_product_and_real_quantity_changes():
    before = synthetic_enclosed_room_assembly()
    after = regenerate_building_assemblies(before, synthetic_room_edit_requests())
    frozen = (before.to_json(), after.to_json())
    report = build_revision_diff(before, after)
    assert report.added_count == report.removed_count == 0
    assert report.changed_count > 0
    components = [row for row in report.changes if row.collection == 'assembly_components']
    assignments = [row for row in report.changes if row.collection == 'building_assemblies']
    assert components and assignments
    assert any('geometry' in row.change_classes for row in components)
    assert any('product' in row.change_classes for row in assignments)
    assert any(field.path == 'geometry' for row in components for field in row.field_changes)
    deltas = [row for row in report.quantity_deltas if row.category == 'assembly_component_geometry_volume']
    assert deltas
    # Preserve the existing report contract: re-keyed material variants are
    # explicit added/removed rows with null absent sides, never guessed matches.
    signed = [row.delta if row.status == 'changed' else row.after if row.status == 'added' else -row.before for row in deltas]
    assert all(value is not None for value in signed)
    assert sum(signed) == pytest.approx(.0933381656966453, abs=1e-12)
    assert RevisionDiff.from_dict(json.loads(report.to_json())).to_json() == report.to_json()
    reordered = replace(after, building_assemblies=tuple(reversed(after.building_assemblies)),
                        assembly_components=tuple(reversed(after.assembly_components)))
    assert build_revision_diff(before, reordered).to_json() == report.to_json()
    assert (before.to_json(), after.to_json()) == frozen


def test_regenerated_ceiling_membership_change_is_an_actual_graph_revision():
    from oabm.building_assemblies import CeilingParameterOverride
    from oabm.building_assemblies.room_fixtures import synthetic_room_requests
    before = synthetic_enclosed_room_assembly()
    request = replace(synthetic_room_requests()[-1], parameter_overrides=(
        CeilingParameterOverride(parameter_id='ceiling.module_spacing_m', value_kind='length', unit='m', value=.6096),))
    after = regenerate_building_assemblies(before, (request,))
    report = build_revision_diff(before, after)
    assignment = next(row for row in report.changes if row.entity_id == request.assembly_id)
    assert 'graph' in assignment.change_classes
    assert any(field.path == 'component_ids' for field in assignment.field_changes)
    assert report.added_count and report.removed_count
    assert any(row.collection == 'assembly_components' and row.change == 'added' for row in report.changes)
    assert any(row.collection == 'assembly_components' and row.change == 'removed' for row in report.changes)


def test_provenance_record_reordering_does_not_invent_a_revision():
    from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_source
    source = synthetic_enclosed_room_source()
    wall = source.walls[0]
    p = wall.provenance[0]
    records = (p, replace(p, method=(p.method or 'synthetic') + ':second-source'))
    before = replace(source, walls=(replace(wall, provenance=records), *source.walls[1:]))
    after = replace(before, walls=(replace(before.walls[0], provenance=tuple(reversed(records))), *before.walls[1:]))
    report = build_revision_diff(before, after)
    assert not report.changes and not report.quantity_deltas
