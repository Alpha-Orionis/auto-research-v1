# Reliable experiment loop

This is the canonical protocol for the six reliability changes. The default
remains one explicitly approved experiment. Automatic continuation is opt-in,
bounded, and keeps the scientific decision with Main.

## 1. Workload ownership and stopping

The supervisor owns a POSIX process group or a Windows Job Object. On Windows
the process starts suspended, enters its job, then resumes; descendants cannot
race job assignment. Timeout, explicit cancellation, and supervisor errors
stop the whole workload and verify that no managed descendants remain. If a
parent exits while descendants keep running, those descendants are stopped
and a nominal zero exit becomes 125, not success. Reservations remain held
during cleanup. Independently submitted tasks are not workload descendants;
`--parent-id` is only a tracking link, not cascading cancellation.

POSIX programs must not deliberately detach from the managed process group.
These mechanisms supervise cooperative project commands; they are not an
adversarial sandbox. A dead worker with surviving workload processes is
ORPHANED and blocks conflicting work until its ownership is reconciled.

## 2. Concurrent queue edits

Use `queue-upsert --row-json '<object>'` to add or update one unsubmitted row.
All writers use the queue's short `.csv.lock`: reload, merge only the intended
columns, fsync, then atomically replace. Worker transitions merge only runtime
columns and never overwrite newly appended ideas or unrelated scientist edits.
Do not edit the CSV directly while workers are running; an editor that ignores
the lock can still overwrite data. Submitted experiment definitions are
immutable; a changed implementation needs a new ID.

Example (Bash quoting; PowerShell users can build JSON with ConvertTo-Json):

```bash
python3 scripts/experiment_runner.py queue-upsert \
  --row-json '{"id":"idea-003","order":"3","status":"IDEA","notes":"test one controlled change"}'
```

## 3. Review, decision and dependencies

The reviewer must emit `REVIEW_REPORT` followed by a JSON object, not just
announce an intention to review. The schema is in the review-report template.
The runner validates experiment identity, correctness and frozen-constraint
verdicts, concrete evidence locations, missing evidence, ranked next options,
and a final OpenCode `stop` event without errors. Evidence must exist in this
run and be approved for review. `VALID` cannot accompany failed execution or
incomplete evidence. Successful review produces `review.md` and
`review-report.json`; it does not establish acceptance.

Main independently checks the result, maintains the research notes and
experiment log, and records its final choice:

```text
python scripts/experiment_runner.py decide --id exp-001 --assessment ACCEPTED --rationale "Criterion met; evidence inspected" --next-id exp-002
```

The runner stores `decision.json` and the manifest decision. ACCEPTED requires
successful execution and a VALID review. Missing or stale declared artifacts
cannot be accepted. Queue order requires a recorded Main decision after a
review. By default `depends_on` additionally requires SUCCEEDED, ACCEPTED and
valid declared artifacts. Explicit `dependency_policy=reviewed` permits a
failure-analysis experiment to depend on a reviewed failure; it does not turn
failure into success.

## 4. Bounded completion -> Main -> next experiment

Use a local OpenCode server, not a second CLI process reopening the same
session. The bridge uses the documented session status and asynchronous prompt
APIs. It defers while Main is busy/retrying or idle evidence is unavailable,
and validates that the session directory is exactly the active WORK_DIR.

1. Fill `project-contract.md` with objective, frozen inputs, environment,
   success and stop rules, data-flow approvals, and count/time/cost limits.
2. Prepare an APPROVED batch of single-use queue IDs. Set each item's bounded
   command, deadline, resource budget, review approval and GPU IDs if needed.
3. Start `opencode serve --hostname 127.0.0.1 --port 4096` with your configured
   provider. Authenticate it with `OPENCODE_SERVER_PASSWORD` when appropriate;
   the bridge reads that value in memory, never from its JSON configuration.
   Attach your UI to that same server and select `research-agent`.
   Keep `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the launching
   environment unless intentionally disabled. The Python worker launcher also
   injects the variable when absent; explicit false is preserved.
4. Copy `templates/bridge.template.json` to a private `bridge.json`. Fill its
   exact Main `session_id`, approval reference, allowed IDs and three budgets.
   It intentionally contains no dataset, working directory or GPU defaults.
   `frozen_paths` lists individual protected project files to hash; this is not
   a recursive dataset snapshot. Keep this file out of public commits.
5. After explicit batch approval, seal its contract and execution definitions:

   ```text
   python scripts/experiment_runner.py bridge-seal --config bridge.json --approval-reference "approved bounded batch and Main wake"
   ```

6. Start the bridge in a dedicated terminal or tmux session:

   ```text
   python scripts/experiment_runner.py bridge --config bridge.json
   ```

   `--once` performs one cycle for diagnostics. Main launches the first approved
   item. A completed run/review then wakes Main automatically when idle.
7. The wake command requires Main to investigate actual job/PID ancestry,
   logs, artifacts and CPU/RAM/GPU evidence; output `RESOURCE_REPORT`; inspect
   the structured review; record `decide` with a new approved next ID; then:

   ```text
   python scripts/experiment_runner.py ack --event-id <completion-event-id> --id exp-001
   ```

   An acknowledgement is rejected until Main records a final decision. The
   bridge launches that choice after Main becomes idle, checking overlap,
   approvals, frozen fingerprints and budgets again. It never substitutes its
   own scientific choice. Failed IDs are never silently reused or retried.

Completion IDs and deterministic message IDs have durable receipts. The
bridge looks up the actual message before sending again. An ambiguous send
is retried only after separate idle polls prove the message absent for at
least 30 seconds, with the same ID and at most three attempts. A busy or
unreachable server never counts as absence. Delivered-but-deleted messages
and exhausted attempts remain explicit DELIVERY_UNKNOWN for investigation.
This is durable at-least-once handoff with receipt-based deduplication, not
a claim of transactional exactly-once delivery across OpenCode and local files.

Create `.research/STOP` to prevent further automatic launches. It does not
silently kill current work; use explicit `cancel` for that. Contract/approved
command/input changes, exhausted budgets, or missing authority stop the loop.
Re-sealing is explicit re-approval and resets the loop clock only when no
managed work is active; remove a STOP file explicitly if resuming. The count
and cumulative-time budgets conservatively include every submitted allowed
ID, even launch failures, using approved maximum durations rather than GPU
time estimates. `max_elapsed_minutes` gates new launches and the remaining
deadline; it is not an OS-wide cost or resource quota.

Requests, completed Main text responses and bridge/launch decisions are
recorded in `.research/bridge/loop.jsonl`, rotated at 10 MiB with three backups.
Per-experiment evidence, task state and acknowledgement files are retained.
Logs containing experiment data are local and must not be published.

## 5. GPU windows, ancestry and reservations

Declare `--gpu-ids` for GPU experiments **and generic tasks**. The queue's
optional `gpu_ids` supplies the sealed loop's selection. Every path uses the
same project-local kernel lock namespace. Conflicting managed tasks are
rejected; foreground and background paths both reserve cards. The child gets
the selected CUDA visibility; omission hides CUDA devices (CPU-only).
CUDA visibility is not a hostile-process sandbox or a cross-project scheduler.
Separate projects and unmanaged processes require independent coordination.

Device queries record utilization, memory, power and UUID. Compute-process
queries are attributed using managed process groups/descendant ancestry,
not command-name searches. Whole-device utilization is not per-process
utilization. External processes and unavailable ownership remain visible;
do not ascribe all device activity to the project.

`latest_resources.gpu_window` always supplies window maturity, overall and
per-device averages, project GPU IDs, ownership, alert condition, current
status, sample count and consecutive windows. Defaults are a 300-second
time-weighted window, 30% low threshold and three **non-overlapping** bad
windows. Flags: `--gpu-window-seconds`, `--gpu-low-threshold`,
`--gpu-bad-windows`. Missing samples or a gap over 2.5 sampling intervals
restart maturation; missing averages remain null, never zero. Low-utilization
warnings need complete mature windows with proved managed GPU workload.
CPU preparation, review, idle gaps and unknown ownership cannot establish
under-load GPU failure. Only raised/resolved transitions create alerts.

Main's RESOURCE_REPORT must include:

- Experiment running or not, job/PID/process ancestry, log progress and artifacts.
- CPU, RAM, current per-GPU utilization/memory/power and compute process proof.
- Window maturity, overall and each-card mean, project-owned IDs, alert readiness.
- Any missing measurement as UNKNOWN, with the next diagnostic action.

## 6. Reproducible evidence bundle

Each run records a sanitized resolved command, declared seed, actual Python
and platform, actual Git HEAD and working-diff hash (or explicit null), and
log hashes in `.research/runs/<id>/evidence/`. A seed field records the declared
seed; the command/project must actually apply and verify it. Untracked code
must be explicitly included in approved input snapshots.

Optional queue columns:

| Column | Meaning |
| --- | --- |
| `evidence_files_json` | Explicit project file allowlist for immutable pre-run configuration/code snapshots; 8 MiB per file. |
| `artifact_paths_json` | Required output files, hashed after execution; unchanged pre-existing artifacts are invalid. |
| `metrics_path`, `baseline_metrics_path` | Explicit JSON metric files; numeric baseline deltas are recorded. |
| `environment_keys_json` | Named allowlist: CUDA_VISIBLE_DEVICES, OMP_NUM_THREADS, MKL_NUM_THREADS, PYTHONHASHSEED only. |
| `include_git_diff` | `true` explicitly stores the working diff; inspect/approve its contents first. Default stores only a hash. |
| `evidence_review_approved` | `true` explicitly authorizes sending this bundle to the configured reviewer provider. |
| `seed`, `gpu_ids`, `dependency_policy` | Declared reproduction, resource and dependency controls. |

No blanket environment dump, recursive dataset copy or dependency installation
is performed. Secret-like paths/keys are rejected/redacted, but command output
and arbitrary source content cannot be guaranteed secret-free: inspect them
before approving provider review. Bundle content is excluded from the review
prompt unless `evidence_review_approved=true`; the default scope remains the
manifest and logs. References do not grant permission to inspect outside this
run. Reviewers still have read-only permissions; Main decides whether evidence
supports the pre-agreed objective.

## Validation and upstream references

Run `python -B -m unittest discover -s tests -v` and `bash -n install.sh`.
Tests use fake OpenCode, synthetic self-bounded processes and mocked NVIDIA
queries, not paid models or real GPU workloads. CI covers Linux/Windows and
Python 3.10/3.12. Live provider/server integration must still be validated
against your installed supported OpenCode V1 version before a costly batch.

- [OpenCode local server APIs](https://opencode.ai/docs/server/)
- [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
