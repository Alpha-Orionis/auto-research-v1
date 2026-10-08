#!/usr/bin/env python3
"""Minimal synchronous ZCode backend for the Auto Research experiment loop.

Consumes the same `experiment-queue.csv` schema and writes the same
`.research/runs/<id>/` artifact layout as `scripts/experiment_runner.py`,
so one project can drive the loop with either agent frontend.

Deliberately minimal: one approved experiment at a time, executed in the
foreground, no detached workers, no resource sentinel, no auto-launched
review. After a run the ZCode main session ("Main") orchestrates the
read-only reviewer subagent and records the final decision with `decide`.
See zcode/PROTOCOL.md and docs/zcode.md for the rules this enforces.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

QUEUE_NAME = "experiment-queue.csv"
RUNS_DIR = ".research/runs"
LOCK_PATH = ".research/lock"
QUEUE_LOCK = QUEUE_NAME + ".lock"

REQUIRED_COLUMNS = (
    "id", "order", "status", "depends_on", "hypothesis", "primary_metric",
    "command_json", "working_directory", "approval_reference",
    "review_data_scope", "dependency_policy", "time_limit_minutes",
    "evidence_files_json", "notes",
)
USED_STATUSES = {
    "RUNNING", "INTERRUPTED", "REVIEW_PENDING", "REVIEWING",
    "REVIEW_FAILED", "REVIEWED", "DECIDED", "SUCCEEDED", "FAILED",
}
DECIDABLE_STATUSES = {"REVIEW_PENDING", "INTERRUPTED", "REVIEWED"}
VALID_POLICIES = ("", "accepted_artifacts", "reviewed")
RUN_SCHEMA = "zcode-auto-research-run-1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fail(message: str) -> None:
    raise SystemExit(f"zcode_runner: {message}")


@contextmanager
def exclusive_lock(path: Path, kind: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            holder = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            holder = "<unreadable>"
        age = time.time() - path.stat().st_mtime if path.exists() else -1.0
        fail(
            f"another holder owns the {kind}: {holder or 'unknown'} "
            f"(lock age {age:.0f}s). Verify that owner stopped and reconcile "
            "its outcome explicitly before removing the lock file by hand."
        )
    try:
        os.write(fd, f"{kind} pid={os.getpid()} at={utc_now()}".encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_queue(root: Path):
    queue = root / QUEUE_NAME
    if not queue.is_file():
        fail(f"{QUEUE_NAME} not found under {root}")
    with queue.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            fail(f"{QUEUE_NAME} is missing columns: {missing}")
        rows = list(reader)
    ids = [row.get("id", "").strip() for row in rows]
    if len(ids) != len(set(ids)) or any(not i for i in ids):
        fail("duplicate or empty experiment id in the queue")
    return reader.fieldnames, rows


def save_queue(root: Path, fields, rows) -> None:
    queue = root / QUEUE_NAME
    with exclusive_lock(root / QUEUE_LOCK, "queue file"):
        temp = queue.with_suffix(".csv.tmp")
        with temp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp, queue)


def find_row(rows, experiment_id: str):
    for row in rows:
        if row.get("id", "").strip() == experiment_id:
            return row
    fail(f"no queue row for {experiment_id!r}")


def stamp(row: dict, text: str) -> None:
    row["notes"] = f"{row.get('notes', '')} | {text}".strip(" |")


def parse_time_limit(row: dict):
    raw = (row.get("time_limit_minutes") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        fail(f"time_limit_minutes is not a number: {raw!r}")
    if value <= 0:
        fail("time_limit_minutes must be positive when set")
    return value * 60.0


def parse_json_array(row: dict, column: str):
    raw = (row.get(column) or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        fail(f"{column} is not valid JSON")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        fail(f"{column} must be a JSON array of non-empty strings")
    return value


def contained(root: Path, relative: str, what: str) -> Path:
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        fail(f"{what} leaves the project: {relative}")
    return candidate


def dependency_check(root: Path, rows, row: dict) -> None:
    policy = (row.get("dependency_policy") or "").strip()
    if policy not in VALID_POLICIES:
        fail(f"invalid dependency_policy {policy!r} (use accepted_artifacts or reviewed)")
    for dep_id in filter(None, (d.strip() for d in (row.get("depends_on") or "").split(";"))):
        dep = next((r for r in rows if r.get("id", "").strip() == dep_id), None)
        if dep is None:
            fail(f"unknown dependency {dep_id!r}")
        if policy == "reviewed":
            if dep.get("status", "").strip() not in {"REVIEWED", "DECIDED"}:
                fail(f"dependency {dep_id!r} is {(dep.get('status') or 'UNSET')!r}; the reviewed policy needs REVIEWED")
            continue
        manifest = root / RUNS_DIR / dep_id / "manifest.json"
        if not manifest.is_file():
            fail(f"dependency {dep_id!r} has no recorded run")
        run = json.loads(manifest.read_text(encoding="utf-8"))
        if run.get("outcome") != "SUCCEEDED":
            fail(f"dependency {dep_id!r} outcome is {run.get('outcome')!r}; the default policy needs SUCCEEDED")
        decision = root / RUNS_DIR / dep_id / "decision.json"
        if dep.get("status", "").strip() != "DECIDED" or not decision.is_file():
            fail(f"dependency {dep_id!r} lacks a recorded Main decision")
        verdict = json.loads(decision.read_text(encoding="utf-8")).get("assessment")
        if verdict != "ACCEPTED":
            fail(f"dependency {dep_id!r} decision is {verdict!r}; the default policy needs ACCEPTED")


def earlier_order_check(root: Path, rows, row: dict) -> None:
    try:
        order = float((row.get("order") or "").strip())
    except ValueError:
        fail(f"order is not a number: {(row.get('order') or '')!r}")
    for other in rows:
        if other is row or (other.get("status") or "").strip() == "CANCELLED":
            continue
        try:
            other_order = float((other.get("order") or "").strip())
        except ValueError:
            continue
        if other_order >= order:
            continue
        status = (other.get("status") or "").strip()
        decided = (
            status == "DECIDED"
            and (root / RUNS_DIR / other.get("id", "").strip() / "decision.json").is_file()
        )
        if not decided:
            fail(
                f"earlier queue item {other.get('id')!r} (order {other.get('order')}) is "
                f"{status or 'UNSET'!r}; run and decide it first, or mark it CANCELLED"
            )


def load_review(root: Path, experiment_id: str) -> dict:
    report_path = root / RUNS_DIR / experiment_id / "review-report.json"
    if not report_path.is_file():
        fail(f"no review receipt at {report_path.relative_to(root).as_posix()}; Main must complete the review first")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("experiment_id") != experiment_id:
        fail("review receipt names a different experiment id")
    if not (root / RUNS_DIR / experiment_id / "review.md").is_file():
        fail("review.md is missing; an incomplete review cannot support a decision")
    return report


def command_run(root: Path, experiment_id: str) -> None:
    fields, rows = load_queue(root)
    row = find_row(rows, experiment_id)
    status = (row.get("status") or "").strip()
    if status in USED_STATUSES:
        fail(
            f"experiment id {experiment_id!r} is already used (status {status}); "
            "a retry needs a new approved row and id"
        )
    if status != "APPROVED":
        fail(f"refusing to run: status is {status or 'UNSET'!r}, need APPROVED")
    if not (row.get("approval_reference") or "").strip():
        fail("APPROVED row lacks approval_reference")
    command = parse_json_array(row, "command_json")
    if not command:
        fail("command_json must be a JSON array with the executable and its arguments")
    timeout_seconds = parse_time_limit(row)
    dependency_check(root, rows, row)
    earlier_order_check(root, rows, row)

    workdir = contained(root, (row.get("working_directory") or "").strip() or ".", "working_directory")
    if not workdir.is_dir():
        fail(f"working_directory is not a directory: {(row.get('working_directory') or '.')!r}")

    evidence = []
    for relative in parse_json_array(row, "evidence_files_json"):
        path = contained(root, relative, "evidence file")
        if not path.is_file():
            fail(f"evidence file not found: {relative}")
        evidence.append({"path": relative.replace("\\", "/"), "sha256": sha256_file(path)})

    run_dir = contained(root, f"{RUNS_DIR}/{experiment_id}", "run directory")
    with exclusive_lock(root / LOCK_PATH, "experiment lock"):
        if run_dir.exists():
            fail(f"run directory already exists; ids are one-time: {run_dir.relative_to(root).as_posix()}")
        row["status"] = "RUNNING"
        row["started_at"] = utc_now()
        save_queue(root, fields, rows)
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        timed_out = False
        return_code = None
        with (run_dir / "stdout.log").open("xb") as out, (run_dir / "stderr.log").open("xb") as err:
            try:
                completed = subprocess.run(
                    command, cwd=workdir, stdout=out, stderr=err,
                    stdin=subprocess.DEVNULL, timeout=timeout_seconds,
                )
                return_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
        elapsed = round(time.monotonic() - started, 3)
        finished = utc_now()
        outcome = "INTERRUPTED" if timed_out else ("SUCCEEDED" if return_code == 0 else "FAILED")
        manifest = {
            "schema_version": 1,
            "run_schema": RUN_SCHEMA,
            "backend": "zcode",
            "id": experiment_id,
            "row_at_start": {k: row.get(k, "") for k in fields},
            "command": command,
            "working_directory": row.get("working_directory", ""),
            "return_code": return_code,
            "outcome": outcome,
            "started_at": row["started_at"],
            "finished_at": finished,
            "elapsed_seconds": elapsed,
            "time_limit_minutes": row.get("time_limit_minutes", ""),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "frozen_evidence": evidence,
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        row["status"] = "INTERRUPTED" if timed_out else "REVIEW_PENDING"
        row["finished_at"] = finished
        row["outcome"] = outcome
        row["result_reference"] = (run_dir / "manifest.json").relative_to(root).as_posix()
        stamp(row, f"{finished} backend=zcode outcome={outcome}"
                   + (f" rc={return_code}" if return_code is not None else " rc=timeout"))
        save_queue(root, fields, rows)
        print(json.dumps({
            "id": experiment_id,
            "backend": "zcode",
            "outcome": outcome,
            "return_code": return_code,
            "elapsed_seconds": elapsed,
            "run_dir": run_dir.relative_to(root).as_posix(),
            "result_reference": row["result_reference"],
        }))


def command_decide(root: Path, experiment_id: str, assessment: str, rationale: str, next_id: str) -> None:
    fields, rows = load_queue(root)
    row = find_row(rows, experiment_id)
    status = (row.get("status") or "").strip()
    if status not in DECIDABLE_STATUSES:
        fail(
            f"cannot decide from status {status or 'UNSET'!r}; "
            f"expected one of {sorted(DECIDABLE_STATUSES)}"
        )
    manifest_path = root / RUNS_DIR / experiment_id / "manifest.json"
    if not manifest_path.is_file():
        fail("no run manifest; decide needs a recorded run")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("id") != experiment_id:
        fail("manifest names a different experiment id")

    if assessment in {"ACCEPTED", "REJECTED"}:
        report = load_review(root, experiment_id)
        if status != "REVIEWED":
            fail(f"{assessment} requires a completed recorded review first (status {status!r})")
        if assessment == "ACCEPTED":
            if manifest.get("outcome") != "SUCCEEDED":
                fail(
                    f"cannot ACCEPT outcome {manifest.get('outcome')!r}; "
                    "failed execution cannot become a valid result"
                )
            if report.get("assessment") != "VALID":
                fail(
                    f"cannot ACCEPT a {report.get('assessment')!r} review; VALID requires "
                    "passing correctness and constraints with no missing evidence"
                )

    if next_id:
        find_row(rows, next_id)

    decided_at = utc_now()
    decision = {
        "schema_version": 1,
        "id": experiment_id,
        "assessment": assessment,
        "rationale": rationale,
        "next_id": next_id or "",
        "decided_at": decided_at,
        "decided_by": "Main (ZCode session)",
        "manifest_outcome": manifest.get("outcome"),
    }
    decision_path = root / RUNS_DIR / experiment_id / "decision.json"
    if decision_path.exists():
        fail("decision.json already exists; decisions are append-only; a changed assessment needs a new experiment id")
    decision_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    row["status"] = "DECIDED"
    stamp(row, f"{decided_at} decision={assessment}")
    save_queue(root, fields, rows)
    print(json.dumps(decision, ensure_ascii=False))


def command_status(root: Path, experiment_id: str) -> None:
    fields, rows = load_queue(root)
    row = find_row(rows, experiment_id)
    result = {"id": experiment_id, "row": row}
    manifest_path = root / RUNS_DIR / experiment_id / "manifest.json"
    if manifest_path.is_file():
        result["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
    decision_path = root / RUNS_DIR / experiment_id / "decision.json"
    if decision_path.is_file():
        result["decision"] = json.loads(decision_path.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="zcode_runner.py",
        description="Minimal synchronous ZCode backend for the bounded experiment loop.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    run_p = sub.add_parser("run", help="execute one APPROVED experiment row in the foreground")
    run_p.add_argument("--id", required=True)
    run_p.add_argument("--root", default=".", help="project root that contains the queue (default: cwd)")

    decide_p = sub.add_parser("decide", help="record Main's final decision after review")
    decide_p.add_argument("--id", required=True)
    decide_p.add_argument("--assessment", required=True, choices=["ACCEPTED", "REJECTED", "INCONCLUSIVE"])
    decide_p.add_argument("--rationale", required=True)
    decide_p.add_argument("--next-id", default="")
    decide_p.add_argument("--root", default=".")

    status_p = sub.add_parser("status", help="print the queue row and recorded artifacts")
    status_p.add_argument("--id", required=True)
    status_p.add_argument("--root", default=".")

    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if args.verb == "run":
        command_run(root, args.id)
    elif args.verb == "decide":
        command_decide(root, args.id, args.assessment, args.rationale, args.next_id)
    else:
        command_status(root, args.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
