import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-skill-updater" / "scripts" / "update_skills.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("update_skills", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UpdateSkillsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _run_main(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.mod.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_wrapper_continues_after_soft_precheck_fail_and_returns_1(self):
        check_proc = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout='{"type":"row","skill":"x","result":"FAIL"}\n',
            stderr="warning: probe failed\n",
        )
        apply_proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"success":0,"precheck_fail":1}\n',
            stderr="",
        )
        with patch.object(self.mod.subprocess, "run", return_value=check_proc), patch.object(
            self.mod, "_run", return_value=apply_proc
        ) as apply_run:
            code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 1)
        self.assertIn('{"success":0,"precheck_fail":1}', stdout)
        self.assertIn("warning: probe failed", stderr)
        apply_run.assert_called_once()
        self.assertEqual(apply_run.call_args.kwargs["input_text"], check_proc.stdout)

    def test_wrapper_stops_on_check_infrastructure_error(self):
        check_proc = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="fatal\n",
            stderr="installer missing\n",
        )
        with patch.object(self.mod.subprocess, "run", return_value=check_proc), patch.object(
            self.mod, "_run"
        ) as apply_run:
            code, stdout, stderr = self._run_main([])
        self.assertEqual(code, 2)
        self.assertIn("fatal", stdout)
        self.assertIn("installer missing", stderr)
        apply_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
