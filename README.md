# codex-skill-updater

`~/.codex/skills` に入っているユーザースキルを更新する **Codex用スキル** です。  
混在ソース（GitHub / private repo / local archive / source_map）に対応しています。  
`codex-skill-updater/config/skills_source_map.json`（必要に応じて `codex-skill-updater/config/skills_source_map.local.json`）に更新元情報を記述し、更新処理で参照します。

## 前提コマンド

- `python3`: 更新処理スクリプト本体を実行
- `git`: GitHub から skill を取得（public/private 両方）

private repo を更新する場合は、実行環境で GitHub SSH 認証を事前設定してください。  
例: `ssh -T git@github.com`でログイン可能な状態にしておく

## 使い方（通常）

1. インストール
まず、Codex にこのスキルをインストールするよう指示すると、codex組み込みの `skill-installer` スキルを利用してインストールされます。

2. 更新実施
Codexに次のように指示します。
- 「インストール済みスキルを最新化して」
- 「更新可能なスキルが有るか確認して」

※更新前の確認フェーズで、差分がないものは更新不要なのでスキップします。

### 実行時に内部で使う主なコマンド

- `python3 codex-skill-updater/scripts/check_skill_updates.py`: インストール済み skill の更新可否を確認
- `python3 codex-skill-updater/scripts/apply_skill_updates.py`: 判定結果に基づいて安全に更新
- `python3 codex-skill-updater/scripts/update_skills.py`: 上記2段階をまとめて実行（通常はこちら）
- `git clone --sparse ...`: GitHub から対象 skill ディレクトリのみ取得

## 仕様（更新時の挙動）

- バックアップとロールバックあり
- 同一版で更新不要なら更新しない（スキップ）
- 更新が必要かの確認時は並列実行。最終更新は直列実行。

## source_map 運用

- `codex-skill-updater/config/skills_source_map.json`: Git追跡対象（公開してよい情報のみ）
- `codex-skill-updater/config/skills_source_map.local.json`: GitHubのprivateリポジトリなどローカル専用（`.gitignore`）

## 主なファイル

- `codex-skill-updater/SKILL.md`: Codexが読むスキル定義
- `codex-skill-updater/scripts/update_skills.py`: 入口（check + apply）
- `codex-skill-updater/scripts/check_skill_updates.py`: 事前チェック
- `codex-skill-updater/scripts/apply_skill_updates.py`: 実更新
- `codex-skill-updater/config/skills_source_map.json`: 公開マップ
- `codex-skill-updater/config/skills_source_map.local.example.json`: ローカルマップ例
