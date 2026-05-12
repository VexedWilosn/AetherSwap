"""Compatibility entrypoints for the deprecated buy pipeline.

The original pipeline depended on iFlow as its deal source. iFlow is no longer
available, so the start hook is intentionally a no-op while the sell-pipeline
export remains available for modules that import it.
"""

from app.sell_pipeline import run_sell_phase_on_inventory_update  # noqa: F401


def start_pipeline(config: dict | None = None) -> None:
    return None

