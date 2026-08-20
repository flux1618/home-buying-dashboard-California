"""Enforces ADR 0002: the scoring core imports nothing that touches a network.

Without this test the architecture decision is a comment in a markdown file. With it,
CI fails the moment someone adds `import requests` to a scoring module — which is the
exact shortcut that turns a testable engine back into an untestable one.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parents[1] / "analyzer" / "core"

FORBIDDEN = {
    # network
    "requests", "httpx", "urllib", "urllib3", "http", "socket", "aiohttp", "ftplib",
    # frameworks
    "fastapi", "flask", "starlette", "uvicorn", "django",
    # models
    "openai", "anthropic", "ollama", "transformers", "litellm",
    # db
    "sqlite3", "psycopg2", "sqlalchemy", "asyncpg",
    # third-party config we deliberately avoid in favour of stdlib tomllib
    "yaml", "pydantic",
}

CORE_MODULES = sorted(CORE.glob("*.py"))


def top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def test_core_has_modules_to_check():
    assert CORE_MODULES, "no core modules found — the check would vacuously pass"


@pytest.mark.parametrize("module", CORE_MODULES, ids=lambda p: p.name)
def test_core_module_imports_nothing_forbidden(module: pathlib.Path):
    offending = top_level_imports(module) & FORBIDDEN
    assert not offending, (
        f"{module.name} imports {sorted(offending)}, violating ADR 0002. "
        f"The scoring core must stay pure — put network calls in a source adapter."
    )


def test_core_never_imports_the_sources_layer():
    """The dependency arrow points one way: sources -> core, never back.

    This is the structural half of ADR 0002. The import ban above stops a network call
    from being added to a scoring module directly; this stops it from arriving by proxy
    through a station. If the core ever needs something from `sources/`, the fact belongs
    in `PropertyFacts` instead.
    """
    offenders = {}
    for module in CORE_MODULES:
        text = module.read_text()
        tree = ast.parse(text, filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sources"):
                offenders[module.name] = node.module
            if isinstance(node, ast.ImportFrom) and node.level and node.module == "sources":
                offenders[module.name] = "..sources"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("analyzer.sources"):
                        offenders[module.name] = alias.name
    assert not offenders, (
        f"core modules import the sources layer: {offenders}. "
        f"That inverts the dependency and makes the engine untestable offline."
    )


# Facts a station may provide that are deliberately *not* scoring inputs. Everything a
# station provides normally has to land on PropertyFacts, because a fact that reaches
# nothing is dead weight and usually means a typo. These are the documented exceptions:
# reporting-only facts that travel on the analysis document for a human to read and are
# invisible to the engine.
#
# Adding to this set is a design decision, not a fix. If a value should move the score,
# put it on PropertyFacts and score it. If it should inform without deciding, put it here
# and write down why.
REPORTING_ONLY_FACTS = {
    # FEMA National Risk Index hazard percentiles. Caveats, never deductions — the ratings
    # are binned per hazard rather than on a shared scale, so they cannot be combined into
    # a defensible points penalty, and pricing hazard exposure is what an insurance quote
    # is for. See docs/adr/0009-hazard-risk-is-a-caveat.md.
    "hazard_profile",
    # Bounded Redfin aggregate context; it is neither property- nor street-level and does not
    # score. It is intentionally exposed explicitly rather than read on every property analysis.
    "market_velocity",
}


def test_every_station_declares_what_it_provides():
    """A station may only write facts it declared, so the data flow stays readable."""
    from analyzer.core.scoring import PropertyFacts
    from analyzer.pipeline import build_stations
    from analyzer.core.profile import load_profile

    known = set(PropertyFacts.__dataclass_fields__) | REPORTING_ONLY_FACTS
    for station in build_stations(load_profile(), {}):
        assert station.name, "every station needs a name"
        undeclared = set(station.provides) - known
        assert not undeclared, (
            f"station {station.name!r} claims to provide {sorted(undeclared)}, "
            f"which are neither fields on PropertyFacts nor declared reporting-only "
            f"in REPORTING_ONLY_FACTS"
        )


def test_reporting_only_facts_really_are_invisible_to_the_engine():
    """The exemption above must stay an exemption, not become a back door.

    If a name appears both in REPORTING_ONLY_FACTS and on PropertyFacts, the comment
    claiming it does not affect scoring is false and every score in the decision journal
    quietly stops being reproducible.
    """
    from analyzer.core.scoring import PropertyFacts

    overlap = REPORTING_ONLY_FACTS & set(PropertyFacts.__dataclass_fields__)
    assert not overlap, (
        f"{sorted(overlap)} is documented as reporting-only but is a scoring input"
    )


def test_core_is_importable_with_no_third_party_deps():
    """Smoke test: a full analysis runs on a stdlib-only interpreter."""
    from datetime import date

    from analyzer.core.analyze import analyze
    from analyzer.core.profile import load_profile
    from analyzer.core.scoring import PropertyFacts

    doc = analyze(
        PropertyFacts(price=880_000, sqft=1600, beds=3, baths=3, garage_spaces=2,
                      year_built=2015, flood_zone="X", water_sewer="public",
                      commute_min=15.0, fiber_available=True,
                      school_district="Alhambra Unified School District",
                      school_proficiency_pct=58.5, school_zone_of_choice=False),
        load_profile(),
        date.today().year,
        address="test",
    )
    assert doc["score"]["verdict"] == "TAKE"
