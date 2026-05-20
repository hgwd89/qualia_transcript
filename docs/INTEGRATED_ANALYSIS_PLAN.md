# Integrated Analysis AI Dry-Run Plan

## Scope for Next Implementation (Not Implemented Yet)

1. Build AI input from existing no-ai payload  
   - Reuse integrated no-ai payload as the only source object.
   - Keep `source_segment_ids` and `source_segment_quotes` mandatory in the prompt input.

2. Restrict quote handling to deterministic references  
   - Ask AI to return only `quote_id` / `finding_id` / `summary`.
   - Do not let AI generate new quote body text.
   - Resolve quote body from stored `source_segment_quotes` on the app side.

3. Add explicit AI-run option only  
   - `--ai` enables AI dry-run explicitly.
   - Without `--ai`, keep current no-ai behavior.
   - `--no-ai` remains available as an explicit no-ai override.
   - `--save` remains disabled for this phase.

## Out of Scope for This Phase

- `--save` implementation and persistence flow
- Interview UI rendering for integrated analysis
- Word/Excel deliverable reflection of integrated analysis output
- Cross-project integrated analysis
- Large-scale production data processing

## Constraints for AI Dry-Run

- OpenAI API call count: max 1 call per run
- `source_segment_ids` and `source_segment_quotes` are required
- Segments with `exclude` flag are removed from AI input
- `needs_review` segments are handled as `cautions`
- `quote` flag segments are prioritized for candidate evidence
- Unresolved participant/speaker assignments are added to `cautions`


## Implemented in `feature/integrated-analysis-ai-flag`

- CLI now exposes `--ai` as the only way to enable AI dry-run.
- Default CLI behavior remains no-ai.
- `--ai` and `--no-ai` are mutually exclusive.
- No validation command in this step calls OpenAI API.
