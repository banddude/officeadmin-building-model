"""Explicit caller-confirmed rectangular structural members (#290).

A ``StructuralMember`` is a known framing member — beam, joist, stud,
header, plate or rafter — that a human confirmed from an explicit input.
Nothing here recognizes framing from plans or typical construction: an
absent member is unknown, and recognition under private plans or unknown
other-trade systems stays unresolved in #240.

Members are rectangular solids in their canonical pose. The router treats
each one as conservative hard no-go geometry around its canonical
identity; IFC, GLB, drawings and quantities consume the same object, so
no second structural representation exists anywhere.
"""

from dataclasses import dataclass

from .common import ContractError, Entity, Pose, Size3
from .entities import _validate_id

#: The framing kinds a member may declare. The kind is explicit caller
#: input, never inferred from geometry or typical construction.
STRUCTURAL_MEMBER_KINDS = frozenset({
    "beam", "joist", "stud", "header", "plate", "rafter",
})


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")


def _rotate_vector(
    vector: tuple[float, float, float], quaternion
) -> tuple[float, float, float]:
    """Rotate a vector by a quaternion (q * v * q^-1, expanded)."""

    x, y, z = vector
    qx, qy, qz, qw = quaternion.x, quaternion.y, quaternion.z, quaternion.w
    tx = 2 * (qy * z - qz * y)
    ty = 2 * (qz * x - qx * z)
    tz = 2 * (qx * y - qy * x)
    return (
        x + qw * tx + (qy * tz - qz * ty),
        y + qw * ty + (qz * tx - qx * tz),
        z + qw * tz + (qx * ty - qy * tx),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralMember(Entity):
    """One confirmed rectangular structural member.

    ``material`` stays unknown until a caller supplies it; an unsupplied
    material is never guessed from the kind. Dimensions are positive and
    finite through :class:`Size3`, and the member must sit on a known
    level, which the model validator enforces.
    """

    kind: str
    level_id: str
    pose: Pose
    size: Size3
    material: str | None = None

    def __post_init__(self) -> None:
        super(StructuralMember, self).__post_init__()
        _text(self.kind, f"{self.id}.kind")
        if self.kind not in STRUCTURAL_MEMBER_KINDS:
            raise ContractError(
                f"{self.id}.kind must be one of {sorted(STRUCTURAL_MEMBER_KINDS)!r}, "
                f"got {self.kind!r}"
            )
        _validate_id(self.level_id, f"{self.id}.level_id")
        if not isinstance(self.pose, Pose):
            raise ContractError(f"{self.id}.pose must be a Pose")
        if not isinstance(self.size, Size3):
            raise ContractError(f"{self.id}.size must be a Size3")
        if self.material is not None:
            _text(self.material, f"{self.id}.material")

    def axis_aligned_bounds(self) -> tuple[float, float, float, float, float, float]:
        """Conservative world-space bounds of the posed rectangular solid.

        A rotated member's axis-aligned box covers more volume than the
        member itself; routing accepts that overexclusion deliberately
        rather than penetrate a rotated flange. ``min_x, min_y, min_z,
        max_x, max_y, max_z``.
        """

        half = (self.size.x / 2.0, self.size.y / 2.0, self.size.z / 2.0)
        rotation = self.pose.rotation
        rotated = any((rotation.x, rotation.y, rotation.z))
        corners: list[tuple[float, float, float]] = []
        for sx in (-half[0], half[0]):
            for sy in (-half[1], half[1]):
                for sz in (-half[2], half[2]):
                    local = (sx, sy, sz)
                    point = _rotate_vector(local, rotation) if rotated else local
                    corners.append((
                        self.pose.position.x + point[0],
                        self.pose.position.y + point[1],
                        self.pose.position.z + point[2],
                    ))
        return (
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            min(point[2] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
            max(point[2] for point in corners),
        )
