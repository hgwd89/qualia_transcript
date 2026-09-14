# Readiness Artifact Snapshot Contract

Production readiness must not combine evidence from different filesystem generations of the same `GeneratedFile`.

For a generated artifact with registered `artifact_sha256`, readiness opens the managed path through the ancestry-pinned managed-read boundary, copies those exact bytes into an immutable verified snapshot while checking SHA-256, and performs XLSX/DOCX/CSV structural validation against that same snapshot. Structural validation must not first read the mutable pathname and then perform byte-integrity verification through a second open.

This closes the race in which generation A could satisfy OOXML/CSV structure checks, the pathname could be replaced, and generation B could independently satisfy the registered SHA-256. Hash validity and structural validity now describe one byte sequence.

Legacy ordinary `GeneratedFile` rows without `artifact_sha256` remain compatibility-only: readiness warns that their original bytes are unproven and performs structural validation against the current pathname. Formal approved-analysis artifacts remain fail-closed when byte-integrity metadata is missing or invalid.

`tests/smoke_readiness_artifact_snapshot_consistency.py` reproduces the former race by starting with a valid XLSX and replacing it immediately before the hash-bound managed open with a hash-matching generic ZIP. Readiness must reject the replacement as structurally invalid without reporting a SHA-256 mismatch. The regression runs on both Windows and Ubuntu in `.github/workflows/safe-check.yml`.
