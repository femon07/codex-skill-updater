import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-skill-updater" / "scripts" / "check_skill_updates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_skill_updates", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CheckSkillUpdatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_retryable_list_error(self):
        self.assertTrue(self.mod._is_retryable_list_error("Error: Failed to fetch skills: HTTP 403"))
        self.assertFalse(self.mod._is_retryable_list_error("Error: Skills path not found: HTTP 404"))

    def test_resolve_candidates_uses_fallback_when_index_unavailable(self):
        c = self.mod.RemoteCatalog(names=set(), index_available=False)
        candidates = self.mod._resolve_candidates(
            name="docx",
            source_bucket="user",
            meta={},
            openai_curated=c,
            openai_system=c,
            anthropics_skills=c,
        )
        repos = {(repo, path) for repo, path, _ in candidates}
        self.assertIn(("openai/skills", "skills/.curated/docx"), repos)
        self.assertIn(("anthropics/skills", "skills/docx"), repos)

    def test_load_remote_catalog_uses_cache_on_fetch_failure(self):
        cache_key = self.mod._catalog_cache_key("openai/skills", "skills/.curated", "main")
        cache = {cache_key: {"names": ["docx", "pdf"]}}
        fail_proc = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: Failed to fetch skills: HTTP 403",
        )
        with patch.object(self.mod, "_run", return_value=fail_proc):
            catalog = self.mod._load_remote_catalog("openai/skills", "skills/.curated", cache, retries=1)
        self.assertTrue(catalog.index_available)
        self.assertEqual(catalog.names, {"docx", "pdf"})

    def test_load_remote_catalog_updates_cache_on_success(self):
        cache = {}
        payload = json.dumps([{"name": "docx"}, {"name": "pdf"}])
        ok_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")
        with patch.object(self.mod, "_run", return_value=ok_proc):
            catalog = self.mod._load_remote_catalog("openai/skills", "skills/.curated", cache, retries=1)
        cache_key = self.mod._catalog_cache_key("openai/skills", "skills/.curated", "main")
        self.assertTrue(catalog.index_available)
        self.assertEqual(catalog.names, {"docx", "pdf"})
        self.assertEqual(cache[cache_key]["names"], ["docx", "pdf"])


if __name__ == "__main__":
    unittest.main()
