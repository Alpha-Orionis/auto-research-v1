"""Offline checks for detached workers, foreground responsiveness and telemetry."""
from __future__ import annotations

import csv
import argparse
import io
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
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

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
identity_match = re.search(r"completed experiment ([A-Za-z0-9._-]+)", sys.argv[-1])
identity = identity_match.group(1).rstrip(".") if identity_match else "synthetic"
report = dict(schema_version=1, experiment_id=identity, assessment="INCONCLUSIVE",
              correctness=dict(verdict="UNKNOWN", reason="Synthetic review"),
              constraints=dict(verdict="PASS", reason="Synthetic scope inspected"),
              evidence=[dict(path=f".research/runs/{identity}/stdout.log", finding="Synthetic evidence inspected")],
              missing_evidence=["real validation"], next_options=[dict(priority=1, action="validate", rationale="Synthetic only")])
print(json.dumps({"type":"text","part":{"text":"REVIEW_REPORT " + json.dumps(report)}}))
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
            self.assertIn("REVIEW_REPORT", report)

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
    def guard(self):
        temp = tempfile.TemporaryDirectory(prefix="generic-monitor-check-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        directory = root / ".research/tasks/synthetic"
        directory.mkdir(parents=True)
        record = dict(id="synthetic", kind="command", status="RUNNING", worker_pid=os.getpid(),
                      heartbeat_at=runtime.runner.utc_now(),
                      status_reference=".research/tasks/synthetic/status.json",
                      telemetry_reference=".research/tasks/synthetic/resources.jsonl",
                      anomaly_reference=".research/tasks/synthetic/anomalies.jsonl")
        guard = runtime.Guard(root, "synthetic", record, dict(sample_seconds=0.1, stall_seconds=900, gpu_ids=[]))
        for name in ("stdout.log", "stderr.log"):
            (directory / name).write_text("")
        return guard, mock.Mock(pid=os.getpid()), directory / "stdout.log", directory / "stderr.log"

    def healthy_sample(self):
        return dict(timestamp="synthetic", disk_free_bytes=1024 ** 3, memory_total_bytes=1000,
                    memory_available_bytes=500, counter_status="available", unavailable_counters=[],
                    gpu_status="not_requested", gpu_device_ids=[], gpus=[])

    def test_resource_anomalies_include_observations_thresholds_and_hints(self):
        sample = self.healthy_sample()
        sample.update(disk_free_bytes=1, memory_available_bytes=1, gpu_status="available",
                      gpu_device_ids=[0], gpus=[dict(index=0, memory_used_mib=95, memory_total_mib=100)])
        alerts, checked = resource_monitor.detect_anomalies(sample, 901, 900)
        self.assertEqual(set(alerts), {"quiet_output", "low_disk_space", "low_host_memory", "high_device_memory"})
        self.assertTrue(set(alerts) <= checked)
        for value in alerts.values():
            self.assertTrue(value["observed"])
            self.assertTrue(value["threshold"])
            self.assertTrue(value["hint"])

    def test_gpu_partial_or_non_finite_sampling_is_reported(self):
        completed = subprocess.CompletedProcess([], 0, "1, nan, 10, 100\n", "")
        with mock.patch.object(resource_monitor.shutil, "which", return_value="nvidia-smi"), mock.patch.object(resource_monitor.subprocess, "run", return_value=completed):
            values, state = resource_monitor.gpu_counters([1, 2])
        self.assertEqual(state, "partially_unavailable")
        self.assertIsNone(values[0]["utilization_percent"])
        json.dumps(values, allow_nan=False)

    def test_missing_counters_remain_unknown_and_raise_availability_warning(self):
        with mock.patch.object(resource_monitor, "windows_counters", return_value={}), \
             mock.patch.object(resource_monitor, "linux_counters", return_value={}), \
             mock.patch.object(resource_monitor.shutil, "disk_usage", side_effect=OSError("synthetic counter access failure")):
            sample = resource_monitor.Sampler(PROJECT).sample(os.getpid())
        alerts, checked = resource_monitor.detect_anomalies(sample, None, 900)
        self.assertIsNone(sample["disk_free_bytes"])
        self.assertEqual(sample["counter_status"], "partially_unavailable")
        self.assertIn("counter_unavailable", alerts)
        self.assertNotIn("low_disk_space", checked)
        self.assertNotIn("quiet_output", checked)

    def test_anomaly_journal_records_raised_and_recovered_conditions_once(self):
        guard, child, stdout, stderr = self.guard()
        healthy = self.healthy_sample()
        low = dict(healthy, disk_free_bytes=1)
        with mock.patch.object(guard.sampler, "sample", side_effect=[low, healthy, healthy]), \
             mock.patch.object(runtime.time, "monotonic", return_value=100) as clock:
            guard.tick(child, stdout, stderr)
            clock.return_value = 101
            guard.tick(child, stdout, stderr)
            clock.return_value = 102
            guard.tick(child, stdout, stderr)
        rows = [json.loads(line) for line in (guard.directory / "anomalies.jsonl").read_text().splitlines()]
        self.assertEqual([(row["code"], row["change"]) for row in rows], [("low_disk_space", "raised"), ("low_disk_space", "resolved")])
        self.assertEqual(rows[1]["observed"]["disk_free_bytes"], healthy["disk_free_bytes"])
        self.assertEqual(guard.record["active_alerts"], [])
        events = runtime.events(guard.root)
        self.assertEqual([event["kind"] for event in events], ["warning", "resolved"])
        self.assertEqual(events[0]["alert"]["observed"]["disk_free_bytes"], 1)

    def test_unknown_sample_does_not_claim_resource_recovery(self):
        guard, child, stdout, stderr = self.guard()
        low = dict(self.healthy_sample(), disk_free_bytes=1)
        unknown = dict(self.healthy_sample(), disk_free_bytes=None, counter_status="partially_unavailable",
                       unavailable_counters=["disk_free_bytes"])
        with mock.patch.object(guard.sampler, "sample", side_effect=[low, unknown]), \
             mock.patch.object(runtime.time, "monotonic", return_value=100) as clock:
            guard.tick(child, stdout, stderr)
            clock.return_value = 101
            guard.tick(child, stdout, stderr)
        self.assertTrue(guard.alerts["low_disk_space"]["evidence_stale"])
        self.assertIn("counter_unavailable", guard.alerts)
        self.assertFalse(any(event["kind"] == "resolved" for event in runtime.events(guard.root)))

    def test_retrying_event_output_does_not_duplicate_committed_anomaly_records(self):
        guard, child, stdout, stderr = self.guard()
        low = dict(self.healthy_sample(), disk_free_bytes=1)
        with mock.patch.object(guard.sampler, "sample", return_value=low), \
             mock.patch.object(runtime.time, "monotonic", return_value=100) as clock:
            with mock.patch.object(runtime, "emit", side_effect=OSError("synthetic event write failure")):
                guard.tick(child, stdout, stderr)
            self.assertEqual(len(guard.pending_alert_records), 1)
            clock.return_value = 101
            guard.tick(child, stdout, stderr)
            clock.return_value = 102
            guard.tick(child, stdout, stderr)
        journal = [json.loads(line) for line in (guard.directory / "anomalies.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["code"] == "low_disk_space" for row in journal), 1)
        self.assertEqual(sum(event.get("alert", {}).get("code") == "low_disk_space" for event in runtime.events(guard.root)), 1)
        self.assertEqual(guard.pending_alert_records, [])

    def test_sampling_failure_records_warning_and_does_not_stop_observed_work(self):
        guard, child, stdout, stderr = self.guard()
        with mock.patch.object(guard.sampler, "sample", side_effect=RuntimeError("synthetic sampling failure")), \
             mock.patch.object(runtime.runner, "stop_child") as stop:
            guard.tick(child, stdout, stderr)
        stop.assert_not_called()
        self.assertEqual(guard.record["status"], "RUNNING")
        self.assertIn("counter_unavailable", guard.alerts)
        self.assertTrue((guard.directory / "anomalies.jsonl").is_file())

    def test_telemetry_write_failure_is_reported_without_stopping_work(self):
        guard, child, stdout, stderr = self.guard()
        real_open = Path.open
        def denied_telemetry(path, *args, **kwargs):
            if path.name == "resources.jsonl":
                raise PermissionError("synthetic telemetry write denial")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(guard.sampler, "sample", return_value=self.healthy_sample()), \
             mock.patch.object(Path, "open", new=denied_telemetry), \
             mock.patch.object(runtime.runner, "stop_child") as stop:
            guard.tick(child, stdout, stderr)
        stop.assert_not_called()
        self.assertIn("monitor_record_unavailable", guard.alerts)
        self.assertEqual(guard.record["monitoring_errors"], {"resources": "PermissionError"})

    def test_alert_query_is_read_only_and_reports_stale_heartbeat(self):
        guard, _, _, _ = self.guard()
        guard.record["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        guard.save()
        before = {path.relative_to(guard.root): path.read_bytes() for path in guard.root.rglob("*") if path.is_file()}
        result = io.StringIO()
        with redirect_stdout(result):
            runtime.dispatch(argparse.Namespace(action="alerts", id="synthetic"), guard.root, guard.root / "experiment-queue.csv", ["opencode"])
        after = {path.relative_to(guard.root): path.read_bytes() for path in guard.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertIn("heartbeat_stale", {value["code"] for value in json.loads(result.getvalue())["alerts"]})

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
