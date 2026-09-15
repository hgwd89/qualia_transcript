# Semantic Analysis Source Provenance

## Purpose

Persisted `semantic_clusters` rows are durable research artifacts. A finished row must retain enough information to prove which canonical respondent segments and request scope produced it, and later code must be able to determine whether those canonical inputs still match the database.

Durable job input fencing prevents supported canonical edits while an `analyze_semantic` worker is active. That is necessary but not sufficient after the job finishes: later research corrections can legitimately change segment text, role, participant assignment, timing, ordering, or the respondent segment set.

## Generation-time contract

`services.semantic_analysis.run_semantic_cluster_analysis()` now captures semantic source provenance before embedding/provider work when `save=True`.

The fingerprint covers:

- project ID
- interview ID
- `max_segments`
- `no_ai`
- the exact deterministic respondent candidate prefix
- for each candidate: segment ID, sequence, text, speaker label, speaker role, participant ID, start time, and end time

Candidate ordering is `(Segment.seq, Segment.id)`. The ID tiebreaker matters because historical data can contain duplicate sequence values and `max_segments` selects a prefix.

After provider work, the durable result-write guard reserves the canonical write transaction. The semantic source fingerprint is recomputed inside that reservation. The result is saved only when it exactly matches the pre-provider fingerprint. A mismatch fails closed and no `AIAnalysis` row is committed.

## Persisted proof

Saved semantic payloads contain:

- `semantic_request`
  - `max_segments`
  - `no_ai`
- `source_provenance`
  - provenance version
  - SHA-256 fingerprint

`services.semantic_source_provenance.semantic_analysis_source_provenance_status()` recomputes the same manifest against the current database and returns false when the source is missing, malformed, unsupported, or changed.

Historical semantic rows that predate this contract intentionally fail closed as unprovable rather than being guessed current.

## Scope semantics

`max_segments` is part of the generation contract. If it is `None`, every current respondent candidate participates in the fingerprint. If it is `N`, only the deterministic first `N` respondent candidates are part of that result's source generation. A change outside that consumed prefix does not make the historical result stale; a change that alters the consumed prefix does.

This is deliberate. Provenance should describe the source actually consumed by the generation request, not unrelated canonical rows that the request explicitly excluded.

## Review/approval boundary

This change does not make semantic clustering formally approvable. The formal review path requires standard `findings` with resolvable evidence semantics, while `semantic_clusters` has a different result schema. The provenance contract here establishes source identity/currentness without weakening the existing approval gate.

## Regression coverage

`tests/smoke_semantic_source_provenance.py` uses a temporary SQLite database and patched local embedding/clustering functions. It performs no OpenAI or other external provider calls.

It verifies:

- provenance and request parameters are persisted with saved semantic analysis;
- canonical source changes make the saved result stale;
- `max_segments` currentness matches the exact consumed prefix;
- duplicate segment sequence values still produce deterministic candidate order;
- a source mutation during provider work is detected at result commit and no analysis row is saved.

The regression is permanent in the Windows/Ubuntu `Durable Processing Jobs` workflow and in `scripts/check_processing_jobs.ps1`.
