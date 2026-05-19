# Qualia Transcript Requirements v0.2

## 1. 目的

Qualia Transcript を、定性調査における実務運用可能なローカル分析基盤として安定化する。主目的は、文字起こしから納品物生成までを一貫管理しつつ、根拠トレース可能性と非破壊性を担保すること。

## 2. MVP対象範囲

- 音声ファイルを対象とした転記・セグメント管理
- 質問フロー連動マッピング
- 話者対応（speaker_label ↔ participant）
- Segment flag / QuoteCandidate 管理
- per_question analysis（根拠ID付き）
- semantic clustering（補助分析）
- Review Queue による例外レビュー
- Word/Excel 出力（approved 制御あり）

## 3. MVP非対象範囲

- 動画処理の正式対応
- 全量バッチ運用の自動化
- 外部連携（Miro/Sheets など）の本格実装
- 非承認分析の正式出力反映

## 4. 基本設計原則

- 大規模リファクタを避け、小差分で進める。
- 既存機能を壊さない。
- 主evidenceは `question / flow / UtteranceMapping / PerQuestionAnalysis / QuoteCandidate / Segment` とする。
- `semantic_clusters` は探索的・補助的evidenceとして扱う。

## 5. データ非破壊方針

- `Segment.text` は変更しない。
- `raw_transcripts` は変更しない。
- AI補正・辞書補正・引用抽出・分析処理・出力処理で原文を書き換えない。
- 補助情報は派生データとして別フィールド/別モデルに保持する。

## 6. Evidence設計

- 主evidenceは質問単位とマッピング単位で構成する。
- AI出力は `quote_id` / `source_segment_ids` ベースで根拠指定する。
- AIが引用本文を新規生成する方式は禁止する。
- 引用本文はローカルDBから再解決する。

## 7. QuoteCandidate

- `SegmentFlag.quote` は簡易候補。
- `QuoteCandidate` は正式引用候補として管理する。
- `QuoteCandidate.quote_text` は `Segment.text` または `reviewed_text` 由来に限定する。
- `approved` でない QuoteCandidate は正式引用として出力に反映しない。

## 8. Review Queue

- 全件レビューを前提にしない。
- 低信頼度・未分類・納品物影響箇所のみをレビュー対象化する。
- 例: unclassified mapping, low/medium confidence, unresolved speaker, quote candidate, needs_review segment, AI draft
- 目的はユーザー確認負荷の削減。

## 9. AIAnalysis status

- `AIAnalysis.status`: `draft / reviewed / approved / rejected`
- `draft` を初期値とする。
- `approved` 以外は正式Word/Excelに反映しない。

## 10. APIUsageLog

有料/外部APIの実行記録を保持する。最低限以下を記録対象とする。

- provider
- model
- operation_type
- request_count
- token usage
- audio_duration_sec
- estimated_cost

## 11. chunk overlap

- 長尺音声のchunk処理は overlap 3-5秒を標準とする。
- 境界欠落を防止しつつ、重複候補を識別可能にする。
- Segment側追加候補は `duplicate_candidate` 程度に留める。

## 12. PerQuestionAnalysis

- `PerQuestionAnalysis`（または同等構造）は以下を必須化する。
  - `source_segment_ids`
  - `source_segment_quotes`
  - `quote_ids`
- `source_segment_ids` なしの `draft` 保存は後方互換のため許容する。
- `source_segment_ids` / `source_segment_quotes` なしの `per_question` は `approved` にできない。
- 正式出力は trace 付きの `approved` AIAnalysis のみ対象にする。
- 引用本文は必ずローカル再解決する。

## 13. Semantic Clusterの位置づけ

- `semantic_clusters` は主evidenceにしない。
- 探索的テーマ把握・補助示唆に限定する。
- 統合分析では主に question/flow/mapping/per-question 根拠を優先する。

## 14. ProductHint方針

- ProductHint外部APIはデフォルトOFF。
- 外部検索を使う場合も transcript本文は非破壊。
- 辞書補助は表示・検索支援に限定する。

## 15. 出力方針

- 正式出力（Word/Excel）は承認済み根拠のみ反映する。
- `AIAnalysis.status=approved` のみ正式分析として反映。
- `QuoteCandidate.status=approved` のみ正式引用として反映。
- `SegmentFlag.quote` は補助マーカーとして扱う。

## 16. Check方針

- `check_all` デフォルトは safe のみ。
- `-AllLocal` は外部APIなしチェックのみ。
- 有料APIチェックは明示実行時のみ。
- 変更時は実行したチェック / 未実行理由を明示する。

## 17. 実装優先順位

1. ドキュメント整備（AGENTS/requirements）
2. モデル加算（QuoteCandidate, ReviewItem, APIUsageLog, AIAnalysis status）
3. ローカルチェック整備
4. Evidenceモデル強化（per_question trace必須化）
5. Review Queue UI
6. Integrated analysis AI dry-run
7. approval反映の出力制御

## 18. 受け入れ基準

- `Segment.text` が非破壊であること。
- `raw_transcripts` が非破壊であること。
- semantic_clusters を主evidenceとして扱っていないこと。
- 主evidenceが question/flow/mapping/per-question/quote/segment で構成されること。
- AIが引用本文を生成していないこと。
- quoteはローカル再解決されること。
- approvedでないAIAnalysis/QuoteCandidateが正式出力に反映されないこと。
- Review Queueでレビュー対象が絞られていること。
- 有料API実行が明示実行に限定され、APIUsageLogで追跡できること。
