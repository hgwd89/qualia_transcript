# Question Identity and Evidence Scope

`InterviewFlowQuestion.question_code` is a human-readable research identity inside one interview flow. It is not a project-global identifier.

## Identity boundary

The same `question_code` may legitimately appear in different flows or flow versions, for example `Q1` in both version 1 and version 2 of a guide. Within a single flow, however, the code must be unique because mappings, analysis labels, exports, and human review use it to understand which research question a quote belongs to.

New writes are protected in two layers:

- the flow route rejects a duplicate code before mutation and returns HTTP 409;
- SQLite installations install `trg_question_code_insert` and `trg_question_code_update`, which join through `interview_flow_sections` and reject concurrent or non-route duplicate writes inside the same flow.

Historical duplicate rows are not renamed, merged, deleted, or silently reassigned by startup. Research history must not be rewritten merely to satisfy a new invariant.

## Canonical evidence identity

`question_code` is a label. `InterviewFlowQuestion.id` is the canonical question identity.

For `per_question` and `cross_participant` analyses, `AIAnalysis.question_id` therefore defines the evidence boundary. Approval resolution requires a candidate segment to be mapped to that exact question ID. A finding-provided `question_codes` string cannot widen that scope to another question that happens to use the same code in another flow.

Cross-participant generation also verifies that the selected question belongs to the requested project and only reads interviews assigned to that question's flow. Model-returned question-code labels are overwritten with the canonical application-owned code before the analysis is saved.

## Integrated analysis

The current integrated-analysis implementation uses one project flow as its source. New integrated results persist:

- `source_flow_id` — the exact flow used to construct the prompt;
- `source_question_ids` — the exact question IDs from that flow;
- normalized finding `question_codes` limited to codes that actually exist in that source flow.

Approval evidence resolution uses `source_flow_id` as the boundary before applying any question-code qualifier. A segment from another flow with the same code is therefore not eligible evidence.

For a historical integrated analysis that lacks `source_flow_id`, approval may infer the flow only when the project has exactly one flow. If multiple flows exist, the source is ambiguous and approval fails closed rather than guessing.

## Readiness

Production readiness reports `duplicate_question_code` as a blocker when a non-empty code occurs more than once inside the same flow. Reusing that code in another flow or flow version remains valid and must not trigger the blocker. The audit is read-only and never rewrites historical question rows.

## Regression contract

`tests/smoke_question_code_identity.py` uses only temporary local data and no external AI provider. It verifies:

- same-flow duplicate insert/update rejection;
- same code remains legal across different flows and projects;
- route-level duplicate rejection without mutation;
- historical duplicates remain intact when guards are installed and future duplicates are still blocked;
- exact `question_id` prevents same-code evidence from another flow being accepted;
- integrated evidence stays inside persisted `source_flow_id`;
- ambiguous legacy integrated analysis cannot be approved by rebinding to an arbitrary flow;
- cross-participant generation rejects foreign-project questions before a provider call and persists canonical question identity;
- integrated generation persists canonical source-flow metadata and discards unknown model-provided question-code labels.

`tests/smoke_question_code_readiness.py` uses a temporary SQLite database to prove historical same-flow duplicates become a readiness blocker while identical codes in another flow remain valid. Both question-identity regressions are providerless and never touch the real research database.
