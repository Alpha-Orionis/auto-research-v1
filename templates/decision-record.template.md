# Decision Record

- Experiment:
- Date:
- Code revision:
- Decision: ACCEPTED / REJECTED / INCONCLUSIVE

## Criteria

List the success criteria agreed before execution.

## Evidence

Record measurements, comparison results, and relevant output locations.

## Deviations and limitations

Describe incomplete runs, protocol changes, uncertainty, or other limitations.

## Rationale

Explain how the evidence supports the decision.

## Next step

State the next action or why work should stop.

For managed runs, also persist the decision with runner `decide --id ...
--assessment ... --rationale ... [--next-id ...]`. In a sealed loop acknowledge
the completion event with `ack`; prose alone does not drive continuation.
