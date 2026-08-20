"""The station contract.

A station is one source, doing one job. It reads from the context, talks to exactly one
external service, and returns facts wrapped in provenance — or a Degradation explaining
why it could not.

Three rules, and they are what make the pipeline predictable:

  1. A station never raises. Any failure becomes a Degradation.
  2. A station never guesses. If the source is silent, the fact stays None, and None
     means "unknown" to the scoring engine, which handles unknowns explicitly.
  3. A station never writes another station's facts. What it `provides` is declared,
     and the pipeline enforces it.

Only one station is `fatal`: geocoding. Without coordinates there is nothing downstream
to ask about. Every other station going dark degrades the analysis rather than ending it,
which is the behaviour a tool used against real customer infrastructure has to have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.provenance import Degradation, Value
from .http import SourceRejected, SourceUnavailable


@dataclass
class Context:
    """Accumulated state, passed down the line and added to as it goes."""

    address: str
    price: float
    lat: float | None = None
    lon: float | None = None
    matched_address: str | None = None
    census_block_geoid: str | None = None
    county_fips: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Value] = field(default_factory=dict)
    api_keys: dict[str, str] = field(default_factory=dict)

    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class StationResult:
    """What one station produced."""

    station: str
    facts: dict[str, Any] = field(default_factory=dict)
    values: dict[str, Value] = field(default_factory=dict)
    context_updates: dict[str, Any] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    degradation: Degradation | None = None

    @property
    def ok(self) -> bool:
        return self.degradation is None


class Station:
    """Base class. Subclasses implement `fetch`; `run` handles the failure contract."""

    name: str = "unnamed"
    provides: tuple[str, ...] = ()
    fatal: bool = False
    description: str = ""

    # -- subclass interface --------------------------------------------------

    def fetch(self, ctx: Context) -> StationResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def skip_reason(self, ctx: Context) -> str | None:
        """Return a reason string to skip without attempting the network."""
        if not self.fatal and not ctx.has_location():
            return "no coordinates available from geocoding"
        return None

    # -- pipeline interface --------------------------------------------------

    def run(self, ctx: Context) -> StationResult:
        """Never raises. Anything that goes wrong comes back as a Degradation."""
        skip = self.skip_reason(ctx)
        if skip:
            return self.degrade(skip)
        try:
            result = self.fetch(ctx)
        except SourceRejected as exc:
            return self.degrade(f"source declined the request ({exc})")
        except SourceUnavailable as exc:
            return self.degrade(f"source unreachable ({exc})")
        except (LookupError, TypeError, ValueError) as exc:
            # The source answered, but not with what was asked for: a shape change, or
            # simply no record. Both are degradations, not crashes. Public data changes
            # without telling anybody, and `LookupError` covers KeyError and IndexError
            # as well as a station's own "no record found".
            return self.degrade(f"{type(exc).__name__}: {exc}")

        undeclared = set(result.facts) - set(self.provides)
        if undeclared:
            raise AssertionError(
                f"station {self.name!r} wrote undeclared facts: {sorted(undeclared)}"
            )
        return result

    def degrade(self, reason: str) -> StationResult:
        """Report a failure along with the facts it costs us.

        `provides` is the station's own declaration of what it is responsible for, so a
        degradation can name the missing facts without anyone maintaining a second list.
        """
        return StationResult(
            station=self.name,
            degradation=Degradation(
                station=self.name, reason=reason, missing=tuple(self.provides)
            ),
        )

    @staticmethod
    def task(text: str, *, blocking: bool = False, reason: str | None = None) -> dict[str, Any]:
        entry: dict[str, Any] = {"task": text, "blocking": blocking}
        if reason:
            entry["reason"] = reason
        return entry
