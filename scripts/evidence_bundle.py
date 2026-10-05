"""Explicitly scoped reproducibility snapshots; never collect the whole environment."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

SENSITIVE = re.compile(r"(?i)(secret|password|credential|private.?key|access.?token|api.?key)")
MAX_FILE_BYTES = 8 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def scoped_path(root, name):
    path = (root / name).resolve()
    path.relative_to(root)
    if not path.is_file() or path.name.startswith(".env") or path.suffix in {".pem", ".key"} or SENSITIVE.search(name):
        raise ValueError("Evidence must be an approved non-secret file inside the project.")
    return path


def list_field(row, name):
    value = json.loads(row.get(name) or "[]")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(name + " must be a JSON string array.")
    return value


def sanitized(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE.search(str(key)) else sanitized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def safe_argv(argv):
    result, hide_next = [], False
    for value in argv:
        if hide_next:
            result.append("[REDACTED]")
            hide_next = False
        elif SENSITIVE.search(value):
            result.append(value.split("=", 1)[0] + "=[REDACTED]" if "=" in value else "[REDACTED]")
            hide_next = "=" not in value and value.startswith("-")
        else:
            result.append(value)
    return result


def git_info(root, include_diff):
    def query(*args):
        completed = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
                                   capture_output=True, timeout=10, shell=False)
        return completed.stdout if completed.returncode == 0 else None
    try:
        head = query("rev-parse", "HEAD")
        difference = query("diff", "--binary", "HEAD", "--", ".", ":(exclude).env*", ":(exclude)**/.env*")
        if difference is not None and len(difference) > MAX_FILE_BYTES:
            difference = None
        return dict(commit=head.decode().strip() if head else None,
                    diff_sha256=hashlib.sha256(difference).hexdigest() if difference is not None else None,
                    diff=difference.decode("utf-8", "replace") if include_diff and difference is not None else None)
    except (OSError, subprocess.TimeoutExpired):
        return dict(commit=None, diff_sha256=None, diff=None)


def prepare(root, run_dir, row, argv, write_json):
    directory = run_dir / "evidence"
    directory.mkdir()
    files = []
    for index, name in enumerate(list_field(row, "evidence_files_json")):
        source = scoped_path(root, name)
        if source.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Evidence file exceeds the 8 MiB snapshot limit.")
        target = directory / f"input-{index}{source.suffix}"
        shutil.copyfile(source, target)
        files.append(dict(source=name, snapshot=target.relative_to(root).as_posix(), sha256=digest(target)))
    environment = {}
    # Only a small, named non-secret allowlist; no dependency enumeration,
    # credentials, HOME, PATH, or blanket environment snapshot.
    allowed = {"CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "PYTHONHASHSEED"}
    for name in list_field(row, "environment_keys_json"):
        if name not in allowed:
            raise ValueError("Unsupported environment snapshot key: " + name)
        environment[name] = os.environ.get(name)
    artifacts = []
    for name in list_field(row, "artifact_paths_json"):
        path = (root / name).resolve()
        path.relative_to(root)
        if path.exists():
            path = scoped_path(root, name)
        artifacts.append(dict(path=name, before_sha256=digest(path) if path.is_file() else None,
                              before_mtime_ns=path.stat().st_mtime_ns if path.is_file() else None))
    execution = dict(schema_version=1, command=safe_argv(argv), seed=row.get("seed") or None,
                     python=sys.version, platform=platform.platform(), environment=environment,
                     configured_revision=row.get("code_revision") or None,
                     git=git_info(root, row.get("include_git_diff", "").lower() == "true"),
                     input_snapshots=files, required_artifacts=artifacts,
                     metrics_path=row.get("metrics_path") or None, baseline_metrics_path=row.get("baseline_metrics_path") or None)
    execution["baseline_snapshot"] = None
    if execution["baseline_metrics_path"]:
        baseline = scoped_path(root, execution["baseline_metrics_path"])
        if baseline.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Baseline exceeds the evidence limit.")
        execution["baseline_snapshot"] = sanitized(json.loads(baseline.read_text(encoding="utf-8")))
    write_json(directory / "execution.json", execution)
    return execution


def finalize(root, run_dir, execution, write_json):
    artifacts = []
    for item in execution["required_artifacts"]:
        try:
            path = scoped_path(root, item["path"])
            sha = digest(path)
            fresh = sha != item["before_sha256"] or path.stat().st_mtime_ns != item["before_mtime_ns"]
            artifacts.append(dict(path=item["path"], sha256=sha, bytes=path.stat().st_size, valid=fresh))
        except (OSError, ValueError):
            artifacts.append(dict(path=item["path"], sha256=None, valid=False))
    metrics, baseline = None, execution.get("baseline_snapshot")
    for name in ("metrics_path",):
        if execution.get(name):
            path = scoped_path(root, execution[name])
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Metrics exceed the evidence limit.")
            value = sanitized(json.loads(path.read_text(encoding="utf-8")))
            if name == "metrics_path":
                metrics = value
            else:
                baseline = value
    comparison = {}
    if isinstance(metrics, dict) and isinstance(baseline, dict):
        for key, value in metrics.items():
            old = baseline.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(old, (int, float)) and not isinstance(old, bool) and math.isfinite(value) and math.isfinite(old):
                comparison[key] = dict(current=value, baseline=old, delta=value-old)
    result = dict(schema_version=1, artifacts=artifacts,
                  artifacts_valid=bool(artifacts) and all(item["valid"] for item in artifacts),
                  metrics=metrics, baseline=baseline, comparison=comparison,
                  log_hashes={name: digest(run_dir / name) for name in ("stdout.log", "stderr.log") if (run_dir / name).is_file()})
    write_json(run_dir / "evidence" / "results.json", result)
    return result


def validate_review(text, experiment_id=None):
    marker = re.search(r"\bREVIEW_REPORT\b\s*:?\s*(?:```(?:json)?\s*)?(\{)", text)
    if not marker:
        raise ValueError("A structured REVIEW_REPORT is required, not a promise to review.")
    report, _ = json.JSONDecoder().raw_decode(text[marker.start(1):])
    if not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("assessment") not in {"VALID", "INVALID", "INCONCLUSIVE"}:
        raise ValueError("Invalid review schema or assessment.")
    identity = report.get("experiment_id")
    if not isinstance(identity, str) or not identity or (experiment_id is not None and identity != experiment_id):
        raise ValueError("Review experiment identity does not match.")
    for name in ("correctness", "constraints"):
        check = report.get(name)
        if not isinstance(check, dict) or check.get("verdict") not in {"PASS", "FAIL", "UNKNOWN"} or not isinstance(check.get("reason"), str) or not check["reason"].strip():
            raise ValueError("Review needs a reasoned " + name + " verdict.")
        if report["assessment"] == "VALID" and check["verdict"] != "PASS":
            raise ValueError("VALID requires passing correctness and constraints.")
    evidence = report.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Review must cite inspected evidence.")
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("finding"), str) or not item["finding"].strip():
            raise ValueError("Evidence needs a path and concrete finding.")
        path = item["path"].replace("\\", "/")
        if not path.startswith(f".research/runs/{identity}/") or ".." in path.split("/"):
            raise ValueError("Review evidence must belong to this run's approved bundle.")
    missing = report.get("missing_evidence")
    if not isinstance(missing, list) or any(not isinstance(item, str) or not item.strip() for item in missing):
        raise ValueError("Review needs an explicit missing_evidence array.")
    if report["assessment"] == "VALID" and missing:
        raise ValueError("Missing evidence cannot establish a VALID result.")
    options = report.get("next_options")
    if not isinstance(options, list) or not options:
        raise ValueError("Review needs ranked next options; Main makes the decision.")
    priorities = set()
    for option in options:
        if not isinstance(option, dict) or type(option.get("priority")) is not int or option["priority"] < 1 or option["priority"] in priorities or any(not isinstance(option.get(name), str) or not option[name].strip() for name in ("action", "rationale")):
            raise ValueError("Next options need unique positive priorities, actions and rationales.")
        priorities.add(option["priority"])
    return report
