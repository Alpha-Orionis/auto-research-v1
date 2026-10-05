# Auto Research v1

Project-local OpenCode agents for planning, running, and reviewing bounded
experiments in software, data, and model projects.

The project includes a primary research agent, three reviewers, a reusable
research workflow, blank templates, and a Python runner. Experiments run one
at a time. After a process exits, the runner records its outcome and starts
the read-only Experiment Reviewer. Review findings guide the next decision.

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
```

On Windows, use `python` instead of `python3`. Run one approved item at a
time; earlier items and dependencies must complete review before advancing.
The runner launches the reviewer after success, failure, or timeout. It
returns after that review and does not select or launch the next experiment.
`REVIEWED` means the review completed, not that the hypothesis succeeded.

Artifacts are stored under `.research/runs/<id>/`. A completed review requires
a non-empty report and a complete OpenCode JSON event receipt. Failed or
incomplete reviews block subsequent runs.

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
row and ID. See [Usage](docs/usage.md) for the full recovery rules.

## Limits and private data

Experiments execute as the current operating-system user. The runner is not
a sandbox. Its timeout supervises the direct child process; experiment
commands must manage any background children themselves. Resource limits
recorded in the queue are not enforced CPU, memory, or accelerator quotas.
Use one runner per project and machine on a local filesystem.

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
| `templates/` | Blank plans, queues, logs, reviews, and decisions |
| `docs/usage.md` | Detailed setup, permissions, and recovery |
| `install.sh` | Prerequisite checks and project-local installation |
| `tests/` | Offline runner and installer checks |

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
