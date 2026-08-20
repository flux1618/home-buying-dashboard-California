"""The only place in the codebase that touches a socket.

Stdlib `urllib` rather than `requests`, for the same reason the core has no
dependencies: a tool meant to run on a Raspberry Pi in five years should not need a
package index to start.

Everything a flaky public API can do to you is handled here, once:

  timeout      every call is bounded; no station can hang the pipeline
  retry        transient failures and 5xx get a bounded exponential backoff
  cache        responses land on disk, so re-running an analysis costs nothing
  rate limit   a minimum gap between calls to the same host, because Nominatim
               and OSRM are volunteer-run and asking politely is the rent

Errors are narrowed to two kinds, and the distinction is the whole point:

  SourceUnavailable  the source could not answer.        -> degrade
  SourceRejected     the source answered, refusing us.   -> degrade, and say why

Neither is ever raised past a station. Stations catch both and return a Degradation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USER_AGENT = (
    "home-buying-dashboard-california/0.2 "
    "(+https://github.com/flux1618/home-buying-dashboard-California; personal research tool)"
)

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
# Overridable so the container can point it at a mounted volume. In an image the home
# directory is ephemeral, so the default would put the cache inside the container's
# writable layer and throw it away on every restart — which means re-asking a free county
# GIS server for parcel facts it already gave us. `HBA_CACHE_DIR` is read at import so a
# deployment can set it once in the environment rather than threading it through calls.
CACHE_DIR = Path(
    os.environ.get("HBA_CACHE_DIR")
    or Path.home() / ".cache" / "home-buying-dashboard"
)
CACHE_TTL_SECONDS = 7 * 24 * 3600

# Volunteer-run services. The gap is not optional.
MIN_INTERVAL_BY_HOST = {
    "nominatim.openstreetmap.org": 1.1,
    "router.project-osrm.org": 0.5,
}

_last_call: dict[str, float] = {}


class SourceUnavailable(RuntimeError):
    """The source did not answer: timeout, DNS failure, 5xx, unparseable body."""


class SourceRejected(RuntimeError):
    """The source answered and declined: 401, 403, 429, or an error in the payload."""


@dataclass
class Response:
    url: str
    data: Any
    from_cache: bool


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    return CACHE_DIR / f"{digest}.json"


def _read_cache(url: str, ttl: float) -> Any | None:
    path = _cache_path(url)
    if not path.exists():
        return None
    if ttl >= 0 and time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(url: str, data: Any) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(json.dumps(data))
    except OSError:
        pass  # a cache that cannot be written is not a reason to fail an analysis


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).netloc
    gap = MIN_INTERVAL_BY_HOST.get(host)
    if not gap:
        return
    elapsed = time.time() - _last_call.get(host, 0.0)
    if elapsed < gap:
        time.sleep(gap - elapsed)
    _last_call[host] = time.time()


def build_url(base: str, params: dict[str, Any]) -> str:
    """Query string with `None` values dropped, so callers can pass optionals inline."""
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{base}?{urllib.parse.urlencode(clean)}"


def _fetch_body(
    url: str,
    *,
    timeout: float,
    retries: int,
    headers: dict[str, str] | None,
    accept: str,
    parse,
    describe_parse_failure: str,
) -> Any:
    """The shared retry / throttle / decode loop.

    Factored out when a second content type arrived. The retry policy, the 401/403/429
    "a refusal is final" rule, and the throttle are the parts that must not diverge
    between JSON and HTML callers, and the only honest way to guarantee that is for both
    to run the same code.
    """
    request_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    request_headers.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(2**attempt * 0.5, 4.0))
        _throttle(url)
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # A refusal is final. Retrying a 403 just wastes the user's time.
            if exc.code in (401, 403, 429):
                raise SourceRejected(f"HTTP {exc.code} {exc.reason}") from exc
            last_error = SourceUnavailable(f"HTTP {exc.code} {exc.reason}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = SourceUnavailable(f"{type(exc).__name__}: {exc}")
            continue

        try:
            return parse(body)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = SourceUnavailable(f"{describe_parse_failure}: {exc}")
            continue

    raise last_error or SourceUnavailable("exhausted retries with no recorded error")


def get_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    cache: bool = True,
    cache_ttl: float = CACHE_TTL_SECONDS,
    headers: dict[str, str] | None = None,
) -> Response:
    """Fetch and parse JSON, or raise SourceUnavailable / SourceRejected."""
    if cache:
        hit = _read_cache(url, cache_ttl)
        if hit is not None:
            return Response(url=url, data=hit, from_cache=True)

    data = _fetch_body(
        url,
        timeout=timeout,
        retries=retries,
        headers=headers,
        accept="application/json",
        parse=json.loads,
        describe_parse_failure="response was not JSON",
    )
    if cache:
        _write_cache(url, data)
    return Response(url=url, data=data, from_cache=False)


def get_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    cache: bool = True,
    cache_ttl: float = CACHE_TTL_SECONDS,
    headers: dict[str, str] | None = None,
) -> Response:
    """Fetch a text/HTML body.

    Added for exactly one source: California's CAASPP assessment portal, which publishes
    school-level proficiency as a server-rendered ASP.NET page and offers no JSON API.
    Reading a percentage out of HTML is more fragile than reading it out of JSON, so the
    school station treats a parse miss as "unavailable" rather than as zero — see the
    reasoning there. The alternative was a commercial ranking API, which would have meant
    paying for a derived score instead of reading the state's own measurement.

    Cached as a JSON wrapper so it shares the on-disk cache with `get_json` rather than
    inventing a second cache format. HTML is heavier than JSON, and one of these pages
    per school per level is a real cost against a state portal.
    """
    if cache:
        hit = _read_cache(url, cache_ttl)
        if isinstance(hit, dict) and "text" in hit:
            return Response(url=url, data=hit["text"], from_cache=True)

    body = _fetch_body(
        url,
        timeout=timeout,
        retries=retries,
        headers=headers,
        accept="text/html,application/xhtml+xml",
        parse=lambda raw: raw,
        describe_parse_failure="response could not be read as text",
    )
    if cache:
        _write_cache(url, {"text": body})
    return Response(url=url, data=body, from_cache=False)


def post_json(
    url: str,
    fields: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    cache: bool = True,
    cache_ttl: float = CACHE_TTL_SECONDS,
    headers: dict[str, str] | None = None,
) -> Response:
    """Form-encoded POST returning JSON.

    Needed because an ArcGIS attendance-boundary polygon is too long for a query string:
    a Los Angeles high-school boundary came back with 838 vertices, and passing that back
    as a GET parameter overruns the server's URL limit. ArcGIS accepts the identical
    parameters as a POST body, so the only difference is transport.

    Cached on the URL plus a hash of the body, so a repeated spatial join is free the
    second time even though POSTs are not normally cacheable. That is safe here because
    these endpoints are read-only queries that happen to need a large request.
    """
    body = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None})
    cache_key = f"{url}#POST#{hashlib.sha256(body.encode()).hexdigest()}"

    if cache:
        hit = _read_cache(cache_key, cache_ttl)
        if hit is not None:
            return Response(url=url, data=hit, from_cache=True)

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    request_headers.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(2**attempt * 0.5, 4.0))
        _throttle(url)
        try:
            request = urllib.request.Request(
                url, data=body.encode(), headers=request_headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise SourceRejected(f"HTTP {exc.code} {exc.reason}") from exc
            last_error = SourceUnavailable(f"HTTP {exc.code} {exc.reason}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = SourceUnavailable(f"{type(exc).__name__}: {exc}")
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = SourceUnavailable(f"response was not JSON: {exc}")
            continue

        if cache:
            _write_cache(cache_key, data)
        return Response(url=url, data=data, from_cache=False)

    raise last_error or SourceUnavailable("exhausted retries with no recorded error")


def clear_cache() -> int:
    """Delete cached responses. Returns how many files were removed."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
