# Qualia Transcript Local Launcher (Windows)

PowerShell から毎回 `python app.py` を手動実行しなくても、スクリプトで起動・停止できます。

## 事前準備

1. 依存関係をインストールする  
`pip install -r requirements.txt`
2. `.env` に API キーを設定する  
`OPENAI_API_KEY=...`
3. `.env` は Git 管理しない（このリポジトリでは `.gitignore` に設定済み）

## 起動方法

`start_app.ps1` をダブルクリック、または PowerShell で実行します。

```powershell
.\start_app.ps1
```

動作:
- プロジェクトディレクトリに移動
- ポート `5000` の使用状況を確認
- ポート使用中の場合は `http://127.0.0.1:5000/` の HTTP 200 応答を確認
- HTTP 200 なら「既に起動中」として二重起動せずブラウザだけ開く
- HTTP 200 でなければ起動せず、ログを表示して終了
- 未使用なら Flask をバックグラウンド起動
- `logs/flask_out.log` / `logs/flask_err.log` にログ保存
- 最大30秒、1秒ごとに HTTP 200 を確認
- HTTP 200 到達後、`http://127.0.0.1:5000/` を既定ブラウザで開く

## 既に起動中のアプリを開く

```powershell
.\open_app.ps1
```

## 停止方法

```powershell
.\stop_app.ps1
```

動作:
- ポート `5000` を使用しているプロセスを確認
- Qualia Transcript の Flask プロセスと判定できる場合のみ停止
- 無関係なプロセスは停止しない

## トラブルシュート

- アプリが開かない  
`logs/flask_err.log` を確認してください。
- 起動はしたが画面が表示されない  
`http://127.0.0.1:5000/` に手動アクセスして確認してください。
- ポート `5000` が埋まっている  
`start_app.ps1` が使用中プロセスを表示します。HTTP 200 でなければ `logs/flask_err.log` を確認し、必要なら競合プロセスを停止して再実行してください。
- PowerShell の実行ポリシーで拒否される  
管理者権限 PowerShell で `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を設定後に再実行してください。

## Safe Smoke Check（非破壊）

外部API呼び出し・Whisper実行・マッピング/分析実行なしで、最低限の状態確認だけ行うには以下を使います。

```powershell
python tests/smoke_safe.py
powershell -ExecutionPolicy Bypass -File scripts/check_safe.ps1
```

- Safe Smoke Check は無料・非破壊（外部API呼び出しなし）です。

## Segment Flag Smoke Check（外部APIなし・可逆DB更新あり）

- Segment flag の作成/重複防止/削除/復元を確認します。
- 外部APIは呼びません。
- 実行中に `segment_flags` を一時更新しますが、テスト終了時に元状態へ復元します。
- 完全read-onlyではないため、Safe Smoke Check（デフォルト）には含めません。

```powershell
python tests/smoke_flags.py
powershell -ExecutionPolicy Bypass -File scripts/check_flags.ps1
```

## Speaker Assignment Smoke Check（外部APIなし・可逆DB更新あり）

- `speaker_assignments` の作成/重複upsert防止/復元を確認します。
- 外部APIは呼びません。
- 実行中に `speaker_assignments` を一時更新しますが、テスト終了時に元状態へ復元します。

```powershell
python tests/smoke_speaker_assignments.py
powershell -ExecutionPolicy Bypass -File scripts/check_speaker_assignments.ps1
```

## Analysis Smoke Check（有料API）

- Analysis Smoke Check は OpenAI API を1回呼びます。必要時のみ実行してください。
- Safe Smoke Check には含めません。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
```

## Mapping Smoke Check（有料API）

- Mapping Smoke Check は OpenAI API を1回呼びます。必要時のみ実行してください。
- Safe Smoke Check には含めません。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_mapping.ps1
```

## Transcription Smoke Check（有料API）

- Transcription Smoke Check は OpenAI transcription API を1回呼びます。必要時のみ実行してください。
- Safe Smoke Check には含めません。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_transcription.ps1
```

## raw_transcripts 運用ルール

- `outputs/raw_transcripts/*.json` は OpenAI transcription API 返却原文の不変スナップショットです。
- 逐語本文の検証用データであり、通常画面で扱う表示本文とは別扱いです。
- 個人情報・機密発言を含む可能性があるため、機微情報として扱ってください。
- Git管理しないでください（本リポジトリでは `outputs/` 全体を除外しています）。
- 外部共有しないでください。
- 不要になった場合は手動削除してください。
- 削除前に、DB参照や既存出力との関係を確認してください。
- 現時点では自動cleanupは行いません。

## 商品候補補足（楽天API）

- 逐語本文は変更せず、`/interviews/<id>` 画面でセグメントごとに「候補を照合」を押すと補足候補を表示します。
- 表示例: `（この商品と思われる: ...）`
- 商品候補補足を使うと、発話から抽出された商品候補語（キーワード）が楽天APIへ送信されます。
- 発言本文そのものは上書きしません（逐語本文は別扱いのまま保持します）。
- 補足は「この商品と思われる」という推定情報であり、逐語本文と同一扱いにはしません。
- `rakuten_application_id` / `rakuten_access_key` は秘匿情報として扱ってください。
- `rakuten_affiliate_id` は比較的秘匿度が低いIDですが、運用上は公開前提でない限り設定画面・ログで不用意に共有しない方針を推奨します。
- 設定画面で以下を設定してください。
  - `product_hint_provider`: `rakuten` または `none`
  - `rakuten_application_id`
  - `rakuten_access_key`（任意）
  - `rakuten_affiliate_id`（任意）

## Check Runner（1コマンド実行）

- デフォルトは safe check のみ実行します。
- 有料APIチェックはオプション指定時のみ実行します。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Mapping
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Analysis
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Transcription
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -Outputs
powershell -ExecutionPolicy Bypass -File scripts/check_all.ps1 -AllPaid
```

## Output Smoke Check（外部APIなし）

- Output Smoke Check は OpenAI API を呼びません。
- Word/Excel 生成の最低限動作を確認します。
- Safe Smoke Check には含めません。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_outputs.ps1
```

## Output Flag Smoke Check（外部APIなし・可逆DB更新あり）

- Segment flag（`favorite / quote / exclude / needs_review`）がWord/Excel出力へ反映されるか確認します。
- 外部APIは呼びません。
- 実行中に `segment_id=257` の flag を一時更新しますが、テスト終了時に実行前状態へ復元します。
- `Segment.text` は変更しません。

```powershell
python tests/smoke_outputs_flags.py
powershell -ExecutionPolicy Bypass -File scripts/check_outputs_flags.ps1
```

## Semantic Cluster Analysis CLI

- Flask UIに組み込む前の単体実行CLIです。
- respondent発話を対象に、embedding + クラスタリングで自然発生テーマを抽出します。
- `--no-ai` を付けると、OpenAI要約を呼ばずクラスタ結果のみ確認できます。
- 切片化（fragmentation）は `Segment.text` を変更しない派生処理です。
- `--dry-run` はDB保存しません。`--save` 指定時のみ `AIAnalysis.analysis_type="semantic_clusters"` を保存します。

```powershell
python scripts/run_semantic_analysis.py --interview-id 10 --dry-run --max-segments 50 --no-ai
python scripts/run_semantic_analysis.py --interview-id 10 --save
```

- 逐語本文（`Segment.text`）と `outputs/raw_transcripts` は変更しません。

## ドメイン辞書プロファイル（非破壊補足）

- プロジェクトの調査テーマ/カテゴリ/辞書プロファイルに応じて、商品名候補の表記ゆれ補足を行います。
- 辞書補足は本文置換ではなく、補足・正規化・検索支援に限定します。
- 外部API検索時は `normalized_term` / `search_keyword` を優先する場合があります。
- `Segment.text` と raw transcript は不変です。
