#!/usr/bin/env python3
"""Preflight-check whether installed Codex skills can be updated safely."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
SKILLS_ROOT = CODEX_HOME / "skills"
DIST_ROOT = SKILLS_ROOT / "dist"
INSTALLER_DIR = SKILLS_ROOT / ".system" / "skill-installer" / "scripts"
LIST_SCRIPT = INSTALLER_DIR / "list-skills.py"
INSTALL_SCRIPT = INSTALLER_DIR / "install-skill-from-github.py"
DEFAULT_REF = "main"
DEFAULT_JOBS = 4
MAX_JOBS = 8
DEFAULT_FORMAT = "ndjson"


@dataclass
class SkillEntry:
    name: str
    local_path: Path
    source_bucket: str
    remote_repo: str | None
    remote_path: str | None
    check: str
    strategy: str
    note: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_remote_set(repo: str, path: str) -> set[str]:
    cmd = [
        "python3",
        str(LIST_SCRIPT),
        "--repo",
        repo,
        "--ref",
        DEFAULT_REF,
        "--path",
        path,
        "--format",
        "json",
    ]
    proc = _run(cmd)
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"failed to load remote skills: {repo}:{path}")
    data = json.loads(proc.stdout)
    return {row["name"] for row in data}


def _load_meta(path: Path) -> dict:
    meta_path = path / ".skill-meta.json"
    if not meta_path.is_file():
        return {}
    try:
        with meta_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _collect_local_skills() -> list[tuple[str, Path, str, dict]]:
    skills: list[tuple[str, Path, str, dict]] = []

    for path in sorted(SKILLS_ROOT.iterdir()):
        if path.name.startswith("."):
            continue
        skill_md = path / "SKILL.md"
        scan_path = path
        if path.is_symlink():
            resolved = path.resolve()
            skill_md = resolved / "SKILL.md"
            scan_path = resolved
        if skill_md.is_file():
            skills.append((path.name, path, "user", _load_meta(scan_path)))
    return skills


def _probe_install(repo: str, remote_path: str) -> tuple[bool, str]:
    temp_root = Path(tempfile.mkdtemp(prefix="skill-update-probe-"))
    try:
        cmd = [
            "python3",
            str(INSTALL_SCRIPT),
            "--repo",
            repo,
            "--ref",
            DEFAULT_REF,
            "--path",
            remote_path,
            "--dest",
            str(temp_root),
        ]
        proc = _run(cmd)
        if proc.returncode == 0:
            return True, "ok"
        err = (proc.stderr or proc.stdout).strip().splitlines()
        return False, err[-1] if err else "install probe failed"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _resolve_candidates(
    name: str,
    source_bucket: str,
    meta: dict,
    openai_curated: set[str],
    openai_system: set[str],
    anthropics_skills: set[str],
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(repo: str, path: str, reason: str) -> None:
        key = (repo, path)
        if key in seen:
            return
        seen.add(key)
        candidates.append((repo, path, reason))

    source = meta.get("source")
    if source == "github" and meta.get("repo") and meta.get("skillPath"):
        repo = str(meta["repo"])
        skill_path = str(meta["skillPath"]).strip("/")
        add(repo, skill_path, "meta github skillPath")
        if not skill_path.startswith("skills/"):
            add(repo, f"skills/{skill_path}", "meta github + skills/ prefix")
    elif source == "registry":
        # Registry does not expose a direct repo/path in metadata.
        reg_name = str(meta.get("name", name))
        if reg_name in openai_curated:
            add("openai/skills", f"skills/.curated/{reg_name}", "registry matched openai curated")
        if reg_name in openai_system:
            add("openai/skills", f"skills/.system/{reg_name}", "registry matched openai system")
        if reg_name in anthropics_skills:
            add("anthropics/skills", f"skills/{reg_name}", "registry matched anthropics public")
    else:
        # No useful metadata: resolve from known public lists.
        if name in openai_curated:
            add("openai/skills", f"skills/.curated/{name}", "name matched openai curated")
        if name in openai_system or source_bucket == "system":
            add("openai/skills", f"skills/.system/{name}", "name matched openai system")
        if name in anthropics_skills:
            add("anthropics/skills", f"skills/{name}", "name matched anthropics public")

    return candidates


def _strategy_for_skip(
    name: str,
    local_path: Path,
    meta: dict,
) -> tuple[str, str]:
    source = str(meta.get("source", "unknown"))
    dist_skill = DIST_ROOT / f"{name}.skill"

    if local_path.is_symlink():
        return (
            "update-source-repo",
            f"symlink targetを更新して反映 ({local_path.resolve()})",
        )
    if dist_skill.is_file():
        return (
            "install-from-local-archive",
            f"local archiveあり: {dist_skill}",
        )
    return (
        "manual-source-map-required",
        f"repo/path未解決 (meta source={source})。明示マップが必要",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight-check whether installed Codex skills can be updated safely.")
    parser.add_argument(
        "--format",
        choices=["ndjson", "tsv"],
        default=DEFAULT_FORMAT,
        help=f"Output format (default: {DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Parallel probe workers ({1}-{MAX_JOBS}, default: {DEFAULT_JOBS})",
    )
    return parser.parse_args(argv)


def _normalize_jobs(raw_jobs: int) -> int:
    return max(1, min(MAX_JOBS, raw_jobs))


def _evaluate_skill(
    item: tuple[str, Path, str, dict],
    openai_curated: set[str],
    openai_system: set[str],
    anthropics_skills: set[str],
) -> SkillEntry:
    name, local_path, source_bucket, meta = item
    if local_path.is_symlink():
        strategy, strategy_note = _strategy_for_skip(
            name,
            local_path,
            meta,
        )
        return SkillEntry(
            name=name,
            local_path=local_path,
            source_bucket=source_bucket,
            remote_repo=None,
            remote_path=None,
            check="SKIP",
            strategy=strategy,
            note=strategy_note,
        )

    candidates = _resolve_candidates(
        name=name,
        source_bucket=source_bucket,
        meta=meta,
        openai_curated=openai_curated,
        openai_system=openai_system,
        anthropics_skills=anthropics_skills,
    )
    if not candidates:
        strategy, strategy_note = _strategy_for_skip(
            name,
            local_path,
            meta,
        )
        return SkillEntry(
            name=name,
            local_path=local_path,
            source_bucket=source_bucket,
            remote_repo=None,
            remote_path=None,
            check="SKIP",
            strategy=strategy,
            note=strategy_note,
        )

    ok = False
    repo = None
    remote_path = None
    note = "install probe failed"
    for candidate_repo, candidate_path, reason in candidates:
        cand_ok, cand_note = _probe_install(candidate_repo, candidate_path)
        if cand_ok:
            ok = True
            repo = candidate_repo
            remote_path = candidate_path
            note = f"ok ({reason})"
            break
        repo = candidate_repo
        remote_path = candidate_path
        note = f"{cand_note} ({reason})"
    return SkillEntry(
        name=name,
        local_path=local_path,
        source_bucket=source_bucket,
        remote_repo=repo,
        remote_path=remote_path,
        check="OK" if ok else "FAIL",
        strategy="update-via-github",
        note=note,
    )


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    jobs = _normalize_jobs(args.jobs)

    if not LIST_SCRIPT.is_file() or not INSTALL_SCRIPT.is_file():
        print("skill-installer scripts were not found in ~/.codex/skills/.system", file=sys.stderr)
        return 2

    openai_curated = _load_remote_set("openai/skills", "skills/.curated")
    openai_system = _load_remote_set("openai/skills", "skills/.system")
    anthropics_skills = _load_remote_set("anthropics/skills", "skills")

    local_skills = _collect_local_skills()
    if jobs == 1:
        rows = [
            _evaluate_skill(item, openai_curated, openai_system, anthropics_skills)
            for item in local_skills
        ]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            rows = list(
                executor.map(
                    lambda item: _evaluate_skill(item, openai_curated, openai_system, anthropics_skills),
                    local_skills,
                )
            )
    rows = sorted(rows, key=lambda r: r.name)

    total = len(rows)
    ok = sum(1 for r in rows if r.check == "OK")
    fail = sum(1 for r in rows if r.check == "FAIL")
    skip = sum(1 for r in rows if r.check == "SKIP")
    if args.format == "tsv":
        print("skill\tbucket\tresult\tstrategy\trepo\tremote_path\tnote")
        for row in rows:
            print(
                f"{row.name}\t{row.source_bucket}\t{row.check}\t"
                f"{row.strategy}\t"
                f"{row.remote_repo or '-'}\t"
                f"{row.remote_path or '-'}\t{row.note}"
            )
        print("")
        print(f"summary: total={total} ok={ok} fail={fail} skip={skip}")
    else:
        for row in rows:
            print(
                json.dumps(
                    {
                        "type": "row",
                        "skill": row.name,
                        "bucket": row.source_bucket,
                        "result": row.check,
                        "strategy": row.strategy,
                        "repo": row.remote_repo,
                        "remote_path": row.remote_path,
                        "note": row.note,
                    },
                    ensure_ascii=False,
                )
            )
        print(
            json.dumps(
                {
                    "type": "summary",
                    "total": total,
                    "ok": ok,
                    "fail": fail,
                    "skip": skip,
                },
                ensure_ascii=False,
            )
        )
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
