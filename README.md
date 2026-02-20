# skill-updater

`~/.codex/skills` に入っているユーザースキルを、安全に更新するためのツールです。  
混在ソース（GitHub / private repo / local archive / source_map）に対応しています。

## できること

- 事前チェック（更新経路の解決）
- 戦略別の更新実行（GitHub, local archive, source_map）
- バックアップとロールバック
- 同一内容なら自動スキップ（`no_changes_detected`）
- 並列ステージングによる高速化（`--jobs`）
- 通常時は中間ファイルを残さない運用

## 前提

- `python3`
- `gh`（GitHub CLI、private repo を使う場合はログイン済み推奨）
- `~/.codex/skills/.system/skill-installer` が存在すること

## source_map の扱い（公開/ローカル）

- `skills_source_map.json`: 追跡対象。公開して問題ないマップのみ入れる
- `skills_source_map.local.json`: ローカル専用（`.gitignore` 済み）
- private repo の情報は必ず `*.local.json` 側に入れる

初回作成:

```bash
cp skills_source_map.local.example.json skills_source_map.local.json
```

## まず使うコマンド

リポジトリ配下で実行します。

```bash
# dry-run（通常モード: 中間ファイルを作らない）
python3 skill-updater/scripts/update_skills.py \
  --dry-run \
  --allow-manual-map \
  --source-map ./skills_source_map.json \
  --source-map-local ./skills_source_map.local.json \
  --jobs 4

# 本番更新
python3 skill-updater/scripts/update_skills.py \
  --allow-manual-map \
  --source-map ./skills_source_map.json \
  --source-map-local ./skills_source_map.local.json \
  --jobs 4
```

## デバッグモード

固定ファイル名で結果を保存します。

```bash
python3 skill-updater/scripts/update_skills.py \
  --dry-run \
  --allow-manual-map \
  --source-map ./skills_source_map.json \
  --source-map-local ./skills_source_map.local.json \
  --jobs 4 \
  --debug-artifacts
```

生成されるファイル:

- `skill_update_check.debug.tsv`
- `skill_update_apply_report.debug.json`

## 出力の意味（重要）

`check` の `OK/SKIP/FAIL` は「最新版かどうか」ではありません。  
「どの更新経路で扱えるか」の判定です。

- `OK`: 自動更新ルート（例: `update-via-github`）を解決できた
- `SKIP`: `manual-source-map-required` など、追加情報が必要
- `FAIL`: 経路候補はあるが事前プローブに失敗

実際に更新する/しないは `apply` 側の判定です。  
差分がなければ `SKIPPED` / `no_changes_detected` になります。

## source_map（手動マップ）

`manual-source-map-required` のスキルは source_map に登録します。  
private 情報は `skills_source_map.local.json` にのみ登録してください。

```json
{
  "skill-name": {
    "repo": "owner/repo",
    "path": "skills/skill-name",
    "ref": "main"
  }
}
```

## 高速化と安全性

- `--jobs` で並列度を調整（推奨: `3-4`, 上限: `8`）
- 並列化されるのは主に **probe/stage**
- 最終反映（backup/apply/rollback）は直列実行
- `--fail-fast` 指定時は安全側で逐次処理

## 主なファイル

- `skill-updater/scripts/update_skills.py`: 入口（check + apply）
- `skill-updater/scripts/check_skill_updates.py`: 事前チェック
- `skill-updater/scripts/apply_skill_updates.py`: 実更新
- `skills_source_map.json`: 手動マップ
