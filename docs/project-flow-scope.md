# Project Flow Scope

`Interview.flow_id` is research provenance. The application must not infer an intended guide from relationship order when multiple interview flows exist.

## Project pipeline

`resolve_pipeline_interview_flow()` is the canonical resolver.

- An existing interview flow must belong to the same project.
- A missing flow may be auto-assigned only when the project has exactly one configured flow.
- With multiple configured flows, a missing interview flow returns `ambiguous_flow_assignment` and the interview remains unchanged.
- With no configured flow, processing returns `missing_project_flow`.
- The sole-flow condition is rechecked after the durable result-write reservation is acquired, before `Interview.flow_id` is committed.

## Cross-participant analysis

Cross-participant analysis is scoped by `InterviewFlowQuestion.id`. The analysis page lists questions from every configured flow. The analyzer reads only interviews assigned to the selected question's flow.

Human flow title/version pairs are not unique. The analysis chooser and saved-result/review presentation therefore include the stable `InterviewFlow.id` alongside title/version so two identically named/versioned guides cannot become visually indistinguishable.

## Integrated analysis

The current integrated-analysis artifact is single-flow. New results persist `source_flow_id`, `source_question_ids`, and `source_interview_ids`.

Before provider work, integrated analysis requires exactly one configured flow, at least one participant-linked interview, an explicit matching `flow_id` on every participant-linked interview, and status `mapped`, `analyzed`, or `done` for every participant-linked interview. Each target interview must also contribute at least one classified (`is_unclassified=false`) respondent mapping to a question in the sole source flow. An interview whose only respondent mappings are unclassified is rejected with `integrated_interview_no_mapped_evidence` rather than being recorded in `source_interview_ids` without entering the provider prompt.

The same integrated scope is revalidated inside serialized durable-job admission. Otherwise the request returns a structured scope error instead of selecting an arbitrary flow, silently omitting interviews, or creating a job from a stale preflight snapshot.

Evidence resolution for new integrated results is restricted to the persisted `source_interview_ids` as well as the persisted source flow. Adding another interview later cannot expand the evidence set of an older analysis.

Explicit multi-flow integrated analysis is a separate feature because it requires an explicit source-selection contract rather than an implicit first-flow rule.

## Regression

`tests/smoke_project_flow_scope.py` uses a temporary SQLite database and no external provider. It verifies ambiguous pipeline assignment does not mutate `flow_id`, invalid integrated scope is rejected before job creation, serialized admission rechecks scope, all flow questions are exposed for cross analysis, valid single-flow analysis persists exact provenance, incomplete interviews block provider work, and later interviews cannot become evidence for an older integrated result.

`tests/smoke_analysis_flow_provenance.py` verifies that unclassified-only respondent evidence cannot enter integrated provenance, duplicate flow title/version pairs remain distinguishable by stable flow ID in the chooser, and saved cross-analysis results expose the stable source-flow identity on both analysis and review pages without rewriting the stored analysis title.