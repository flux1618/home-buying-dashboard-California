# ADR 0003 — No Zillow/Redfin extraction and no MLS integration

**Status:** Accepted · 2026-08-19

## Context

The obvious feature request for a home-buying tool is "pull listings automatically." It's also the feature that would make the tool feel finished. Two blockers make it a bad idea rather than a hard one.

Zillow's and Redfin's terms of use prohibit automated collection of their listing data. Real MLS access runs through an IDX feed that requires a licensed-agent relationship or brokerage sponsorship. Neither is a technical obstacle that cleverness solves — they're the conditions of access.

There's a portfolio dimension too. A repo that quietly depends on scraping a site that forbids it is a liability in an interview, not an asset. Anyone senior enough to be worth impressing will notice.

## Decision

Listing acquisition stays **manual or document-based, permanently.**

- Candidate addresses are entered by hand, or extracted from documents the buyer legitimately possesses (see [ADR 0004](0004-llm-scope-boundary.md)).
- The system builds on genuinely open sources: Spartanburg County GIS, [FEMA](https://msc.fema.gov/portal/home), [Census](https://tigerweb.geo.census.gov/), [FRED](https://fred.stlouisfed.org/), [FCC BDC](https://www.fcc.gov/BroadbandData), [OpenStreetMap](https://overpass-api.de/).
- Aggregate market data comes from [Redfin's Data Center](https://www.redfin.com/news/data-center/), which is published for exactly this purpose. That is a different thing from extracting listings.
- This is recorded as a **permanent non-goal**, not a backlog item. It does not appear on the roadmap.

## Alternatives considered

| Option | Why not |
|---|---|
| Extract Zillow / Redfin listings | Prohibited by their terms. Also brittle — layout changes break it constantly. |
| MLS IDX feed | Requires agent licensure or brokerage sponsorship. Not available to me. |
| Third-party listing API (ATTOM, Rentcast, etc.) | Legitimate and would work. Real cost per call for a project that will analyze ~50 addresses. Revisit only if volume ever justifies it. |
| Buyer's agent exports a CSV of favorites | **Genuinely viable and the preferred path if it comes up.** Data arrives through a person with the right to share it. Worth asking an agent for once one is engaged. |

## Consequences

**Good.** No terms-of-service exposure. No brittle extraction to maintain. The data foundation is public-record and government sources, which are stable, documented, and citable. The constraint forces the tool to be a *decision* tool rather than another search interface — which is the more defensible product anyway.

**Bad.** Address entry is manual. No automatic price-drop detection across the whole market; watch-list monitoring can only cover addresses already entered.

**Cost of being wrong.** None meaningfully. If an agent relationship later produces a legitimate export path, that's an additive `sources/` adapter behind the same interface. Nothing needs to be undone.
