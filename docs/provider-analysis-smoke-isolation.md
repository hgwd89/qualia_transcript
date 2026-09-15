# Provider Analysis Smoke Isolation

## Purpose

`tests/smoke_analysis.py` is an explicitly requested provider-backed smoke test. It verifies the real OpenAI analysis path, but it must never use canonical research rows as disposable test fixtures.

The smoke therefore separates **credential access** from **research-data mutation**.

## Canonical database boundary

The configured canonical SQLite database may be opened only through an explicit read-only URI:

- `mode=ro`;
- `PRAGMA query_only=ON`;
- only the stored `openai_api_key` setting may be read.

The canonical database is never passed to Flask-SQLAlchemy by this smoke. Before application/model imports, `config.DATABASE_URI` is redirected to a newly created temporary SQLite path. Upload, output, backup, and runtime-lock paths are redirected to the same temporary workspace.

For additional detection, the smoke fingerprints the canonical SQLite database and WAL bytes before and after execution and fails if the durable bytes change.

## Disposable research fixture

The provider call runs against a self-contained temporary fixture containing its own:

- `Project`;
- `Participant`;
- `Interview`;
- respondent `Segment`.

The smoke calls `analyze_interview_summary()` with an explicit no-op result-write guard because the entire database is disposable. It verifies that exactly one provider call occurs, one temporary `AIAnalysis` is persisted, and only the temporary interview transitions to `analyzed`.

No existing project/interview/segment ID is selected. In particular, the historical hard-coded `interview_id=4` contract is prohibited.

## Secret handling

If `OPENAI_API_KEY` is already configured in the environment, the normal provider client uses it. Otherwise the smoke may copy the stored secret envelope from the canonical `app_settings` row into the temporary database. On Windows this can remain a current-user DPAPI-protected value; the smoke does not need to expose plaintext.

## Windows cleanup

`create_app()` holds a process-lifetime runtime lock. Because the smoke redirects that lock into its temporary workspace, it must explicitly call `release_process_runtime_locks()` after disposing SQLAlchemy and before the temporary directory is removed. This prevents an open Windows lock-file handle from turning successful isolation into cleanup failure.

## Providerless permanent regression

`tests/smoke_analysis_provider_isolation.py` executes the same isolation path with an injected fake provider. It creates a separate sentinel SQLite database standing in for canonical research data and verifies:

- the isolated analysis completes successfully;
- the sentinel database SHA-256 is unchanged;
- the sentinel row is unchanged;
- Flask application tables were never initialized in the sentinel database;
- the provider smoke contains no hard-coded live interview selector;
- canonical credential access remains explicitly read-only;
- the process runtime lock is explicitly released.

This regression makes no external API call and runs permanently on both Windows and Ubuntu in `Durable Processing Jobs`.

## Operator entry point

The paid/provider-backed check remains manual-only and runs only when explicitly requested:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
```

It must not be added to normal CI. CI runs only the providerless isolation regression.
