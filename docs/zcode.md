# Using the ZCode backend

The ZCode backend drives the same bounded experiment loop as the
OpenCode runner: one `experiment-queue.csv`, the same column schema, the
same `.research/runs/<id>/` artifacts, and the same review-then-decide
discipline. Projects can switch frontends without changing their queue.

## Setup

The `install.sh` installer deploys the OpenCode files. For a ZCode
project, copy the backend manually from a clone of this repository:

```bash
cp -r zcode/ /path/to/project/zcode/
cp templates/ /path/to/project/templates/  # if not present already
```

Then, inside the project, create the blank queue from the template and
keep local state out of version control (the repository `.gitignore`
already lists `.research/` and the queue if you copied it):

```bash
cp templates/experiment-queue.template.csv experiment-queue.csv
```

Requirements: Python 3.10+ (standard library only) and a ZCode session
with permission to spawn read-only subagents.

## Operating the loop

1. Open the project in ZCode. The main session follows
   `zcode/agents/research-agent.md`; add that file to your project
   instructions if your setup does not pick it up automatically.
2. Draft a row: hypothesis, primary metric, `command_json` (a JSON
   array, no shell), working directory inside the project, declared
   seed, `evidence_files_json` allowlist of inputs to hash,
   `review_data_scope` naming exactly what the reviewer may read, and a
   `procedure_reference` plan document.
3. Obtain user approval and set `status=APPROVED` with an
   `approval_reference` citing it.
4. Run, review, decide:

   ```bash
   python3 zcode/zcode_runner.py run --id exp-001
   # Main spawns the read-only reviewer (zcode/agents/experiment-reviewer.md),
   # writes review-report.json + review.md, sets status REVIEWED
   python3 zcode/zcode_runner.py decide --id exp-001 \
       --assessment ACCEPTED --rationale "criterion met" --next-id exp-002
   python3 zcode/zcode_runner.py status --id exp-001
   ```

`ACCEPTED` requires a SUCCEEDED run and a VALID review; the runner
refuses otherwise. Failures and interruptions are decided REJECTED or
INCONCLUSIVE and stay recorded; a redo is a new approved id.

## Notes and limits

- The runner is not a sandbox. Experiments execute as the current
  operating-system user; `time_limit_minutes` kills the child process
  only, and deliberately detached descendants are outside its reach.
- Evidence handling is a hashing subset of the OpenCode bundle: files in
  `evidence_files_json` are SHA-256-hashed into the manifest; snapshot
  copies, metrics deltas, and git-diff bundling remain OpenCode-runner
  features.
- There is no sentinel telemetry and no automatic bridge. Main chooses
  the next experiment explicitly after each decision.
- The reviewer subagent sends whatever Main lists in the review request
  to the configured model provider; keep credentials and personal data
  out of commands, logs, and `review_data_scope`.
- See `zcode/PROTOCOL.md` for the full state machine, dependency rules,
  and recovery procedure.
