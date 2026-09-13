# Participant Identity Contract

`Participant.participant_code` is a project-local research identity, not merely a display label. It is used in exports and can be used to scope AI finding evidence back to participant utterances. Two participants in the same project must therefore never share the same code.

## Forward-write contract

New schemas declare a composite uniqueness constraint on `(project_id, participant_code)` named `uq_participant_project_code`.

Older SQLite installations may already have a `participants` table without that constraint. Startup does not rebuild that table or rewrite participant rows. Instead it installs non-destructive `BEFORE INSERT` and `BEFORE UPDATE OF project_id, participant_code` triggers. Those triggers reject future project-local duplicates while leaving historical data byte-for-byte/logically unchanged.

Route-level create/edit validation provides a clear user-facing conflict before commit. The database constraint or legacy trigger remains the final concurrency guard if two writes race after the route check.

## Automatic code allocation

Automatic `Pxx` allocation is monotonic over existing numeric `P<number>` codes. It uses the highest existing numeric suffix plus one rather than `COUNT(*) + 1`.

Deleting an unused participant therefore does not cause a later participant to inherit the deleted participant's old automatic code. For example, if `P01`, `P02`, and `P03` exist and `P02` is deleted, the next automatic code is `P04`, not `P03`.

Custom non-`Pxx` codes remain allowed, but they are subject to the same project-local uniqueness rule.

## Historical duplicates

Historical duplicate codes are not silently renamed, merged, deleted, or reassigned. Such mutation could destroy research traceability.

Production readiness reports `duplicate_participant_code` as a blocker and includes the affected project/code groups. Human remediation is therefore explicit and auditable.

Project-scoped readiness shadows `participants` with a project-filtered TEMP VIEW before the base readiness audit runs. A duplicate in another project must not block delivery of the selected project, while global database/recovery integrity checks remain global.

## Evidence traceability

Analysis evidence resolution may use `finding.participant_codes` to filter respondent segments. Project-local participant-code uniqueness is therefore required to make that qualifier unambiguous. The canonical evidence remains `source_segment_ids`; participant code is a human-readable/scoping identity that must resolve consistently within a project.

## Regression contract

`tests/smoke_participant_code_identity.py` uses a temporary legacy-style SQLite database with duplicate historical participant rows. It verifies that:

- startup preserves the historical duplicates;
- legacy insert/update guards are installed;
- a new duplicate is rejected at the database boundary;
- create/edit routes reject explicit duplicates;
- automatic codes do not reuse a deleted count slot;
- global readiness blocks the historical duplicate;
- project-scoped readiness reports the blocker only for the owning project and counts only that project's participants.

The smoke uses only temporary database/upload/output/backup paths and never touches real research data.
