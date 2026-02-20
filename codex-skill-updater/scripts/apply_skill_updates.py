#!/usr/bin/env python3
"""Apply skill updates from check_skill_updates.tsv with safe rollback."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
SKILLS_ROOT = CODEX_HOME / "skills"
DIST_ROOT = SKILLS_ROOT / "dist"
INSTALLER_SCRIPT = SKILLS_ROOT / ".system" / "skill-installer" / "scripts" / "install-skill-from-github.py"
DEFAULT_JOBS = 4
MAX_JOBS = 8


def _default_source_map_paths() -> tuple[Path, Path]:
    script_dir = Path(__file__).resolve().parent
    config_dir = script_dir.parent / "config"
    if config_dir.is_dir():
        return config_dir / "skills_source_map.json", config_dir / "skills_source_map.local.json"
    return Path("skills_source_map.json"), Path("skills_source_map.local.json")


@dataclass
class UpdateRow:
    skill: str
    bucket: str
    result: str
    strategy: str
    repo: str
    remote_path: str
    note: str


@dataclass
class UpdateResult:
    skill: str
    strategy: str
    status: str
    reason: str
    commands: list[str] = field(default_factory=list)
    backup_path: str | None = None
    rollback: str | None = None


@dataclass
class StageTaskResult:
    index: int
    row: UpdateRow
    commands: list[str]
    result: UpdateResult | None = None
    temp_root: Path | None = None
    staged: Path | None = None


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _load_rows(path: Path) -> list[UpdateRow]:
    rows: list[UpdateRow] = []
    with path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp, delimiter="\t")
        for row in reader:
            def norm(key: str) -> str:
                value = row.get(key, "")
                if value is None:
                    return ""
                return str(value).strip()

            skill = norm("skill")
            if not skill or skill.startswith("summary:"):
                continue
            rows.append(
                UpdateRow(
                    skill=skill,
                    bucket=norm("bucket"),
                    result=norm("result"),
                    strategy=norm("strategy"),
                    repo=norm("repo"),
                    remote_path=norm("remote_path"),
                    note=norm("note"),
                )
            )
    return rows


def _load_source_map(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"source map must be a JSON object: {path}")
    out: dict[str, dict[str, str]] = {}
    for skill, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        repo = str(cfg.get("repo", "")).strip()
        spath = str(cfg.get("path", "")).strip()
        ref = str(cfg.get("ref", "main")).strip() or "main"
        if repo and spath:
            out[str(skill)] = {"repo": repo, "path": spath, "ref": ref}
    return out


def _load_merged_source_map(public_path: Path, local_path: Path) -> dict[str, dict[str, str]]:
    merged = _load_source_map(public_path)
    merged.update(_load_source_map(local_path))
    return merged


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _fingerprint_tree(root: Path) -> str:
    if not root.is_dir():
        raise RuntimeError(f"not a directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L")
            digest.update(rel)
            digest.update(b"\0")
            digest.update(os.readlink(path).encode("utf-8", errors="replace"))
            digest.update(b"\0")
            continue
        if path.is_dir():
            digest.update(b"D")
            digest.update(rel)
            digest.update(b"\0")
            continue
        if path.is_file():
            digest.update(b"F")
            digest.update(rel)
            digest.update(b"\0")
            with path.open("rb") as fp:
                for chunk in iter(lambda: fp.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def _read_archive_path_from_note(note: str) -> Path | None:
    match = re.search(r"(/[^\s]+\.skill)", note)
    if match:
        return Path(match.group(1))
    return None


def _stage_from_installer(skill: str, repo: str, skill_path: str, ref: str, commands: list[str]) -> tuple[Path, Path]:
    tmp_root = Path(tempfile.mkdtemp(prefix=f"skill-stage-{skill}-"))
    cmd = [
        "python3",
        str(INSTALLER_SCRIPT),
        "--repo",
        repo,
        "--path",
        skill_path,
        "--ref",
        ref,
        "--name",
        skill,
        "--dest",
        str(tmp_root),
    ]
    commands.append(" ".join(cmd))
    proc = _run(cmd)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise RuntimeError(msg or "install-skill-from-github failed")
    staged = tmp_root / skill
    if not (staged / "SKILL.md").is_file():
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise RuntimeError("staged skill is invalid (missing SKILL.md)")
    return tmp_root, staged


def _stage_from_archive(skill: str, note: str) -> tuple[Path, Path]:
    archive = _read_archive_path_from_note(note) or (DIST_ROOT / f"{skill}.skill")
    if not archive.is_file():
        raise RuntimeError(f"archive not found: {archive}")
    tmp_root = Path(tempfile.mkdtemp(prefix=f"skill-stage-{skill}-"))
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(tmp_root)
    except Exception as exc:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise RuntimeError(f"failed to extract archive: {archive} ({exc})") from exc

    staged = tmp_root / skill
    if not (staged / "SKILL.md").is_file():
        # fallback: locate unique folder containing SKILL.md
        candidates = [p.parent for p in tmp_root.rglob("SKILL.md")]
        if len(candidates) == 1:
            staged = candidates[0]
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)
            raise RuntimeError("archive layout is ambiguous (SKILL.md not uniquely resolvable)")
    return tmp_root, staged


def _target_root(bucket: str) -> Path:
    return SKILLS_ROOT / ".system" if bucket == "system" else SKILLS_ROOT


def _create_backup(skill: str, bucket: str, backup_root: Path, no_backup: bool) -> tuple[Path | None, bool]:
    dest = _target_root(bucket) / skill
    if no_backup or not dest.exists():
        return None, dest.exists()
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_rel = f"{bucket}__{skill}"
    backup_path = backup_root / backup_rel
    _copy_tree(dest, backup_path)
    return backup_path, True


def _apply_staged(skill: str, bucket: str, staged: Path) -> None:
    dest = _target_root(bucket) / skill
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    shutil.copytree(staged, dest)
    if not (dest / "SKILL.md").is_file():
        raise RuntimeError("post-update validation failed (missing SKILL.md)")


def _restore_from_backup(skill: str, bucket: str, backup_path: Path | None, had_dest: bool) -> str:
    dest = _target_root(bucket) / skill
    try:
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            else:
                shutil.rmtree(dest)
        if backup_path and backup_path.exists():
            shutil.copytree(backup_path, dest)
            return "restored_from_backup"
        if had_dest:
            return "failed_no_backup"
        return "not_needed"
    except Exception as exc:
        return f"rollback_error: {exc}"


def _filter_rows(rows: list[UpdateRow], strategies: set[str], skills: set[str]) -> list[UpdateRow]:
    out = []
    for row in rows:
        if strategies and row.strategy not in strategies:
            continue
        if skills and row.skill not in skills:
            continue
        out.append(row)
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply Codex skill updates from TSV.")
    parser.add_argument("--check-file", default="-")
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--allow-manual-map", action="store_true")
    parser.add_argument("--source-map", default="")
    parser.add_argument("--source-map-local", default="")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--report", default="")
    parser.add_argument("--debug-artifacts", action="store_true")
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Parallel stage workers ({1}-{MAX_JOBS}, default: {DEFAULT_JOBS})",
    )
    return parser.parse_args(argv)


def _normalize_jobs(raw_jobs: int, fail_fast: bool) -> int:
    if fail_fast:
        return 1
    return max(1, min(MAX_JOBS, raw_jobs))


def _stage_one(
    index: int,
    row: UpdateRow,
    allow_manual_map: bool,
    source_map: dict[str, dict[str, str]],
    dry_run: bool,
) -> StageTaskResult:
    commands: list[str] = []
    temp_root: Path | None = None
    staged: Path | None = None
    try:
        if row.strategy == "update-via-github":
            if row.repo in {"", "-"} or row.remote_path in {"", "-"}:
                raise RuntimeError("missing repo/remote_path in check file")
            temp_root, staged = _stage_from_installer(
                skill=row.skill,
                repo=row.repo,
                skill_path=row.remote_path,
                ref="main",
                commands=commands,
            )
        elif row.strategy == "sync-from-claude-mirror":
            return StageTaskResult(
                index=index,
                row=row,
                commands=commands,
                result=UpdateResult(
                    skill=row.skill,
                    strategy=row.strategy,
                    status="SKIPPED",
                    reason="claude_mirror_disabled_per_policy",
                ),
            )
        elif row.strategy == "install-from-local-archive":
            temp_root, staged = _stage_from_archive(row.skill, row.note)
        elif row.strategy in {"manual-source-map-required", "manual-system-source-map"}:
            if not allow_manual_map:
                return StageTaskResult(
                    index=index,
                    row=row,
                    commands=commands,
                    result=UpdateResult(
                        skill=row.skill,
                        strategy=row.strategy,
                        status="SKIPPED",
                        reason="manual_source_map_not_enabled",
                    ),
                )
            cfg = source_map.get(row.skill)
            if not cfg:
                return StageTaskResult(
                    index=index,
                    row=row,
                    commands=commands,
                    result=UpdateResult(
                        skill=row.skill,
                        strategy=row.strategy,
                        status="SKIPPED",
                        reason="skill_not_found_in_source_map",
                    ),
                )
            temp_root, staged = _stage_from_installer(
                skill=row.skill,
                repo=cfg["repo"],
                skill_path=cfg["path"],
                ref=cfg.get("ref", "main"),
                commands=commands,
            )
        else:
            return StageTaskResult(
                index=index,
                row=row,
                commands=commands,
                result=UpdateResult(
                    skill=row.skill,
                    strategy=row.strategy,
                    status="SKIPPED",
                    reason="unsupported_strategy",
                ),
            )

        dest = _target_root(row.bucket) / row.skill
        if dest.is_dir() and _fingerprint_tree(staged) == _fingerprint_tree(dest):
            if temp_root and temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
            return StageTaskResult(
                index=index,
                row=row,
                commands=commands,
                result=UpdateResult(
                    skill=row.skill,
                    strategy=row.strategy,
                    status="SKIPPED",
                    reason="no_changes_detected",
                    commands=commands,
                ),
            )

        if dry_run:
            if temp_root and temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
            return StageTaskResult(
                index=index,
                row=row,
                commands=commands,
                result=UpdateResult(
                    skill=row.skill,
                    strategy=row.strategy,
                    status="DRY_RUN",
                    reason="staged_and_validated",
                    commands=commands,
                ),
            )

        return StageTaskResult(
            index=index,
            row=row,
            commands=commands,
            temp_root=temp_root,
            staged=staged,
        )
    except Exception as exc:
        if temp_root and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
        return StageTaskResult(
            index=index,
            row=row,
            commands=commands,
            result=UpdateResult(
                skill=row.skill,
                strategy=row.strategy,
                status="FAILED",
                reason=str(exc),
                commands=commands,
                rollback="not_needed",
            ),
        )


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.debug_artifacts and not args.report:
        args.report = "skill_update_apply_report.debug.json"
    check_file: Path | None = None
    if args.check_file != "-":
        check_file = Path(args.check_file).resolve()
        if not check_file.is_file():
            print(f"Error: check file not found: {check_file}", file=sys.stderr)
            return 2
    if not INSTALLER_SCRIPT.is_file():
        print(f"Error: installer script not found: {INSTALLER_SCRIPT}", file=sys.stderr)
        return 2

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = Path(args.backup_root).resolve() if args.backup_root else (Path.cwd() / "backups" / ts)
    report_path = Path(args.report).resolve() if args.report else None
    strategies = {s.strip() for s in args.strategy if s.strip()}
    skills = {s.strip() for s in args.skill if s.strip()}

    source_map = {}
    source_map_path: Path | None = None
    source_map_local_path: Path | None = None
    if args.allow_manual_map:
        default_public_map, default_local_map = _default_source_map_paths()
        source_map_path = Path(args.source_map).resolve() if args.source_map else default_public_map.resolve()
        source_map_local_path = Path(args.source_map_local).resolve() if args.source_map_local else default_local_map.resolve()
        source_map = _load_merged_source_map(source_map_path, source_map_local_path)

    if args.check_file == "-":
        fd, tmp_name = tempfile.mkstemp(prefix="skill-check-", suffix=".tsv")
        os.close(fd)
        tmp_check = Path(tmp_name)
        try:
            tmp_check.write_text(sys.stdin.read(), encoding="utf-8")
            rows = _load_rows(tmp_check)
        finally:
            tmp_check.unlink(missing_ok=True)
    else:
        rows = _load_rows(check_file)
    selected = _filter_rows(rows, strategies, skills)
    jobs = _normalize_jobs(args.jobs, args.fail_fast)
    ordered_results: dict[int, UpdateResult] = {}
    staged_for_apply: list[StageTaskResult] = []

    stage_inputs: list[tuple[int, UpdateRow]] = []
    for idx, row in enumerate(selected):
        if row.result == "FAIL":
            ordered_results[idx] = UpdateResult(
                skill=row.skill,
                strategy=row.strategy,
                status="SKIPPED",
                reason="precheck_result_is_fail",
            )
            continue
        if row.bucket == "system":
            ordered_results[idx] = UpdateResult(
                skill=row.skill,
                strategy=row.strategy,
                status="SKIPPED",
                reason="system_updates_disabled_per_policy",
            )
            continue
        stage_inputs.append((idx, row))

    if jobs == 1:
        stage_outputs = []
        for idx, row in stage_inputs:
            out = _stage_one(idx, row, args.allow_manual_map, source_map, args.dry_run)
            stage_outputs.append(out)
            if args.fail_fast and out.result is not None and out.result.status in {"FAILED", "FAILED_ROLLBACK"}:
                break
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            stage_outputs = list(
                executor.map(
                    lambda x: _stage_one(x[0], x[1], args.allow_manual_map, source_map, args.dry_run),
                    stage_inputs,
                )
            )

    for out in stage_outputs:
        if out.result is not None:
            ordered_results[out.index] = out.result
            if args.fail_fast and out.result.status in {"FAILED", "FAILED_ROLLBACK"}:
                break
            continue
        staged_for_apply.append(out)

    for out in sorted(staged_for_apply, key=lambda r: r.index):
        backup_path: Path | None = None
        had_dest = False
        try:
            backup_path, had_dest = _create_backup(out.row.skill, out.row.bucket, backup_root, args.no_backup)
            _apply_staged(out.row.skill, out.row.bucket, out.staged)
            ordered_results[out.index] = UpdateResult(
                skill=out.row.skill,
                strategy=out.row.strategy,
                status="SUCCESS",
                reason="updated",
                commands=out.commands,
                backup_path=str(backup_path) if backup_path else None,
            )
        except Exception as exc:
            rollback = _restore_from_backup(out.row.skill, out.row.bucket, backup_path, had_dest)
            status = "FAILED" if rollback in {"restored_from_backup", "not_needed", "failed_no_backup"} else "FAILED_ROLLBACK"
            ordered_results[out.index] = UpdateResult(
                skill=out.row.skill,
                strategy=out.row.strategy,
                status=status,
                reason=str(exc),
                commands=out.commands,
                backup_path=str(backup_path) if backup_path else None,
                rollback=rollback,
            )
            if args.fail_fast:
                if out.temp_root and out.temp_root.exists():
                    shutil.rmtree(out.temp_root, ignore_errors=True)
                break
        finally:
            if out.temp_root and out.temp_root.exists():
                shutil.rmtree(out.temp_root, ignore_errors=True)

    results = [ordered_results[i] for i in sorted(ordered_results)]

    summary = {
        "total_rows": len(rows),
        "selected_rows": len(selected),
        "success": sum(1 for r in results if r.status == "SUCCESS"),
        "failed": sum(1 for r in results if r.status in {"FAILED", "FAILED_ROLLBACK"}),
        "skipped": sum(1 for r in results if r.status == "SKIPPED"),
        "dry_run": sum(1 for r in results if r.status == "DRY_RUN"),
    }
    report: dict[str, Any] = {
        "generated_at": dt.datetime.now().isoformat(),
        "dry_run": args.dry_run,
        "check_file": str(check_file) if check_file else "-",
        "backup_root": str(backup_root),
        "source_map_used": args.allow_manual_map,
        "source_map_path": str(source_map_path) if source_map_path else None,
        "source_map_local_path": str(source_map_local_path) if source_map_local_path else None,
        "summary": summary,
        "results": [r.__dict__ for r in results],
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False))
    if report_path:
        print(f"report: {report_path}")
    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
