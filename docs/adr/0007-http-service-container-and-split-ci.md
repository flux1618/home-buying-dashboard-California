# 0007 — One engine behind three doors, and a CI split by what can fail

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** nothing
**Related:** [0001](0001-static-snapshot-plus-local-container.md) (static snapshot + local container), [0002](0002-pure-scoring-core.md) (pure core), [0006](0006-source-station-contract.md) (source station contract)

## Context

After ADR 0006 the engine worked, but it could only be reached one way: a CLI call for one
address. Two things were missing, and they pull in different directions.

The first is **batch use**. Shopping for a house is not analysing one property, it is
ranking eight of them and re-running that list weekly as prices move. Doing that through
the single-address CLI means eight invocations and a hand-assembled comparison.

The second is **a door other than a terminal**. ADR 0001 committed to a local container as
the real tool, with the public site as a snapshot. A container that only exposes a CLI is
not something a browser can point at, which defeats the purpose.

The obvious way to get both is to write a web app that calls the analysis logic, and to add
whatever helper functions the HTTP layer needs along the way. That is how a codebase ends up
with two subtly different implementations of the same scoring — the CLI's and the API's —
that drift until they disagree about the same house.

There is also a CI question that only appears once tests exist in two categories. This
project reads six public sources, two of which are degraded on any given day (ADR 0006).
A test suite that hits them will fail for reasons unrelated to any commit.

## Decision

**One engine, three chassis.** The CLI, the batch runner, and the HTTP service are three
thin adapters over the same `analyze()` call. No adapter contains scoring logic, and
anything two adapters both need gets factored into a shared function rather than
reimplemented:

| Shared function | Used by CLI as | Used by service as |
|---|---|---|
| `parse_shortlist_text(text, source)` | `read_shortlist(path)` | request body of `POST /shortlist` |
| `render_markdown(result)` | `write_markdown(result, path)` | `?format=markdown` response |
| `write_summary_stream(result, handle)` | `write_summary_csv(result, path)` | `?format=csv` response |

The file-based wrappers now exist only to open a file and delegate. This is the enforceable
version of the rule: if the API and the CLI can produce different output for the same
input, one of them is calling something the other is not.

**`service/` is a separate top-level package, not `analyzer/service/`.** `tests/test_core_purity.py`
bans importing `fastapi` anywhere under `analyzer/core/`. Putting the web framework in a
different package makes that ban structural rather than a rule someone has to remember, and
`tests/test_service.py` asserts the reverse direction too — nothing under `analyzer/` may
import `service`, `fastapi`, `uvicorn`, or `pydantic`.

**FastAPI is an optional extra (`pip install '.[api]'`).** The core, CLI, and batch runner
work with none of it installed. CI proves this rather than asserting it: one job installs
with no extras, verifies `fastapi` and `pydantic` are genuinely unimportable, and then runs
a full scoring analysis. ADR 0002 stops being a promise and becomes a property of the build.

### HTTP status decisions

These were not obvious and each one is a judgement call worth recording.

**A degraded source is `200`, not `503`.** If the FCC key is missing, the tax calculation,
flood determination, and commute time all still succeeded. Returning `503` would throw away
work that completed and tell the caller nothing about what actually failed. The response
carries `degraded_sources` and the per-value confidence labels from ADR 0006, so a caller
that cares can check. This follows directly from the station contract: partial data with
visible provenance beats no data.

**An unresolvable address is `422`, not `500`.** A `500` invites a retry. Retrying an
address the Census geocoder cannot match will fail identically forever, because the problem
is the input, not the server. `422` with a hint about street number and ZIP tells the caller
what to change.

**A shortlist with a missing required column is `422`; an empty upload is `400`.** The
distinction is whether the request was well-formed enough to have semantics. A zero-byte
upload is a malformed request. A syntactically valid CSV lacking a `price` column is a
well-formed request the engine cannot satisfy.

**Handlers are sync `def`, not `async def`.** The pipeline is blocking `urllib`. Declared
sync, Starlette runs each handler in a threadpool and concurrency works. Declared `async`,
the same blocking calls would stall the event loop and one slow county request would freeze
every other in-flight analysis. This is the single easiest way to make a FastAPI service
mysteriously serial.

**The service is stateless.** No database, no saved shortlists. Per-user state is a
deliberate later decision, and adding a database now would mean choosing a persistence
model before there is a real access pattern to design for.

### Container decisions

**Multi-arch by construction, not by flag.** `linux/amd64` and `linux/arm64` from one
Dockerfile with no conditionals, because the target is a K3s cluster on Raspberry Pi 5
nodes while CI runs on amd64. An image that only builds for one is an image that cannot be
tested where it runs.

**Python 3.12 in the image**, though the code supports 3.11+. `uvloop` and `httptools` ship
prebuilt arm64 wheels for 3.12; on a version without them, an arm64 build compiles from
source under QEMU emulation and takes tens of minutes.

**The rulebook is located by a search order, not by a path expression.** `HBA_PROFILE`, then
beside the package, then the working directory — resolved at call time. `HBA_CACHE_DIR`
works the same way. Both are read with stdlib `os`, so ADR 0002 still holds.

This was not planned. `load_profile()` originally found `buyer_profile.toml` by walking up
from its own source file, which is correct in a checkout and wrong the instant the package
is pip-installed, because the path then resolves inside `site-packages`. That one assumption
broke two things in sequence:

1. The **container** started cleanly and failed on its first request. Caught locally by
   installing into a bare virtualenv and running it the way the image does.
2. **CI**, immediately after — roughly 200 tests died with a `FileNotFoundError` pointing at
   a site-packages directory nobody had ever put config in. `pytest` does not add the working
   directory to `sys.path` (unlike `python -m pytest`), so the installed copy was imported
   and the beside-package lookup missed.

The first fix was a single env var, which would have left the second failure latent. The
search order is the actual fix, and it is resolved at call time rather than as a module
constant: a constant is computed once at import, so setting the variable from a fixture or an
entrypoint has no effect, and the code doing the setting looks correct while being useless.
`tests/test_profile_location.py` pins all three branches and the not-frozen-at-import
property.

**Only config is copied into the runtime stage; the code lives in the venv.** Copying the
source tree as well would leave two copies of every module in the image, and which one ran
would depend on `sys.path` order.

**The healthcheck calls `/health`, which makes no external requests.** A probe that touched
the county GIS server would mark the container unhealthy whenever someone else's server had
a bad afternoon, and restarting the container would fix none of it.

**Compose binds `127.0.0.1:8000`, not `0.0.0.0`.** The service has no authentication and its
responses contain real household financial figures. Exposing it to the LAN should be a
deliberate act. `FCC_API_KEY` is passed through from the host environment and never baked
into a layer, because image layers are immutable and a later `rm` does not remove a secret
from history.

### CI split

Two workflows, divided by what a failure *means*:

**`tests.yml` runs on every push and must be boring.** Offline suite on Python 3.11 and
3.13, the stdlib-only purity job, CLI and batch entry points via `--dry-run`, and a
multi-arch container build followed by a single-arch load and smoke test. Nothing here
touches the network. A red build means a commit broke something.

The test jobs install **editable** (`pip install -e`). A non-editable install puts a copy in
`site-packages`, and since `pytest` does not add the working directory to `sys.path`, the
suite would test that copy instead of the commit — a very quiet way for CI to stop verifying
the thing in front of it. The purity job is the deliberate exception: it installs
non-editable precisely *because* it needs to exercise the installed layout.

**`live.yml` runs nightly on a schedule.** `pytest -m live` against the real endpoints,
plus one full CLI analysis of a real address. A failure means an assumption about someone
else's data changed — a renamed GIS layer, a retired geocoder vintage, a new API key
requirement. On a scheduled failure it opens or comments on a single `source-drift` issue
rather than filing a new one nightly.

The live workflow deliberately has **no retries** and **does not assert an exact score**.
Retries would paper over exactly the flakiness the workflow exists to surface. Pinning a
score would fail every time a millage rate or insurance figure moved, which is reality
changing rather than code breaking; determinism given fixed inputs is already covered
offline.

## Alternatives considered

**Put the API inside `analyzer/`.** Fewer top-level directories. Rejected because the purity
guarantee then depends on a linting rule instead of on where files physically live.

**Make FastAPI a hard dependency.** Simpler install story. Rejected because it would make
the "core is stdlib-only" claim untestable — you cannot prove an absence in an environment
where the thing is always installed.

**One CI workflow with live tests marked `continue-on-error`.** Fewer files. Rejected
because a job that is always allowed to fail is a job nobody reads. The signal disappears.

**Run live tests on every push.** Faster detection of source drift. Rejected: with two of
six sources already degraded, main would be red for reasons unrelated to any commit, and
the habit of ignoring red builds is much more expensive than a few hours of detection lag.

**Async handlers with an async HTTP client.** More idiomatic FastAPI. Rejected for now
because it would mean rewriting the source stations away from `urllib`, and ADR 0002's
stdlib-only core is worth more than concurrency this workload does not need.

## Consequences

**Good**

- One scoring implementation reachable three ways, with the shared-function table above as
  the check that it stays one.
- The container is the tool ADR 0001 described: point a browser at localhost, no data
  leaving the machine except requests to public sources.
- CI proves the purity claim by execution rather than by inspection.
- Source drift surfaces as a dated issue thread instead of being discovered mid-analysis.
- Every push is verified on two Python versions and two CPU architectures.

**Bad**

- Four more files to keep in sync (`Dockerfile`, `docker-compose.yml`, two workflows).
- The multi-arch build needs QEMU emulation in CI, which makes that job the slowest by a
  wide margin.
- The `analyze` request model duplicates a small amount of validation the CLI's argparse
  also does. Accepted: sharing it would drag Pydantic toward the core.
- The nightly workflow can open an issue for a problem that fixes itself by morning, and
  the issue stays open until someone closes it.
- The container build could not be verified in the environment where it was written, since
  Docker was unavailable there. Its first real execution was in CI, where both architectures
  built and the smoke test booted on the first attempt.

**Cost if wrong**

The reversible parts are cheap: workflows are config, and the container is additive. The
expensive mistake would be the shared-function refactor going the other way — an adapter
growing its own copy of formatting or parsing logic because a shared signature was
inconvenient. That drift is invisible in output review, because each door looks correct in
isolation. `tests/test_service.py` compares service output against the batch runner's for
identical input specifically to catch it.
