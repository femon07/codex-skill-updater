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
import time
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
DEFAULT_LIST_RETRIES = 3
LIST_RETRY_BACKOFF_SECONDS = 1.0
CACHE_ROOT = SKILLS_ROOT / ".cache"
CATALOG_CACHE_FILE = CACHE_ROOT / "skill-updater-remote-catalogs.json"


@dataclass
class SkillEntry:
    name: str
    local_path: Path
    source_bucket: str
    remote_repo: str | None
    remote_path: str | None
    remote_ref: str | None
    check: str
    strategy: str
    note: str


@dataclass
class RemoteCatalog:
    names: set[str]
    index_available: bool


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )


def _catalog_cache_key(repo: str, path: str, ref: str) -> str:
    return f"{repo}:{path}:{ref}"


def _load_catalog_cache() -> dict[str, dict]:
    if not CATALOG_CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(CATALOG_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        out[key] = value
    return out


def _save_catalog_cache(cache: dict[str, dict]) -> None:
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        CATALOG_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Cache write errors must not break update flow.
        pass


def _is_retryable_list_error(stderr: str) -> bool:
    msg = stderr.lower()
    return any(
        token in msg
        for token in (
            "http 403",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "timed out",
            "temporary failure",
        )
    )


def _load_remote_catalog(repo: str, path: str, cache: dict[str, dict], retries: int) -> RemoteCatalog:
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
    attempts = max(1, retries)
    last_err = ""
    for attempt in range(1, attempts + 1):
        proc = _run(cmd)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            names = {row["name"] for row in data}
            key = _catalog_cache_key(repo, path, DEFAULT_REF)
            cache[key] = {
                "names": sorted(names),
                "updated_at": int(time.time()),
                "repo": repo,
                "path": path,
                "ref": DEFAULT_REF,
            }
            return RemoteCatalog(names=names, index_available=True)

        stderr = (proc.stderr or proc.stdout or "").strip()
        last_err = stderr
        if attempt < attempts and _is_retryable_list_error(stderr):
            sleep_seconds = LIST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(
                f"warning: catalog fetch failed for {repo}:{path} (attempt {attempt}/{attempts}): {stderr}",
                file=sys.stderr,
            )
            time.sleep(sleep_seconds)
            continue
        break

    key = _catalog_cache_key(repo, path, DEFAULT_REF)
    cached = cache.get(key, {})
    cached_names_raw = cached.get("names", [])
    if isinstance(cached_names_raw, list) and all(isinstance(x, str) for x in cached_names_raw):
        print(
            f"warning: using cached catalog for {repo}:{path} because fetch failed: {last_err}",
            file=sys.stderr,
        )
        return RemoteCatalog(names=set(cached_names_raw), index_available=True)

    print(
        f"warning: catalog unavailable for {repo}:{path}; fallback candidate probing enabled. last_error={last_err}",
        file=sys.stderr,
    )
    return RemoteCatalog(names=set(), index_available=False)


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


def _normalize_ref(raw_ref: object) -> str:
    ref = str(raw_ref or "").strip()
    return ref or DEFAULT_REF


def _probe_install(repo: str, remote_path: str, ref: str) -> tuple[bool, str]:
    temp_root = Path(tempfile.mkdtemp(prefix="skill-update-probe-"))
    try:
        cmd = [
            "python3",
            str(INSTALL_SCRIPT),
            "--repo",
            repo,
            "--ref",
            ref,
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
    meta: dict,
    openai_curated: RemoteCatalog,
    anthropics_skills: RemoteCatalog,
) -> list[tuple[str, str, str, str]]:
    candidates: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(repo: str, path: str, ref: str, reason: str) -> None:
        key = (repo, path, ref)
        if key in seen:
            return
        seen.add(key)
        candidates.append((repo, path, ref, reason))

    source = meta.get("source")
    if source == "github" and meta.get("repo") and meta.get("skillPath"):
        repo = str(meta["repo"])
        skill_path = str(meta["skillPath"]).strip("/")
        ref = _normalize_ref(meta.get("ref"))
        add(repo, skill_path, ref, "meta github skillPath")
        if not skill_path.startswith("skills/"):
            add(repo, f"skills/{skill_path}", ref, "meta github + skills/ prefix")
    elif source == "registry":
        # Registry does not expose a direct repo/path in metadata.
        reg_name = str(meta.get("name", name))
        if reg_name in openai_curated.names:
            add("openai/skills", f"skills/.curated/{reg_name}", DEFAULT_REF, "registry matched openai curated")
        if reg_name in anthropics_skills.names:
            add("anthropics/skills", f"skills/{reg_name}", DEFAULT_REF, "registry matched anthropics public")
        if not openai_curated.index_available:
            add("openai/skills", f"skills/.curated/{reg_name}", DEFAULT_REF, "registry fallback heuristic openai curated")
        if not anthropics_skills.index_available:
            add("anthropics/skills", f"skills/{reg_name}", DEFAULT_REF, "registry fallback heuristic anthropics public")
    else:
        # No useful metadata: resolve from known public lists.
        if name in openai_curated.names:
            add("openai/skills", f"skills/.curated/{name}", DEFAULT_REF, "name matched openai curated")
        if name in anthropics_skills.names:
            add("anthropics/skills", f"skills/{name}", DEFAULT_REF, "name matched anthropics public")
        if not openai_curated.index_available:
            add("openai/skills", f"skills/.curated/{name}", DEFAULT_REF, "name fallback heuristic openai curated")
        if not anthropics_skills.index_available:
            add("anthropics/skills", f"skills/{name}", DEFAULT_REF, "name fallback heuristic anthropics public")

    return candidates


def _strategy_for_skip(
    name: str,
    meta: dict,
) -> tuple[str, str]:
    source = str(meta.get("source", "unknown"))
    dist_skill = DIST_ROOT / f"{name}.skill"

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
    parser.add_argument(
        "--list-retries",
        type=int,
        default=DEFAULT_LIST_RETRIES,
        help=f"Remote catalog retries (default: {DEFAULT_LIST_RETRIES})",
    )
    return parser.parse_args(argv)


def _normalize_jobs(raw_jobs: int) -> int:
    return max(1, min(MAX_JOBS, raw_jobs))


def _evaluate_skill(
    item: tuple[str, Path, str, dict],
    openai_curated: RemoteCatalog,
    anthropics_skills: RemoteCatalog,
) -> SkillEntry:
    name, local_path, source_bucket, meta = item
    candidates = _resolve_candidates(
        name=name,
        meta=meta,
        openai_curated=openai_curated,
        anthropics_skills=anthropics_skills,
    )
    if not candidates:
        strategy, strategy_note = _strategy_for_skip(
            name,
            meta,
        )
        return SkillEntry(
            name=name,
            local_path=local_path,
            source_bucket=source_bucket,
            remote_repo=None,
            remote_path=None,
            remote_ref=None,
            check="SKIP",
            strategy=strategy,
            note=strategy_note,
        )

    ok = False
    repo = None
    remote_path = None
    remote_ref = None
    note = "install probe failed"
    for candidate_repo, candidate_path, candidate_ref, reason in candidates:
        cand_ok, cand_note = _probe_install(candidate_repo, candidate_path, candidate_ref)
        if cand_ok:
            ok = True
            repo = candidate_repo
            remote_path = candidate_path
            remote_ref = candidate_ref
            note = f"ok ({reason})"
            break
        repo = candidate_repo
        remote_path = candidate_path
        remote_ref = candidate_ref
        note = f"{cand_note} ({reason})"
    return SkillEntry(
        name=name,
        local_path=local_path,
        source_bucket=source_bucket,
        remote_repo=repo,
        remote_path=remote_path,
        remote_ref=remote_ref,
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

    cache = _load_catalog_cache()
    openai_curated = _load_remote_catalog("openai/skills", "skills/.curated", cache, args.list_retries)
    anthropics_skills = _load_remote_catalog("anthropics/skills", "skills", cache, args.list_retries)
    _save_catalog_cache(cache)

    local_skills = _collect_local_skills()
    if jobs == 1:
        rows = [
            _evaluate_skill(item, openai_curated, anthropics_skills)
            for item in local_skills
        ]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            rows = list(
                executor.map(
                    lambda item: _evaluate_skill(item, openai_curated, anthropics_skills),
                    local_skills,
                )
            )
    rows = sorted(rows, key=lambda r: r.name)

    total = len(rows)
    ok = sum(1 for r in rows if r.check == "OK")
    fail = sum(1 for r in rows if r.check == "FAIL")
    skip = sum(1 for r in rows if r.check == "SKIP")
    if args.format == "tsv":
        print("skill\tbucket\tresult\tstrategy\trepo\tremote_path\tref\tnote")
        for row in rows:
            print(
                f"{row.name}\t{row.source_bucket}\t{row.check}\t"
                f"{row.strategy}\t"
                f"{row.remote_repo or '-'}\t"
                f"{row.remote_path or '-'}\t"
                f"{row.remote_ref or '-'}\t{row.note}"
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
                        "ref": row.remote_ref,
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
