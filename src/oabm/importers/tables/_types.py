"""Value types produced by deterministic table extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_BBOX_DECIMALS = 3
_CONFIDENCE_DECIMALS = 4

# Confidence is a fixed linear blend of three deterministic signals so the
# same definition covers every backend. Camelot reports accuracy and
# whitespace percentages; Docling does not, so those terms drop out there.
_ACCURACY_WEIGHT = 0.6
_WHITESPACE_WEIGHT = 0.2
_DENSITY_WEIGHT = 0.2


def _round_pt(value: float) -> float:
    return round(float(value), _BBOX_DECIMALS)


def normalize_cell_text(text: Any) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends.

    Cell text is whitespace-normalized and nothing else; glyphs are never
    rewritten, so unresolved font encodings stay visible instead of being
    silently repaired.
    """

    if text is None:
        return ""
    return " ".join(str(text).split())


def table_confidence(
    accuracy_pct: float | None,
    whitespace_pct: float | None,
    n_cells: int,
    n_nonempty: int,
) -> float:
    """Return the documented deterministic confidence score in ``[0, 1]``.

    ``confidence = 0.6 * accuracy/100 + 0.2 * (100 - whitespace)/100
    + 0.2 * nonempty_fraction``

    - ``accuracy`` is the backend's cell-level accuracy percentage (Camelot
      parsing report); ``None`` for backends without one (Docling).
    - ``whitespace`` is the percentage of cells holding only whitespace
      (0 is a clean table, per Camelot's parsing report); ``None`` when
      unavailable.
    - ``nonempty_fraction`` is the fraction of cells with visible text, which
      every backend can produce.

    Unavailable terms contribute nothing, so a Docling table with fully dense
    text scores 0.2. All inputs are deterministic given the same PDF.
    """

    density = (n_nonempty / n_cells) if n_cells else 0.0
    score = _DENSITY_WEIGHT * density
    if accuracy_pct is not None:
        score += _ACCURACY_WEIGHT * max(0.0, min(100.0, float(accuracy_pct))) / 100.0
    if whitespace_pct is not None:
        clean = max(0.0, min(100.0, 100.0 - float(whitespace_pct)))
        score += _WHITESPACE_WEIGHT * clean / 100.0
    return round(score, _CONFIDENCE_DECIMALS)


@dataclass(frozen=True, slots=True)
class ExtractedCell:
    """One table cell in displayed, bottom-origin PDF points."""

    text: str
    bbox_pt: tuple[float, float, float, float]
    row: int
    col: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_cell_text(self.text))
        bbox = tuple(_round_pt(v) for v in self.bbox_pt)  # type: ignore[arg-type]
        if len(bbox) != 4:
            raise ValueError("cell bbox_pt must hold four coordinates")
        object.__setattr__(self, "bbox_pt", bbox)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        return {
            "text": self.text,
            "bbox_pt": list(self.bbox_pt),
            "row": self.row,
            "col": self.col,
        }


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """One extracted table, rows in the page's displayed reading order.

    ``bbox_pt`` is the table's bounding box in displayed, bottom-origin PDF
    points (the repo's convention). ``rows`` holds :class:`ExtractedCell`
    tuples in reading order of the page as displayed. For ``/Rotate`` 90
    and 270 that is the pre-rotation content layout (the logical row stays
    symbol-plus-description); for ``/Rotate`` 180 the sheet is shown upside
    down, so rows and columns both reverse. Consumers that need a specific
    order can sort deterministically by cell ``bbox_pt``.
    """

    bbox_pt: tuple[float, float, float, float]
    rows: tuple[tuple[ExtractedCell, ...], ...]
    backend: str
    flavor: str
    confidence: float
    warnings: tuple[str, ...] = field(default=())
    n_rows: int = field(init=False)
    n_cols: int = field(init=False)

    def __post_init__(self) -> None:
        bbox = tuple(_round_pt(v) for v in self.bbox_pt)  # type: ignore[arg-type]
        if len(bbox) != 4:
            raise ValueError("table bbox_pt must hold four coordinates")
        object.__setattr__(self, "bbox_pt", bbox)
        object.__setattr__(
            self, "rows", tuple(tuple(cell for cell in row) for row in self.rows)
        )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must lie in [0, 1]")
        object.__setattr__(self, "confidence", round(float(self.confidence), _CONFIDENCE_DECIMALS))
        object.__setattr__(self, "warnings", tuple(str(w) for w in self.warnings))
        object.__setattr__(self, "n_rows", len(self.rows))
        object.__setattr__(self, "n_cols", len(self.rows[0]) if self.rows else 0)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        return {
            "bbox_pt": list(self.bbox_pt),
            "rows": [[cell.to_dict() for cell in row] for row in self.rows],
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "backend": self.backend,
            "flavor": self.flavor,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }
