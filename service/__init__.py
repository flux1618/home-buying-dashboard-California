"""HTTP door onto the analyzer. One engine, another chassis.

This package exists as a *separate top-level package* rather than inside `analyzer/`
for a reason worth stating plainly: `tests/test_core_purity.py` forbids `fastapi` from
appearing anywhere under `analyzer/core/`, and the cheapest way to guarantee that is to
keep the web framework physically outside the package the engine lives in.

The dependency arrow points one way.

    service  ->  analyzer.pipeline  ->  analyzer.core

Nothing in `analyzer/` imports anything from `service/`. That means the CLI, the batch
runner, and the container all keep working with FastAPI uninstalled, and `pip install .`
with no extras still gives you a working stdlib-only engine.

FastAPI is an optional extra (`pip install '.[api]'`) for the same reason.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """Lazy re-export so `import service` does not require FastAPI to be installed."""
    from .app import create_app as factory

    return factory(*args, **kwargs)
