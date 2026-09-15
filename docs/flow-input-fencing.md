# Flow Input Fencing Contract

## Purpose

Interview-flow structure is canonical research input. Mapping consumes the complete question set for an interview flow, while the project pipeline and integrated analysis consume project flow structure or flow-selection state.

A durable worker must not read one flow generation, call an external provider, and then commit its result after the canonical flow/question set has changed.

## Serialized write boundary

Flow mutations use `services.flow_input_guard.begin_flow_input_write()`.

The guard and durable-job admission both acquire SQLite `BEGIN IMMEDIATE` before making their final active-job decision. This establishes one serialization order:

- if the flow mutation acquires the reservation first, it commits before a conflicting job can be admitted and that later job reads the new canonical generation;
- if the durable job is already active, the flow mutation observes it and fails closed without changing canonical flow rows.

Stale jobs are recovery-checked before the final write reservation so a dead worker does not indefinitely block legitimate research corrections.

## Conflict scope

Every project flow-set or flow-structure mutation conflicts with active:

- `project_pipeline`
- `analyze_integrated`

These jobs can consume project-wide flow selection or integrated flow structure.

A mutation that changes the question set of an existing flow additionally conflicts with active `map` jobs whose interviews are assigned to that exact flow. Mapping jobs on another flow in the same project do not block the write.

The guard deliberately does not block unrelated jobs such as transcription or participant-summary analysis when the changed flow state is not one of their provider inputs.

## Covered routes

The canonical Flask flow routes acquire the guard before committing:

- create flow
- add section
- add question
- delete flow

Question creation performs its sequence/code checks after the serialized reservation is acquired. Flow deletion performs usage checks under the same reservation before deleting.

## Why mapping needs this fence

`services.mapper.run_mapping()` builds the provider prompt from the current complete question list, performs the external call, then uses the durable result-write guard before replacing canonical mappings. The result-write guard proves worker-attempt ownership; it does not independently fingerprint the question list used by the provider call.

Without flow-input fencing, a question could be inserted during the provider call and the worker could commit mappings derived from the older question set into the newer canonical research state. The write fence closes that mixed-generation path.

Analysis generation has additional source-provenance validation at result commit, but flow-input fencing still prevents conflicting canonical mutations while project-wide integrated work is active and keeps the durable-job contract consistent.

## Regression coverage

`tests/smoke_flow_input_fencing.py` uses only a temporary SQLite database and no external API. It verifies that:

- an active mapping job blocks question-set mutation on its own flow;
- the same mutation succeeds after that mapping job becomes terminal;
- an active integrated-analysis job blocks both project flow-set and flow-structure mutation;
- a mapping job on a different flow does not overblock the target flow;
- a stale mapping job is recovered before the final flow-write decision.

The regression runs on both Ubuntu and Windows in the permanent `Durable Processing Jobs` workflow and is also included in `scripts/check_processing_jobs.ps1`.
