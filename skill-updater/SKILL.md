---
name: skill-updater
description: Update installed user Codex skills safely with prechecks, strategy-based execution, backups, rollback, and source-map support for private/custom skills.
---

# Skill Updater

Use this skill when you want to keep installed user skills up to date in `~/.codex/skills` and handle mixed sources (GitHub, source-map, local archive, private repos).

## Quick Start

1. Move to this skill directory.
- `cd /home/r-endou/work/skill/skill-updater/skill-updater`

2. Recalculate update candidates and strategies.
- `python3 scripts/check_skill_updates.py > skill_update_check.tsv`

3. Dry-run before real update.
- `python3 scripts/apply_skill_updates.py --dry-run --allow-manual-map --check-file skill_update_check.tsv --report skill_update_apply_report.dryrun.json`

4. Apply updates.
- `python3 scripts/apply_skill_updates.py --allow-manual-map --check-file skill_update_check.tsv --report skill_update_apply_report.latest.json`

5. Recheck after update.
- `python3 scripts/check_skill_updates.py > skill_update_check.after.tsv`

## Strategy Model

`check_skill_updates.py` classifies each skill into one strategy:

- `update-via-github`
  - Update from detected `repo/path` using `install-skill-from-github.py`
- `install-from-local-archive`
  - Install from local `.skill` archive (usually `~/.codex/skills/dist/<name>.skill`)
- `manual-source-map-required`
  - Source cannot be inferred; you must define it in source map

`~/.codex/skills/.system` はこのワークフローの更新対象外です。

## Handling Newly Added Skills

When new skills are added later and show up as `manual-source-map-required`:

1. Add an entry to `config/skills_source_map.json`.
2. Re-run dry-run with manual map enabled.
3. Apply once dry-run is clean.

Use this format:

```json
{
  "skill-name": {
    "repo": "owner/repo",
    "path": "skills/skill-name",
    "ref": "main"
  }
}
```

- `path` may be `.` when the skill is at repo root.
- You can override source map path via `--source-map /path/to/skills_source_map.json`.

## Safety Rules

- Default behavior creates backups under `backups/<timestamp>/`.
- On failure, rollback is attempted automatically.
- If staged content is identical to installed content, update is skipped (`no_changes_detected`).
- Use `--fail-fast` to stop on first failure.
- Use `--strategy` and `--skill` to run partial updates safely.

## Notes

- This workflow updates user skills in `~/.codex/skills` (except `.system`).
- Restart Codex after updates to ensure new skill contents are picked up.
