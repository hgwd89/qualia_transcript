# Managed Media Content Identity Contract

## Scope

This contract applies to uploaded `MediaFile` source data and the private transcription snapshots created from it. It complements the managed-storage pathname/generation fencing documented in `docs/architecture.md`; it does not replace that boundary.

## New uploads

Every newly registered media upload stores both `file_size_bytes` and a lowercase SHA-256 digest in `MediaFile.content_sha256`.

The digest is computed in the same copy loop that writes bytes to the already-opened ancestry-pinned managed destination. It is not computed later by reopening `stored_path`. The registered digest therefore describes the exact bytes written by the upload operation.

The existing managed-write commit guard remains authoritative for namespace consistency. The exact written file generation is re-pinned through the metadata DB commit, the public managed pathname is verified after commit, and a namespace race is compensated according to the existing commit-window contract.

## Transcription input

`create_media_read_snapshot()` opens the registered media through the ancestry-pinned managed read boundary and copies that exact file object to an owner-private temporary snapshot outside `UPLOAD_DIR`.

For rows with registered integrity metadata, the copy must satisfy all of the following before the snapshot can be returned to OpenAI, PyAV, or faster-whisper:

- the opened source size matches `MediaFile.file_size_bytes` when that field is present;
- the file generation stays unchanged for the full copy;
- the copied byte count matches the opened source size;
- the SHA-256 calculated from the exact open handle matches `MediaFile.content_sha256`.

A same-path replacement with the same byte length is therefore rejected by the digest check rather than being accepted as the original source media.

## Legacy rows

`content_sha256` is nullable for backward compatibility. Existing databases receive the column through the non-destructive SQLite startup migration.

A legacy row whose digest is NULL remains readable so older projects are not made inaccessible. Such a row is explicitly unverified by persistent content identity. The application must not auto-backfill a missing digest from the current pathname, because doing so could bless bytes that replaced the original upload after registration.

Any future migration that establishes digests for legacy rows must use an explicit provenance-aware procedure rather than opportunistic read-time backfill.

## Required regression behavior

Safe regression coverage must prove that:

- new uploads persist the SHA-256 of the exact written bytes;
- the schema migration is additive and preserves legacy data;
- a regular-file replacement with the same size but different bytes is rejected before transcription;
- NULL-digest legacy rows remain readable without automatic digest assignment;
- pinned managed-write and DB commit-window fencing remain intact.
