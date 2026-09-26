"""``to_ifc`` output is byte-deterministic.

Exporting one canonical model twice is rendering the same content twice: the
STEP header must not carry the wall clock, and every ``IfcRoot`` GlobalId must
come from model content rather than from ``ifcopenshell.api``'s random
generator or creation order. These tests pin identical bytes across repeated
exports and a fresh process (with a different hash seed, so any hash-order
dependence shows), unique valid GlobalIds, the untouched canonical identity
mapping, and exact round trip with strict EXPRESS validation on every valid
golden case.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.validate
import pytest

from oabm.ifc import canonical_id_to_ifc_guid, round_trip, to_ifc
from oabm.qa import load_golden_cases, load_golden_model

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = ROOT / "fixtures" / "golden" / "v1"
VALID_CASES = tuple(
    sorted(case.name for case in load_golden_cases(GOLDEN_ROOT) if case.valid)
)

_IFC_GUID_RE = re.compile(r"^[0-9A-Za-z_$]{22}$")

# Literal GlobalIds for canonical identity, as main derives them from the
# canonical ids via canonical_id_to_ifc_guid. The determinism pass must never
# touch them; if one of these values drifts, stable identity broke.
_PINNED_CANONICAL_GUIDS = {
    "model:golden-rectangular-room": "1_KLeANYDPffqplyptU7xQ",
    "level:rect-ground": "2ob03zfK5Hb8Am4_7mDIVh",
    "space:rect": "1kwOjD0UXQ88lvgTGK9bNx",
    "wall:rect-south": "08bB$1dDvVM80IQ36AIkP4",
    "route:panel-evse-direct": "15Ar99TXvQ2eIiG1FE6tWr",
}


def _case(name: str):
    return next(item for item in load_golden_cases(GOLDEN_ROOT) if item.name == name)


def _export_bytes(name: str, path: Path) -> bytes:
    to_ifc(load_golden_model(_case(name)), path)
    return path.read_bytes()


def _fresh_process_export(name: str, path: Path) -> None:
    """Export in a brand-new interpreter with a different hash seed."""

    script = path.parent / "export_one.py"
    script.write_text(
        "\n".join(
            (
                "import sys",
                "from oabm.ifc import to_ifc",
                "from oabm.qa import load_golden_cases, load_golden_model",
                f"root = {str(GOLDEN_ROOT)!r}",
                "case = next(",
                "    item for item in load_golden_cases(root) if item.name == sys.argv[1]",
                ")",
                "to_ifc(load_golden_model(case), sys.argv[2])",
            )
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        [src] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PYTHONHASHSEED"] = "424242"
    subprocess.run(
        [sys.executable, str(script), name, str(path)],
        check=True,
        env=env,
        cwd=str(ROOT),
    )


@pytest.mark.parametrize("name", VALID_CASES)
def test_repeated_exports_and_a_fresh_process_are_byte_identical(
    tmp_path: Path, name: str
) -> None:
    first = hashlib.sha256(_export_bytes(name, tmp_path / "first.ifc")).hexdigest()
    second = hashlib.sha256(_export_bytes(name, tmp_path / "second.ifc")).hexdigest()
    assert first == second

    _fresh_process_export(name, tmp_path / "third.ifc")
    third = hashlib.sha256((tmp_path / "third.ifc").read_bytes()).hexdigest()
    assert third == first


@pytest.mark.parametrize("name", VALID_CASES)
def test_every_ifc_root_global_id_is_unique_and_valid(name: str) -> None:
    ifc = to_ifc(load_golden_model(_case(name)))
    guids = [item.GlobalId for item in ifc.by_type("IfcRoot")]

    assert guids, name
    assert len(guids) == len(set(guids))
    for guid in guids:
        assert _IFC_GUID_RE.fullmatch(guid), guid


def test_canonical_global_ids_are_unchanged() -> None:
    for canonical_id, expected in _PINNED_CANONICAL_GUIDS.items():
        assert canonical_id_to_ifc_guid(canonical_id) == expected

    ifc = to_ifc(load_golden_model(_case("rectangular-room")))
    for canonical_id, expected in _PINNED_CANONICAL_GUIDS.items():
        if canonical_id.startswith("route:"):
            continue  # the route's system is authored in the panel-to-evse case
        item = ifc.by_guid(expected)
        assert item.GlobalId == expected


@pytest.mark.parametrize("name", VALID_CASES)
def test_round_trip_is_exact_and_strict_validation_reports_nothing(
    tmp_path: Path, name: str
) -> None:
    model = load_golden_model(_case(name))
    assert round_trip(model).to_dict() == model.to_dict()

    ifc = to_ifc(model, tmp_path / "export.ifc")
    logger = ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(ifc, logger, express_rules=True)
    assert logger.statements == []
