# codex-skill-updater

`~/.codex/skills` に入っているユーザースキルを更新する **Codex用スキル** です。  
混在ソース（GitHub / private repo / local archive / source_map）に対応しています。  
`codex-skill-updater/config/skills_source_map.json`（必要に応じて `codex-skill-updater/config/skills_source_map.local.json`）に更新元情報を記述し、更新処理で参照します。

## 前提コマンド

- `python3`: 更新処理スクリプト本体を実行
- `git`: GitHub から skill を取得（public/private 両方）

private repo を更新する場合は、実行環境で GitHub SSH 認証を事前設定してください。  
例: `ssh -T git@github.com` でログイン可能な状態にしておく

## クイックスタート

1. スキルをインストール
- Codex に「このリポのスキルをインストールして」と指示します。

2. スキルディレクトリへ移動
- `cd ~/.codex/skills/codex-skill-updater`

3. 初回のみ local source map を作成
- `cp config/skills_source_map.local.example.json config/skills_source_map.local.json`

4. ドライラン
- `python3 scripts/update_skills.py --dry-run --allow-manual-map --source-map ./config/skills_source_map.json --source-map-local ./config/skills_source_map.local.json --jobs 4`

5. 問題なければ本適用
- `python3 scripts/update_skills.py --allow-manual-map --source-map ./config/skills_source_map.json --source-map-local ./config/skills_source_map.local.json --jobs 4`

6. 必要時のみデバッグ出力
- `python3 scripts/update_skills.py --dry-run --allow-manual-map --source-map ./config/skills_source_map.json --source-map-local ./config/skills_source_map.local.json --debug-artifacts`
- 出力: `skill_update_check.debug.ndjson`, `skill_update_apply_report.debug.json`

## 実行時に内部で使う主なコマンド

- `python3 codex-skill-updater/scripts/check_skill_updates.py`: インストール済み skill の更新可否を確認
- `python3 codex-skill-updater/scripts/apply_skill_updates.py`: 判定結果に基づいて安全に更新
- `python3 codex-skill-updater/scripts/update_skills.py`: 上記2段階をまとめて実行（通常はこちら）
- `git clone --sparse ...`: GitHub から対象 skill ディレクトリのみ取得

## 仕様（更新時の挙動）

- `check_skill_updates.py` の strategy は `update-via-github` / `install-from-local-archive` / `manual-source-map-required`
- バックアップとロールバックあり（保存先は常に `$CODEX_HOME/backups/<timestamp>`）
- バックアップは実行単位で最新2世代を保持し、更新処理が失敗なしで完了した場合のみ古い世代を削除
- 同一版で更新不要なら更新しない（スキップ）
- 更新が必要かの確認時は並列実行。最終更新は直列実行。
- `--backup-root` での保存先指定はサポートしない（固定先のみ）

## source_map 運用

- `codex-skill-updater/config/skills_source_map.json`: Git追跡対象（公開してよい情報のみ）
- `codex-skill-updater/config/skills_source_map.local.json`: GitHubのprivateリポジトリなどローカル専用（`.gitignore`）
- 同じ skill キーが両方にある場合、`skills_source_map.local.json` が優先されます。
- `skills_source_map.local.json` の内容が `skills_source_map.json` を上書きするため、ダミー値を入れると更新失敗要因になります。

## 主なファイル

- `codex-skill-updater/SKILL.md`: Codexが読むスキル定義
- `codex-skill-updater/scripts/update_skills.py`: 入口（check + apply）
- `codex-skill-updater/scripts/check_skill_updates.py`: 事前チェック
- `codex-skill-updater/scripts/apply_skill_updates.py`: 実更新
- `codex-skill-updater/config/skills_source_map.json`: 公開マップ
- `codex-skill-updater/config/skills_source_map.local.example.json`: ローカルマップ例
