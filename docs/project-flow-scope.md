# Project Flow Scope

`Interview.flow_id` is research provenance. The application must not infer an intended guide from relationship order when multiple interview flows exist.

## Project pipeline

`resolve_pipeline_interview_flow()` is the canonical resolver.

- An existing interview flow must belong to the same project.
- A missing flow may be auto-assigned only when the project has exactly one configured flow.
- With multiple configured flows, a missing interview flow returns `ambiguous_flow_assignment` and the interview remains unchanged.
- With no configured flow, processing returns `missing_project_flow`.

## Cross-participant analysis

Cross-participant analysis is scoped by `InterviewFlowQuestion.id`. The analysis page lists questions from every configured flow. The analyzer reads only interviews assigned to the selected question's flow.

## Integrated analysis

The current integrated-analysis artifact is single-flow. New results persist `source_flow_id`, `source_question_ids`, and `source_interview_ids`.

Before provider work, integrated analysis requires exactly one configured flow, at least one participant-linked interview, an explicit matching `flow_id` on every participant-linked interview, and status `mapped`, `analyzed`, or `done` for every participant-linked interview. Otherwise it returns a structured scope error instead of selecting an arbitrary flow or silently omitting interviews.

Evidence resolution for new integrated results is restricted to the persisted `source_interview_ids` as well as the persisted source flow. Adding another interview later cannot expand the evidence set of an older analysis.

Explicit multi-flow integrated analysis is a separate feature because it requires an explicit source-selection contract rather than an implicit first-flow rule.

## Regression

`tests/smoke_project_flow_scope.py` uses a temporary SQLite database and no external provider. It verifies ambiguous pipeline assignment does not mutate `flow_id`, invalid integrated scope is rejected before job creation, all flow questions are exposed for cross analysis, valid single-flow analysis persists exact provenance, incomplete interviews block provider work, and later interviews cannot become evidence for an older integrated result.