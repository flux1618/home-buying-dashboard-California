"""FastAPI wrapper around the analyzer.

Design constraints, and why each one is there:

**No new domain logic.** Every endpoint here is a translation layer. It parses a request,
calls `analyzer.pipeline.run` or `analyzer.batch`, and shapes the reply. If a rule about
what a house is worth ever appears in this file it is in the wrong place, because the
CLI and the batch runner would not get it. The endpoints are thin on purpose.

**Degradation is a 200, not a 503.** A missing broadband key or a dead county server does
not mean the request failed — it means the answer has a hole in it, and the answer says
where. Returning an error would throw away the tax, flood, and commute work that did
succeed. The only 4xx/5xx cases are a request that cannot be parsed and an address that
cannot be geocoded, because without coordinates there is nothing to ask anyone about.

**Blocking work in a threadpool.** The pipeline is synchronous `urllib` and each station
waits on a public server. Declaring the handlers `def` rather than `async def` lets
Starlette run them in its worker threadpool, so one slow county request does not stall
the whole event loop. Making them `async def` without an async HTTP client would be the
worst of both worlds — the syntax of concurrency with none of the behaviour.

**Stateless.** No database, no saved shortlists. Persisted state is a separate decision
with its own privacy consequences (see docs/THREAT_MODEL.md), and this service is the
container Bao points at locally, not a hosted multi-user app.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
import time
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from analyzer import batch
from analyzer.core.profile import BuyerProfile, load_profile
from ledger import Ledger, connect
from ledger.repo import JOURNAL_KINDS, STATUSES, LedgerError, PropertyNotFound
from analyzer.core.amortization import schedule as build_schedule
from analyzer.core.analyze import ENGINE_VERSION
from analyzer.core.cost import solve_max_price
from analyzer.extract import extract_from_document
from analyzer.extract.calllog import read_records, summarize
from analyzer.extract.documents import DocumentError
from analyzer.extract.providers import ProviderError, build_provider
from analyzer.pipeline import PipelineAborted, run

# The profile is read once at startup rather than per request. It is a config file, not
# user input, and re-reading it on every call would let a half-saved edit produce two
# properties scored against two different rulebooks in the same batch.
_PROFILE: BuyerProfile | None = None


def profile() -> BuyerProfile:
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = load_profile()
    return _PROFILE


# =============================================================================
# Request and response models
# =============================================================================


class PropertyRequest(BaseModel):
    """Only address and price are required.

    Everything else is a fact no public source will tell you — HOA dues, component
    ages, garage bay count. Omitting them is safe and explicitly supported: `None`
    means unknown, and the engine treats unknown differently from bad. Sending `0`
    for an unknown roof age would claim the roof is brand new, which is why these
    are nullable rather than defaulted to zero.
    """

    model_config = {"extra": "forbid"}

    address: str = Field(min_length=3, max_length=200)
    price: float = Field(gt=0, le=100_000_000)
    hoa_monthly: float = Field(default=0.0, ge=0, le=10_000)
    roof_age_years: int | None = Field(default=None, ge=0, le=200)
    hvac_age_years: int | None = Field(default=None, ge=0, le=200)
    garage_spaces: int | None = Field(default=None, ge=0, le=10)

    @field_validator("address")
    @classmethod
    def strip_address(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("address cannot be blank")
        return cleaned


class MaxPriceRequest(BaseModel):
    """Nothing about a specific house is required, because this is the pre-shopping question.

    Every field is optional. `sqft` and `year_built` only sharpen the maintenance reserve,
    and leaving them out is honest rather than broken -- the response says which band it
    fell back to.
    """

    model_config = {"extra": "forbid"}

    # Percent, not a fraction. `dti=22` is unambiguous over HTTP in a way that 0.22 is
    # not: a caller who means 22 and sends 22 should not silently get a 2200% ceiling.
    dti_pct: float | None = Field(default=None, gt=0, le=100)
    sqft: float | None = Field(default=None, gt=0, le=50_000)
    year_built: int | None = Field(default=None, ge=1700, le=2100)
    hoa_monthly: float = Field(default=0.0, ge=0, le=10_000)
    owner_occupied: bool = True


class AmortizationRequest(BaseModel):
    """Every field optional, defaulting to the profile's own loan.

    A caller with no arguments gets the schedule for the house the tool is actually aimed
    at, which makes this endpoint useful for a sanity check as well as a what-if.
    """

    model_config = {"extra": "forbid"}

    # Price or loan amount, not both. A caller who sends both has two different beliefs
    # about the down payment and there is no safe way to guess which one they meant, so
    # this rejects instead of silently picking one.
    price: float | None = Field(default=None, gt=0, le=100_000_000)
    loan_amount: float | None = Field(default=None, gt=0, le=100_000_000)

    # Percent, matching MaxPriceRequest.dti_pct. `rate_pct=6.67` over HTTP is unambiguous
    # in a way that 0.0667 is not, and the core rejects fractions above 1 as a backstop.
    rate_pct: float | None = Field(default=None, gt=0, le=100)
    term_months: int | None = Field(default=None, ge=1, le=600)
    extra_monthly: float = Field(default=0.0, ge=0, le=1_000_000)

    # 360 rows is roughly 40 KB of JSON. Useful for plotting a curve, wasteful for a
    # summary, so the caller says which they want and the default is the cheap one.
    include_payments: bool = False

    @field_validator("loan_amount")
    @classmethod
    def _not_both(cls, v, info):
        if v is not None and info.data.get("price") is not None:
            raise ValueError("send price or loan_amount, not both")
        return v


class SensitivityRequest(BaseModel):
    """Stated same-house rate scenarios, with the HTTP percent/dollar convention.

    This accepts assumptions only. A rate band is useful because it keeps the house and household
    inputs fixed while showing their consequences; it cannot say where rates or home prices go.
    """

    model_config = {"extra": "forbid"}

    price: float = Field(gt=0, le=100_000_000)
    rate_start_pct: float = Field(default=5.0, ge=0, le=100)
    rate_end_pct: float = Field(default=7.5, ge=0, le=100)
    rate_step_pct: float = Field(default=0.25, gt=0, le=100)
    baseline_rate_pct: float | None = Field(default=None, ge=0, le=100)
    baseline_source: str | None = Field(default=None, max_length=500)
    dti_pct: float | None = Field(default=None, gt=0, le=100)
    sqft: float | None = Field(default=None, gt=0, le=50_000)
    year_built: int | None = Field(default=None, ge=1700, le=2100)
    hoa_monthly: float = Field(default=0.0, ge=0, le=10_000)
    owner_occupied: bool = True
    future_rate_pct: float | None = Field(default=None, ge=0, le=100)
    future_price: float | None = Field(default=None, gt=0, le=100_000_000)

    @field_validator("baseline_source")
    @classmethod
    def strip_baseline_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("baseline_source cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def future_scenario_is_complete(self) -> "SensitivityRequest":
        if (self.future_rate_pct is None) != (self.future_price is None):
            raise ValueError("future_rate_pct and future_price must be supplied together")
        return self


class StatusRequest(BaseModel):
    """A status move, and optionally the reason for it.

    `note` is optional in the schema but the journal entry is not: the transition is
    recorded either way. Making the note required would produce "n/a" as the most common
    reason on record, which is worse than an empty one.
    """
    model_config = {"extra": "forbid"}

    status: Literal["candidate", "touring", "offer", "passed", "archived"]
    note: str | None = Field(default=None, max_length=2000)


class JournalRequest(BaseModel):
    """One journal entry.

    `resolves` is what makes this a decision journal: an outcome entry points back at the
    assumption it settles, so the ledger can later show which claims were never checked.
    """
    model_config = {"extra": "forbid"}

    kind: Literal["assumption", "decision", "observation", "outcome", "status"]
    body: str = Field(min_length=1, max_length=4000)
    property_key: str | None = None
    resolves: int | None = Field(default=None, ge=1)
    author: str | None = Field(default=None, max_length=120)


class AnalysisResponse(BaseModel):
    """The full document plus what went wrong getting it.

    `degraded_sources` is promoted to the top level rather than buried in the document
    because it is the field a caller needs to branch on. A client rendering this should
    be able to decide whether to show a warning banner without walking the provenance
    tree.
    """

    document: dict[str, Any]
    degraded_sources: list[str]
    complete: bool
    stations_run: list[str]
    elapsed_seconds: float
    engine_version: str


# =============================================================================
# App
# =============================================================================


def create_app() -> FastAPI:
    app = FastAPI(
        title="Los Angeles Home Buying Analyzer",
        version=ENGINE_VERSION,
        description=(
            "Deterministic property scoring over public data sources. "
            "Same engine as the CLI and the batch runner — this is only an HTTP door."
        ),
    )

    # -- errors ---------------------------------------------------------------

    @app.exception_handler(PipelineAborted)
    def geocoding_failed(request: Request, exc: PipelineAborted) -> JSONResponse:
        """422, not 500. The service worked; the address did not resolve.

        A 500 would tell a caller to retry, and retrying an address that does not
        exist will fail identically every time. 422 tells them to fix the input.
        """
        return JSONResponse(
            status_code=422,
            content={
                "error": "could_not_locate_address",
                "detail": str(exc),
                "hint": (
                    "Include the street number, city, and ZIP. The Census geocoder "
                    "matches addresses, not place names or intersections."
                ),
            },
        )

    # -- operational ----------------------------------------------------------

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        """Liveness only. Deliberately does not call any external source.

        A health check that reaches out to the county GIS server would report the
        container as unhealthy whenever the county has a bad afternoon, and a
        restarting container fixes nothing about someone else's server. External
        source status belongs in /sources, which is a diagnostic, not a probe.
        """
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    @app.get("/profile", tags=["ops"])
    def read_profile() -> dict[str, Any]:
        """The rulebook every score in this service was produced against.

        Exposed because a score is meaningless without it. "52 out of 100" only means
        something once you can see that a 2-car garage was wanted and 74 is the TAKE
        floor. This is also what makes the tool re-usable for a different buyer.
        """
        p = profile()
        return {
            "name": p.name,
            "verdict_bands": {
                "take_min": p.verdict_take_min,
                "watch_min": p.verdict_watch_min,
                "unevaluated_score": p.unevaluated_score,
            },
            "preferences": {
                "min_beds": p.min_beds,
                "min_baths": p.min_baths,
                "min_sqft": p.min_sqft,
                "min_garage_spaces": p.min_garage_spaces,
                "max_hoa_monthly": p.max_hoa_monthly,
                "require_fiber": p.require_fiber,
            },
            "penalties": p.penalties,
            "engine_version": ENGINE_VERSION,
        }

    @app.get("/sources", tags=["ops"])
    def sources() -> dict[str, Any]:
        """Which stations exist, what each one provides, and which are fatal.

        This is the honest-limitations endpoint. Two of the seven sources do not work as
        originally designed — the authoritative county server has a broken certificate
        chain and the FCC map needs a key — and a caller deserves to see that before
        trusting a number, not after.
        """
        from analyzer.pipeline import build_stations

        return {
            "mnemonic": (
                "GAFRCBS — Geocode, Assess, FEMA flood, Risk index, Commute, "
                "Broadband, Score"
            ),
            "stations": [
                {"name": s.name, "provides": list(s.provides), "fatal": s.fatal}
                for s in build_stations(profile(), {})
            ],
            "known_limitations": "see docs/KNOWN_LIMITATIONS.md",
        }

    @app.get("/rates/mortgage30us", tags=["market"])
    def mortgage30us_rate_snapshot() -> dict[str, Any]:
        """Committed weekly FRED MORTGAGE30US baseline; not a live lender quote."""
        path = pathlib.Path(__file__).resolve().parents[1] / "data" / "mortgage30us.json"
        try:
            snapshot = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="mortgage-rate snapshot is not available") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="mortgage-rate snapshot is invalid") from exc
        if not isinstance(snapshot, dict):
            raise HTTPException(status_code=503, detail="mortgage-rate snapshot is invalid")
        return snapshot

    @app.get("/market-velocity", tags=["market"])
    def market_velocity() -> dict[str, Any]:
        """Committed aggregate market context; never a live listing or MLS lookup."""
        from analyzer.sources.velocity import read_snapshot

        try:
            return read_snapshot()
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "market_velocity_unavailable", "reason": str(exc)},
            ) from exc

    # -- analysis -------------------------------------------------------------

    @app.post("/analyze", response_model=AnalysisResponse, tags=["analysis"])
    def analyze_property(
        payload: Annotated[PropertyRequest, Body()],
    ) -> AnalysisResponse:
        """Score one property. The vertical slice, over HTTP.

        Synchronous `def` on purpose — see the module docstring.
        """
        started = time.monotonic()
        result = run(
            payload.address,
            payload.price,
            profile=profile(),
            hoa_monthly=payload.hoa_monthly,
            roof_age_years=payload.roof_age_years,
            hvac_age_years=payload.hvac_age_years,
            garage_spaces=payload.garage_spaces,
        )
        return AnalysisResponse(
            document=result.document,
            degraded_sources=result.degraded_stations,
            complete=result.complete,
            stations_run=result.stations_run,
            elapsed_seconds=round(time.monotonic() - started, 2),
            engine_version=ENGINE_VERSION,
        )

    @app.post("/max-price", tags=["analysis"])
    def max_price(payload: Annotated[MaxPriceRequest, Body()]) -> dict[str, Any]:
        """Invert the affordability question: how much house does a DTI ceiling buy.

        No address, no network, no stations. This is pure arithmetic over the profile, so
        it answers instantly and works with every source offline -- which is exactly when
        somebody browsing listings wants it.
        """
        solution = solve_max_price(
            profile(),
            dti_ceiling=None if payload.dti_pct is None else payload.dti_pct / 100.0,
            sqft=payload.sqft,
            year_built=payload.year_built,
            hoa_monthly=payload.hoa_monthly,
            current_year=datetime.now().year,
            owner_occupied=payload.owner_occupied,
        )
        return {
            "max_price": solution.to_dict(),
            "engine_version": ENGINE_VERSION,
            "assumptions": {
                "down_payment": profile().down_payment,
                "mortgage_rate": profile().mortgage_rate,
                "loan_term_months": profile().loan_term_months,
                "annual_insurance": profile().annual_insurance,
                "gross_annual_income": profile().gross_annual_income,
            },
            # Spelled out because the down payment is fixed while the price moves, which
            # means a higher solved price is a lower down-payment percentage.
            "note": (
                "The down payment is held fixed from the profile, so a higher solved "
                "price means a smaller percentage down. Check the notes for a mortgage "
                "insurance warning."
            ),
        }

    @app.post("/amortization", tags=["analysis"])
    def amortization(payload: Annotated[AmortizationRequest, Body()]) -> dict[str, Any]:
        """Where the money goes, month by month.

        Pure arithmetic over the profile, like /max-price: no address, no network, no
        stations, so it answers instantly and works when every source is offline.

        This is principal and interest only. Taxes, insurance, HOA, and mortgage insurance
        do not amortize -- paying extra principal does not shorten them -- so every figure
        here is smaller than the PITI figure from /analyze. The payload carries `excludes`
        so a consumer rendering both side by side cannot conflate them.
        """
        p = profile()

        if payload.loan_amount is not None:
            loan = payload.loan_amount
            basis = "loan_amount as given"
        else:
            price = payload.price if payload.price is not None else p.target_price
            loan = price - p.down_payment
            basis = f"price ${price:,.0f} less the profile down payment ${p.down_payment:,.0f}"
            if loan <= 0:
                # 422, not 500. The arithmetic is fine; the request does not describe a loan.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"the profile down payment ${p.down_payment:,.0f} covers the whole "
                        f"price ${price:,.0f}, so there is nothing to amortize"
                    ),
                )

        rate = p.mortgage_rate if payload.rate_pct is None else payload.rate_pct / 100.0
        term = payload.term_months or p.loan_term_months

        try:
            result = build_schedule(loan, rate, term, extra_monthly=payload.extra_monthly)
        except ValueError as exc:
            # The core raises on inputs that cannot amortize -- a payment that never covers
            # the interest, for instance. That is the caller's input, so it is a 422 and the
            # core's message is passed through, because it names the actual numbers.
            raise HTTPException(status_code=422, detail=str(exc)) from None

        return {
            "amortization": result.to_dict(include_payments=payload.include_payments),
            "engine_version": ENGINE_VERSION,
            "loan_basis": basis,
            "assumptions": {
                "down_payment": p.down_payment,
                "mortgage_rate": rate,
                "loan_term_months": term,
                "rate_source": "profile" if payload.rate_pct is None else "request",
            },
            "note": (
                "Principal and interest only. Property tax, insurance, HOA dues, and "
                "mortgage insurance are not part of a schedule and do not shrink when you "
                "pay extra principal, so this is a smaller number than PITI from /analyze."
            ),
        }

    @app.post("/sensitivity", tags=["analysis"])
    def sensitivity(payload: Annotated[SensitivityRequest, Body()]) -> dict[str, Any]:
        """Apply supplied rate/price scenarios without fetching or forecasting anything."""
        from analyzer.core.sensitivity import BaselineRate, rate_band, wait_vs_buy
        from analyzer.sensitivity_cli import baseline_from_snapshot

        p = profile()
        if payload.baseline_rate_pct is None:
            try:
                # This adapter reads only the committed artifact. If it is absent, it returns the
                # profile's explicitly configured assumption rather than inventing a market rate.
                baseline = baseline_from_snapshot(p)
            except ValueError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"mortgage-rate snapshot is invalid: {exc}",
                ) from exc
        else:
            source = "explicit HTTP baseline assumption (not a market observation)"
            if payload.baseline_source:
                source += f"; caller label: {payload.baseline_source}"
            baseline = BaselineRate(payload.baseline_rate_pct / 100.0, source)

        try:
            band = rate_band(
                p,
                payload.price,
                baseline=baseline,
                start_rate=payload.rate_start_pct / 100.0,
                end_rate=payload.rate_end_pct / 100.0,
                step=payload.rate_step_pct / 100.0,
                dti_ceiling=None if payload.dti_pct is None else payload.dti_pct / 100.0,
                sqft=payload.sqft,
                year_built=payload.year_built,
                hoa_monthly=payload.hoa_monthly,
                current_year=datetime.now().year,
                owner_occupied=payload.owner_occupied,
            ).to_dict()
            comparison = (
                None
                if payload.future_rate_pct is None
                else wait_vs_buy(
                    p,
                    baseline_rate=baseline.annual_rate,
                    baseline_price=payload.price,
                    future_rate=payload.future_rate_pct / 100.0,
                    future_price=payload.future_price,
                    sqft=payload.sqft,
                    year_built=payload.year_built,
                    hoa_monthly=payload.hoa_monthly,
                    current_year=datetime.now().year,
                    owner_occupied=payload.owner_occupied,
                ).to_dict()
            )
        except ValueError as exc:
            # The core's messages name a non-dividing rate grid or infeasible scenario more
            # precisely than an HTTP-layer rewording could, and these are caller assumptions.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"band": band, "wait_vs_buy": comparison}

    @app.post("/shortlist", tags=["analysis"])
    def analyze_shortlist(
        file: UploadFile,
        dry_run: Annotated[
            bool,
            Query(description="Validate the CSV and return what would be analysed, no requests made."),
        ] = False,
        fmt: Annotated[Literal["json", "csv", "markdown"], Query(alias="format")] = "json",
    ) -> Any:
        """Score a whole shortlist from an uploaded CSV.

        `dry_run` matters more over HTTP than it does on the command line. A ten-row
        shortlist is around a minute of sequential requests to free public servers, and
        discovering a misnamed column on row nine of that is a waste of someone else's
        bandwidth as well as your time. Validation is offline and instant.
        """
        raw = file.file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        if len(raw) > 2_000_000:
            # A house shortlist is kilobytes. Anything this large is a mistake or an
            # attempt to make the container do a lot of unpaid work.
            raise HTTPException(status_code=413, detail="file too large for a shortlist")

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400, detail="file must be UTF-8 encoded CSV"
            ) from None

        try:
            rows, rejected, unknown_headers = batch.parse_shortlist_text(
                text, source=file.filename or "uploaded file"
            )
        except ValueError as exc:
            # A missing required column is the caller's mistake, not a server fault.
            raise HTTPException(status_code=422, detail=str(exc)) from None

        if dry_run:
            return {
                "dry_run": True,
                "would_analyse": [
                    {"line": r.line, "address": r.address, "price": r.price} for r in rows
                ],
                "rejected": [
                    {"line": r.line, "problem": r.problem} for r in rejected
                ],
                "unknown_headers": unknown_headers,
            }

        if not rows:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"no usable rows: {len(rejected)} rejected. "
                    f"Call with ?dry_run=true to see why."
                ),
            )

        entries = batch.analyse_shortlist(rows, profile=profile())
        result = batch.BatchResult(
            entries=entries,
            rejected=rejected,
            unknown_headers=unknown_headers,
            profile_name=profile().name,
        )

        if fmt == "csv":
            buffer = io.StringIO()
            batch.write_summary_stream(result, buffer)
            return PlainTextResponse(buffer.getvalue(), media_type="text/csv")
        if fmt == "markdown":
            return PlainTextResponse(
                batch.render_markdown(result), media_type="text/markdown"
            )

        return {
            "profile": result.profile_name,
            "engine_version": ENGINE_VERSION,
            "counts": {
                "scored": len(result.scored),
                "errors": len(result.entries) - len(result.scored),
                "rejected": len(result.rejected),
                "take": len(result.by_verdict("TAKE")),
                "watch": len(result.by_verdict("WATCH")),
                "pass": len(result.by_verdict("PASS")),
            },
            "unknown_headers": result.unknown_headers,
            "rejected": [{"line": r.line, "problem": r.problem} for r in result.rejected],
            "ranked": batch.summary_rows(result),
            "documents": [
                {"address": e.row.address, "document": e.document}
                for e in result.ranked
                if e.ok
            ],
        }

    # -- ledger ---------------------------------------------------------------
    #
    # Phase 3. Every handler below opens its own connection and closes it.
    #
    # That is not laziness about pooling, it is a requirement: these endpoints are
    # synchronous `def`, so FastAPI runs them in a worker thread, and a sqlite3 connection
    # may not be used from a thread other than the one that created it. A module-level
    # connection would work in testing and then raise ProgrammingError under real
    # concurrency -- the worst possible failure schedule. Opening a SQLite connection is
    # microseconds; there is nothing to save here.
    #
    # Keys contain spaces (`606 ANDRE CT SPARTANBURG SC 29301`), so callers must
    # percent-encode them in the path. That is a fair trade for a key a human can read in
    # a log line, and every HTTP client does it automatically.

    @app.exception_handler(PropertyNotFound)
    def property_not_in_ledger(request: Request, exc: PropertyNotFound) -> JSONResponse:
        """404. The key is well-formed, there is simply nothing saved under it."""
        return JSONResponse(
            status_code=404,
            content={
                "error": "property_not_in_ledger",
                "detail": str(exc),
                "hint": "GET /ledger/properties lists every key that exists.",
            },
        )

    @app.exception_handler(LedgerError)
    def ledger_refused(request: Request, exc: LedgerError) -> JSONResponse:
        """422, not 500. The ledger understood the request and declined it.

        Every LedgerError message names the rule and the alternative, so it is returned
        verbatim rather than replaced with a generic string.
        """
        return JSONResponse(
            status_code=422,
            content={"error": "ledger_refused", "detail": str(exc)},
        )

    @app.get("/ledger", tags=["ledger"])
    def ledger_stats() -> dict[str, Any]:
        """Counts, and the schema version of the file actually open.

        The schema version is included because it is the fastest way to tell a stale
        container from a stale database when something looks wrong.
        """
        conn = connect()
        try:
            return Ledger(conn).stats()
        finally:
            conn.close()

    @app.get("/ledger/properties", tags=["ledger"])
    def ledger_list(
        status: Annotated[Literal["candidate", "touring", "offer", "passed", "archived"] | None, Query()] = None,
        include_archived: Annotated[bool, Query(description="Archived houses are hidden by default.")] = False,
    ) -> dict[str, Any]:
        conn = connect()
        try:
            rows = Ledger(conn).list_properties(status=status, include_archived=include_archived)
        finally:
            conn.close()
        return {"count": len(rows), "properties": rows}

    @app.post("/ledger/properties", status_code=201, tags=["ledger"])
    def ledger_save(payload: Annotated[PropertyRequest, Body()]) -> dict[str, Any]:
        """Analyze an address and append the result to the ledger.

        Deliberately runs the analysis here rather than accepting a document from the
        caller. A `POST` that stored whatever JSON it was handed would let a hand-edited
        score into the record, and the whole value of the table is that every row is
        something the engine actually said.

        201 whether or not the property already existed, because a new *analysis* was
        created either way. `first_time` says which it was.
        """
        started = time.monotonic()
        result = run(
            payload.address,
            payload.price,
            profile=profile(),
            hoa_monthly=payload.hoa_monthly,
            roof_age_years=payload.roof_age_years,
            hvac_age_years=payload.hvac_age_years,
            garage_spaces=payload.garage_spaces,
        )
        conn = connect()
        try:
            saved = Ledger(conn).save_analysis(result.document, profile=profile())
        finally:
            conn.close()
        return {
            "property": saved["property"],
            "analysis_id": saved["analysis_id"],
            "first_time": saved["created"],
            "diff": saved["diff"],
            "degraded_sources": result.degraded_stations,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "engine_version": ENGINE_VERSION,
        }

    @app.get("/ledger/properties/{key:path}/document", tags=["ledger"])
    def ledger_document(key: str) -> dict[str, Any]:
        """The stored document, exactly as it was written.

        Not recomputed. A re-run under a newer engine is a different answer, and this
        endpoint exists to answer "what did we know when we decided".
        """
        conn = connect()
        try:
            return Ledger(conn).latest_document(key)
        finally:
            conn.close()

    @app.patch("/ledger/properties/{key:path}/status", tags=["ledger"])
    def ledger_set_status(key: str, payload: Annotated[StatusRequest, Body()]) -> dict[str, Any]:
        conn = connect()
        try:
            return Ledger(conn).set_status(key, payload.status, note=payload.note)
        finally:
            conn.close()

    @app.delete("/ledger/properties/{key:path}", tags=["ledger"])
    def ledger_forget(key: str) -> dict[str, Any]:
        """Delete a mistyped address. Refuses once there is a record worth keeping.

        409 rather than 422 on refusal: nothing about the request is malformed, it conflicts
        with the state of the resource. The fix is a different verb -- set the status to
        `passed` -- not a corrected payload.
        """
        conn = connect()
        try:
            return Ledger(conn).forget_property(key)
        except PropertyNotFound:
            raise
        except LedgerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            conn.close()

    @app.get("/ledger/properties/{key:path}", tags=["ledger"])
    def ledger_property(key: str) -> dict[str, Any]:
        """One house: identity, status, full history, journal, and the diff.

        Declared after the more specific `/document` and `/status` routes above. FastAPI
        matches in declaration order and `{key:path}` is greedy, so putting this first
        would swallow both of them.
        """
        conn = connect()
        try:
            return Ledger(conn).get_property(key)
        finally:
            conn.close()

    @app.get("/ledger/journal/open", tags=["ledger"])
    def ledger_open_assumptions() -> dict[str, Any]:
        """Assumptions and decisions nothing has come back to close."""
        conn = connect()
        try:
            entries = Ledger(conn).open_assumptions()
        finally:
            conn.close()
        return {"count": len(entries), "open": entries}

    # -- extraction -----------------------------------------------------------

    @app.get("/extract/schema", tags=["extraction"])
    def extract_schema() -> dict[str, Any]:
        """The declared schema, and the fields that are refused on principle.

        Both halves are published deliberately. A caller can see what extraction will attempt,
        and can also see the list of things it will never return no matter how it is asked --
        which is a more useful description of a boundary than prose about one.
        """
        from analyzer.core.extraction import FIELDS, FORBIDDEN_FIELDS, schema_for_prompt

        return {
            "fields": schema_for_prompt(),
            "count": len(FIELDS),
            "forbidden": [
                {"field": name, "because": reason} for name, reason in sorted(FORBIDDEN_FIELDS.items())
            ],
            "rule": "the model reads, the code decides — docs/adr/0004-llm-scope-boundary.md",
            "confirmation": (
                "Every returned field is confidence=extracted and confirmed=false. A human must "
                "confirm a field before it can affect a score."
            ),
        }

    @app.post("/extract", tags=["extraction"])
    def extract_document(
        file: UploadFile,
        provider: Annotated[
            str,
            Query(description="offline (default, deterministic, no key), ollama, or openai."),
        ] = "offline",
        name: Annotated[
            list[str] | None,
            Query(description="Personal names to redact before sending. Repeatable."),
        ] = None,
        all_pages: Annotated[
            bool,
            Query(description="Send every page, not only those mentioning a schema keyword."),
        ] = False,
    ) -> dict[str, Any]:
        """Extract declared fields from an uploaded document.

        Defaults to the offline provider, which sends nothing anywhere. That is the important
        default over HTTP specifically: this endpoint accepts a file from whoever can reach the
        port, and a default that forwarded it to a third party would turn one open port into a
        data-exfiltration path. Reaching a real model requires asking for it by name.

        The upload is written to a temporary file and deleted in a `finally`, so a document does
        not outlive the request on disk. The response reports what was refused alongside what
        was accepted -- a run that refused six fields is a successful run with a visible
        boundary, and hiding the refusals would remove the only evidence the boundary works.

        A provider failure is a 200 with `error` set, matching the degradation rule at the top
        of this file: the record of what was sent is complete either way, and the caller gets
        the redaction report even when the model never answered.
        """
        raw = file.file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        if len(raw) > 20_000_000:
            # A 40-page inspection PDF is a couple of megabytes. 20MB is generous room for a
            # scan while still refusing to let one request make the container do a lot of
            # unpaid work.
            raise HTTPException(status_code=413, detail="file too large; 20MB limit")

        suffix = pathlib.Path(file.filename or "upload.txt").suffix.lower() or ".txt"
        if suffix not in {".txt", ".md", ".markdown", ".text", ".pdf"}:
            raise HTTPException(
                status_code=415,
                detail=f"unsupported type {suffix!r}; send .txt, .md, or .pdf",
            )

        try:
            engine = build_provider(provider)
        except ProviderError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(raw)
            handle.close()
            try:
                run_result = extract_from_document(
                    handle.name,
                    provider=engine,
                    known_names=tuple(name or ()),
                    filter_pages=not all_pages,
                )
            except DocumentError as exc:
                # A PDF that needs OCR, or a file that is not the type its name claims. The
                # caller's problem, and the message says which.
                raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            pathlib.Path(handle.name).unlink(missing_ok=True)

        payload = run_result.to_dict()
        # The temp path is an implementation detail and leaks the container's filesystem
        # layout; the caller knows what it uploaded.
        payload["document"]["path"] = file.filename or "uploaded file"
        return payload

    @app.get("/extract/log", tags=["extraction"])
    def extract_log(
        limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    ) -> dict[str, Any]:
        """The model call log: what was sent, when, to whom, and how much was refused.

        Never contains document text or extracted values -- see the module docstring in
        `analyzer/extract/calllog.py` for why a log that stored what it sent would be the
        largest data-at-rest risk in the project.
        """
        records = read_records(limit=limit)
        return {"count": len(records), "summary": summarize(records), "calls": records}

    @app.get("/ledger/journal", tags=["ledger"])
    def ledger_journal(
        property_key: Annotated[str | None, Query(description="Omit for every entry, including general ones.")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        conn = connect()
        try:
            entries = Ledger(conn).journal(key=property_key, limit=limit)
        finally:
            conn.close()
        return {"count": len(entries), "entries": entries}

    @app.post("/ledger/journal", status_code=201, tags=["ledger"])
    def ledger_add_journal(payload: Annotated[JournalRequest, Body()]) -> dict[str, Any]:
        conn = connect()
        try:
            return Ledger(conn).add_journal_entry(
                kind=payload.kind,
                body=payload.body,
                key=payload.property_key,
                resolves=payload.resolves,
                author=payload.author,
            )
        finally:
            conn.close()

    return app


app = create_app()
