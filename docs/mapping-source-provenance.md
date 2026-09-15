# Mapping Source Provenance

## Purpose

An AI `UtteranceMapping` is only research-valid for the exact canonical input generation supplied to the mapping provider. Execution-time durable-job fencing prevents many concurrent writes, but it cannot prove that a mapping saved yesterday still describes canonical segment and interview-flow content after later edits.

Every new AI mapping generation therefore records a deterministic source fingerprint and revalidates it before canonical replacement.

That proof is also a downstream-input contract. A persisted mapping-dependent AI analysis must not treat the mere existence of `UtteranceMapping` rows as proof that those classifications are still valid. Per-question, cross-participant, and integrated analysis require the mapping state they consume to be current before provider work and whenever analysis source provenance is revalidated later.

## Exact source manifest

`mapping-input-v1` fingerprints only fields that affect the mapping provider input, candidate membership, or deterministic ordering:

- project ID, interview ID, selected flow ID;
- respondent segments ordered by `(seq, id)`, including segment ID, `seq`, `speaker_role`, and text;
- flow questions ordered by `(section.seq, section.id, question.seq, question.id)`, including section ID/sequence, question ID/sequence, `question_code`, and `question_text`.

Unconsumed metadata such as participant labels or segment timestamps is deliberately excluded so unrelated edits do not create false staleness.

The provider prompt is rendered from the same immutable manifest whose SHA-256 is persisted. There is no separate provenance query that could claim a different source generation from the one logically supplied to the provider.

## Save fencing

The mapping worker:

1. captures one canonical source manifest;
2. derives both provider prompt and source proof from that manifest;
3. validates provider result ownership/completeness;
4. verifies the durable result-write lease;
5. recomputes current canonical mapping input and requires the saved proof to match;
6. only then replaces the previous mapping set and commits the new mappings plus provenance.

If segment membership/text/order, selected flow, or consumed question identity/text/order changes while provider work is in flight, the worker fails closed before deleting existing mappings.

## Downstream mapping-input currentness

`services/mapping_input_guard.py` is the ORM-side acceptance boundary for mapping-dependent analysis input.

For an interview with mapping data, every current respondent segment must have mapping coverage. A non-null `question_id` must belong to the interview's currently selected flow. Historical duplicate rows are tolerated only when every row for the same segment represents the same effective question assignment; conflicting duplicates that assign the same segment to different questions fail closed. This preserves the existing deterministic segment-level deduplication contract without allowing ambiguous classification input.

Human review and AI provenance have different semantics:

- `mapped_by="human"` and historical `mapped_by="manual"` rows are canonical human decisions and do not require provider provenance;
- every remaining `mapped_by="ai"` row must have a provenance sidecar;
- all remaining AI rows must share one coherent source generation;
- that generation must still match the current `mapping-input-v1` manifest.

Mixed human/AI mapping is therefore valid when the human overrides are canonical and the remaining AI classifications still belong to one current proven generation. Human override does not force an unnecessary remap of unrelated, still-current AI rows.

`services/analysis_source_provenance.py` applies this guard inside `_mapped_respondent_segments()`. This is deliberate: the same helper builds mapping-dependent provider prompts and their long-lived `analysis-input-v1` provenance. Stale/unproven mapping therefore blocks provider work before a new canonical `AIAnalysis` is created, and the same condition later makes an existing mapping-dependent analysis stale at approval/formal-export currentness checks.

Per-participant analysis is not mapping-dependent and is not subject to this mapping gate.

## Production-readiness contract

`services/mapping_input_readiness_sqlite.py` reconstructs the same mapping manifest and currentness rules from the caller-owned read-only SQLite snapshot. The final production-readiness entry point executes that check inside the same transaction used by the existing hardened readiness audit.

Any mapped interview whose downstream mapping input is incomplete, conflicting-duplicate, cross-flow, unsupported, provenance-missing, mixed-generation, or stale is a `mapping_input_currentness_invalid` blocker. Semantically identical historical duplicates remain accepted because they collapse to the same segment/question input. Untouched interviews with no mapping state remain governed by the existing unmapped/readiness signals and are not falsely promoted into mapping blockers.

Project-scoped readiness filters canonical `main` rows by the selected project while retaining the same snapshot/change-detection guarantees.

## Storage and historical compatibility

Provenance is stored in the one-to-one `utterance_mapping_provenance` sidecar table keyed by `mapping_id`. The existing `utterance_mappings` schema is not rewritten.

This choice is intentional:

- existing SQLite installations receive the missing sidecar through normal `db.create_all()` table creation;
- historical mappings remain byte-for-byte/history-preserving rather than being backfilled with invented provenance;
- an AI mapping without a provenance sidecar is explicitly unprovable and fails currentness validation.

Human mappings do not require AI source provenance. The sidecar contract is used only for `mapped_by="ai"` generations.

## Crash-window recovery

A mapping durable job may crash after the mapping transaction commits but before the processing-job row is marked succeeded. Recovery may adopt that committed result only when all of the following are true:

- mappings were created inside the current durable attempt window;
- the current respondent mapping set is an AI generation;
- every AI mapping in the generation has the same provenance proof;
- the generation covers the complete current respondent set;
- the proof still matches current canonical segment and flow-question input.

Missing, mixed, stale, or incomplete provenance causes recovery to return no completed result and forces normal retry behavior instead of silently adopting an invalid generation.

## Regression coverage

`tests/smoke_mapping_source_provenance.py` is providerless and verifies mapping save/recovery provenance.

`tests/smoke_mapping_input_currentness.py` is providerless and verifies:

- a complete current AI generation is accepted;
- mixed human/current-AI mapping remains valid;
- source drift makes AI mapping stale;
- stale mapping is rejected before mapping-dependent analysis provider work;
- removing the remaining AI sidecar invalidates existing analysis currentness even when mapping rows and source text are otherwise unchanged;
- partial and cross-flow mappings fail closed;
- semantically identical historical duplicates remain accepted while conflicting duplicates fail closed;
- ORM and read-only SQLite currentness agree.

`tests/smoke_mapping_input_readiness.py` verifies both database-wide and project-scoped final readiness block stale mapping input.

The provider/input regression runs permanently in `Durable Processing Jobs` on Windows and Ubuntu and in `scripts/check_processing_jobs.ps1`. The final-readiness regression runs permanently in both Windows and Ubuntu jobs of `Safe Smoke Check`.
