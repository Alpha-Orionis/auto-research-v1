# ZCode experiment loop protocol

This is the operating protocol for the ZCode backend. It implements the
same reliable loop as `docs/reliability.md` with the same
`experiment-queue.csv` schema and the same `.research/runs/<id>/`
artifact layout, adapted to how ZCode sessions actually run: one
foreground synchronous executor, a read-only reviewer subagent, and a
main session ("Main") that owns every scientific decision.

## Roles

- **Main (the ZCode main session).** Follows `agents/research-agent.md`.
  Drafts queue rows, requests approval, launches runs, spawns the
  reviewer, verifies the review against raw evidence, records decisions,
  and chooses the next experiment. The only role that may decide.
- **Experiment Reviewer (read-only subagent).** Instantiated by Main
  with the prompt from `agents/experiment-reviewer.md`. Reads only the
  paths named in the review request, emits `REVIEW_REPORT` plus one JSON
  object per `templates/review-report.template.md`. Recommends; never
  decides, edits, or launches. The reviewer must not share code paths
  with the experiment implementation.
- **User.** The only approval source. A row moves to `APPROVED` solely
  with an `approval_reference` citing the user (date plus conversation
  or file reference).

## State machine

```text
IDEA/READY → APPROVED → RUNNING → (SUCCEEDED|FAILED → REVIEW_PENDING) →
REVIEWING → REVIEWED → [Main decide] → DECIDED
timeout → INTERRUPTED → [Main decide] → DECIDED
```

Rules, enforced by `zcode_runner.py` where marked:

1. **Serial** (enforced): at most one RUNNING experiment, guarded by the
   `.research/lock` file lock.
2. **Single-use ids** (enforced): a used id is never rerun; a retry is a
   new approved row that cites the old one.
3. **Approval before run** (enforced): only APPROVED rows with a
   non-empty `approval_reference` execute.
4. **Review before dependence** (enforced): a dependency is consumable
   only when its decision.json says ACCEPTED and its run outcome is
   SUCCEEDED (`dependency_policy=reviewed` allows depending on a
   reviewed failure for failure analysis). Earlier queue orders must be
   decided or CANCELLED first.
5. **Failures stay recorded** (enforced for artifacts): FAILED and
   INTERRUPTED runs keep their rows and run directories; nothing is
   deleted or overwritten. Decisions are append-only.
6. **No post-hoc edits**: frozen results are never modified after the
   fact; later findings become a new experiment id explicitly marked
   post-hoc.

## Artifacts (`.research/runs/<id>/`)

- `manifest.json`: the full queue row at start, resolved command, return
  code, outcome, timestamps, Python/platform, and the SHA-256 of every
  file in `evidence_files_json`.
- `stdout.log`, `stderr.log`.
- `review-report.json` + `review.md`: written by Main from the reviewer
  subagent's output.
- `decision.json`: written by `decide`; never rewritten.

All writes are exclusive-create; the queue is rewritten atomically under
`experiment-queue.csv.lock`, touching only runtime columns (`status`,
`started_at`, `finished_at`, `outcome`, `result_reference`,
`review_result_reference`, and appended notes) so concurrent edits to
other columns survive.

## Decisions

```bash
python3 zcode/zcode_runner.py decide --id exp-001 \
    --assessment ACCEPTED|REJECTED|INCONCLUSIVE \
    --rationale "evidence checked" --next-id exp-002
```

`ACCEPTED` requires a SUCCEEDED run, a REVIEWED status, and a `VALID`
review receipt. `REJECTED` requires a completed review. `INCONCLUSIVE`
may reconcile an INTERRUPTED or otherwise undecided run without a
review; it is the honest terminal state when evidence is missing, and
the redo happens under a new id.

## Differences from the OpenCode runner

| Capability | OpenCode runner | ZCode backend |
| --- | --- | --- |
| Execution | Detached worker, task ids, heartbeats | Foreground synchronous `run` |
| Timeout supervision | Process group / Job Object including descendants | Kills the child process only; deliberately detached descendants are out of scope |
| Review launch | Automatic reviewer subprocess with event receipts | Main spawns the read-only subagent and records the receipt files |
| Evidence bundle | Snapshots, artifact hashing, metrics deltas, git diff | SHA-256 hashing of `evidence_files_json` into the manifest |
| Queue editing | `queue-upsert` with lock | Edit while idle, or under `experiment-queue.csv.lock` |
| Bridge/auto-continuation | Optional sealed bridge | None; Main chooses the next experiment explicitly |
| Resource telemetry | Sentinel with alerts | None; use your normal monitoring |

Recovery: if a run dies between queue writes, the row stays RUNNING and
the lock message names the holder. Verify the process actually stopped,
reconcile explicitly (`decide --assessment INCONCLUSIVE`, or redo under
a new id), then remove the lock by hand. Never relaunch an id whose
outcome is uncertain.
