# ADR 0004 — LLMs do extraction only and never arithmetic

**Status:** Accepted · 2026-08-19

## Context

A planned feature accepts an inspection report, HOA bylaws, or a seller's disclosure and returns structured data. That is a genuine strength of language models: unstructured text in, schema-shaped output out.

The temptation is to keep going — let the model also estimate the tax bill, or judge whether the house is a good deal. That temptation should be refused, for two reasons.

Language models produce plausible numbers. A tax calculation that is confidently wrong by $2,000 a year is worse than no calculation, because it will be trusted. And the arithmetic here is not hard — it's a handful of deterministic formulas over known inputs. Delegating solved math to a probabilistic system trades correctness for nothing.

## Decision

A hard line through the system.

**Permitted:**
- Extract fields from documents into a declared schema — roof age, HVAC age, HOA dues, easements, disclosed defects.
- Every extracted field carries a page or line citation back to the source document.
- Every extracted field is `confidence: extracted` and requires human confirmation before it affects a score.
- Summarize long documents into a reviewable list of findings.

**Forbidden:**
- Any dollar figure. Payments, taxes, insurance, closing costs, maintenance reserve, DTI.
- The property score, or any component of it.
- The buy/watch/pass verdict.
- Filling a gap when a source is unavailable. An unreachable source yields `unavailable`, never a guess.

The mnemonic: **the model reads, the code decides.**

## Alternatives considered

| Option | Why not |
|---|---|
| LLM computes ownership cost from a document | Silent arithmetic errors in the highest-stakes numbers in the app. |
| LLM produces the final score | Unauditable and non-reproducible. The same house would score differently on different days. |
| LLM fills gaps when a source is down | Manufactures data that looks identical to measured data. Directly contradicts the provenance model. |
| No LLM at all | Safest, but discards the one place the technology is genuinely the right tool — a 40-page inspection PDF. |

## Consequences

**Good.** Every dollar figure is reproducible and traceable to a formula. Extraction errors surface at a human confirmation step rather than silently propagating into a verdict. The boundary is simple enough to state in one sentence, which means it will actually be respected.

**Bad.** More clicks — extracted fields need confirming. Extraction will sometimes be wrong, and the confirmation step is load-bearing rather than decorative.

**Privacy.** Documents may contain names, financial details, and inspection findings. Redaction before send, retention limits, and provider choice are covered in [THREAT_MODEL.md](../THREAT_MODEL.md).

**Cost of being wrong.** Asymmetric, which is why the line sits here. Being too strict costs some convenience. Being too loose puts a confidently-wrong number in front of a six-figure decision.
