# Auto Research v1

Project-local OpenCode agents for planning, running, and reviewing bounded
experiments in software, data, and model projects.

The project includes a primary research agent, three reviewers, a reusable
research workflow, blank templates, and a Python runner with detached workers
and a resource sentinel. Experiments run one at a time in the background.
After a process exits, its worker records the outcome and starts the read-only
Experiment Reviewer. Bounded subtasks can run alongside it, while the
foreground conversation remains available.

The reliability update adds whole-workload cleanup, concurrent-safe queue
merges, structured reviews and explicit Main decisions, an opt-in bounded
completion/wake/next-experiment bridge, GPU window averages and reservations,
and scoped reproducibility bundles. See the canonical
[Reliable experiment loop](docs/reliability.md) protocol.

## Requirements

- Python **3.10 or newer**. The runner and installer use the standard library.
- OpenCode **V1, version 1.2.0 or newer within V1**, with a configured model
  provider. OpenCode V2 is currently unsupported.
- Bash for `install.sh`: use Linux, WSL, or Git Bash. Native Windows users can
  use the files directly or copy them as described in [Usage](docs/usage.md).

Install Python and the supported OpenCode version separately. The installer
checks these prerequisites and installs project files locally. Configure
provider credentials through OpenCode's supported authentication flow.
See the [OpenCode documentation](https://opencode.ai/docs/) for CLI setup.

## Quick start on Linux

Clone or download this repository, enter its directory, and run:

```bash
bash install.sh --check
bash install.sh
```

Installation prepares a blank `experiment-queue.csv` and checks the reviewer
configuration. Open the project in OpenCode and select `research-agent`.
The reviewers are named `experiment-reviewer`, `paper-reviewer`, and
`doc-reviewer`.

To install the agents and runner into an existing project:

```bash
bash install.sh --target /path/to/project
```

The installer copies the supplied agents, skill, runner, and templates. It
preserves existing queue contents and refuses conflicting destination files
before copying. Repeating an installation with identical files is safe.
Local state and queue entries are added to the target project's `.gitignore`.

Use `--python /path/to/python` to choose an interpreter. If OpenCode needs an
explicit executable or interpreter command, supply a JSON argument array:

```bash
bash install.sh --python python3 --opencode-command-json '["/path/to/opencode"]'
```

Use the same `--opencode-command-json` value when invoking the runner. On
Windows, implicit `.cmd` and `.bat` launchers are rejected; use the real
executable or the explicit Node entry point from your installation.

## Queue an experiment

Fill a row in `experiment-queue.csv` using the template's columns. Record:

- A unique ID and numeric order; dependencies use IDs separated by `;`.
- The hypothesis, procedure, inputs, revision, metric, and limits.
- `command_json`, a JSON array containing the executable and its arguments.
- The user's experiment approval in `approval_reference`.
- The allowed manifest/log data in `review_data_scope`, and approval for its
  model-provider data flow in `review_approval_reference`.

Set `status` to `APPROVED` only when the recorded authorization covers the
experiment. Then, from the installed project's root:

```bash
python3 scripts/experiment_runner.py run --id exp-001
python3 scripts/experiment_runner.py status --id exp-001
```

On Windows, use `python` instead of `python3`. Run one approved item at a
time; earlier items need review and Main's final decision before advancing.
Dependencies normally require successful, accepted, valid artifacts.
The launch command returns a task ID immediately; `submitted` confirms the
handoff, not successful execution. Its background worker launches the reviewer
after success, failure, or timeout, without another foreground message. It
does not select the next experiment. With a sealed bounded bridge, completion
wakes Main and the bridge launches Main's approved next choice automatically.
`REVIEWED` means the review
completed, not that the hypothesis succeeded. Use `--foreground` only when
you explicitly want a terminal command to wait for execution and review.

Artifacts are stored under `.research/runs/<id>/`. A completed review requires
a structured `REVIEW_REPORT` and a complete OpenCode JSON event receipt. Failed or
incomplete reviews block subsequent runs.

## Background subtasks and monitoring

For an approved, non-interactive script in your project:

```bash
python3 scripts/experiment_runner.py task --id prepare-001 \
  --command-json '["python3", "scripts/prepare_inputs.py"]' \
  --time-limit-minutes 10 --resource-limit 'one CPU process' \
  --approval-reference 'approved preparation step' --parent-id exp-001
python3 scripts/experiment_runner.py status
python3 scripts/experiment_runner.py alerts --id exp-001
python3 scripts/experiment_runner.py events
python3 scripts/experiment_runner.py cancel --id prepare-001
```

Replace the script and approval reference with your actual approved plan;
`--parent-id` is optional and refers to an existing task. Each task has its
own logs, heartbeat, deadline, and cancellation record. The default limit is
four simultaneous background workers, including at most one experiment or
review worker. Parent links are for tracking; cancelling a parent does not
cancel its independently approved children.

The sentinel samples CPU, RAM, process memory, and free disk space every
30 seconds and updates a heartbeat about every five seconds during a managed
process. Long silence in logs and low resources produce warnings, not
automatic termination. `--gpu-ids 0,1` reserves devices and adds NVIDIA
telemetry; omission means CPU-only with CUDA hidden. Mature time-weighted
per-device averages and ancestry ownership reduce false alerts.
No GPU cache pool is created. Telemetry stays in local task files,
outside the automatic review scope.

Anomalies are recorded separately in `anomalies.jsonl`, with their type,
severity, observation, threshold, timestamp, and suggested next check.
`alerts` reports current resource warnings and task/heartbeat problems;
`events` reports newly raised and resolved conditions. Unavailable counters
or GPU sampling produce an availability warning, and unknown measurements
cannot establish recovery. Monitoring observation or recording failures are
reported through available local channels and do not trigger a task kill.

`status` and `events` read local records without waiting for completion.
Events support `--after <next_cursor>` for incremental checks. The Research
Agent reads these records when active; the opt-in bounded bridge also wakes
the same idle Main chat on completion. See [Usage](docs/usage.md)
for monitoring, cancellation, and recovery details.

## Recover interrupted work

```bash
python3 scripts/experiment_runner.py recover
```

Recovery resumes pending reviews or uses a completed review receipt. It never
relaunches an experiment whose outcome is uncertain. Verify the original
process has stopped, then reconcile its observed outcome explicitly:

```bash
python3 scripts/experiment_runner.py reconcile --id exp-001 --outcome FAILED --return-code 1
python3 scripts/experiment_runner.py review --id exp-001 --retry
```

The last command is only for an explicitly authorized retry of a failed
review. Experiment IDs are single-use; experiment retries need a new approved
row and ID. Recovery commands also return background task IDs; use `status`
to inspect them. See [Usage](docs/usage.md) for the full recovery rules.

## Limits and private data

Experiments execute as the current operating-system user. The runner is not
a sandbox. Its timeout supervises the owned process group/Windows Job Object,
including descendants that stay within it. Resource limits
recorded in the queue are not enforced CPU, memory, or accelerator quotas.
Use one machine per project on a local filesystem. Registered background
subtasks are independent workers; deliberately detached POSIX descendants
are outside cooperative process-group containment. Detachment survives the launcher exiting;
shutdown or a host that kills all descendants can stop the workers.

The Experiment Reviewer is restricted to runner outputs. OpenCode sends
review input to the configured model provider. Keep credentials and personal
data out of commands and captured logs, and approve only an acceptable data
scope. Reviewers treat documents and output as evidence, not instructions.

Private datasets, experiment records, credentials, generated results, and
local runner state belong outside public commits. `.gitignore` provides
defaults; inspect each public commit for your project's data.

## Files

| Location | Purpose |
| --- | --- |
| `.opencode/agents/` | Research Agent and the three reviewers |
| `.opencode/skills/research-optimization/` | Bounded research workflow |
| `scripts/experiment_runner.py` | Execution, automatic review, and recovery |
| `scripts/task_runtime.py` | Detached tasks, heartbeat, status, events, and cancellation |
| `scripts/resource_monitor.py` | Read-only CPU, memory, disk, optional NVIDIA telemetry, and anomaly assessment |
| `templates/` | Blank plans, queues, logs, reviews, and decisions |
| `docs/usage.md` | Detailed setup, permissions, and recovery |
| `install.sh` | Prerequisite checks and project-local installation |
| `tests/` | Offline runner, background, monitoring, and installer checks |

## Development checks

```bash
bash -n install.sh
python3 -m unittest discover -s tests -v
```

Checks use synthetic experiments and a fake OpenCode executable, without
calling a model provider. GitHub Actions runs them on Linux and Windows with
Python 3.10 and 3.12; shell installer checks on Windows use Git Bash.

## License

No license has been selected. Public availability does not grant general
reuse rights; the repository owner must choose a license for redistribution.
