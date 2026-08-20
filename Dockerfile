# Multi-arch by construction, not by flag.
#
# Two architectures matter here for a concrete reason: Bao's K3s cluster runs on Raspberry
# Pi 5 nodes (arm64) and CI runs on GitHub's hosted runners (amd64). An image that only
# builds on one is an image that cannot be tested where it runs. Nothing below is
# architecture-specific — no compiled wheels are pinned, no binaries are downloaded — so
# `docker buildx build --platform linux/amd64,linux/arm64` works with no conditionals.
#
# Two stages, because the build needs a compiler toolchain for uvloop and httptools and
# the runtime does not. Shipping gcc to a Raspberry Pi is a slower pull and a wider attack
# surface for no benefit.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

# 3.12 rather than 3.14: uvloop and httptools ship prebuilt arm64 wheels for it, so an
# arm64 build is a download instead of a twenty-minute compile. The code needs >=3.11
# for stdlib tomllib and nothing newer.

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

ARG TARGETARCH
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# The whole source is copied before installing rather than staging pyproject.toml first.
# The manifest-first trick needs stub packages on disk for the install to succeed, and the
# real source then *shadows* the installed stub at runtime — which works, but depends on
# import order in a way that is easy to break and hard to debug. A BuildKit pip cache
# gives the same fast rebuilds without that subtlety.
COPY pyproject.toml ./
COPY analyzer/ ./analyzer/
COPY service/ ./service/
# The saved-property ledger (Phase 3). Stdlib-only — sqlite3 is in CPython, so this adds
# nothing to the arm64 wheel problem that dictated the Python version above.
COPY ledger/ ./ledger/

# Cache keyed by architecture. Sharing one cache between an amd64 and an arm64 build would
# let a wheel built for the wrong platform be reused.
# `.[api]` and not `.[api,dev]` — pytest does not belong in a runtime image; tests run in
# CI against the source tree, which is where a failure is actionable.
RUN --mount=type=cache,id=pip-$TARGETARCH,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip setuptools wheel \
 && pip install '.[api]'

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Los Angeles Home Buying Analyzer" \
      org.opencontainers.image.description="Deterministic property scoring over public data" \
      org.opencontainers.image.source="https://github.com/flux1618/home-buying-dashboard-California" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HBA_CACHE_DIR=/var/cache/hba \
    HBA_DATA_DIR=/var/lib/hba \
    HBA_PROFILE=/app/buyer_profile.toml

# Non-root. The container reads public data and writes a response cache; it has no reason
# to hold root, and a stray path bug should not be able to write outside its own cache.
RUN useradd --create-home --uid 10001 analyst

COPY --from=build /opt/venv /opt/venv

WORKDIR /app

# Code lives in the venv (installed in stage 1). Only configuration and sample data are
# copied here, so there is exactly one copy of every module in the image. Copying the
# source tree as well would leave two, and which one ran would depend on import order.
#
# HBA_PROFILE above is what makes this work: the installed package cannot find the profile
# by walking up from its own file, because that path now lands in site-packages.
COPY --chown=analyst:analyst buyer_profile.toml ./
COPY --chown=analyst:analyst examples/ ./examples/

# The response cache is a volume so a restart does not re-ask the county for facts it
# already has. Public GIS servers are free and slow; re-asking them for the same parcel
# on every container start is rude and pointless.
RUN mkdir -p /var/cache/hba && chown analyst:analyst /var/cache/hba
VOLUME ["/var/cache/hba"]

# The ledger is a *separate* volume from the cache, and the distinction matters. The cache
# is disposable — delete it and the county tells you the same facts again. The ledger is
# not: it holds the record of which houses were considered and what was decided, and there
# is nowhere to fetch that back from. Sharing one volume would invite treating both as
# scratch space the first time the cache needs clearing.
RUN mkdir -p /var/lib/hba && chown analyst:analyst /var/lib/hba
VOLUME ["/var/lib/hba"]

USER analyst
EXPOSE 8000

# Checks liveness only, never an external source. A probe that depends on the county GIS
# server would mark this container unhealthy whenever someone else's server has a bad
# afternoon, and restarting this container fixes none of that.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# One worker by default. The workload is IO-bound on public endpoints that are themselves
# rate-limited, so more workers would mostly mean more concurrent requests to servers that
# have asked for polite use. Override with UVICORN_WORKERS if you ever front a real load.
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8000"]
