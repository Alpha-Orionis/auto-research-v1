"""Detached local workers and a non-blocking status/event interface."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import experiment_runner as runner
from resource_monitor import Sampler


TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "INTERRUPTED"}
EVENT_ID = re.compile(r"^[0-9]+-[a-f0-9]{8}$")


def local_path(root, relative):
    return runner.contained_path(root, relative, "Task artifact")


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise runner.RunnerError("Cannot read local task metadata; inspect or restore its files.") from error
    if not isinstance(value, dict):
        raise runner.RunnerError("Malformed local task metadata.")
    return value


def job_path(root, job_id):
    runner.validate_id(job_id)
    return local_path(root, f".research/tasks/{job_id}")


def snapshot(root, job_id):
    directory = job_path(root, job_id)
    record = read_json(local_path(root, f".research/tasks/{job_id}/status.json"))
    if record.get("id") != job_id:
        raise runner.RunnerError("Task identity does not match its directory.")
    record = dict(record)
    if record.get("status") not in TERMINAL:
        pid = record.get("worker_pid")
        launch = directory / "launch.json"
        if pid is None and launch.is_file():
            pid = read_json(launch).get("pid")
        if pid is not None:
            record["worker_alive"] = runner.pid_is_alive(pid)
            if not record["worker_alive"]:
                record["status"] = "ORPHANED" if runner.pid_is_alive(record.get("child_pid")) else "INTERRUPTED"
        else:
            # A missing launch receipt is uncertain, not permission to relaunch.
            record["worker_alive"] = None
        heartbeat = record.get("heartbeat_at")
        if heartbeat:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
                record["heartbeat_age_seconds"] = round(max(0, age), 1)
                record["heartbeat_stale"] = age > 45
            except (ValueError, TypeError):
                record["heartbeat_stale"] = True
    return record


def list_jobs(root):
    directory = local_path(root, ".research/tasks")
    if not directory.exists():
        return []
    jobs = []
    for path in sorted(directory.iterdir()):
        if path.is_dir() and (path / "status.json").is_file():
            jobs.append(snapshot(root, path.name))
    return jobs


@contextmanager
def event_writer_lock(root):
    # Serialize publication so an incremental reader cannot skip an older
    # event that a concurrent writer has not yet committed. Reads stay unlocked.
    path = local_path(root, ".research/events/writer.lock")
    deadline = time.monotonic() + 2
    while True:
        lock = runner.process_lock(path)
        try:
            lock.__enter__()
            break
        except (runner.RunnerError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def event_order(event_id):
    stamp, suffix = event_id.split("-", 1)
    return int(stamp), suffix


def emit(root, job_id, kind, message):
    with event_writer_lock(root):
        directory = local_path(root, ".research/events")
        latest = max((event_order(path.stem)[0] for path in directory.glob("*.json")
                      if EVENT_ID.fullmatch(path.stem)), default=0)
        # Keep cursor order monotonic even if the machine's wall clock changes.
        event_id = f"{max(time.time_ns(), latest + 1)}-{uuid.uuid4().hex[:8]}"
        runner.atomic_json(local_path(root, f".research/events/{event_id}.json"), {
            "event_id": event_id, "timestamp": runner.utc_now(), "task_id": job_id,
            "kind": kind, "message": message,
        })


def events(root, after=None, limit=20):
    if after is not None and not EVENT_ID.fullmatch(after):
        raise runner.RunnerError("Invalid event cursor.")
    if not 1 <= limit <= 100:
        raise runner.RunnerError("Event limit must be between 1 and 100.")
    directory = local_path(root, ".research/events")
    names = sorted((path.stem for path in directory.glob("*.json") if EVENT_ID.fullmatch(path.stem)), key=event_order)
    names = [name for name in names if after is None or event_order(name) > event_order(after)][:limit]
    return [read_json(local_path(root, f".research/events/{name}.json")) for name in names]


def monitor_options(args):
    if not 0.1 <= args.sample_seconds <= 3600 or args.stall_seconds <= 0:
        raise runner.RunnerError("Sampling must be 0.1-3600 seconds and the quiet-output threshold must be positive.")
    if not 1 <= args.max_background_jobs <= 32:
        raise runner.RunnerError("Background concurrency limit must be 1-32.")
    if args.gpu_ids and not re.fullmatch(r"[0-9]+(,[0-9]+)*", args.gpu_ids):
        raise runner.RunnerError("--gpu-ids must be comma-separated numeric NVIDIA device IDs.")
    gpu_ids = [int(value) for value in args.gpu_ids.split(",")] if args.gpu_ids else []
    return dict(sample_seconds=args.sample_seconds, stall_seconds=args.stall_seconds, gpu_ids=sorted(set(gpu_ids)))


def launch(root, job_id, config, limit):
    directory = job_path(root, job_id)
    with runner.process_lock(local_path(root, ".research/tasks/launch.lock")):
        if directory.exists():
            raise runner.RunnerError("Task IDs are single-use; choose a new ID.")
        active = [job for job in list_jobs(root) if job.get("status") not in TERMINAL]
        if len(active) >= limit:
            raise runner.RunnerError("Background concurrency limit reached; inspect existing tasks.")
        if config["kind"] == "experiment" and any(job.get("kind") == "experiment" for job in active):
            raise runner.RunnerError("An experiment/review worker is already active in this project.")
        parent = config.get("parent_id")
        if parent:
            snapshot(root, parent)
        directory.mkdir(parents=True, mode=0o700)
        record = {
            "version": 1, "id": job_id, "kind": config["kind"], "operation": config.get("operation"),
            "parent_id": parent, "status": "QUEUED", "phase": "starting",
            "created_at": runner.utc_now(), "heartbeat_at": None, "finished_at": None,
            "worker_pid": None, "child_pid": None, "return_code": None,
            "status_reference": f".research/tasks/{job_id}/status.json",
            "stdout_reference": f".research/tasks/{job_id}/worker.stdout.log",
            "stderr_reference": f".research/tasks/{job_id}/worker.stderr.log",
            "telemetry_reference": f".research/tasks/{job_id}/resources.jsonl",
        }
        runner.atomic_json(directory / "config.json", config)
        runner.atomic_json(directory / "status.json", record)
        command = [sys.executable, str(Path(__file__).resolve()), "--project", str(root), "--id", job_id]
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        worker = None
        try:
            with (directory / "worker.stdout.log").open("wb") as stdout, (directory / "worker.stderr.log").open("wb") as stderr:
                worker = subprocess.Popen(
                    command, cwd=root, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    shell=False, close_fds=True, start_new_session=os.name != "nt", creationflags=flags,
                )
            # Parent and worker write different files to avoid a startup race.
            runner.atomic_json(directory / "launch.json", {"pid": worker.pid, "created_at": runner.utc_now()})
        except OSError as error:
            if worker is not None:
                runner.stop_child(worker)
            record.update(status="FAILED", phase="launch_failed", finished_at=runner.utc_now())
            runner.atomic_json(directory / "status.json", record)
            raise runner.RunnerError("Background worker could not be started; inspect its local files.") from error
    return {"task_id": job_id, "submitted": True, "worker_pid": worker.pid, "status_reference": record["status_reference"]}


def dispatch(args, root, queue_path, prefix):
    if args.action == "status":
        result = {"tasks": [snapshot(root, args.id)] if args.id else list_jobs(root)}
        ledger = local_path(root, ".research/runner/state.json")
        if ledger.is_file():
            state = runner.read_state(ledger)
            result["experiments"] = [
                {"id": key, "status": value["status"], "outcome": value.get("outcome"), "review_result_reference": value.get("review_result_reference")}
                for key, value in state["experiments"].items()
            ]
    elif args.action == "events":
        items = events(root, args.after, args.limit)
        result = {"events": items, "next_cursor": items[-1]["event_id"] if items else args.after}
    elif args.action == "cancel":
        record = snapshot(root, args.id)
        if record.get("status") in TERMINAL or record.get("status") == "ORPHANED":
            raise runner.RunnerError("No live worker can cancel this task; inspect its recorded process state.")
        runner.atomic_json(job_path(root, args.id) / "cancel.json", {"requested_at": runner.utc_now()})
        result = {"task_id": args.id, "cancellation_requested": True}
    else:
        options = monitor_options(args)
        if args.action == "task":
            if not args.approval_reference.strip() or not args.resource_limit.strip() or args.time_limit_minutes <= 0:
                raise runner.RunnerError("A background task requires an approval reference and positive time/resource limits.")
            try:
                command = json.loads(args.command_json)
            except ValueError as error:
                raise runner.RunnerError("--command-json must be an argument array.") from error
            if not isinstance(command, list):
                raise runner.RunnerError("--command-json must be an argument array.")
            cwd = runner.contained_path(root, args.working_directory, "Task working directory")
            if not cwd.is_dir():
                raise runner.RunnerError("Task working directory does not exist.")
            command = runner.resolve_command(command, cwd)
            config = dict(kind="command", command=command, working_directory=cwd.relative_to(root).as_posix(),
                          timeout_seconds=args.time_limit_minutes * 60, approval_reference=args.approval_reference,
                          resource_limit=args.resource_limit, parent_id=args.parent_id, monitor=options)
            result = launch(root, args.id, config, args.max_background_jobs)
        else:
            # Validate a run's cheap gates here; CLI/model preflight stays in the worker.
            if args.action == "run":
                fields, rows = runner.read_queue(queue_path)
                state = runner.read_state(local_path(root, ".research/runner/state.json"))
                if args.id in state["experiments"] or any(record.get("status") != "REVIEWED" for record in state["experiments"].values()):
                    raise runner.RunnerError("Resolve existing experiment state before starting a new run.")
                row = runner.queue_item(rows, args.id)
                runner.check_queue_order(row, rows, state)
                runner.check_dependencies(row, rows, state)
                command, cwd, _ = runner.approved_command(root, row)
                runner.resolve_command(command, cwd)
                job_id = args.id
            else:
                job_id = args.action + "-" + uuid.uuid4().hex[:12]
            argv = ["--project", str(root), "--queue", queue_path.relative_to(root).as_posix(),
                    "--opencode-command-json", json.dumps(prefix), args.action, "--foreground"]
            if args.action in {"run", "review", "reconcile"}:
                argv += ["--id", args.id]
            if args.action == "review" and args.retry:
                argv.append("--retry")
            if args.action == "reconcile":
                argv += ["--outcome", args.outcome, "--return-code", str(args.return_code)]
            result = launch(root, job_id, dict(kind="experiment", operation=args.action, experiment_id=getattr(args, "id", None),
                            runner_argv=argv, parent_id=None, monitor=options), args.max_background_jobs)
    print(json.dumps(result, ensure_ascii=False))
    return 0


class Guard:
    def __init__(self, root, job_id, record, options):
        self.root, self.job_id, self.record = root, job_id, record
        self.directory = job_path(root, job_id)
        self.options = options
        self.sampler = Sampler(root, options["gpu_ids"])
        self.last_heartbeat = self.last_sample = 0.0
        self.last_pid = None
        self.last_size = None
        self.last_progress = time.monotonic()
        self.warnings = set()

    def save(self):
        runner.atomic_json(self.directory / "status.json", self.record)

    def check_cancel(self):
        if (self.directory / "cancel.json").exists():
            raise runner.ProcessCancelled("Task cancellation was explicitly requested.")

    def tick(self, child, stdout, stderr):
        self.check_cancel()
        now = time.monotonic()
        new_phase = child.pid != self.last_pid
        if new_phase:
            self.last_pid, self.last_size = child.pid, None
            self.last_progress = now
            self.record["child_pid"] = child.pid
            self.record["phase"] = "review" if stdout.name.endswith("events.jsonl") else ("experiment" if self.record["kind"] == "experiment" else "task")
        size = sum(path.stat().st_size if path.exists() else 0 for path in (stdout, stderr))
        if self.last_size is None or size != self.last_size:
            self.last_progress, self.last_size = now, size
        warnings = set()
        quiet = now - self.last_progress
        self.record["output_quiet_seconds"] = round(quiet, 1)
        if quiet >= self.options["stall_seconds"]:
            warnings.add("quiet_output")
        if new_phase or now - self.last_sample >= self.options["sample_seconds"]:
            sample = self.sampler.sample(child.pid)
            sample["phase"] = self.record["phase"]
            with (self.directory / "resources.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(sample) + "\n")
                handle.flush()
            self.record["latest_resources"] = sample
            self.last_sample = now
        resources = self.record.get("latest_resources", {})
        if resources.get("disk_free_bytes") is not None and resources["disk_free_bytes"] < 256 * 1024 * 1024:
            warnings.add("low_disk_space")
        total, available = resources.get("memory_total_bytes"), resources.get("memory_available_bytes")
        if total and available is not None and available / total < 0.02:
            warnings.add("low_host_memory")
        for gpu in resources.get("gpus", []):
            used, capacity = gpu.get("memory_used_mib"), gpu.get("memory_total_mib")
            if capacity and used is not None and used / capacity >= 0.95:
                warnings.add("high_device_memory")
        warning_changed = warnings != self.warnings
        for warning in sorted(warnings - self.warnings):
            emit(self.root, self.job_id, "warning", warning + "; inspect evidence. This is not an automatic termination trigger.")
        self.warnings = warnings
        self.record["warnings"] = sorted(warnings)
        if new_phase or warning_changed or now - self.last_heartbeat >= 5:
            self.record["heartbeat_at"] = runner.utc_now()
            self.save()
            self.last_heartbeat = now


def worker(root, job_id):
    directory = job_path(root, job_id)
    with runner.process_lock(directory / "worker.lock"):
        record = read_json(directory / "status.json")
        config = read_json(directory / "config.json")
        if record.get("status") != "QUEUED" or record.get("id") != job_id:
            raise runner.RunnerError("Worker task was already started or has invalid metadata.")
        record.update(status="RUNNING", worker_pid=os.getpid(), heartbeat_at=runner.utc_now(), phase="preflight")
        guard = Guard(root, job_id, record, config["monitor"])
        guard.save()
        emit(root, job_id, "started", "Background worker started; use status for progress.")
        runner.SUPERVISION_OBSERVER = guard.tick
        try:
            # Do not start a workload before the parent has durably registered
            # the worker PID. A failed handoff cannot leave an untracked child.
            deadline = time.monotonic() + 10
            while not (directory / "launch.json").is_file():
                guard.check_cancel()
                if time.monotonic() >= deadline:
                    raise runner.RunnerError("Worker launch handoff was not completed.")
                time.sleep(0.05)
            if read_json(directory / "launch.json").get("pid") != os.getpid():
                raise runner.RunnerError("Worker launch identity does not match its receipt.")
            guard.check_cancel()
            if config["kind"] == "command":
                cwd = runner.contained_path(root, config["working_directory"], "Task working directory")
                command = runner.resolve_command(config["command"], cwd)
                record.update(stdout_reference=f".research/tasks/{job_id}/stdout.log",
                              stderr_reference=f".research/tasks/{job_id}/stderr.log")
                return_code, timed_out = runner.supervise(
                    command, cwd, directory / "stdout.log", directory / "stderr.log", config["timeout_seconds"], lambda pid: None,
                )
                record.update(return_code=return_code, status="TIMED_OUT" if timed_out else ("SUCCEEDED" if return_code == 0 else "FAILED"))
            elif config["kind"] == "experiment":
                record["return_code"] = runner.main(config["runner_argv"])
                record["status"] = "SUCCEEDED" if record["return_code"] == 0 else "FAILED"
                ledger = runner.read_state(local_path(root, ".research/runner/state.json"))
                result = ledger["experiments"].get(config.get("experiment_id"), {})
                record.update(experiment_status=result.get("status"), experiment_outcome=result.get("outcome"),
                              review_result_reference=result.get("review_result_reference"))
            else:
                raise runner.RunnerError("Unsupported task operation.")
        except runner.ProcessCancelled:
            record.update(status="CANCELLED", return_code=130)
        except Exception as error:
            print(f"worker: {error}", file=sys.stderr)
            record.update(status="FAILED", return_code=2, error_type=type(error).__name__)
        finally:
            runner.SUPERVISION_OBSERVER = None
            if record.get("child_pid") and runner.pid_is_alive(record["child_pid"]):
                record["status"] = "ORPHANED"
            else:
                record["child_pid"] = None
            record.update(finished_at=runner.utc_now(), heartbeat_at=runner.utc_now(), phase="finished")
            guard.save()
            emit(root, job_id, "completed" if record["status"] == "SUCCEEDED" else "attention", "Background task ended: " + record["status"] + ". Inspect status and its local evidence.")
        return 0 if record["status"] == "SUCCEEDED" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--id", required=True)
    options = parser.parse_args()
    try:
        raise SystemExit(worker(options.project.resolve(), options.id))
    except (runner.RunnerError, OSError) as error:
        print(f"worker: {error}", file=sys.stderr)
        raise SystemExit(2)
