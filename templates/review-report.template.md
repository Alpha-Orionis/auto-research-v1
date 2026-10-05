# Experiment Review Report

Emit `REVIEW_REPORT` followed by one JSON object:

```json
{
  "schema_version": 1,
  "experiment_id": "<this experiment ID>",
  "assessment": "INCONCLUSIVE",
  "correctness": {"verdict": "UNKNOWN", "reason": "<observed evidence>"},
  "constraints": {"verdict": "UNKNOWN", "reason": "<frozen constraint evidence>"},
  "evidence": [{"path": ".research/runs/<id>/stdout.log", "finding": "<concrete finding>"}],
  "missing_evidence": ["<missing measurement, or empty array if none>"],
  "next_options": [{"priority": 1, "action": "<controlled next option>", "rationale": "<basis>"}]
}
```

Assessment: VALID / INVALID / INCONCLUSIVE. Verdicts: PASS / FAIL / UNKNOWN.
VALID requires passing correctness and constraints, no missing evidence and
successful execution. Cite existing, approved files in this run. Options need
unique positive priorities. Recommend; Main verifies, records `decide` and
chooses the next experiment. A complete JSON stop receipt is also required.
