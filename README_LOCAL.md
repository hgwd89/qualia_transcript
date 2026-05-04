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

## Analysis Smoke Check（有料API）

- Analysis Smoke Check は OpenAI API を1回呼びます。必要時のみ実行してください。
- Safe Smoke Check には含めません。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check_analysis.ps1
```
