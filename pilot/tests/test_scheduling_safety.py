import sys
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PILOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PILOT), str(PILOT / "engine")]
from budget import BudgetLedger, ProbeTask
import probe_residue_check
import scheduler


class SchedulingSafetyTest(unittest.TestCase):
    def test_token_budget_is_cumulative(self):
        tasks = [ProbeTask(task_id=str(i), script="x", oracle="x", layer="function", tokens=6)
                 for i in range(2)]
        admission = BudgetLedger(max_agents=3, max_tokens_per_round=10).admit(tasks)
        self.assertEqual(len(admission.admitted), 1)
        self.assertEqual(admission.deferred[0][1], "max_tokens_per_round reached")

    def test_failed_upload_check_is_not_reported_clean(self):
        with patch.object(probe_residue_check, "docker", return_value=""), \
             patch.object(probe_residue_check, "ai_row_counts", return_value={}), \
             patch.object(probe_residue_check, "stray_dirs", return_value=[]), \
             patch.object(probe_residue_check, "upload_files", side_effect=RuntimeError("unavailable")):
            problems, detail = probe_residue_check.collect_problems()
        self.assertIn("unavailable", problems)
        self.assertIsNone(detail["non_seed_uploads"])

    def test_failed_task_is_reported_once_and_exits_nonzero(self):
        task = SimpleNamespace(task_id="failed")
        result = {"round": 1, "executed": [{"task_id": "failed", "returncode": 1}],
                  "admission": {"admitted_count": 1, "deferred_count": 0, "oracle_distribution": {}},
                  "deferred": []}
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "report.json"
            with patch.object(scheduler, "REPORT", report), \
                 patch.object(scheduler, "load_tasks", return_value=[task]), \
                 patch.object(scheduler, "_contract_limits", return_value={"max_agents": 1, "max_tokens_per_round": 0}), \
                 patch.object(scheduler, "_merge_outputs", return_value={}), \
                 patch.object(scheduler, "run_round", return_value=result) as run:
                with self.assertRaises(SystemExit) as exit_status:
                    scheduler.main()
            self.assertEqual(exit_status.exception.code, 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(json.loads(report.read_text())["completion"], "failed")

    def test_broken_plan_is_not_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target.json"
            target.write_text(json.dumps({"contract": "layers.yaml", "source_roots": {},
                                          "annotation_table": "meta.py", "evidence_sources": []}))
            (Path(temp) / "units.json").write_text("{broken")
            env = dict(os.environ, LAYERED_AUDIT_TARGET=str(target))
            result = subprocess.run([sys.executable, str(PILOT / "engine/incremental_plan.py"),
                                     "--should-run", "function"], env=env, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertNotEqual(result.returncode, 3)

    def test_merge_failure_replaces_stale_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "report.json"
            report.write_text('{"completion":"complete"}')
            with patch.object(scheduler, "REPORT", report), \
                 patch.object(scheduler, "load_tasks", return_value=[]), \
                 patch.object(scheduler, "_contract_limits", return_value={"max_agents": 1, "max_tokens_per_round": 0}), \
                 patch.object(scheduler, "_merge_outputs", side_effect=ValueError("bad shard")):
                with self.assertRaises(SystemExit) as exit_status:
                    scheduler.main()
            payload = json.loads(report.read_text())
            self.assertEqual(exit_status.exception.code, 1)
            self.assertEqual(payload["completion"], "failed")
            self.assertIn("bad shard", payload["merge_error"])

    def test_shell_skip_and_error_are_distinct(self):
        text = (PILOT / "run_pipeline.sh").read_text()
        fn = "run_layer () {" + text.split("run_layer () {", 1)[1].split("\n}\n", 1)[0] + "\n}"
        script = "set -e\npython3() { return \"$PLAN_RC\"; }\n" + fn + "\nrun_layer function true\necho continued\n"
        skip = subprocess.run(["bash", "-c", script], env=dict(os.environ, PLAN_RC="3"), capture_output=True, text=True)
        error = subprocess.run(["bash", "-c", script], env=dict(os.environ, PLAN_RC="1"), capture_output=True, text=True)
        self.assertEqual(skip.returncode, 0)
        self.assertIn("continued", skip.stdout)
        self.assertEqual(error.returncode, 1)
        self.assertNotIn("continued", error.stdout)


if __name__ == "__main__":
    unittest.main()
