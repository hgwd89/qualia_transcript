# Local backup and recovery runbook

## Scope

A Qualia Transcript backup is a single ZIP archive containing:

- a consistent SQLite snapshot created with SQLite's online backup API
- `uploads/` including source media stored by the application
- `outputs/` including immutable raw transcript snapshots and generated deliverables
- `manifest.json` with the exact archive membership, byte sizes, and SHA-256 for every backed-up file

`backups/` is excluded from Git. Backup archives can contain respondent data, audio/video, raw transcripts, generated reports, and application settings. Treat the archive as confidential research data.

## Create a backup

From the repository root:

```powershell
python scripts/backup_local_data.py --label before_major_change
```

The default destination is `backups/`. The command does not merely copy the live SQLite file: it creates a SQLite-consistent snapshot, builds the archive, then reopens and validates the archive.

A backup is considered valid only when:

- the manifest exists and has the supported format/version
- the ZIP contains exactly the files declared by the manifest, with no undeclared extras or duplicate paths
- every file's byte size and SHA-256 match the manifest
- the backed-up SQLite database passes `PRAGMA integrity_check`

## Validate without restoring

Validation is the default restore behavior and is non-destructive:

```powershell
python scripts/restore_local_data.py backups\qualia_backup_....zip
```

No DB, upload, or output file is changed.

## Apply a restore

Stop Qualia Transcript first. Do not restore while the Flask app is running.

```powershell
.\stop_app.ps1
python scripts/restore_local_data.py backups\qualia_backup_....zip --apply --yes
```

Before overwriting the current state, the restore command creates a new `pre_restore` safety backup when a current database exists. It then restores the database, `uploads/`, and `outputs/` as one recovery set and verifies the restored database again.

The `--apply --yes` pair is intentionally required. `--apply` without `--yes` is rejected.

## Recovery rules

- Do not manually unzip a backup into the application directory.
- Do not restore only the SQLite DB while leaving unrelated `uploads/` or `outputs/` in place; that can create broken references or mismatched research data.
- Keep at least one verified backup outside the working repository/device for projects that cannot be reconstructed from source media.
- After restore, run `scripts/check_local_data_integrity.ps1` before resuming production work.
- If a restore fails after changes begin, the restore service attempts to roll the DB/uploads/outputs back to the state captured immediately before replacement; the pre-restore ZIP is an additional recovery point.

## Regression check

The implementation is covered by a temporary-data round-trip smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_local_production.ps1
```

It verifies runtime defaults, portable launcher behavior, backup creation, validation-only behavior, DB/file round-trip restore, removal of stale files, tamper detection, and rejection of files not declared in the manifest. It does not call OpenAI, Whisper, or any external API.
