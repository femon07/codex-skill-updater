import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-skill-updater" / "scripts" / "apply_skill_updates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_skill_updates", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ApplySkillUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _run_main(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.mod.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_backup_root_and_retention_defaults(self):
        self.assertEqual(self.mod.BACKUPS_ROOT.name, "skill-updater-backups")
        self.assertEqual(self.mod.DEFAULT_BACKUP_KEEP_GENERATIONS, 3)

    def test_load_merged_source_map_local_overrides_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pub = tmp_path / "public.json"
            local = tmp_path / "local.json"
            pub.write_text(
                json.dumps({"a": {"repo": "org/a", "path": "skills/a", "ref": "main"}}),
                encoding="utf-8",
            )
            local.write_text(
                json.dumps({"a": {"repo": "org2/a", "path": ".", "ref": "dev"}}),
                encoding="utf-8",
            )
            merged = self.mod._load_merged_source_map(pub, local)
        self.assertEqual(merged["a"]["repo"], "org2/a")
        self.assertEqual(merged["a"]["path"], ".")
        self.assertEqual(merged["a"]["ref"], "dev")

    def test_validate_source_map_entry_rejects_placeholder(self):
        err = self.mod._validate_source_map_entry(
            "codex-skill-updater",
            {"repo": "owner/private-codex-skill-updater", "path": "codex-skill-updater", "ref": "main"},
        )
        self.assertIsNotNone(err)

    def test_validate_source_map_entry_accepts_valid_repo(self):
        err = self.mod._validate_source_map_entry(
            "codex-skill-updater",
            {"repo": "femon07/codex-skill-updater", "path": "codex-skill-updater", "ref": "main"},
        )
        self.assertIsNone(err)

    def test_stage_one_manual_missing_map_is_config_error(self):
        row = self.mod.UpdateRow(
            skill="private-skill",
            bucket="user",
            result="SKIP",
            strategy="manual-source-map-required",
            repo="",
            remote_path="",
            ref="",
            note="",
        )
        staged = self.mod._stage_one(0, row, True, {}, True)
        self.assertIsNotNone(staged.result)
        self.assertEqual(staged.result.status, "CONFIG_ERROR")
        self.assertEqual(staged.result.reason, "skill_not_found_in_source_map")

    def test_stage_one_update_via_github_uses_row_ref(self):
        row = self.mod.UpdateRow(
            skill="sample-skill",
            bucket="user",
            result="OK",
            strategy="update-via-github",
            repo="org/repo",
            remote_path="skills/sample-skill",
            ref="feature/test",
            note="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            staged = tmp_path / "staged"
            staged.mkdir()
            (staged / "SKILL.md").write_text("name: sample\n", encoding="utf-8")
            with patch.object(
                self.mod,
                "_stage_from_installer",
                return_value=(tmp_path, staged),
            ) as stage_from_installer, patch.object(
                self.mod,
                "_target_root",
                return_value=tmp_path / "root",
            ):
                result = self.mod._stage_one(0, row, False, {}, True)
        self.assertIsNotNone(result.result)
        self.assertEqual(result.result.status, "DRY_RUN")
        stage_from_installer.assert_called_once_with(
            skill="sample-skill",
            repo="org/repo",
            skill_path="skills/sample-skill",
            ref="feature/test",
            commands=[],
        )

    def test_fingerprint_tree_ignores_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            left = tmp_path / "left"
            right = tmp_path / "right"
            (left / ".git").mkdir(parents=True)
            (right / ".git").mkdir(parents=True)
            (left / "SKILL.md").write_text("name: sample\n", encoding="utf-8")
            (right / "SKILL.md").write_text("name: sample\n", encoding="utf-8")
            (left / ".git" / "index").write_text("aaa", encoding="utf-8")
            (right / ".git" / "index").write_text("bbb", encoding="utf-8")
            self.assertEqual(self.mod._fingerprint_tree(left), self.mod._fingerprint_tree(right))

    def test_apply_staged_does_not_copy_git_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            staged = tmp_path / "staged"
            target_root = tmp_path / "root"
            staged.mkdir()
            target_root.mkdir()
            (staged / "SKILL.md").write_text("name: sample\n", encoding="utf-8")
            (staged / ".git").mkdir()
            (staged / ".git" / "index").write_text("index", encoding="utf-8")
            with patch.object(self.mod, "_target_root", return_value=target_root):
                self.mod._apply_staged("sample", "user", staged)
            self.assertTrue((target_root / "sample" / "SKILL.md").is_file())
            self.assertFalse((target_root / "sample" / ".git").exists())

    def test_calculate_exit_code(self):
        self.assertEqual(
            self.mod._calculate_exit_code({"failed": 1, "config_error": 0}, strict_config=False),
            1,
        )
        self.assertEqual(
            self.mod._calculate_exit_code({"failed": 0, "config_error": 1}, strict_config=False),
            0,
        )
        self.assertEqual(
            self.mod._calculate_exit_code({"failed": 0, "config_error": 1}, strict_config=True),
            3,
        )

    def test_main_marks_precheck_fail_and_counts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            check = Path(tmp) / "check.ndjson"
            check.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "row",
                                "skill": "x",
                                "bucket": "user",
                                "result": "FAIL",
                                "strategy": "update-via-github",
                                "repo": "org/x",
                                "remote_path": "skills/x",
                                "ref": "main",
                                "note": "probe failed",
                            }
                        ),
                        json.dumps({"type": "summary", "total": 1, "ok": 0, "fail": 1, "skip": 0}),
                    ]
                ),
                encoding="utf-8",
            )
            with patch.object(self.mod, "INSTALLER_SCRIPT", Path(__file__)):
                code, stdout, _ = self._run_main(
                    ["--check-file", str(check), "--check-format", "ndjson", "--dry-run"]
                )
        self.assertEqual(code, 0)
        summary = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(summary["precheck_fail"], 1)

    def test_main_strict_config_returns_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            check = tmp_path / "check.ndjson"
            check.write_text(
                json.dumps(
                    {
                        "type": "row",
                        "skill": "private-unknown",
                        "bucket": "user",
                        "result": "SKIP",
                        "strategy": "manual-source-map-required",
                        "repo": None,
                        "remote_path": None,
                        "ref": None,
                        "note": "manual map required",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            source = tmp_path / "source.json"
            local = tmp_path / "local.json"
            source.write_text("{}", encoding="utf-8")
            local.write_text("{}", encoding="utf-8")
            with patch.object(self.mod, "INSTALLER_SCRIPT", Path(__file__)):
                code, stdout, _ = self._run_main(
                    [
                        "--check-file",
                        str(check),
                        "--check-format",
                        "ndjson",
                        "--dry-run",
                        "--allow-manual-map",
                        "--source-map",
                        str(source),
                        "--source-map-local",
                        str(local),
                        "--strict-config",
                    ]
                )
        self.assertEqual(code, 3)
        summary = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(summary["config_error"], 1)


if __name__ == "__main__":
    unittest.main()
