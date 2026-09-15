# Mapping Result Scope and Ownership

## Purpose

Durable mapping sends one interview's respondent segments and that interview's selected flow questions to the AI provider. Provider output is untrusted derived data: integer IDs that happen to exist in the database are not sufficient proof that those rows belong to the requested research scope.

A mapping result must therefore prove two ownership boundaries before any canonical `utterance_mappings` rows are replaced:

- every returned `segment_id` belongs to the exact respondent segment set supplied for the requested interview;
- every non-null `question_id` belongs to the exact selected interview flow supplied to the provider.

This prevents a provider response containing a valid ID from another interview, flow, or project from creating a cross-scope mapping while still satisfying SQLite foreign keys.

## Completeness contract

A successful mapping result must contain exactly one result row for every respondent segment sent to the provider.

The mapper fails closed when the provider output:

- contains a segment ID outside the requested interview scope;
- contains a question ID outside the interview's selected flow;
- repeats a segment ID;
- omits any respondent segment supplied to the provider.

Validation happens before the durable result-write guard and before existing canonical mappings are deleted. Invalid provider output therefore leaves the existing mapping set untouched and does not mark the interview as mapped.

Low-confidence in-scope classifications continue to normalize to `unclassified`; scope validation is performed against the provider's original non-null question ID before that normalization.

## Deterministic input order

Historical databases can contain duplicate `Segment.seq`, section `seq`, or question `seq` values. Provider prompt order is therefore deterministic:

- respondent segments: `(Segment.seq, Segment.id)`;
- flow sections: `(section.seq, section.id)`;
- questions within a section: `(question.seq, question.id)`.

The ID tie-breakers prevent the same canonical source set from being presented to the provider in an unstable order.

## Regression coverage

`tests/smoke_mapping_result_scope.py` uses a temporary SQLite database and replaces the structured provider call with local fixtures. It performs no OpenAI or other external provider call.

It verifies that:

- a foreign-project segment ID is rejected without deleting the prior mapping;
- a foreign-flow question ID is rejected without deleting the prior mapping;
- duplicate segment rows are rejected;
- partial provider results are rejected;
- a complete in-scope result can replace the mapping set;
- duplicate segment sequence values use the segment ID as a deterministic prompt-order tie-breaker.

The regression is permanent in the Windows/Ubuntu `Durable Processing Jobs` workflow and in `scripts/check_processing_jobs.ps1`.
