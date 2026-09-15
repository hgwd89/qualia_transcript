# Mapping Source Provenance

## Purpose

An AI `UtteranceMapping` is only research-valid for the exact canonical input generation supplied to the mapping provider. Execution-time durable-job fencing prevents many concurrent writes, but it cannot prove that a mapping saved yesterday still describes canonical segment and interview-flow content after later edits.

Every new AI mapping generation therefore records a deterministic source fingerprint and revalidates it before canonical replacement.

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

## Final production readiness

The final professional-readiness audit validates persisted mapping provenance again from its caller-owned, single SQLite read snapshot. For interviews in `mapped`, `analyzed`, or `done` state, every AI mapping still present must be provable against the current canonical `mapping-input-v1` manifest.

Delivery is blocked when any of the following is true:

- AI mappings exist but `utterance_mapping_provenance` is unavailable;
- an AI mapping has missing, malformed, incomplete, or unsupported provenance;
- current AI mappings for one interview contain multiple provenance generations;
- stored project/interview/flow scope no longer matches canonical scope;
- the persisted SHA-256 no longer matches current respondent segment or flow-question input;
- an AI mapping references a segment that is no longer a respondent source input;
- provenance sidecar rows are orphaned from their mapping rows.

Human overrides are deliberately compatible with this contract. If a reviewer changes one mapping to `mapped_by="human"`, that row no longer requires AI provenance. Remaining AI mappings must still carry one current source proof. Final readiness therefore does not require AI mappings to cover the entire respondent set, while crash-window recovery remains stricter because it is specifically adopting a complete machine-generated result transaction.

Project-scoped final readiness validates only AI mappings belonging to that project. The check runs inside the same SQLite read transaction used by the hardened readiness audit, so mapping currentness cannot be evaluated against a different database generation from the rest of the delivery gate.

## Regression coverage

`tests/smoke_mapping_source_provenance.py` is providerless and uses a temporary SQLite database. It verifies:

- source mutation during the provider window fails before canonical replacement and preserves the previous mapping;
- a valid AI generation persists a current proof;
- segment edits after completion make the generation stale;
- question edits after completion make the generation stale;
- crash-window recovery accepts a current proven generation;
- crash-window recovery rejects stale or provenance-missing generations;
- the provenance sidecar schema is created normally.

`tests/smoke_mapping_provenance_readiness.py` verifies the delivery gate separately:

- current proven AI mappings pass mapping-readiness validation;
- the final hardened readiness entry point actually executes the mapping check;
- post-generation segment or question edits block delivery;
- missing and mixed provenance block delivery;
- a human override may coexist with remaining current AI mappings;
- missing provenance-sidecar schema with AI mappings blocks delivery.

The generation regression runs permanently in `Durable Processing Jobs` on Windows and Ubuntu and in `scripts/check_processing_jobs.ps1`. The final-readiness regression runs permanently in `Safe Smoke Check` on Windows and Ubuntu.
