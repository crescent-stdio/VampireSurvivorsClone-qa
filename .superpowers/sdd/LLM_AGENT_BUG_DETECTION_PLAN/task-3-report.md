# Task 3 Report: Private Numeric Scoring and Aggregate Metrics

## Outcome

Implemented a pure scoring and reporting layer for Track B. It consumes existing
trace, oracle, and `qa-inspection/v2` artifacts; it does not launch Unity, invoke a
planner, call an inspection model, or add Task 4 orchestration.

## Files

- Added `qa_smoke/detection_benchmark.py`.
- Added `qa_smoke/test_detection_benchmark.py`.
- Added this report at
  `.superpowers/sdd/LLM_AGENT_BUG_DETECTION_PLAN/task-3-report.md`.

## Schemas and Interfaces

- `TraceEvaluation` is the evaluator-private input for one clean or fault trace.
  It carries execution, coverage, oracle, raw-transition, and three-pass inspection
  inputs without performing I/O.
- `NumericTarget` records the exact target field, comparison, expected and observed
  values, evidence references, and whether values require exact integer equality.
- `TraceScore` records a three-pass majority verdict, valid/detected/agreement pass
  counts, private target relations, matched target findings, and preserved incidental
  candidates.
- `PairScore` combines symmetric clean/fault trace results and assigns either `VALID`
  or one explicit invalid-pair status.
- `private_numeric_targets()` defines relations for all 11 unique configured fault IDs.
- `score_trace()` and `score_pair()` implement target-only scoring and pair validity.
- `wilson_interval()`, `proportion()`, and `aggregate_benchmark()` implement counts,
  Wilson 95% intervals, per-fault rates, equal-weight macro detection, secondary micro
  detection, coverage, precision, specificity, clean false-positive rate, paired
  success rate, and inspection agreement.
- `build_benchmark_report()` and `benchmark_report_json()` emit
  `qa-detection-benchmark/v1` JSON-compatible data.
- `render_benchmark_markdown()` emits Korean operator Markdown with English metric
  names, raw values, Wilson 95% intervals, a trace-level confusion matrix, per-fault
  results, and invalid-pair counts.

### Trace statuses

- Conditional confusion statuses: `TP`, `FN`, `FP`, `TN`.
- Invalid statuses: `BASELINE_CONFLICT`, `FAULT_NOT_ACTIVATED`, `NOT_REACHED`,
  `UNOBSERVABLE`, `INSPECTION_ERROR`, and execution `ERROR`.
- Invalid pairs remain in raw status counts and are excluded from conditional
  confusion and detection denominators.

### Majority and matching rules

- Each trace accepts three inspection passes. Missing or explicit failed artifacts are
  invalid pass results; more than three passes is a contract error.
- Two valid target matches decide detection; two valid non-matches decide no
  detection. A split with an invalid third pass, or fewer than two valid results,
  becomes `INSPECTION_ERROR`.
- Numeric target matching requires the canonical public field, comparison operator,
  target values, and every target observation reference.
- Integer targets use exact equality. Float targets use absolute tolerance `1e-5`.
- Behavior findings, malformed findings, and numeric findings with a wrong field,
  operator, value, or evidence reference never enter TP/FP. Valid off-target findings
  remain in `incidental_candidates`.
- A clean FP is possible only when a majority explicitly flags the same private target
  relation whose clean oracle passed.

## TDD Evidence

### Initial RED

The complete Task 3 test module was written before the production module. Collection
failed for the intended missing-feature reason:

```text
$ .venv/bin/pytest -q qa_smoke/test_detection_benchmark.py
ImportError: cannot import name 'detection_benchmark' from 'qa_smoke'
1 error in 0.11s
```

The first minimal implementation exposed a hand-checked Wilson constant error:

```text
FAILED test_wilson_95_interval_uses_literal_known_values_and_handles_zero_denominator
Obtained: (0.2076549551, 0.9385096847)
Expected: (0.2076596008, 0.9385080553)
1 failed, 24 passed
```

The implementation was corrected to use the standard 97.5th percentile normal
quantile rather than rounded `1.96`.

### Report RED

The JSON/Markdown consistency test was strengthened before report rendering changes:

```text
FAILED test_json_and_korean_markdown_present_the_same_trace_level_metrics
Expected Markdown raw counts, Wilson 95% CI, and per-fault rows were absent.
1 failed
```

### Scoring-evidence RED

The 11-fault literal table was strengthened before adding serialized private target
relations:

```text
FAILED test_every_private_fault_requires_its_literal_numeric_relation[...] (11 cases)
AttributeError: 'TraceScore' object has no attribute 'target_relations'
11 failed
```

### Focused GREEN

```text
$ .venv/bin/pytest -q qa_smoke/test_detection_benchmark.py
..........................                                               [100%]
26 passed in 0.39s
```

The table-driven suite covers all 11 literal fault relations plus malformed values,
wrong fields/operators/values/evidence, behavior-only findings, float tolerance,
exact integers, clean target FP semantics, partial inspection errors, every invalid
pair status, Wilson intervals, macro/micro divergence, and JSON/Markdown consistency.

## Full Verification

```text
$ .venv/bin/pytest -q qa_smoke
........................................................................ [ 24%]
........................................................................ [ 48%]
........................................................................ [ 73%]
........................................................................ [ 97%]
......                                                                   [100%]
294 passed in 1.52s
```

```text
$ .venv/bin/python -c '<compare configured fault IDs with FAULT_IDS>'
private registry: 11 unique faults, exact config match
```

```text
$ .venv/bin/python -m compileall -q \
    qa_smoke/detection_benchmark.py qa_smoke/test_detection_benchmark.py
exit code 0
```

## Self-Review

- Compared the private registry with both legacy and v4 ground-truth configuration;
  the sets match exactly at 11 unique fault IDs.
- Checked each relation against the existing bridge telemetry and oracle contract.
- Confirmed no source tools, subprocesses, Unity launch, network calls, model calls,
  model-version changes, CLI registration, or campaign orchestration were added.
- Confirmed all expected values in tests are literal and independent of production
  target builders.
- Confirmed text-only and off-target alerts are retained for audit while excluded from
  target confusion counts.
- Confirmed the confusion unit is `TraceScore`, never an individual inspection pass.
- Confirmed equal-weight macro detection differs from micro detection in a literal
  two-fault test (`0.75` versus `2/3`).
- Confirmed runtime Markdown is Korean with standard English metric names, while
  source, comments, schemas, tests, and this repository report remain English.

## Concerns

- The macro interval is reported as the equal-weight mean of the included per-fault
  Wilson lower and upper bounds. Per-fault and micro intervals are ordinary binomial
  Wilson intervals; the macro bound is explicitly stratified by the same equal fault
  weighting as the primary point estimate.
- The pure layer assumes Task 4 passes raw evaluator transitions alongside normalized
  inspection artifacts. It intentionally does not read campaign files or reconstruct
  missing transition evidence.
- Repository-wide pre-commit collection is expected to require the optional `trainer`
  dependency because unrelated `qa_pytorch_ppo` tests import `torch`. The fresh
  `pre-commit run --all-files` attempt passed merge-conflict, YAML, JSON, TOML,
  added-large-file, and shell-syntax hooks, then stopped with eight PPO collection
  errors (`ModuleNotFoundError: No module named 'torch'`). The required `qa_smoke`
  acceptance suite is independently verified above.

## Commit

Planned subject: `feat(qa): add private detection benchmark scoring`

## Review Fix Round 1

Base commit: `f975249 feat(qa): add private detection benchmark scoring`

Planned fix subject: `fix(qa): correct private detection score contracts`

### Files

- Modified `qa_smoke/detection_benchmark.py`.
- Modified `qa_smoke/test_detection_benchmark.py`.
- Appended this review-fix record to
  `.superpowers/sdd/LLM_AGENT_BUG_DETECTION_PLAN/task-3-report.md`.

### Contract corrections

- Replaced the permissive relative-position target set with the sole injected
  `relative_x = world_x - player_x + 7` signal. `relative_y`, other offsets, and
  unrelated coordinate relations cannot score.
- Bound upgrade scoring to the decision's selected menu choice and the corresponding
  owned ability level. Unchanged unselected abilities are no longer private targets,
  and values outside the clean increment/injected unchanged pair are rejected.
- Added the displayed health-ratio relation to the complete `health_bar_desync` target
  set and changed the 11-fault test to compare every generated target relation rather
  than only index zero.
- Required every finding evidence reference to resolve to an actual observation or
  event ID in the trace, while still requiring all target references. Fabricated extra
  references invalidate target scoring; valid additional trace references remain
  allowed.
- Moved inspection parsing ahead of execution/oracle validity classification so invalid
  traces retain normalized target and incidental audit artifacts without entering the
  confusion matrix.
- Replaced priority-collapsed invalid pair status with `PairScore.status = INVALID` and
  a variant-keyed `invalid_traces` map. Aggregate counts now use explicit units:
  `pairs`, `confusion_traces`, and `invalid_traces`. A clean
  `BASELINE_CONFLICT` paired with fault `ERROR` increments both trace statuses.
- Kept the Inspection agreement denominator at three slots per trace and included
  failed and split-vote traces, including invalid pairs. A `[detect, ERROR, detect]`
  trace contributes `2/3`, not `2/2`.
- Removed the statistically invalid averaged `wilson_95` field from equal-weight macro
  detection. Ordinary binomial metrics and per-fault rates retain Wilson 95% intervals;
  Markdown reports macro CI as `N/A`.
- Made the Markdown confusion axes explicit and added per-fault Detection rate, Clean
  FPR, and Paired success rate summaries with their raw counts and Wilson intervals.

### Review-fix RED evidence

Private target completeness and spurious relation tests failed before correction:

```text
$ .venv/bin/pytest -q \
  qa_smoke/test_detection_benchmark.py::test_every_private_fault_requires_its_literal_numeric_relation \
  qa_smoke/test_detection_benchmark.py::test_relative_y_and_unselected_upgrade_ability_are_not_private_targets
FAILED relative_position_mismatch: unexpected relative_y target
FAILED upgrade_ack_without_effect: unexpected unselected ability target
FAILED health_bar_desync: missing player_view.health_ratio target
FAILED spurious relation scoring: relative_y produced TP
4 failed, 8 passed
```

The injected-value guard failed before exact `+7` and selected-level filtering:

```text
$ .venv/bin/pytest -q \
  qa_smoke/test_detection_benchmark.py::test_private_targets_reject_non_injected_relative_and_upgrade_values
FAILED: relative_x +3 produced TP instead of UNOBSERVABLE
1 failed
```

Evidence-universe validation failed before checking all supplied references:

```text
$ .venv/bin/pytest -q \
  qa_smoke/test_detection_benchmark.py::test_target_finding_rejects_fabricated_extra_evidence_but_allows_real_extra_refs
FAILED: finding with fabricated-ref produced TP instead of FN
1 failed
```

The combined invalid/audit/statistics/report regression set failed before the aggregate
contract changes:

```text
$ .venv/bin/pytest -q <five review regression tests>
FAILED invalid trace discarded incidental candidates
FAILED pair status collapsed dual invalid traces
FAILED counts lacked explicit trace units and macro exposed averaged wilson_95
FAILED Inspection agreement reported 5/5 instead of fixed-slot 9/12
FAILED Markdown lacked explicit axes and all per-fault rates
5 failed
```

### Review-fix GREEN evidence

```text
$ .venv/bin/pytest -q qa_smoke/test_detection_benchmark.py
...............................                                          [100%]
31 passed in 0.15s
```

```text
$ .venv/bin/pytest -q qa_smoke
........................................................................ [ 24%]
........................................................................ [ 48%]
........................................................................ [ 72%]
........................................................................ [ 96%]
...........                                                              [100%]
299 passed in 1.25s
```

```text
$ .venv/bin/python -m compileall -q \
    qa_smoke/detection_benchmark.py qa_smoke/test_detection_benchmark.py
$ git diff --check
exit code 0
```

### Review-fix self-review

- Re-read every `QaFaultInjection.cs` transform and its corresponding evaluator in
  `evaluation.py`; the literal table now asserts the complete generated relation list
  for all 11 configured fault IDs.
- Confirmed every confusion count is trace-valued, every invalid count is trace-valued,
  and coverage/paired success remain pair-valued with explicit JSON nesting.
- Confirmed invalid pairs never contribute a valid counterpart to TP/FN/FP/TN.
- Confirmed all traces, including failed and split-vote traces, contribute exactly
  three fixed Inspection agreement slots.
- Confirmed runtime Markdown reads the same JSON rate objects used by serialization.
- Confirmed no Task 4 orchestration, Unity/model calls, model versions, scenarios,
  or unrelated tests changed.

### Remaining concerns

- A selected upgrade that is not already owned has no post-action numeric ability field
  when the injection suppresses acquisition. It is intentionally `UNOBSERVABLE` under
  the numeric-only, real-field rule rather than being inferred from absence or text.
- Repository-wide pre-commit pytest still requires the unrelated optional `trainer`
  dependency (`torch`); the required fresh `qa_smoke` suite passes independently.
