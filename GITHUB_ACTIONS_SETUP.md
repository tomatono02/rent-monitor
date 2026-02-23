# GitHub Actions セットアップ手順（最新版）

最終更新: 2026-02-23

## 1. 目的
- `monitor.py` を GitHub Actions で定期実行
- `seen_ids.json` を自動更新して新着判定を継続
- Slack に新着通知

## 2. 事前準備
1. GitHub でリポジトリ作成
2. ローカルで push
3. 次のファイルがあることを確認
   - `monitor.py`
   - `requirements.txt`
   - `.github/workflows/rent-monitor.yml`

## 3. Secrets 設定
GitHubリポジトリで以下へ移動:
- `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

作成するSecret:
- `SEARCH_URL`（単一URL運用）
- `SEARCH_URLS`（複数URL運用。カンマ区切り or 改行区切り）
- `SLACK_WEBHOOK_URL`
- `SLACK_NOTIFY_ON_NO_NEW`（`true` / `false`）

補足:
- `SEARCH_URL` と `SEARCH_URLS` はどちらか一方で可
- 迷ったら `SEARCH_URL` だけ設定

## 4. Actions有効化と初回実行
1. `Actions` タブを開く
2. 必要なら `Enable` を押して有効化
3. 左で `Rent Monitor` を選択
4. `Run workflow` -> `Run workflow`
5. 実行ログを確認

## 5. 定期実行設定
- ワークフローの `schedule` は `*/30 * * * *`
- 30分ごとに実行
- GitHub側で数分遅延する場合あり

## 6. 新着判定の仕組み
- `seen_ids.json` を実行後に更新
- 変更があれば workflow が自動で commit/push
- 次回は更新済み `seen_ids.json` と比較

## 7. 通知が来ない時の確認
1. Actionsログで `新着件数` を確認
2. `SLACK_NOTIFY_ON_NO_NEW=true` を設定して動作確認
3. `SLACK_WEBHOOK_URL` を再確認

## 8. セキュリティ
- `.env` と Webhook URL をコミットしない
- URL漏えい時は Slack でWebhookを再発行して差し替え
