"""Transport regressions use only the independent synthetic symbol fixture."""
from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from oabm.model import ContractError
from oabm.proposals.symbols import SymbolOutput
from oabm.proposals.symbol_transport import (
    normalized_bounds_schema, normalized_point_schema,
    symbol_output_schema, symbol_transport_prompt,
)
from test_symbol_proposals import fixture


def test_normalized_point_output_is_schema_and_domain_valid():
    request, _, _, output = fixture()
    schema = symbol_output_schema(request)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(output.to_dict())
    assert SymbolOutput.from_dict(output.to_dict()) == output


@pytest.mark.parametrize("field,value", [
    ("bounds", [20, 30, 60, 70]), ("anchor", [40, 50]),
    ("polyline", [[20, 30], [60, 70]]),
    ("bounds", [-.01, .1, .5, .6]), ("anchor", [1.01, .5]),
    ("polyline", [[.1, .2], [.5, 1.01]]),
])
def test_pixel_and_out_of_range_geometry_rejected(field, value):
    request, _, _, output = fixture()
    raw = output.to_dict()
    raw["marks"][0][field] = value
    with pytest.raises(ValidationError):
        Draft202012Validator(symbol_output_schema(request)).validate(raw)
    with pytest.raises(ContractError):
        SymbolOutput.from_dict(raw)


def test_normalized_line_and_crop_edges_are_valid():
    request, _, _, output = fixture()
    mark = replace(output.marks[0], category="linear_symbol", anchor=None,
                   bounds=(0, 0, 1, 1), polyline=((0, 0), (.5, .25), (1, 1)))
    raw = replace(output, marks=(mark,)).to_dict()
    Draft202012Validator(symbol_output_schema(request)).validate(raw)
    assert SymbolOutput.from_dict(raw).marks[0] == mark


def test_cross_field_geometry_remains_domain_validated():
    request, _, _, output = fixture()
    raw = output.to_dict()
    raw["marks"][0]["bounds"] = [.4, .3, .2, .1]
    Draft202012Validator(symbol_output_schema(request)).validate(raw)
    with pytest.raises(ContractError):
        SymbolOutput.from_dict(raw)


def test_exact_crop_identity_and_dimensions_are_explicit():
    request, _, _, output = fixture()
    prompt = symbol_transport_prompt(request)
    assert "crop_px=(100, 20, 300, 180); crop_width_px=200; crop_height_px=160" in prompt
    assert "displayed_page_px=400x200" in prompt
    assert "x/crop_width_px and y/crop_height_px" in prompt
    assert "OWN CROP" in prompt and "top-left" in prompt
    assert request.to_json() in prompt
    raw = output.to_dict()
    raw["marks"][0]["image_id"] = "legend"
    with pytest.raises(ValidationError):
        Draft202012Validator(symbol_output_schema(request)).validate(raw)
    assert prompt.index("image_id=field") < prompt.index("image_id=legend")


def test_distinct_ids_remain_distinct_even_for_same_crop():
    request, _, _, output = fixture()
    other = replace(request.images[0], id="second-target")
    request = replace(request, images=(*request.images, other),
                      target_image_ids=("field", "second-target"))
    prompt = symbol_transport_prompt(request)
    assert "image_id=second-target" in prompt
    assert symbol_output_schema(request)["properties"]["marks"]["items"]["properties"]["image_id"]["enum"] == ["field", "second-target"]


def test_schema_and_prompt_are_repeatable_fresh_and_do_not_mutate_request():
    request, _, _, _ = fixture()
    before = request.to_json()
    first = symbol_output_schema(request)
    second = symbol_output_schema(request)
    assert first == second
    first["properties"]["marks"]["items"]["properties"]["bounds"]["items"]["maximum"] = 99
    assert second == symbol_output_schema(request)
    assert symbol_transport_prompt(request) == symbol_transport_prompt(request)
    assert request.to_json() == before


def test_optional_scope_hook_cannot_override_geometry_or_require_historical_scope():
    request, _, _, output = fixture()
    extension = {"type": "object", "properties": {"bounds": normalized_bounds_schema()}}
    saved = deepcopy(extension)
    schema = symbol_output_schema(request, scope_schema=extension)
    mark = schema["properties"]["marks"]["items"]
    assert "scope" not in mark["required"]
    assert mark["properties"]["bounds"] == normalized_bounds_schema()
    assert mark["properties"]["anchor"]["anyOf"][0] == normalized_point_schema()
    Draft202012Validator(schema).validate(output.to_dict())
    extension["properties"].clear()
    assert mark["properties"]["scope"] == saved


def test_strict_transport_omits_unsupported_unique_items_but_domain_rejects_duplicates():
    request, _, _, output = fixture()
    schema = symbol_output_schema(request)
    def visit(value):
        if isinstance(value, dict):
            assert "uniqueItems" not in value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    raw = output.to_dict()
    raw["scanned_image_ids"] = ["field", "field"]
    Draft202012Validator(schema).validate(raw)
    with pytest.raises(ContractError):
        SymbolOutput.from_dict(raw)
