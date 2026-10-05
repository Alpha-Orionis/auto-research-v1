"""Offline checks for detached workers, foreground responsiveness and telemetry."""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
try:
    import task_runtime as runtime
    import resource_monitor
finally:
    sys.path.pop(0)

FAKE_CLI = '''import json, os, re, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("1.2.99")
    raise SystemExit(0)
agent = sys.argv[sys.argv.index("--agent") + 1]
assert agent in {"experiment-reviewer", "doc-reviewer", "paper-reviewer"}
assert re.search(r"^mode:\\s*(all|primary)\\s*$", Path(".opencode/agents", agent + ".md").read_text(), re.MULTILINE)
with Path("review-calls.txt").open("a") as handle:
    handle.write("x")
time.sleep(float(os.environ.get("BACKGROUND_REVIEW_DELAY", "0")))
print(json.dumps({"type":"text","part":{"text":"Synthetic review completed with evidence."}}))
print(json.dumps({"type":"step_finish","part":{"reason":"stop"}}))
'''


class BackgroundChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="generic-background-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.addCleanup(self.stop_workers)
        agents = self.root / ".opencode/agents"
        agents.mkdir(parents=True)
        for name in ("experiment-reviewer", "doc-reviewer", "paper-reviewer"):
            shutil.copyfile(PROJECT / ".opencode/agents" / (name + ".md"), agents / (name + ".md"))
        fake = self.root / "fake_opencode.py"
        fake.write_text(FAKE_CLI, encoding="utf-8")
        self.prefix = [sys.executable, str(fake)]
        with (PROJECT / "templates/experiment-queue.template.csv").open() as handle:
            self.fields = next(csv.reader(handle))
        self.rows = []
        for order, experiment_id in enumerate(("exp-one", "exp-two"), 1):
            row = dict.fromkeys(self.fields, "")
            row.update(id=experiment_id, order=str(order), status="APPROVED",
                       command_json=json.dumps([sys.executable, "-u", "-c", "import time; time.sleep(2); print('score=1')"]),
                       working_directory=".", time_limit_minutes="1", resource_limit="one process",
                       approval_reference="synthetic authorization", review_data_scope="manifest and logs",
                       review_approval_reference="synthetic review authorization")
            self.rows.append(row)
        self.write_queue()

    def write_queue(self):
        runtime.runner.write_queue(self.root / "experiment-queue.csv", self.fields, self.rows)

    def call(self, *arguments, code=0, env=None):
        result = subprocess.run(
            [sys.executable, str(PROJECT / "scripts/experiment_runner.py"), "--project", str(self.root),
             "--opencode-command-json", json.dumps(self.prefix), *arguments],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, env=env,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        if code == 0:
            return json.loads(result.stdout)
        return result

    def task(self, task_id, script="import time; time.sleep(30)", *extra):
        return self.call("task", "--id", task_id, "--command-json", json.dumps([sys.executable, "-u", "-c", script]),
                         "--time-limit-minutes", "1", "--resource-limit", "one process", "--approval-reference", "synthetic authorization",
                         "--sample-seconds", "0.1", *extra)

    def wait_record(self, task_id, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        path = self.root / f".research/tasks/{task_id}/status.json"
        record = None
        while time.monotonic() < deadline:
            if path.exists():
                record = json.loads(path.read_text())
                if predicate(record):
                    return record
            time.sleep(0.05)
        error_log = path.parent / "worker.stderr.log"
        detail = error_log.read_text(errors="replace")[-2000:] if error_log.exists() else ""
        self.fail(f"Timed out waiting for task {task_id}; last status: {record}; worker stderr: {detail}")

    def stop_workers(self):
        directory = self.root / ".research/tasks"
        if directory.exists():
            for path in directory.glob("*/status.json"):
                record = json.loads(path.read_text())
                if record.get("status") not in runtime.TERMINAL:
                    runtime.runner.atomic_json(path.parent / "cancel.json", {"requested_at": runtime.runner.utc_now()})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                active = [json.loads(path.read_text()) for path in directory.glob("*/status.json")]
                # Terminal work status is persisted before the final event and
                # interpreter shutdown. Wait for the worker to close its files.
                if all(record.get("status") in runtime.TERMINAL and
                       not runtime.runner.pid_is_alive(record.get("worker_pid")) for record in active):
                    return
                time.sleep(0.05)
            self.fail("A synthetic background worker did not stop during cleanup.")

    def test_launcher_exits_and_foreground_status_remains_responsive(self):
        started = time.monotonic()
        submitted = self.task("long-task")
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(submitted["submitted"])
        running = self.wait_record("long-task", lambda item: item.get("child_pid") is not None)
        started = time.monotonic()
        status = self.call("status", "--id", "long-task")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(status["tasks"][0]["status"], "RUNNING")
        self.assertTrue(runtime.runner.pid_is_alive(running["worker_pid"]))
        self.assertNotIn("command", status["tasks"][0])
        self.call("cancel", "--id", "long-task")
        finished = self.wait_record("long-task", lambda item: item["status"] == "CANCELLED")
        self.assertIsNone(finished["child_pid"])

    def test_experiment_and_subtask_run_without_blocking_messages(self):
        submitted = self.call("run", "--id", "exp-one", "--sample-seconds", "0.1")
        self.assertEqual(submitted["task_id"], "exp-one")
        self.wait_record("exp-one", lambda item: item.get("phase") == "experiment")
        self.task("child-task", "print('child result')", "--parent-id", "exp-one")
        state = self.call("status")
        self.assertEqual(len(state["tasks"]), 2)
        child = self.wait_record("child-task", lambda item: item["status"] == "SUCCEEDED")
        self.assertEqual(child["parent_id"], "exp-one")
        final = self.wait_record("exp-one", lambda item: item["status"] == "SUCCEEDED")
        self.assertEqual(final["experiment_status"], "REVIEWED")
        self.assertEqual((self.root / "review-calls.txt").read_text(), "x")
        self.assertTrue((self.root / ".research/runs/exp-one/review.md").is_file())
        samples = [json.loads(line) for line in (self.root / ".research/tasks/exp-one/resources.jsonl").read_text().splitlines()]
        self.assertIn("experiment", {sample["phase"] for sample in samples})
        self.assertIn("review", {sample["phase"] for sample in samples})
        self.assertTrue(all(sample["gpu_status"] == "not_requested" for sample in samples))

    def test_paper_and_document_reviewers_can_run_as_background_subtasks(self):
        for agent in ("doc-reviewer", "paper-reviewer"):
            command = [*self.prefix, "run", "--agent", agent, "--format", "json", "Review synthetic approved local evidence only."]
            self.call("task", "--id", agent, "--command-json", json.dumps(command),
                      "--time-limit-minutes", "1", "--resource-limit", "one synthetic review process",
                      "--approval-reference", "synthetic provider and local evidence approval")
        for agent in ("doc-reviewer", "paper-reviewer"):
            finished = self.wait_record(agent, lambda item: item["status"] == "SUCCEEDED")
            report = runtime.runner.parse_review_events(self.root / finished["stdout_reference"])
            self.assertIn("Synthetic review completed", report)

    def test_quiet_output_warns_without_killing_a_healthy_task(self):
        self.task("quiet-task", "import time; time.sleep(3)", "--stall-seconds", "0.1")
        warned = self.wait_record("quiet-task", lambda item: "quiet_output" in item.get("warnings", []))
        self.assertEqual(warned["status"], "RUNNING")
        self.assertTrue(runtime.runner.pid_is_alive(warned["child_pid"]))
        self.wait_record("quiet-task", lambda item: item["status"] == "SUCCEEDED")
        notifications = self.call("events")
        self.assertTrue(any(item["kind"] == "warning" for item in notifications["events"]))
        self.assertFalse(self.call("events", "--after", notifications["next_cursor"])["events"])

    def test_task_ids_and_concurrency_limit_prevent_duplicate_work(self):
        self.task("first", "import time; time.sleep(30)", "--max-background-jobs", "1")
        self.call("task", "--id", "second", "--command-json", json.dumps([sys.executable, "-c", "print('second')"]),
                  "--time-limit-minutes", "1", "--resource-limit", "one process", "--approval-reference", "synthetic authorization",
                  "--max-background-jobs", "1", code=2)
        self.assertFalse((self.root / ".research/tasks/second").exists())
        self.call("run", "--id", "exp-one", "--max-background-jobs", "1", code=2)
        self.assertFalse((self.root / ".research/runs/exp-one").exists())

    def test_cancelled_experiment_requires_explicit_review(self):
        self.rows[0]["command_json"] = json.dumps([sys.executable, "-c", "import time; time.sleep(30)"])
        self.write_queue()
        self.call("run", "--id", "exp-one")
        self.wait_record("exp-one", lambda item: item.get("phase") == "experiment")
        self.call("cancel", "--id", "exp-one")
        self.wait_record("exp-one", lambda item: item["status"] == "CANCELLED")
        ledger = runtime.runner.read_state(self.root / ".research/runner/state.json")
        self.assertEqual(ledger["experiments"]["exp-one"]["outcome"], "CANCELLED")
        self.assertEqual(ledger["experiments"]["exp-one"]["status"], "REVIEW_PENDING")
        self.call("run", "--id", "exp-two", code=2)
        review = self.call("review", "--id", "exp-one")
        self.wait_record(review["task_id"], lambda item: item["status"] == "SUCCEEDED")
        self.assertEqual(runtime.runner.read_state(self.root / ".research/runner/state.json")["experiments"]["exp-one"]["status"], "REVIEWED")

    def test_review_cancellation_is_recorded_and_can_be_retried(self):
        self.rows[0]["command_json"] = json.dumps([sys.executable, "-c", "print('score=1')"])
        self.write_queue()
        self.call("run", "--id", "exp-one", env=dict(os.environ, BACKGROUND_REVIEW_DELAY="30"))
        self.wait_record("exp-one", lambda item: item.get("phase") == "review")
        self.call("cancel", "--id", "exp-one")
        self.wait_record("exp-one", lambda item: item["status"] == "CANCELLED")
        record = runtime.runner.read_state(self.root / ".research/runner/state.json")["experiments"]["exp-one"]
        self.assertEqual(record["status"], "REVIEW_FAILED")
        self.assertIsNone(record["review_pid"])
        retried = self.call("review", "--id", "exp-one", "--retry")
        self.wait_record(retried["task_id"], lambda item: item["status"] == "SUCCEEDED")

    def test_failed_launch_receipt_stops_worker_before_handoff(self):
        real_atomic = runtime.runner.atomic_json
        def fail_receipt(path, value):
            if path.name == "launch.json":
                raise OSError("synthetic storage failure")
            real_atomic(path, value)
        child = mock.Mock(pid=12345)
        config = dict(kind="command", parent_id=None, monitor=dict(sample_seconds=30, stall_seconds=900, gpu_ids=[]))
        with mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime.runner, "atomic_json", side_effect=fail_receipt), mock.patch.object(runtime.runner, "stop_child") as stop:
            with self.assertRaises(runtime.runner.RunnerError):
                runtime.launch(self.root, "receipt-failure", config, 4)
            stop.assert_called_once_with(child)
        self.assertEqual(runtime.read_json(self.root / ".research/tasks/receipt-failure/status.json")["status"], "FAILED")

    def test_status_commit_retries_a_transient_reader_conflict(self):
        target = self.root / "status.json"
        real_replace = os.replace
        calls = []
        def transient_conflict(source, destination):
            calls.append(destination)
            if len(calls) == 1:
                raise PermissionError("synthetic reader sharing conflict")
            real_replace(source, destination)
        with mock.patch.object(runtime.runner.os, "replace", side_effect=transient_conflict):
            runtime.runner.atomic_json(target, {"status": "SUCCEEDED"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(runtime.read_json(target)["status"], "SUCCEEDED")

    def test_permanent_file_access_conflict_has_a_bounded_deadline(self):
        with mock.patch.object(runtime.runner.os, "replace", side_effect=PermissionError("permanent conflict")), \
             mock.patch.object(runtime.runner.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaises(PermissionError):
                runtime.runner.atomic_json(self.root / "status.json", {"status": "SUCCEEDED"})
        self.assertFalse(list(self.root.glob(".status.json.*.tmp")))

    def test_event_cursor_remains_ordered_when_clock_moves_backwards(self):
        with mock.patch.object(runtime.time, "time_ns", side_effect=[999, 1]):
            runtime.emit(self.root, "synthetic", "completed", "first")
            runtime.emit(self.root, "synthetic", "completed", "second")
        items = runtime.events(self.root)
        self.assertEqual([item["message"] for item in items], ["first", "second"])
        self.assertEqual([item["message"] for item in runtime.events(self.root, items[0]["event_id"])], ["second"])

    def test_concurrent_events_have_distinct_monotonic_cursors(self):
        with mock.patch.object(runtime.time, "time_ns", return_value=999), ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda number: runtime.emit(self.root, "synthetic", "completed", str(number)), range(8)))
        items = runtime.events(self.root)
        self.assertEqual(len(items), 8)
        self.assertEqual(len({item["event_id"] for item in items}), 8)
        self.assertEqual([runtime.event_order(item["event_id"])[0] for item in items], list(range(999, 1007)))


class ResourceChecks(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux zombie process state")
    def test_exited_zombie_is_not_a_live_background_worker(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(runtime.runner.stop_child, child)
        deadline = time.monotonic() + 5
        stat = Path(f"/proc/{child.pid}/stat")
        while time.monotonic() < deadline:
            if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                self.assertFalse(runtime.runner.pid_is_alive(child.pid))
                return
            time.sleep(0.02)
        self.fail("Synthetic child did not reach its exited zombie state.")

    def test_available_local_cpu_memory_and_disk_counters(self):
        sampler = resource_monitor.Sampler(PROJECT)
        sample = sampler.sample(os.getpid())
        self.assertGreater(sample["memory_total_bytes"], 0)
        self.assertGreater(sample["process_rss_bytes"], 0)
        self.assertGreater(sample["disk_free_bytes"], 0)
        self.assertEqual(sample["gpu_status"], "not_requested")

    def test_missing_gpu_tool_is_an_unavailable_counter(self):
        with mock.patch.object(resource_monitor.shutil, "which", return_value=None):
            self.assertEqual(resource_monitor.gpu_counters([0]), ([], "unavailable"))

    def test_gpu_query_is_read_only_and_filters_selected_devices(self):
        completed = subprocess.CompletedProcess([], 0, "1, 20, 10, 100\n2, 30, 40, 100\n", "")
        with mock.patch.object(resource_monitor.shutil, "which", return_value="nvidia-smi"), mock.patch.object(resource_monitor.subprocess, "run", return_value=completed) as query:
            records, status = resource_monitor.gpu_counters([1])
        self.assertEqual(status, "available")
        self.assertEqual([record["index"] for record in records], [1])
        self.assertIn("--id=1", query.call_args.args[0])
        self.assertFalse(query.call_args.kwargs["shell"])
        self.assertEqual(query.call_args.kwargs["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
