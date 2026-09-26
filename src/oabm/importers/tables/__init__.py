"""Deterministic table extraction for legends, schedules and title blocks.

Reads ruled and alignment-structured tables from vector PDF pages with
Camelot (optional ``tables`` extra), with an optional heavier Docling
backend (``docling`` extra). Output values live in displayed, bottom-origin
PDF points like every other lane, and extraction never calls an AI service.
"""

from ._types import ExtractedCell, ExtractedTable
from ._extract import extract_tables

__all__ = [
    "ExtractedCell",
    "ExtractedTable",
    "extract_tables",
]
