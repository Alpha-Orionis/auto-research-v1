---
name: research-optimization
description: Use for bounded, evidence-driven iterative improvement of an existing software, data, or model project.
compatibility: opencode
---

# Research Optimization

Use this workflow when the user wants to improve an existing project through
repeated hypotheses, scoped changes, and measured comparisons. For a one-off
question or ordinary coding task, use the simpler workflow that fits.

## 1. Establish the contract

Before an experiment depends on project-specific boundaries, identify:

- The objective and what counts as success.
- The project area that may change and inputs that must remain fixed.
- The data that may be read, changed, or copied.
- The permitted software environment and dependency policy.
- The resource, time, and cost limits.
- The primary metric, comparison baseline, and stopping rule.

Ask only for missing details that materially affect safety or validity. Never
invent a data location, environment, budget, metric, or authorization.

## 2. Inspect and preserve the baseline

- Start with read-only inspection of the active project and its existing
  changes.
- Record the current revision and the baseline measurement when available.
- Keep user changes intact and distinguish them from agent changes.
- Do not move project files or broaden the working scope to make a workflow
  easier.

## 3. Form a testable hypothesis

For each proposed iteration, state:

- The observed problem or opportunity.
- One primary hypothesis and the change that would test it.
- The baseline and measurements used for comparison.
- The expected resource cost and likely failure modes.

Avoid changing several independent factors at once unless the design
specifically measures their interaction.

## 4. Plan before execution

Write a short plan using the experiment-plan template when useful. Specify
inputs, configuration, command, metrics, expected outputs, time/resource
limits, and the stopping condition.

Do not install dependencies, access external services, change protected
inputs, or start costly or long-running work without the required user
authorization. Do not assume that a prior experiment authorizes a new one.

## Queueing multiple experiments

Use the experiment-queue template as an auditable plan. Give every item an
explicit order, hypothesis, change or procedure, input reference, metric,
resource and time limits, approvals, and status. Use dependencies when one
item requires the result of another.

Use these states:

- DRAFT: incomplete or still being designed.
- READY: complete enough for review, but not approved to run.
- APPROVED: the user has authorized this item or a clearly bounded batch.
- RUNNING: execution has started and its revision and start time are recorded.
- REVIEW_PENDING or REVIEWING: execution ended and its evidence is awaiting review.
- REVIEWED: the reviewer completed; inspect its findings before deciding.
- REVIEW_FAILED or INTERRUPTED: execution or review needs explicit recovery.
- BLOCKED or CANCELLED: the item will not proceed.

Record the experiment outcome separately as SUCCEEDED, FAILED, TIMED_OUT,
INCONCLUSIVE, or another clearly explained result. REVIEWED means only that
the reviewer completed; it does not mean the experiment passed.

A status written in a queue file is a record, not permission. Only move an
item to APPROVED when the user's authorization clearly covers its procedure,
inputs, environment, and limits. A batch approval must identify the items and
shared limits. If that scope changes, pause for approval.

Run one item at a time by default. Start only an approved item whose
dependencies are reviewed and whose preflight checks pass. Do not retry a
failed item automatically.
Record the outcome and review before selecting the next item. If a prior item is marked
RUNNING, verify that it has stopped before starting another; if that cannot be
verified, stop and ask.

This repository includes an opt-in runner. Starting `run` is an explicit
execution action: it still requires an APPROVED queue row and both approval
references. By default the launch command hands one item to a detached worker
and immediately returns a task ID. The worker waits for that process to exit,
writes a durable local result record, and invokes the read-only Experiment
Reviewer without requiring another foreground message. Submission is not
completion. Use `status` to verify execution and review. The worker does not
automatically start the next experiment.

The review may send the files named in `review_data_scope` to the model
provider configured for OpenCode. Do not approve sensitive data for review
unless that provider and data flow are acceptable. Keep credentials out of
command arguments and experiment output. The runner does not record the
inherited environment or command arguments in its manifest.

If the runner stops unexpectedly, use its `recover` command. It resumes
pending reviews, but never restarts an experiment whose process state is
uncertain. Such runs become INTERRUPTED and need an operator to verify that
the experiment has stopped and reconcile its actual outcome. A failed or
uncertain review blocks queue progression until it is explicitly resolved.

## Background work and the sentinel

Use `task` for approved, bounded non-interactive preparation, analysis, or
other long commands. Record an ID, command argument array, time limit,
resource limit, and approval reference. Optional `--parent-id` links it to an
existing task; the linked tasks have independent lifetimes and cancellation.
Use separate output paths when tasks overlap. The default cap is four workers
and only one experiment/review worker per project.

After submitting a task, report the ID and return to the user. Keep foreground
messages responsive: do not synchronously wait, stream logs into the chat, or
poll indefinitely. At the next interaction or authorized progress check,
read `status`, `alerts`, and incremental `events`, keeping the returned event cursor.
Events are local records; they do not wake an idle OpenCode session. Native
synchronous subagent calls remain synchronous. Use an approved headless CLI
command with a CLI-compatible agent if model work needs its own background
task; keep that agent's permissions and provider data scope bounded.
The supplied reviewers use mode all for CLI selection. For generic model
tasks, validate a non-empty report and a final stop event without errors;
process exit alone does not prove review completion.

During managed processes the sentinel updates a heartbeat about every five
seconds and samples resources every 30 seconds by default. Quiet-output,
disk, RAM, and optional selected NVIDIA device warnings require inspection;
they do not trigger an automatic kill or retry. GPU telemetry is read-only,
disabled unless device IDs are specified, and never creates a cache pool.
Telemetry lives in `.research/tasks/` outside the automatic reviewer scope.
Anomaly journals record observed values, thresholds, and raised/resolved
transitions. Unknown counters preserve stale warnings instead of claiming
recovery. Read `alerts` for current conditions and surface new actionable
events once. Sampling or recording failures are reported through available
local channels; they do not authorize a kill, repair, or restart.
Use explicit cancellation and recovery when required. `--foreground` is an
opt-in terminal mode, not the default for interactive agent work.

## 5. Implement and run within scope

- Make the smallest change that tests the hypothesis.
- Use the project's documented environment and reproducible commands.
- Keep generated files in the project location chosen by the user.
- Run only the checks or experiments the user authorized or the task clearly
  requires.
- Stop if the environment, inputs, resource use, or expected duration differs
  materially from the approved plan.

## 6. Evaluate honestly

Compare results with the stated baseline and metric. Record:

- Code revision and relevant configuration.
- Command and environment details needed for reproduction, excluding secrets.
- Measurements, uncertainty, and output location.
- Deviations, errors, and failed or incomplete runs.

Do not alter the evaluation protocol after seeing results without documenting
the change and keeping the original comparison visible. Do not claim causation
from a comparison that does not support it.

## 7. Decide and record

Mark each iteration as:

- **Accepted** when the pre-agreed criteria are met and no protected
  constraint was violated.
- **Rejected** when evidence does not support the hypothesis or a regression
  appears.
- **Inconclusive** when the result is incomplete, noisy, or invalid.

Use the decision-record and experiment-log templates where appropriate. Keep
personal data, credentials, and private results out of public project files.

## 8. Stop and report

Stop at the user's request, the agreed budget or stopping rule, or when
additional progress requires missing authorization. Do not run indefinitely
or continue launching iterations after the task has been completed.

Report the objective, changes, comparison evidence, decision, limitations, and
next useful step. Separate verified facts from assumptions.
