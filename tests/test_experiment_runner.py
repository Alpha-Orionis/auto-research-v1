"""Offline regression checks; fake OpenCode never calls a model or network."""

from __future__ import annotations

import csv
import fnmatch
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("experiment_runner", PROJECT / "scripts" / "experiment_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

FAKE_OPENCODE = '''import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print(os.environ.get("RUNNER_TEST_VERSION", "1.2.99"))
    raise SystemExit(0)
assert "--format" in sys.argv and sys.argv[sys.argv.index("--format") + 1] == "json"
assert sys.argv[sys.argv.index("--agent") + 1] == "experiment-reviewer"
assert "--dir" not in sys.argv
root = Path.cwd()
with (root / "review-call-count.txt").open("a") as handle:
    handle.write("x")
behavior = os.environ.get("RUNNER_TEST_BEHAVIOR", "success")
if behavior == "fallback":
    print("agent not found. Falling back to default agent")
elif behavior == "error":
    print(json.dumps({"type": "error", "error": {"name": "ProviderError"}}))
else:
    if behavior != "empty":
        print(json.dumps({"type": "text", "part": {"text": "Review completed with evidence and limitations."}}))
    print(json.dumps({"type": "step_finish", "part": {"reason": "length" if behavior == "incomplete" else "stop"}}))
'''


class RunnerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="generic-agent-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        agent_dir = self.root / ".opencode" / "agents"
        agent_dir.mkdir(parents=True)
        shutil.copyfile(PROJECT / ".opencode" / "agents" / "experiment-reviewer.md", agent_dir / "experiment-reviewer.md")
        self.fake = self.root / "fake_opencode.py"
        self.fake.write_text(FAKE_OPENCODE, encoding="utf-8")
        self.prefix = [sys.executable, str(self.fake)]
        with (PROJECT / "templates" / "experiment-queue.template.csv").open(encoding="utf-8") as handle:
            self.fields = next(csv.reader(handle))
        self.rows = []
        for order, experiment_id in enumerate(("exp-one", "exp-two"), 1):
            row = dict.fromkeys(self.fields, "")
            row.update(
                id=experiment_id, order=str(order), status="APPROVED",
                hypothesis="A generic metric can be measured.", primary_metric="score",
                command_json=json.dumps([sys.executable, "-c", "print('score=1')"]),
                working_directory=".", time_limit_minutes="1",
                approval_reference="test authorization", review_data_scope="manifest and logs",
                review_approval_reference="test review authorization",
            )
            self.rows.append(row)
        self.queue = self.root / "experiment-queue.csv"
        self.state_path = self.root / ".research" / "runner" / "state.json"
        self.state = {"version": runner.VERSION, "experiments": {}}
        runner.write_queue(self.queue, self.fields, self.rows)

    def run_one(self):
        runner.run_experiment(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, "exp-one", self.prefix)
        return self.state["experiments"]["exp-one"]

    def recover(self):
        runner.recover(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, self.prefix)

    def call_count(self):
        return len((self.root / "review-call-count.txt").read_text())

    def test_completed_experiment_starts_review_and_id_is_single_use(self):
        record = self.run_one()
        self.assertEqual((record["status"], record["outcome"]), ("REVIEWED", "SUCCEEDED"))
        self.assertEqual(self.call_count(), 1)
        self.assertIn("evidence", (self.root / record["review_result_reference"]).read_text())
        manifest = json.loads((self.root / record["result_reference"]).read_text())
        self.assertNotIn("command_json", manifest)
        self.assertNotIn("approval_reference", manifest)
        self.assertEqual(runner.read_state(self.state_path)["experiments"]["exp-one"]["status"], "REVIEWED")
        with self.assertRaises(runner.RunnerError):
            self.run_one()

    def test_failed_experiment_is_still_reviewed(self):
        self.rows[0]["command_json"] = json.dumps([sys.executable, "-c", "raise SystemExit(7)"])
        record = self.run_one()
        self.assertEqual((record["status"], record["outcome"], record["return_code"]), ("REVIEWED", "FAILED", 7))

    def test_error_fallback_empty_and_incomplete_reports_block_progress(self):
        for behavior in ("error", "fallback", "empty", "incomplete"):
            with self.subTest(behavior=behavior), mock.patch.dict(os.environ, {"RUNNER_TEST_BEHAVIOR": behavior}):
                # Each isolated queue uses one fresh id and run directory.
                self.rows[0]["id"] = behavior
                self.rows[1]["id"] = "next-" + behavior
                self.rows[0]["status"] = self.rows[1]["status"] = "APPROVED"
                self.state = {"version": runner.VERSION, "experiments": {}}
                with self.assertRaises(runner.RunnerError):
                    runner.run_experiment(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, behavior, self.prefix)
                self.assertEqual(self.state["experiments"][behavior]["status"], "REVIEW_FAILED")
                with self.assertRaises(runner.RunnerError):
                    runner.run_experiment(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, "next-" + behavior, self.prefix)

    def test_pending_recovery_does_not_rerun_experiment(self):
        record = self.run_one()
        record["status"] = "REVIEW_PENDING"
        record["review_attempts"] = 0
        before = (self.root / record["stdout"]).read_bytes()
        self.recover()
        self.assertEqual(record["status"], "REVIEWED")
        self.assertEqual((self.root / record["stdout"]).read_bytes(), before)
        self.assertEqual(self.call_count(), 2)

    def test_complete_receipt_is_recovered_without_duplicate_review(self):
        record = self.run_one()
        record["status"] = "REVIEWING"
        record["review_pid"] = None
        self.recover()
        self.assertEqual(record["status"], "REVIEWED")
        self.assertEqual(self.call_count(), 1)

    def test_uncertain_run_needs_reconciliation(self):
        record = self.run_one()
        record.update(status="RUNNING", pid=None)
        self.recover()
        self.assertEqual(record["status"], "INTERRUPTED")
        self.assertEqual(self.call_count(), 1)
        runner.reconcile(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, "exp-one", "FAILED", 1, self.prefix)
        self.assertEqual((record["status"], record["outcome"]), ("REVIEWED", "FAILED"))

    def test_alive_query_and_recovery_do_not_terminate_child(self):
        record = self.run_one()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            self.assertTrue(runner.pid_is_alive(child.pid))
            record.update(status="RUNNING", pid=child.pid)
            self.recover()
            self.assertEqual(record["status"], "RUNNING")
            self.assertIsNone(child.poll())
            record["status"] = "INTERRUPTED"
            with self.assertRaises(runner.RunnerError):
                runner.reconcile(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, "exp-one", "FAILED", 1, self.prefix)
        finally:
            runner.stop_child(child)
        self.assertFalse(runner.pid_is_alive(child.pid))

    def test_storage_failure_after_spawn_stops_child(self):
        pids = []
        def fail_after_start(pid):
            pids.append(pid)
            raise OSError("simulated storage failure")
        with self.assertRaises(OSError):
            runner.supervise([sys.executable, "-c", "import time; time.sleep(20)"], self.root, self.root / "out.log", self.root / "err.log", 60, fail_after_start)
        self.assertFalse(runner.pid_is_alive(pids[0]))

    def test_timeout_stops_direct_child(self):
        pids = []
        _, timed_out = runner.supervise([sys.executable, "-c", "import time; time.sleep(20)"], self.root, self.root / "out.log", self.root / "err.log", 1, pids.append)
        self.assertTrue(timed_out)
        self.assertFalse(runner.pid_is_alive(pids[0]))

    def test_process_lock_refuses_second_runner(self):
        path = self.root / "runner.lock"
        with runner.process_lock(path):
            for _ in range(2):
                with self.assertRaises(runner.RunnerError):
                    with runner.process_lock(path):
                        self.fail("second lock was acquired")

    def test_queue_sync_repairs_stale_status(self):
        self.run_one()
        self.rows[0]["status"] = "RUNNING"
        runner.write_queue(self.queue, self.fields, self.rows)
        runner.sync_queue_from_state(self.queue, self.fields, self.rows, self.state)
        _, rows = runner.read_queue(self.queue)
        self.assertEqual(rows[0]["status"], "REVIEWED")

    def test_malformed_csv_rejected(self):
        self.queue.write_text(",".join(self.fields) + "\n" + ",".join(["x"] * (len(self.fields) + 1)) + "\n", encoding="utf-8")
        with self.assertRaises(runner.RunnerError):
            runner.read_queue(self.queue)

    def test_bad_state_artifact_path_rejected(self):
        record = self.run_one()
        record["stdout"] = "../private.txt"
        runner.atomic_json(self.state_path, self.state)
        with self.assertRaises(runner.RunnerError):
            runner.read_state(self.state_path)

    def test_order_and_approval_gate_launch(self):
        with self.assertRaises(runner.RunnerError):
            runner.run_experiment(self.root, self.queue, self.fields, self.rows, self.state_path, self.state, "exp-two", self.prefix)
        self.rows[0]["status"] = "READY"
        with self.assertRaises(runner.RunnerError):
            self.run_one()
        self.assertFalse(self.state["experiments"])

    def test_v2_or_subagent_reviewer_blocked_before_experiment(self):
        for version in ("1.1.99", "2.0.0"):
            with self.subTest(version=version), mock.patch.dict(os.environ, {"RUNNER_TEST_VERSION": version}), self.assertRaises(runner.RunnerError):
                self.run_one()
        agent = self.root / ".opencode" / "agents" / "experiment-reviewer.md"
        agent.write_text(agent.read_text().replace("mode: all", "mode: subagent"))
        with self.assertRaises(runner.RunnerError):
            self.run_one()
        self.assertFalse(self.state["experiments"])

    def test_reviewer_read_rules_cover_relative_and_absolute_paths(self):
        agent = (PROJECT / ".opencode" / "agents" / "experiment-reviewer.md").read_text(encoding="utf-8")
        rules = re.findall(r'''^    ["'](.+?)["']: (allow|deny)$''', agent, re.MULTILINE)
        self.assertEqual(rules[0], ("*", "deny"))
        # OpenCode's wildcard '*' spans path separators, like fnmatchcase.
        def action(path):
            matches = [value for pattern, value in rules if fnmatch.fnmatchcase(path, pattern)]
            return matches[-1]
        for path in (
            ".research/runs/exp-one/stdout.log",
            r".research\runs\exp-one\stdout.log",
            "/workspace/project/.research/runs/exp-one/stdout.log",
            "C:/workspace/project/.research/runs/exp-one/stdout.log",
            r"C:\workspace\project\.research\runs\exp-one\stdout.log",
        ):
            with self.subTest(path=path):
                self.assertEqual(action(path), "allow")
        for path in (".env", "/workspace/project/private.txt", r"C:\workspace\project\private.txt"):
            with self.subTest(path=path):
                self.assertEqual(action(path), "deny")

    @unittest.skipUnless(os.name == "nt", "Windows launcher behavior")
    def test_implicit_batch_launcher_rejected(self):
        batch = self.root / "launcher.cmd"
        batch.write_text("@echo off\n")
        with self.assertRaises(runner.RunnerError):
            runner.resolve_command([str(batch), "untrusted data"], self.root)

    def test_ids_are_portable_between_windows_and_linux(self):
        for experiment_id in ("job.", "CON", "aux.txt", "COM1", "lpt9.result", "NUL"):
            with self.subTest(experiment_id=experiment_id), self.assertRaises(runner.RunnerError):
                runner.validate_id(experiment_id)
        runner.validate_id("exp-001.v2")

    @unittest.skipUnless(os.name == "posix", "POSIX executable behavior")
    def test_posix_relative_executable_with_spaces(self):
        launcher = self.root / "bin with spaces" / "experiment"
        launcher.parent.mkdir()
        launcher.write_text('#!/bin/sh\nprintf "score=2\\n"\n', encoding="utf-8")
        launcher.chmod(0o700)
        self.rows[0]["command_json"] = json.dumps(["./bin with spaces/experiment"])
        record = self.run_one()
        self.assertEqual(record["status"], "REVIEWED")
        self.assertEqual((self.root / record["stdout"]).read_text().strip(), "score=2")

    def test_cli_uses_same_pipeline(self):
        completed = subprocess.run(
            [sys.executable, str(PROJECT / "scripts" / "experiment_runner.py"), "--project", str(self.root),
             "--opencode-command-json", json.dumps(self.prefix), "run", "--id", "exp-one", "--foreground"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(runner.read_state(self.state_path)["experiments"]["exp-one"]["status"], "REVIEWED")


if __name__ == "__main__":
    unittest.main()
