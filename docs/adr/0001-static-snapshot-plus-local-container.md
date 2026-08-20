# ADR 0001 — Public site is a committed snapshot; the API runs locally in a container

**Status:** Accepted · 2026-08-19

## Context

The dashboard needs to compute per-address analysis: geocoding, county tax lookup, FEMA risk, rush-hour routing, broadband. That work needs a runtime. Three things pulled in different directions.

First, the live site is hosted as static files over HTTPS. Browser JavaScript served from that origin **cannot** call a service on a home LAN — mixed-content blocking, CORS, and no public DNS all prevent it. "Run FastAPI on the cluster" does not by itself make the public site work.

Second, this repo is a portfolio piece. A reviewer may open the link at 11pm on a Sunday. If the site depends on a Raspberry Pi cluster in a spare room, a power blip becomes a broken demo. A broken demo is worse than a simpler one.

Third, the real volume is tiny. Over the whole purchase we will evaluate maybe 30–50 addresses. Sizing infrastructure for that as if it were a live service would be dishonest engineering.

## Decision

Two deployment tiers, both built. A third considered and deliberately not taken.

**Tier A — committed snapshot (public).** A CLI run analyzes a list of addresses and writes a versioned JSON artifact into the repo. The static site consumes that artifact. No runtime, no uptime dependency, works offline, costs nothing, and cannot break.

**Tier B — local container (private).** The same engine wrapped in FastAPI, packaged as a multi-arch image for `linux/amd64` and `linux/arm64`. Pointed at from `localhost` or the LAN for live address entry during house tours. This is where iteration actually happens.

**Tier C — public tunnel (not taken).** A Cloudflare Tunnel would expose Tier B to the internet with real TLS and no port forwarding. Rejected for now: it converts a permanently-working demo into one gated on homelab uptime, in exchange for a live text box that Tier B already provides privately.

## Alternatives considered

| Option | Why not |
|---|---|
| Browser calls the upstream APIs directly | Census, county GIS, and FCC endpoints don't all send CORS headers. Also leaks any future API keys into client code. |
| Managed cloud host (Fly / Render / Lambda) | Works, and would be the right call for real traffic. Adds a bill and an account dependency for ~50 lifetime requests. |
| Public tunnel to the homelab (Tier C) | Uptime risk on the artifact a hiring manager judges. Documented as the upgrade path instead. |
| Skip the API, CLI only | Loses the live-entry experience that makes the tool usable in a driveway. Tier B is cheap enough to keep. |

## Consequences

**Good.** The public site can't break. The container is genuinely useful locally rather than decorative. Tiers A and B share one engine, so there is no second implementation to keep in sync. Multi-arch builds exercise the arm64 path the existing K3s cluster actually needs.

**Bad.** Public data is only as fresh as the last committed artifact — someone loading the site sees a snapshot with a date, not live results. That is disclosed in the UI rather than hidden.

**Cost of being wrong.** Low and reversible. Tier C is roughly an afternoon: a tunnel, a DNS record, and a CORS allowlist. Nothing here forecloses it.

**Why this isn't Kubernetes theater.** The cluster is not required for the product to work — that is precisely the point. It hosts the scheduled refresh and the local API because it already exists and already runs arm64 workloads. If the containers were load-bearing for a 50-request lifetime workload, that would be résumé-driven architecture, and it would deserve the criticism.
