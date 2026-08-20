"""Shared test helpers. Kept out of conftest so test modules can import it directly."""

from __future__ import annotations

import json
from pathlib import Path

RESPONSES = Path(__file__).parent / "fixtures" / "responses"


def load_response(name: str) -> dict:
    """A response recorded off the live API by tools/record_fixtures.py."""
    return json.loads((RESPONSES / f"{name}.json").read_text())
