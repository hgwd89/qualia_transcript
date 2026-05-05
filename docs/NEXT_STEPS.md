# Next Analysis Workflow Steps

## Next to Build

1. Unclassified review UI  
   - Review `UtteranceMapping.is_unclassified=true` items in one place
   - Add quick assign actions to questions

2. `speaker_label` ↔ `participant` mapping UI  
   - Bulk-map diarized speaker labels to participants
   - Keep transcript text non-destructive

3. Reflect `quote` flags in deliverables  
   - Prioritize quote-flagged segments in verbatim/doc outputs
   - Keep normal outputs unchanged unless explicitly enabled

4. Semantic cluster list page  
   - List `analysis_type=semantic_clusters` by interview
   - Show cluster summaries with source segment traceability

5. Project-wide semantic clustering  
   - Aggregate respondent fragments across interviews
   - Preserve `source_segment_ids` / `source_segment_quotes` mapping

## Not Yet

- Ask feature
- Miro integration
- Google Sheets integration
- Full-scale production data processing
