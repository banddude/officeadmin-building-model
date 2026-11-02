"""Opt-in read-only labor calculations from original canonical contributions."""
from .core import LaborReport, contribution_inventory, generate_labor_report

__all__ = ['LaborReport', 'contribution_inventory', 'generate_labor_report']
