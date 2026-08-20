# ADR 0014 — The extractor refuses by default, and a field without a quote from the document does not exist

**Status:** Accepted · 2026-08-19

## Context

[ADR 0004](0004-llm-scope-boundary.md) drew the line ten commits before any of this existed: the
model reads, the code decides. It listed what may be extracted, what may never be, and required
a citation and a human confirmation for every field.

Then it sat there as prose. Building the feature turned out to be less about calling a model —
that is fifteen lines of `urllib` — and more about answering a question ADR 0004 states but does
not settle: **what stops the boundary from being crossed?**

The failure mode worth designing against is not a model returning garbage. Garbage is easy;
`json.loads` catches it. The failure mode is a model returning something *reasonable*:

```json
{"field": "estimated_value", "value": 312000, "quote": "Year built: 1998."}
{"field": "roof_age_years", "value": 12, "quote": "The roof was replaced in 2014."}
```

Neither is malformed. Both parse. The first is a confident dollar figure ADR 0004 forbids. The
second is a plausible number attached to a sentence that is not in the document. If the code
accepts either, the boundary was decorative, and the person confirming a finding is being asked
to catch what the system did not.

A third pressure came from testing. A feature whose behaviour can only be observed by spending
money at a third party is a feature with no test suite, and CI has no credentials and should
never have any.

## Decision

Four things, in the order they matter.

### 1. Everything is refused unless it satisfies four checks

`core/extraction.py` accepts a field only when all four hold, and every failure is recorded as a
`Rejection` with a reason rather than dropped:

| Check | Refuses |
|---|---|
| Declared | Any field not in `FIELDS`. |
| Permitted | Fields in `FORBIDDEN_FIELDS` — payments, taxes, repair costs, scores, verdicts — named individually so the log cites ADR 0004 rather than reporting a schema miss. |
| Grounded | Any field whose `quote` is not present in the text that was sent. |
| Plausible | A roof 700 years old, a house built in 1200, negative ages. Wide bounds; they catch unit confusion, not judgement. |

Rejections are output, not errors. A run that accepted four fields and refused six is a
successful run with a visible boundary. Discarding the six would throw away the only evidence
the boundary works.

### 2. A field with no quote from the document does not exist

Citation grounding is the load-bearing defence, so it deserves its own statement: the quote is
checked as a substring of the text the model was sent, after whitespace normalisation and
nothing else.

Whitespace normalisation is the only relaxation, and it is required rather than convenient —
`pdftotext` wraps sentences at 80 columns, so a model quoting a sentence it read across a line
break produces a string absent character-for-character from the source while being unmistakably
present to a human.

It is deliberately **not** relaxed to a similarity ratio. A quote matching at 85% is a quote the
model partly wrote itself, which is the exact case the check exists to catch.

Grounding runs against the **redacted** text, not the original file. Grounding against the
original would accept a quote containing a phone number the model could never have seen — the
check would keep reporting success while no longer testing anything.

### 3. Redaction is a type, not a parameter

`docs/THREAT_MODEL.md` promised redaction that "cannot be skipped by a caller". A boolean
argument cannot deliver that; some call site under deadline pressure sets it to `False` and
nothing fails.

So `redact()` is the only constructor of `RedactedText`, and the send path in `extract/run.py`
hands the provider `redacted.text` and never the `Document`. Python has no private constructors,
so this is not "impossible" — it is *impossible by accident, and obvious in a diff*, which is the
strongest available guarantee. A test asserts `redact()` has never grown a `skip=` parameter.

Redaction lives in `core/`, which by [ADR 0002](0002-pure-scoring-core.md) cannot import
anything that reaches a network. That is what makes it unskippable rather than merely
conventional: there is no arrangement of core code in which document text reaches a model
without passing through it.

### 4. The default provider sends nothing anywhere

Three providers behind one interface: a deterministic offline one, Ollama for a model on the
existing cluster, and any OpenAI-compatible endpoint. **`offline` is the default at every door.**

This matters most at the HTTP endpoint, which accepts a file from whoever can reach the port. A
default that forwarded uploads to a third party would turn one exposed container into a
data-exfiltration path without anyone choosing that.

The offline provider is roughly twenty regexes over common inspection-report phrasing, and its
docstring says in its first line that it is not a language model. It exists because it makes
every rule above testable with no key and no network — grounding, coercion, bounds, forbidden
fields, the call log, both doors — and because a fresh clone gets a working demo instead of a key
prompt. Its quotes are real spans from the document, so it is subject to the same grounding check
as any model. Nothing gets a pass for being local.

### The mnemonic

ADR 0004 gave **the model reads, the code decides**. This ADR adds the enforcement half:
**declared, permitted, grounded, plausible — or refused.**

## Alternatives considered

| Option | Why not |
|---|---|
| Trust the schema and validate loosely | The dangerous response is well-formed. Loose validation catches only the harmless failures. |
| Fuzzy quote matching | Defeats grounding for the one case it exists for: a mostly-copied sentence with the number changed. |
| Ask the model to cite a page number instead of a quote | A page number cannot be verified. A quote can be checked against the bytes that were sent, and that difference is the whole control. |
| `redact=False` for debugging | The escape hatch becomes the default the first time someone is in a hurry. Redacted text is perfectly debuggable. |
| Skip the offline provider and mock in tests | A mock asserts the code matches the mock. The offline provider is a real implementation of the interface, and it also ships as a usable default. |
| Store the sent text in the call log | Would make the log the largest data-at-rest risk in the project — a second copy of the sensitive material with a longer retention than the request. |
| Promote a confirmed finding to `confidence: measured` | Confirmation removes "a model may have invented this", not "the document may be wrong". `measured` means read from a primary source, and a seller's disclosure is not one. |
| Let the model return several defects at once | The schema is one value per field, so a document listing nine deficiencies yields one. A real limitation, recorded in `KNOWN_LIMITATIONS.md` rather than fixed by loosening the schema. |

## Consequences

**Good.** The boundary is executable. `tests/test_extraction_boundary.py` drives responses a real
model plausibly returns and asserts the code's answer, including the parametrised case that every
forbidden field is refused by name. Swapping providers is a config change, and the call log's
acceptance rate makes the swap measurable — a provider that starts fabricating shows up as a rise
in `citation not found` rather than as wrong numbers in front of a buyer. The whole path runs in
CI with no credentials.

**Bad.** Over-refusal is now the common failure. A model that paraphrases a quote instead of
copying it loses a correct field, and the fix is prompting, not loosening the check. The page
filter can drop the page holding the field someone wanted, which is why `--all-pages` exists.
Names that are not supplied and not label-adjacent survive redaction — a real gap, recorded
rather than papered over.

**Deferred.** Confirmed findings do not yet flow into a score. `Finding.to_value()` exists and
raises until confirmed, but nothing stores a confirmation — that needs a ledger table and a UI,
and shipping the refusal machinery first means the boundary is enforced before anything depends
on it.

**Cost of being wrong.** Still asymmetric, and still in the same direction ADR 0004 identified.
Over-refusing costs a field and an annoyed user. Under-refusing puts a fabricated roof age, or a
model's guess at a tax bill, into a six-figure decision — and it arrives wearing the same
formatting as a number that was actually read off a page.
