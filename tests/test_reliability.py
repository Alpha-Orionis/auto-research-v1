"""Six reported bugs: offline regression and a complete bounded-loop integration."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.error import URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_experiment_runner as fixtures
import task_runtime as runtime
import main_bridge as bridge
import resource_monitor as resources
from evidence_bundle import validate_review
from gpu_reservation import reserve

runner = runtime.runner


class FakeAPI:
    def __init__(self, idle=True):
        self.is_idle, self.messages, self.calls = idle, set(), []

    def idle(self):
        return self.is_idle

    def has_message(self, identity):
        return identity in self.messages

    def send(self, identity, text):
        self.calls.append((identity, text))
        self.messages.add(identity)


class ReliabilityChecks(unittest.TestCase):
    def setUp(self):
        fixtures.RunnerChecks.setUp(self)
        self.addCleanup(runtime.reap_workers)

    def run_one(self):
        return fixtures.RunnerChecks.run_one(self)

    def save_queue(self):
        runner.write_queue(self.queue, self.fields, self.rows)

    def config(self):
        (self.root / "project-contract.md").write_text("Frozen synthetic scope; two CPU experiments.")
        config = dict(enabled=False, server_url="http://127.0.0.1:4096", session_id="ses_synthetic",
                      approval_reference="synthetic batch", allowed_ids=["exp-one", "exp-two"],
                      max_experiments=2, max_elapsed_minutes=10, max_total_experiment_minutes=2,
                      poll_seconds=1, opencode_command=self.prefix)
        runner.atomic_json(self.root / "bridge.json", config)
        bridge.seal(self.root, "bridge.json", "synthetic bounded approval")
        return bridge.load_config(self.root, "bridge.json")

    def completion(self):
        self.run_one()
        directory = runtime.job_path(self.root, "exp-one")
        runner.atomic_json(directory / "status.json", dict(id="exp-one", kind="experiment", status="SUCCEEDED", gpu_ids=[]))
        runner.atomic_json(directory / "config.json", dict(kind="experiment", experiment_id="exp-one"))
        runtime.emit(self.root, "exp-one", "completed", "Synthetic completion")
        return runtime.events(self.root)[0]

    def decide(self, next_id=None):
        argv = ["--project", str(self.root), "decide", "--id", "exp-one", "--assessment", "ACCEPTED",
                "--rationale", "Synthetic evidence inspected"]
        if next_id:
            argv += ["--next-id", next_id]
        runner.main(argv)

    def test_worker_merge_preserves_new_rows_and_unrelated_edits(self):
        stale_fields, stale_rows = copy.deepcopy(self.fields), copy.deepcopy(self.rows)
        runner.upsert_queue(self.queue, dict(id="exp-three", order="3", status="IDEA", notes="new hypothesis"))
        runner.upsert_queue(self.queue, dict(id="exp-two", notes="scientist edit"))
        runner.update_queue(self.queue, stale_fields, stale_rows, "exp-one", dict(status="RUNNING"))
        _, rows = runner.read_queue(self.queue)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["notes"], "scientist edit")
        self.assertEqual(rows[2]["notes"], "new hypothesis")

    def test_concurrent_queue_writers_preserve_all_ideas(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: runner.upsert_queue(self.queue, dict(id=f"idea-{number}", order=str(number+3), status="IDEA")), range(8)))
        self.assertEqual(len(runner.read_queue(self.queue)[1]), 10)

    def test_failed_reviewed_dependency_is_not_admitted(self):
        self.rows[1]["depends_on"] = "exp-one"
        self.state["experiments"]["exp-one"] = dict(status="REVIEWED", outcome="FAILED", artifacts_valid=True,
                                                     decision=dict(assessment="ACCEPTED"))
        with self.assertRaises(runner.RunnerError):
            runner.check_dependencies(self.rows[1], self.rows, self.state)
        self.rows[1]["dependency_policy"] = "reviewed"
        runner.check_dependencies(self.rows[1], self.rows, self.state)

    def test_successful_dependency_needs_artifacts_and_main_acceptance(self):
        self.rows[1]["depends_on"] = "exp-one"
        record = dict(status="REVIEWED", outcome="SUCCEEDED", artifacts_valid=False)
        self.state["experiments"]["exp-one"] = record
        with self.assertRaises(runner.RunnerError):
            runner.check_dependencies(self.rows[1], self.rows, self.state)
        record.update(artifacts_valid=True, decision=dict(assessment="ACCEPTED"))
        runner.check_dependencies(self.rows[1], self.rows, self.state)

    def test_review_promise_with_final_stop_is_rejected(self):
        path = self.root / "promise.jsonl"
        path.write_text(json.dumps(dict(type="text", part=dict(text="I will review the results.")))+"\n"+
                        json.dumps(dict(type="step_finish", part=dict(reason="stop"))))
        with self.assertRaises(runner.RunnerError):
            runner.parse_review_events(path)

    def test_review_schema_rejects_wrong_identity_missing_checks_and_outside_evidence(self):
        self.run_one()
        report = json.loads((self.root / ".research/runs/exp-one/review-report.json").read_text())
        for change in (dict(experiment_id="another"), dict(constraints={}), dict(missing_evidence=["missing baseline"]),
                       dict(next_options=[]), dict(evidence=[dict(path="secret.txt", finding="outside scope")])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_review("REVIEW_REPORT "+json.dumps(dict(report, **change)), "exp-one")

    def test_reviewed_without_main_decision_cannot_advance_queue(self):
        self.run_one()
        _, rows = runner.read_queue(self.queue)
        with self.assertRaises(runner.RunnerError):
            runner.check_queue_order(rows[1], rows, self.state)
        self.decide()
        state = runner.read_state(self.state_path)
        runner.check_queue_order(rows[1], rows, state)
        self.assertTrue((self.root / ".research/runs/exp-one/decision.json").is_file())

    def test_actual_configuration_command_metrics_and_hashes_are_snapshotted(self):
        (self.root / "config.json").write_text('{"batch":4}')
        (self.root / "baseline.json").write_text('{"score":1}')
        self.rows[0].update(evidence_files_json='["config.json"]', artifact_paths_json='["metrics.json"]',
                            metrics_path="metrics.json", baseline_metrics_path="baseline.json", seed="123",
                            evidence_review_approved="true", command_json=json.dumps([sys.executable, "-c",
                            "from pathlib import Path; Path('metrics.json').write_text('{\"score\":2}')"]))
        self.save_queue()
        record = self.run_one()
        execution = json.loads((self.root / record["execution_evidence_reference"]).read_text())
        results = json.loads((self.root / record["results_evidence_reference"]).read_text())
        self.assertEqual(execution["command"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(execution["seed"], "123")
        self.assertEqual(results["comparison"]["score"]["delta"], 1)
        self.assertTrue(results["artifacts_valid"])
        (self.root / "config.json").write_text('{"batch":99}')
        snapshot = self.root / execution["input_snapshots"][0]["snapshot"]
        self.assertEqual(json.loads(snapshot.read_text())["batch"], 4)
        self.assertNotIn("PATH", execution["environment"])

    def test_stale_artifact_is_not_valid(self):
        (self.root / "stale.json").write_text('{"score":1}')
        self.rows[0]["artifact_paths_json"] = '["stale.json"]'
        self.save_queue()
        self.assertFalse(self.run_one()["artifacts_valid"])
        with self.assertRaises(runner.RunnerError):
            self.decide()

    def test_changed_approval_during_preflight_never_starts_workload(self):
        self.save_queue()
        def change(*unused):
            runner.upsert_queue(self.queue, dict(id="exp-one", approval_reference="revoked"))
            return self.prefix
        with mock.patch.object(fixtures.runner, "reviewer_command", side_effect=change), self.assertRaises(fixtures.runner.RunnerError):
            self.run_one()
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.root / "review-call-count.txt").exists())

    def test_evidence_scope_cannot_escape_or_snapshot_environment_secrets(self):
        self.rows[0]["environment_keys_json"] = '["OPENAI_API_KEY"]'
        with self.assertRaises(fixtures.runner.RunnerError):
            self.run_one()

    def test_timeout_stops_workload_descendants(self):
        self.check_descendants("timeout")

    def test_cancellation_stops_workload_descendants(self):
        self.check_descendants("cancel")

    def test_parent_exit_with_live_descendants_is_not_success(self):
        self.check_descendants("parent_exit")

    def check_descendants(self, mode):
        # Self-bounded descendant also guarantees cleanup if the regression
        # reappears. Never touch any user or GPU process in these tests.
        script = "import os,time; from pathlib import Path; Path('descendant.pid').write_text(str(os.getpid())); time.sleep(4)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"+repr(script)+"]); time.sleep("+("0.2" if mode == "parent_exit" else "4")+")"
        old = runner.SUPERVISION_OBSERVER
        if mode == "cancel":
            def cancel(child, *unused):
                if (self.root / "descendant.pid").exists():
                    raise runner.ProcessCancelled("synthetic cancellation")
            runner.SUPERVISION_OBSERVER = cancel
        try:
            if mode == "cancel":
                with self.assertRaises(runner.ProcessCancelled):
                    runner.supervise([sys.executable, "-c", parent], self.root, self.root/"out", self.root/"err", 3, lambda pid: None)
            else:
                code, timeout = runner.supervise([sys.executable, "-c", parent], self.root, self.root/"out", self.root/"err", 1, lambda pid: None)
                self.assertEqual(timeout, mode == "timeout")
                if mode == "parent_exit":
                    self.assertEqual(code, 125)
            path = self.root / "descendant.pid"
            self.assertTrue(path.is_file(), "The synthetic descendant actually started")
            self.assertFalse(runner.pid_is_alive(int(path.read_text())))
        finally:
            runner.SUPERVISION_OBSERVER = old

    def test_gpu_reservations_use_one_lock_across_task_types(self):
        with reserve(self.root, [0], runner.process_lock, runner.contained_path):
            with self.assertRaises(runner.RunnerError):
                with reserve(self.root, [0], runner.process_lock, runner.contained_path):
                    self.fail("GPU conflict was admitted")
        with reserve(self.root, [0], runner.process_lock, runner.contained_path):
            pass

    def test_generic_gpu_task_cannot_bypass_experiment_admission(self):
        record = dict(id="existing", status="RUNNING", kind="experiment", gpu_ids=[0])
        config = dict(kind="command", monitor=dict(gpu_ids=[0]), parent_id=None)
        with mock.patch.object(runtime, "list_jobs", return_value=[record]), self.assertRaises(runner.RunnerError):
            runtime.launch(self.root, "overlap", config, 4)
        self.assertFalse(runtime.job_path(self.root, "overlap").exists())

    def test_busy_main_is_not_woken(self):
        config = self.config()
        self.completion()
        api = FakeAPI(idle=False)
        self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DEFERRED")
        self.assertFalse(api.calls)

    def test_completion_delivery_is_deduplicated_and_requires_decision_ack(self):
        config = self.config()
        event = self.completion()
        api = FakeAPI()
        self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DELIVERED")
        self.assertIn("RESOURCE_REPORT", api.calls[0][1])
        self.assertEqual(bridge.cycle(self.root, config, api)["status"], "WAITING_FOR_ACK")
        self.assertEqual(len(api.calls), 1)
        with self.assertRaises(runner.RunnerError):
            bridge.acknowledge(self.root, event["event_id"], "exp-one")
        self.decide()
        bridge.acknowledge(self.root, event["event_id"], "exp-one")

    def test_ambiguous_delivery_is_not_blindly_duplicated(self):
        config = self.config()
        self.completion()
        api = FakeAPI()
        with mock.patch.object(api, "send", side_effect=URLError("synthetic timeout")) as send:
            self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DELIVERY_UNKNOWN")
            self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DELIVERY_UNKNOWN")
            self.assertEqual(send.call_count, 1)

    def test_confirmed_absence_allows_bounded_same_message_retry(self):
        config = self.config()
        event = self.completion()
        api = FakeAPI()
        with mock.patch.object(api, "send", side_effect=URLError("synthetic timeout")):
            bridge.cycle(self.root, config, api)
        bridge.cycle(self.root, config, api)
        path = self.root / f".research/bridge/receipts/{event['event_id']}.json"
        receipt = runtime.read_json(path)
        original_id = receipt["message_id"]
        receipt["missing_since_epoch"] = time.time()-31
        runner.atomic_json(path, receipt)
        self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DELIVERED")
        self.assertEqual(api.calls[0][0], original_id)
        self.assertEqual(runtime.read_json(path)["attempts"], 2)

    def test_completion_after_first_event_page_is_not_lost(self):
        config = self.config()
        for number in range(101):
            runtime.emit(self.root, "synthetic", "started", str(number))
        self.completion()
        api = FakeAPI()
        self.assertEqual(bridge.cycle(self.root, config, api)["status"], "DELIVERED")
        self.assertEqual(len(api.calls), 1)

    def test_frozen_contract_queue_and_stop_file_are_enforced(self):
        config = self.config()
        (self.root / "project-contract.md").write_text("changed constraints")
        with self.assertRaises(runner.RunnerError):
            bridge.advance(self.root, config)
        config = self.config()
        runner.upsert_queue(self.queue, dict(id="exp-two", command_json='["changed"]'))
        with self.assertRaises(runner.RunnerError):
            bridge.advance(self.root, config)
        (self.root / ".research/STOP").touch()
        with self.assertRaises(runner.RunnerError):
            bridge.advance(self.root, config)

    def test_count_time_and_wall_clock_budgets_are_enforced(self):
        config = self.config()
        self.completion()
        self.decide("exp-two")
        for limits in (dict(max_experiments=1), dict(max_total_experiment_minutes=1)):
            with self.subTest(limits=limits), self.assertRaises(runner.RunnerError):
                bridge.advance(self.root, dict(config, **limits))
        path, state = bridge.loop_state(self.root)
        state["started_at_epoch"] = time.time()-10000
        runner.atomic_json(path, state)
        with self.assertRaises(runner.RunnerError):
            bridge.advance(self.root, config)

    def test_end_to_end_completion_main_decision_ack_next_background_run(self):
        config = self.config()
        event = self.completion()
        api = FakeAPI()
        bridge.cycle(self.root, config, api)
        self.decide("exp-two")
        bridge.acknowledge(self.root, event["event_id"], "exp-one")
        try:
            self.assertEqual(bridge.cycle(self.root, config, api)["status"], "SUBMITTED")
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                status = runtime.snapshot(self.root, "exp-two")
                if status["status"] in runtime.TERMINAL and not runner.pid_is_alive(status.get("worker_pid")):
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "SUCCEEDED")
            state = runner.read_state(self.state_path)
            self.assertEqual(state["experiments"]["exp-two"]["status"], "REVIEWED")
            self.assertEqual(bridge.advance(self.root, config)["status"], "ALREADY_SUBMITTED")
        finally:
            directory = runtime.job_path(self.root, "exp-two")
            if directory.is_dir():
                status = runtime.snapshot(self.root, "exp-two")
                if status["status"] not in runtime.TERMINAL:
                    runner.atomic_json(directory / "cancel.json", dict(requested_at=runner.utc_now()))
                deadline = time.monotonic()+5
                while runner.pid_is_alive(status.get("worker_pid")) and time.monotonic() < deadline:
                    time.sleep(0.05)

    def test_bridge_rejects_external_servers_and_wrong_work_dir(self):
        config = self.config()
        runner.atomic_json(self.root / "bridge.json", dict(config, server_url="https://external.example"))
        with self.assertRaises(runner.RunnerError):
            bridge.load_config(self.root, "bridge.json")
        api = bridge.API(self.root, config)
        with mock.patch.object(api, "request", return_value=dict(directory=str(self.root.parent))), self.assertRaises(runner.RunnerError):
            api.idle()

    def test_http_session_status_prompt_and_main_response_contract(self):
        config = self.config()
        event = self.completion()
        root, messages, posts = self.root, {}, []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, body, status=200):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/session/ses_synthetic":
                    self.reply(dict(directory=str(root)))
                elif path == "/session/status":
                    self.reply(dict(ses_synthetic=dict(type="idle")))
                elif path == "/session/ses_synthetic/message":
                    self.reply([dict(info=dict(id="response", role="assistant", parentID=identity, time=dict(completed=1)),
                                     parts=[dict(type="text", text="RESOURCE_REPORT synthetic proof")]) for identity in messages])
                elif path.rsplit("/", 1)[-1] in messages:
                    self.reply(messages[path.rsplit("/", 1)[-1]])
                else:
                    self.reply({}, 404)
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append(value)
                messages[value["messageID"]] = value
                self.reply(None, 204)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config["server_url"] = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(bridge.cycle(root, config)["status"], "DELIVERED")
            self.assertEqual(bridge.cycle(root, config)["status"], "WAITING_FOR_ACK")
            self.assertEqual(len(posts), 1)
            journal = (root / ".research/bridge/loop.jsonl").read_text()
            self.assertIn('"kind": "main_response"', journal)
            self.assertIn("RESOURCE_REPORT synthetic proof", journal)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class WindowChecks(unittest.TestCase):
    def sample(self, values=(10,), owned=True, available=True):
        return dict(gpu_status="available" if available else "unavailable",
                    gpus=[dict(index=index, utilization_percent=value) for index, value in enumerate(values)],
                    gpu_ownership_status="available" if owned else "unknown",
                    project_gpu_ids=list(range(len(values))) if owned else [])

    def test_time_weighted_per_device_and_overall_means(self):
        window = resources.GPUWindow([0, 1], seconds=30, consecutive=2, max_gap=15)
        for now, values in ((0,(10,40)), (10,(20,50)), (20,(40,60)), (30,(60,70))):
            status = window.update(self.sample(values), now)
        self.assertTrue(status["window_mature"])
        self.assertEqual(status["per_gpu_average_percent"], {"0":23.33, "1":50.0})
        self.assertEqual(status["average_gpu_utilization_percent"], 36.67)
        self.assertFalse(status["alert_ready"])

    def test_bad_windows_are_nonoverlapping_not_every_sample(self):
        window = resources.GPUWindow([0], seconds=30, threshold=30, consecutive=2, max_gap=15)
        for now in range(0,61,5):
            status = window.update(self.sample(), now)
            if now == 35:
                self.assertEqual(status["consecutive_bad_windows"], 1)
        self.assertTrue(status["alert_ready"])
        alerts, _ = resources.detect_anomalies(dict(gpu_window=status), None, 900)
        self.assertIn("low_gpu_utilization", alerts)

    def test_missing_samples_reset_window_instead_of_filling_zero(self):
        window = resources.GPUWindow([0], seconds=30, max_gap=15)
        for now in (0,10,20,30):
            window.update(self.sample(), now)
        status = window.update(self.sample(available=False), 40)
        self.assertFalse(status["window_mature"])
        self.assertIsNone(status["average_gpu_utilization_percent"])
        self.assertFalse(status["alert_ready"])
        self.assertEqual(status["consecutive_bad_windows"], 0)

    def test_unowned_or_idle_gpu_does_not_establish_under_load_failure(self):
        window = resources.GPUWindow([0], seconds=30, consecutive=1, max_gap=15)
        for now in (0,10,20,30):
            status = window.update(self.sample(owned=False), now)
        self.assertTrue(status["window_mature"])
        self.assertFalse(status["workload_present"])
        self.assertFalse(status["alert_ready"])

    def test_sampling_gap_requires_new_mature_window(self):
        window = resources.GPUWindow([0], seconds=30, max_gap=15)
        for now in (0,10,20,30,60):
            status = window.update(self.sample(), now)
        self.assertFalse(status["window_mature"])

    def test_compute_ownership_uses_descendants_not_command_names(self):
        output = subprocess.CompletedProcess([], 0, "GPU-synthetic, 101, 500\nGPU-synthetic, 999, 600\n", "")
        with mock.patch.object(resources, "process_members", return_value=[100,101]), \
             mock.patch.object(resources.shutil, "which", return_value="nvidia-smi"), \
             mock.patch.object(resources.subprocess, "run", return_value=output):
            processes, status, _ = resources.gpu_ownership([dict(index=0, uuid="GPU-synthetic")], 100)
        self.assertEqual(status, "available")
        self.assertEqual([item["owned"] for item in processes], [True, False])


if __name__ == "__main__":
    unittest.main()
