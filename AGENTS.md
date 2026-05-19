# AGENTS.md

## Project

This repository is **Qualia Transcript**, a local Flask/SQLite web application for qualitative research interview transcription, segment review, question mapping, quote management, AI-assisted analysis, and Word/Excel deliverable generation.

## v0.2 Core Principles

- Do not treat this as a generic transcription app.
- Preserve traceability from every analysis output back to question, flow, mapping, participant, segment, and timestamp.
- **Segment.text is non-destructive and must never be modified** by AI correction, glossary normalization, quote extraction, analysis, or output generation.
- **raw_transcripts snapshots are immutable** operational artifacts.
- Primary evidence is:
  - question / flow / UtteranceMapping / PerQuestionAnalysis / QuoteCandidate / Segment
- `semantic_clusters` is exploratory/supporting evidence, not primary evidence.

## Safety Rules

- Do not call OpenAI API, Whisper, Rakuten API, embedding APIs, or other paid/external APIs unless explicitly requested.
- Do not run paid checks unless explicitly requested.
- Do not touch `.env`, DB files, `uploads/`, `outputs/`, `outputs/raw_transcripts/`, or `logs/` unless explicitly requested.
- Do not print API keys/secrets.
- Do not put API keys in code.
- Do not perform large refactors.

## Evidence and Quote Rules

- AI must not generate quote body text for deliverables.
- Quotes must be resolved locally from `quote_id` and/or `source_segment_ids` against DB records.
- `SegmentFlag.quote` is a lightweight candidate marker.
- `QuoteCandidate` is the formal quote candidate entity.
- `QuoteCandidate.quote_text` must be derived from `Segment.text` or `reviewed_text` (if introduced).
- Per-question analysis should carry trace fields whenever evidence exists:
  - `source_segment_ids`
  - `source_segment_quotes`
  - `quote_ids`
- Draft per-question analysis may be saved without trace for compatibility, but it must not be approved or reflected in formal outputs until trace is present.

## Analysis Status Rules

- `AIAnalysis.status` lifecycle:
  - `draft` / `reviewed` / `approved` / `rejected`
- Non-approved AI analysis must not be reflected in formal Word/Excel deliverables.
- Non-approved QuoteCandidate must not be reflected as formal quotes in deliverables.

## Review Queue Rules

- Use exception-based review (Review Queue), not all-record manual review.
- Queue only items that affect quality/deliverables, such as:
  - unclassified mappings
  - low/medium confidence mappings
  - unresolved speaker assignment
  - needs_review segments
  - quote candidates
  - AI analysis drafts

## Data Model Direction (v0.2)

- `confidence_level` belongs to **UtteranceMapping**, not Segment.
- Segment-side incremental candidate field may include `duplicate_candidate`.
- Standard chunk overlap target is **3-5 seconds** for long-audio chunk transcription.
- Add `APIUsageLog` for paid API observability with fields such as:
  - provider
  - model
  - operation_type
  - request_count
  - token usage
  - audio_duration_sec
  - estimated_cost

## Product Hint Policy

- ProductHint external API is default OFF for safe/local operation.
- Glossary/product hinting must remain non-destructive to transcript text.

## MVP Scope Notes

- MVP formal target is audio files.
- Video handling is out of MVP formal scope.

## Check Policy

- Keep `check_all.ps1` default as safe-only.
- Do not include paid/API checks in default safe run.
- `-AllLocal` must remain external-API free.

## Implementation Rules

- Make small, reviewable changes.
- Prefer additive and backward-compatible schema updates.
- Keep existing routes/services working.
- Add/update tests or checks with behavior changes.
- Update docs when behavior changes.

## Before Finalizing Changes

- Report what checks were run.
- Report what checks were not run and why.
- Report current `git status --short`.
