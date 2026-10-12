"""Physical electrical installation producers; canonical facts live in model."""

from .boxes import BoxCatalogEntry, BoxIntent, select_electrical_boxes

__all__ = ["BoxCatalogEntry", "BoxIntent", "select_electrical_boxes"]
