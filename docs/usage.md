# Usage

This repository contains project-local OpenCode agent definitions, one
reusable skill, blank templates, and an opt-in Python experiment runner. The
runner uses only Python's standard library and requires Python 3.10 or newer.
The supplied agent permission format supports OpenCode V1, version 1.2.0 or
newer. OpenCode V2 requires migrated agent definitions; the runner rejects
that version before starting an experiment. Older V1 versions incorrectly
disable tools with scoped read permissions and are also rejected.

## Try the agents in this repository

Open this repository as the active OpenCode project. OpenCode discovers the
definitions under:

- .opencode/agents/
- .opencode/skills/<skill-name>/SKILL.md

The Research Agent is the primary agent. It can load the Research Optimization
skill for iterative work. All reviewers deny edits and shell execution.
Paper Reviewer and Documentation Reviewer are subagents. Experiment Reviewer
uses `mode: all` so the CLI can select it directly without falling back to a
default agent.

## Use them in another project

On Linux, WSL, or Git Bash, run `bash install.sh --target /path/to/project`
from this repository after installing the supported Python and OpenCode
versions. `bash install.sh --check` performs preflight checks without
installing files. The installer preserves an existing queue and rejects
conflicting agent, runner, or template files before copying. See
[README](../README.md) for installer options.

Copy the `.opencode/` and `scripts/` directories into the other project's
root to use the agents and runner. Copy `templates/` only if you want the
supplied blank forms.
Review the permissions in each agent definition and the target project's
policies before use.

The agent asks for approval for edits, shell commands, delegation, web access,
and access outside the project. It does not select a model, dataset, software
environment, or resource budget for you.

The queue template records order, dependencies, limits, approvals, and the
experiment command. Fill `command_json` as a JSON array of arguments, for
example `["python", "scripts/run_experiment.py", "--config", "config.json"]`
(use JSON double quotes in the CSV cell). The runner passes the argument array
directly to the executable with `shell=False`; it does not build shell command
strings. An explicitly selected script interpreter is still allowed.
On Windows, use executables or explicit interpreters for script files; implicit
`.cmd` and `.bat` launchers are rejected. If OpenCode is installed through such
a launcher, use the real executable or a Node launcher argument array:

```text
python scripts/experiment_runner.py --opencode-command-json '["node", "path/to/opencode-launcher.js"]' run --id exp-001
```

Global runner options go before the subcommand. The example above uses a
placeholder path; supply the entry point from your OpenCode installation.
Keep the working directory inside the project and use project-relative
references. Do not put credentials or personal paths in the queue or command,
and avoid printing secrets or personal data to captured output. The reviewer
reads only the generated manifest and captured stdout/stderr; include concise
metrics and result summaries in those outputs if they need review.

## Run and automatically review one experiment

1. Copy `templates/experiment-queue.template.csv` to the project root as
   `experiment-queue.csv` and fill in a row. Set `status` to `APPROVED` only
   after the user authorizes the bounded experiment. Record the authorization
   in `approval_reference`.
2. Set `review_data_scope` to cover the run manifest and captured logs, and
   record the separate approval in `review_approval_reference`. The reviewer
   can read only runner output under `.research/runs/`; it cannot inspect the
   rest of the project. OpenCode sends review inputs through the model provider
   configured on this machine, so those inputs may leave the machine.
3. Install Python and OpenCode as appropriate for the project, then run from
   the repository root:

   ```text
   python scripts/experiment_runner.py run --id exp-001
   ```

The runner checks approvals and dependencies, starts only that experiment,
captures stdout and stderr under `.research/runs/<id>/`, and waits for the
process to exit. It then writes a manifest and immediately invokes the
read-only `experiment-reviewer` through `opencode run`. The queue remains
blocked until the review has been recorded. Review success means the reviewer
completed; it does not mean the experiment passed its hypothesis.
The runner reads OpenCode's JSON events and requires a non-empty report plus
a final `stop` event. Errors, agent fallback messages, and incomplete reports
leave the queue blocked. The generated report is stored as `review.md`.
The runner enforces the wall-clock limit for the direct child process;
`resource_limit` is recorded for review but is not an operating-system CPU,
memory, or accelerator quota. Reviewer runs have a 30-minute timeout.
The reviewer process starts with the project root as its working directory;
it does not depend on the newer `opencode run --dir` option. Read rules cover
both absolute paths used by older V1 releases and relative paths used by
newer releases, with Windows and Linux path separators.

## Recovery

Run this from the project root after restarting the runner:

```text
python scripts/experiment_runner.py recover
```

Pending reviews are resumed. A run left in `RUNNING` is marked `INTERRUPTED`
if its recorded process is no longer alive; it is never relaunched
automatically. Verify the original process has stopped, then reconcile the
observed outcome explicitly:

```text
python scripts/experiment_runner.py reconcile --id exp-001 --outcome FAILED --return-code 1
```

A review left in `REVIEWING` is recovered from its complete event receipt
when possible, without launching a duplicate review. Otherwise it is marked
`REVIEW_FAILED` for an explicit retry. Recorded live processes are left alone.

To retry a failed review, explicitly run:

```text
python scripts/experiment_runner.py review --id exp-001 --retry
```

Each experiment ID is single-use. Create a newly approved queue row with a
new ID for an authorized experiment retry. Local manifests, logs, and review
reports are stored under `.research/`, which is ignored by Git. Inspect them
for private data before sharing them. The runner supervises the direct child
process. If an experiment command spawns background children, make that
command manage and stop them itself. If the computer or runner fails in the
small window before an exit result is persisted, recovery requires operator
reconciliation; the runner will not guess or launch the experiment again.

## Linux and Windows

Use Python 3.10 or newer and the supported OpenCode V1 CLI on either platform.
The runner uses `fcntl.flock` on Linux and a file-byte lock on Windows; process
checks use POSIX signals on Linux and a non-destructive process-handle query
on Windows. Queue and state updates use atomic file replacement.

On Linux, run from the project root:

```text
python3 scripts/experiment_runner.py run --id exp-001
python3 scripts/experiment_runner.py recover
```

On Windows, use `python` in place of `python3`. For a virtual environment, use
its interpreter, such as `.venv/bin/python` on Linux or
`.venv/Scripts/python.exe` on Windows. `command_json` must name an executable
available on the current machine; use `python3` for Python experiments on
Linux when `python` is unavailable. Prefer forward slashes for relative paths.
Paths containing spaces remain single arguments in the JSON array.

An executable Linux script needs a valid shebang, LF line endings, and execute
permission. Alternatively, pass it to an explicit interpreter. Git attributes
keep repository text files in LF format. Experiment IDs use 1-64 ASCII letters,
digits, dots, underscores, or hyphens, begin with a letter or digit, and cannot
end in a dot or use Windows device names such as `CON` or `NUL`.

Run one runner per project on one machine. Use a local filesystem with working
file locks and atomic replacement; distributed execution on shared network
storage is not supported. A running experiment cannot be moved between
Windows and Linux; process identifiers and active state belong to the machine
that started it.

## Public repository hygiene

The templates are blank. Keep private data, credentials, local paths, generated
results, and project-specific experiment records out of public commits. The
root .gitignore excludes common local inputs and generated outputs as a
guardrail; review it for your project before relying on it.

No license file is included. Choose a license before granting reuse rights.

OpenCode references:

- Agents: https://opencode.ai/docs/agents/
- Agent skills: https://opencode.ai/docs/skills/
- CLI: https://opencode.ai/docs/cli/

## Offline regression checks

```text
python -m unittest discover -s tests -v
```

These checks use synthetic experiments and a fake OpenCode process. They
exercise completion, failure, timeout, locking, queue gating, and recovery
without sending project data to a model provider.
On Linux, use `python3` if needed. GitHub Actions runs these checks on Linux
and Windows with Python 3.10 and 3.12. Platform-specific checks are skipped
on the other operating system.
