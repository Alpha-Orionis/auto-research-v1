"""Offline checks for the minimal synchronous ZCode backend; no model or network."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("zcode_runner", PROJECT / "zcode" / "zcode_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

SUCCESS_COMMAND = json.dumps([sys.executable, "-c", "print('score=1')"])
FAILURE_COMMAND = json.dumps([sys.executable, "-c", "import sys; sys.exit(3)"])
SLEEP_COMMAND = json.dumps([sys.executable, "-c", "import time; time.sleep(30)"])


class ZcodeRunnerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zcode-runner-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        with (PROJECT / "templates" / "experiment-queue.template.csv").open(encoding="utf-8") as handle:
            self.fields = next(csv.reader(handle))
        self.rows = []

    def queue_path(self) -> Path:
        return self.root / "experiment-queue.csv"

    def write_queue(self, rows=None) -> None:
        with self.queue_path().open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields)
            writer.writeheader()
            writer.writerows(rows if rows is not None else self.rows)

    def read_rows(self):
        with self.queue_path().open(encoding="utf-8", newline="") as handle:
            return {row["id"]: row for row in csv.DictReader(handle)}

    def add_row(self, experiment_id, order="1", status="APPROVED", **overrides):
        row = dict.fromkeys(self.fields, "")
        row.update(
            id=experiment_id, order=order, status=status,
            hypothesis="A generic metric can be measured.", primary_metric="score",
            command_json=SUCCESS_COMMAND, working_directory=".",
            approval_reference="test authorization",
            review_data_scope="manifest and logs",
        )
        row.update(overrides)
        self.rows.append(row)
        return row

    def invoke(self, *argv):
        return runner.main(list(argv))

    def refuse(self, *argv):
        with self.assertRaises(SystemExit) as caught:
            self.invoke(*argv)
        self.assertNotEqual(str(caught.exception), "0")
        return str(caught.exception)

    def mark_reviewed(self, experiment_id, assessment="VALID"):
        """Simulate Main completing the review step."""
        run_dir = self.root / ".research" / "runs" / experiment_id
        report = {
            "schema_version": 1, "experiment_id": experiment_id,
            "assessment": assessment,
            "correctness": {"verdict": "PASS", "reason": "synthetic"},
            "constraints": {"verdict": "PASS", "reason": "synthetic"},
            "evidence": [{"path": f".research/runs/{experiment_id}/stdout.log", "finding": "inspected"}],
            "missing_evidence": [],
            "next_options": [{"priority": 1, "action": "controlled next test", "rationale": "compare"}],
        }
        (run_dir / "review-report.json").write_text(json.dumps(report), encoding="utf-8")
        (run_dir / "review.md").write_text("# REVIEW_REPORT\nsynthetic review\n", encoding="utf-8")
        rows = self.read_rows()
        rows[experiment_id]["status"] = "REVIEWED"
        rows[experiment_id]["review_result_reference"] = f".research/runs/{experiment_id}/review-report.json"
        self.write_queue(list(rows.values()))

    def test_refuses_rows_that_are_not_approved(self):
        self.add_row("exp-one", status="READY")
        self.write_queue()
        message = self.refuse("run", "--id", "exp-one", "--root", str(self.root))
        self.assertIn("APPROVED", message)

    def test_refuses_approved_row_without_reference(self):
        self.add_row("exp-one", approval_reference="")
        self.write_queue()
        self.refuse("run", "--id", "exp-one", "--root", str(self.root))

    def test_refuses_unknown_id(self):
        self.add_row("exp-one")
        self.write_queue()
        self.refuse("run", "--id", "exp-missing", "--root", str(self.root))

    def test_successful_run_records_artifacts(self):
        self.add_row("exp-one", evidence_files_json=json.dumps(["plan.md"]))
        (self.root / "plan.md").write_text("frozen plan", encoding="utf-8")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))

        rows = self.read_rows()
        self.assertEqual(rows["exp-one"]["status"], "REVIEW_PENDING")
        self.assertEqual(rows["exp-one"]["outcome"], "SUCCEEDED")
        self.assertEqual(rows["exp-one"]["result_reference"], ".research/runs/exp-one/manifest.json")
        self.assertIn("outcome=SUCCEEDED", rows["exp-one"]["notes"])
        self.assertEqual(rows["exp-one"]["hypothesis"], "A generic metric can be measured.")

        run_dir = self.root / ".research" / "runs" / "exp-one"
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["backend"], "zcode")
        self.assertEqual(manifest["outcome"], "SUCCEEDED")
        self.assertEqual(manifest["return_code"], 0)
        self.assertEqual(len(manifest["frozen_evidence"]), 1)
        self.assertEqual(manifest["frozen_evidence"][0]["path"], "plan.md")
        self.assertEqual(len(manifest["frozen_evidence"][0]["sha256"]), 64)
        self.assertIn("score=1", (run_dir / "stdout.log").read_text(encoding="utf-8"))

    def test_failed_run_stays_in_the_denominator(self):
        self.add_row("exp-one", command_json=FAILURE_COMMAND)
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        rows = self.read_rows()
        self.assertEqual(rows["exp-one"]["status"], "REVIEW_PENDING")
        self.assertEqual(rows["exp-one"]["outcome"], "FAILED")
        manifest = json.loads((self.root / ".research/runs/exp-one/manifest.json").read_text())
        self.assertEqual(manifest["return_code"], 3)

    def test_timeout_marks_interrupted(self):
        self.add_row("exp-one", command_json=SLEEP_COMMAND, time_limit_minutes="0.02")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        rows = self.read_rows()
        self.assertEqual(rows["exp-one"]["status"], "INTERRUPTED")
        self.assertEqual(rows["exp-one"]["outcome"], "INTERRUPTED")
        manifest = json.loads((self.root / ".research/runs/exp-one/manifest.json").read_text())
        self.assertIsNone(manifest["return_code"])

    def test_ids_are_single_use(self):
        self.add_row("exp-one")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        message = self.refuse("run", "--id", "exp-one", "--root", str(self.root))
        self.assertIn("already used", message)

    def test_experiment_lock_enforces_serial_runs(self):
        self.add_row("exp-one")
        self.write_queue()
        lock = self.root / ".research" / "lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("experiment lock pid=1 at=now", encoding="utf-8")
        message = self.refuse("run", "--id", "exp-one", "--root", str(self.root))
        self.assertIn("experiment lock", message)
        self.assertTrue(lock.exists(), "a foreign lock must not be removed by the refusal")

    def test_working_directory_cannot_leave_the_project(self):
        outside = self.root.parent / "zcode-outside-check"
        outside.mkdir(exist_ok=True)
        self.addCleanup(outside.rmdir)
        self.add_row("exp-one", working_directory=str(outside))
        self.write_queue()
        self.refuse("run", "--id", "exp-one", "--root", str(self.root))

    def test_default_dependency_needs_accepted_success(self):
        self.add_row("exp-dep", order="1")
        self.add_row("exp-two", order="2", depends_on="exp-dep")
        self.write_queue()
        message = self.refuse("run", "--id", "exp-two", "--root", str(self.root))
        self.assertIn("no recorded run", message)

        self.invoke("run", "--id", "exp-dep", "--root", str(self.root))
        message = self.refuse("run", "--id", "exp-two", "--root", str(self.root))
        self.assertIn("lacks a recorded Main decision", message)

        self.mark_reviewed("exp-dep")
        self.invoke("decide", "--id", "exp-dep", "--assessment", "ACCEPTED",
                    "--rationale", "criterion met", "--next-id", "exp-two",
                    "--root", str(self.root))
        self.invoke("run", "--id", "exp-two", "--root", str(self.root))
        self.assertEqual(self.read_rows()["exp-two"]["outcome"], "SUCCEEDED")

    def test_reviewed_policy_allows_reviewed_failure(self):
        self.add_row("exp-dep", order="1", command_json=FAILURE_COMMAND)
        self.add_row("exp-two", order="2", depends_on="exp-dep", dependency_policy="reviewed")
        self.write_queue()
        self.invoke("run", "--id", "exp-dep", "--root", str(self.root))
        self.mark_reviewed("exp-dep", assessment="INVALID")
        self.invoke("decide", "--id", "exp-dep", "--assessment", "REJECTED",
                    "--rationale", "falsified", "--root", str(self.root))
        self.invoke("run", "--id", "exp-two", "--root", str(self.root))
        self.assertEqual(self.read_rows()["exp-two"]["outcome"], "SUCCEEDED")

    def test_earlier_order_needs_a_decision(self):
        self.add_row("exp-one", order="1")
        self.add_row("exp-two", order="2")
        self.write_queue()
        message = self.refuse("run", "--id", "exp-two", "--root", str(self.root))
        self.assertIn("earlier queue item", message)

    def test_decide_guards_acceptance(self):
        self.add_row("exp-ok", order="1")
        self.add_row("exp-bad", order="2", command_json=FAILURE_COMMAND)
        self.write_queue()

        self.invoke("run", "--id", "exp-ok", "--root", str(self.root))
        message = self.refuse("decide", "--id", "exp-ok", "--assessment", "ACCEPTED",
                              "--rationale", "no review yet", "--root", str(self.root))
        self.assertIn("review", message)

        self.mark_reviewed("exp-ok")
        self.invoke("decide", "--id", "exp-ok", "--assessment", "ACCEPTED",
                    "--rationale", "criterion met", "--root", str(self.root))

        self.invoke("run", "--id", "exp-bad", "--root", str(self.root))
        self.mark_reviewed("exp-bad", assessment="INVALID")
        message = self.refuse("decide", "--id", "exp-bad", "--assessment", "ACCEPTED",
                              "--rationale", "must not accept failures", "--root", str(self.root))
        self.assertIn("cannot ACCEPT", message)
        self.invoke("decide", "--id", "exp-bad", "--assessment", "REJECTED",
                    "--rationale", "falsified", "--root", str(self.root))

        decisions = {
            "exp-ok": json.loads((self.root / ".research/runs/exp-ok/decision.json").read_text()),
            "exp-bad": json.loads((self.root / ".research/runs/exp-bad/decision.json").read_text()),
        }
        self.assertEqual(decisions["exp-ok"]["assessment"], "ACCEPTED")
        self.assertEqual(decisions["exp-bad"]["assessment"], "REJECTED")
        self.assertEqual(self.read_rows()["exp-ok"]["status"], "DECIDED")
        self.assertEqual(self.read_rows()["exp-bad"]["status"], "DECIDED")

    def test_decisions_are_append_only(self):
        self.add_row("exp-one")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        self.invoke("decide", "--id", "exp-one", "--assessment", "INCONCLUSIVE",
                    "--rationale", "interrupted evidence", "--root", str(self.root))
        self.refuse("decide", "--id", "exp-one", "--assessment", "REJECTED",
                    "--rationale", "changed my mind", "--root", str(self.root))

    def test_inconclusive_reconciles_without_a_review(self):
        self.add_row("exp-one", command_json=SLEEP_COMMAND, time_limit_minutes="0.02")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        self.invoke("decide", "--id", "exp-one", "--assessment", "INCONCLUSIVE",
                    "--rationale", "interrupted; redo under a new id", "--root", str(self.root))
        self.assertEqual(self.read_rows()["exp-one"]["status"], "DECIDED")

    def test_status_reports_row_manifest_and_decision(self):
        self.add_row("exp-one")
        self.write_queue()
        self.invoke("run", "--id", "exp-one", "--root", str(self.root))
        self.mark_reviewed("exp-one")
        self.invoke("decide", "--id", "exp-one", "--assessment", "ACCEPTED",
                    "--rationale", "criterion met", "--root", str(self.root))
        try:
            captured = subprocess.run(
                [sys.executable, str(PROJECT / "zcode" / "zcode_runner.py"),
                 "status", "--id", "exp-one", "--root", str(self.root)],
                capture_output=True, text=True, timeout=60, check=True,
            )
        except subprocess.CalledProcessError as error:
            self.fail(f"status failed: {error.stderr}")
        report = json.loads(captured.stdout)
        self.assertEqual(report["row"]["status"], "DECIDED")
        self.assertEqual(report["manifest"]["outcome"], "SUCCEEDED")
        self.assertEqual(report["decision"]["assessment"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
