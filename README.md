# skill-updater

`~/.codex/skills` に入っているユーザースキルを更新する **Codex用スキル** です。  
混在ソース（GitHub / private repo / local archive / source_map）に対応しています。
source_map.jsonに更新ソースの情報を記述し、それを元に更新がある場合のみ更新実施します。

## Codexへインストール

まず、Codex にこのスキルをインストールするよう指示します。
（組み込みの `skill-installer` スキルを利用してインストールされる）

## 使い方（通常）

Codexに次のように指示します。
- 「インストール済みスキルを最新化して」
- 「更新可能なスキルが有るか確認して」

更新前の確認フェーズで、差分がないものは更新不要なのでスキップします。

## 仕様（更新時の挙動）

- バックアップとロールバックあり
- 同一版で更新不要なら更新しない（スキップ）
- 更新が必要かの確認時は並列実行。最終更新は直列実行。

## source_map 運用

- `skills_source_map.json`: Git追跡対象（公開してよい情報のみ）
- `skills_source_map.local.json`: GitHubのprivateリポジトリなどローカル専用（`.gitignore`）

## 主なファイル

- `skill-updater/SKILL.md`: Codexが読むスキル定義
- `skill-updater/scripts/update_skills.py`: 入口（check + apply）
- `skill-updater/scripts/check_skill_updates.py`: 事前チェック
- `skill-updater/scripts/apply_skill_updates.py`: 実更新
- `skills_source_map.json`: 公開マップ
- `skills_source_map.local.example.json`: ローカルマップ例
