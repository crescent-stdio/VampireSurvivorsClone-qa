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
