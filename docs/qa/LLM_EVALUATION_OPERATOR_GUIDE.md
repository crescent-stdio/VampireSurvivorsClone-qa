# LLM Gameplay QA Evaluation Operator Guide

This guide operates two fixed campaigns on the production-scene QA Bridge:

- **Track A — autonomous exploration:** play a clean QA build and aggregate evidence-linked bug candidates.
- **Track B — injected-fault detection:** inspect blind clean/fault trace pairs and score numeric target detection privately.

The campaigns answer different questions and publish separate reports. Neither command changes the deterministic regression gate documented in [V4_HARNESS.ko.md](V4_HARNESS.ko.md).

## Architecture

The steering and inspection roles are deliberately separate:

1. The steering role plays through the QA Bridge. Track A and Track B autonomous runs use pure `llm` steering with `gpt-4o-mini`; official Track B traces replay actions captured by deterministic pilots.
2. The runner stores the gameplay trace and removes evaluator-private channels. Source tools are disabled. The inspector receives neither scenario or mission goals nor fault IDs, ground truth, oracle output, expected behavior, source paths, or injection logs.
3. `gpt-5.6-luna` with low reasoning effort inspects generic chunks of at most 32 sanitized observations with a two-observation overlap. Each trace is inspected in three independent passes.
4. Track A aggregates evidence-linked candidates without private truth. Track B scores only target-matching numeric relations in a private evaluator after inspection.

The inspector can see only the sanitized gameplay fields needed to judge the trace. The `player_view` projection is limited to numeric raw/displayed health and experience telemetry. Audit artifacts retain sanitized request payloads, raw model responses, normalized findings, usage, timing, and hashes; they never retain credentials or authorization headers.

## Prerequisites and credentials

Run from the repository root with:

- Unity `6000.0.80f1` and a fresh QA Bridge player built from the commit under evaluation.
- Python `3.10.12`, `uv` `0.12.x`, and `uv sync --locked`.
- Either `QA_API_KEY` or `OPENAI_API_KEY` supplied by a shell secret facility or secret manager. Do not put a key in a command argument, repository file, output directory, or endpoint URL.
- An optional compatible endpoint supplied as `QA_API_URL` or `--api-url`. The explicit option wins over the environment variable. Do not include user information, credentials, query secrets, or fragments in the URL.

PyTorch is not needed for these campaigns. It is an optional trainer dependency; install it with `uv sync --locked --extra trainer` before running the full `qa_pytorch_ppo` test collection.

Build and establish the clean deterministic precondition on macOS:

```sh
uv sync --locked
scripts/qa/build-bridge-player.sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all \
  --output QAArtifacts/acceptance/clean-harness \
  --headless
```

The build is clean when its normal launch has no injected faults. A Track B fault trace enables its one evaluator-private fault at launch; it does not require or authorize a separate modified binary. Do not use a previously fault-patched player as the campaign build.

To select a compatible endpoint without exposing a credential:

```sh
export QA_API_URL=https://provider.example/v1/chat/completions
```

The fixed campaign code selects `gpt-4o-mini` for steering and `gpt-5.6-luna` with `reasoning_effort=low` for inspection. Do not change model constants, prompts, source-tool settings, seeds, missions, or retry limits to compare runs.

## Commands and fixed schedule

### Track A — clean-build exploration

Start with an empty output directory:

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-a \
  --headless
```

The fixed schedule contains five core missions and three long missions on seeds `9101`, `9102`, and `9103`, for 24 gameplay traces:

| Mission | Tier | Maximum game seconds | Maximum steps |
|---|---|---:|---:|
| `core-menu-level1` | core | 60 | 20 |
| `core-combat-survival` | core | 120 | 40 |
| `core-progression-upgrades` | core | 240 | 80 |
| `core-items-chests-inventory` | core | 240 | 80 |
| `core-death-restart-isolation` | core | 240 | 80 |
| `long-sustained-combat` | long | 240 | 80 |
| `long-multi-level-growth-upgrades` | long | 240 | 80 |
| `long-items-chests-area-effects` | long | 240 | 80 |

Every launch uses preset `smoke`, variant `clean`, an empty fault list, pure LLM steering, and no source tools.

Resume an interrupted or completed exact campaign explicitly:

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-a \
  --headless \
  --resume
```

Without `--resume`, any non-empty Track A output directory is rejected before gameplay.

### Track B — blind injected-fault benchmark

Start with an empty output directory:

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-b \
  --headless
```

The fixed Track B schedule is:

- 33 deterministic pilots: 11 private fault cases × three seeds. Pilots create action replays and are excluded from detection scores.
- 66 official traces: each pilot replay is run once clean and once fault-enabled. The adaptive pilot is not reused as the official clean trace.
- 22 autonomous traces: 11 clean/fault pairs on seed `9101`, driven by pure `gpt-4o-mini` with the same neutral reachability objective.

`benchmark-detection` has no `--resume` switch. Re-running the exact command against the same directory resumes automatically only when the checkpoint and campaign identity match exactly.

### Totals and inspection-call budget

The maximum fixed gameplay schedule is `24 + 33 + 66 + 22 = 145` launches.

The chunk count for a trace is based on 32 observations with overlap two: step limits 20, 40, and 80 have maximum chunk counts 1, 2, and 3. Three inspection passes give:

- Track A: `(1 × 3 seeds × 3 passes) + (2 × 3 × 3) + (6 missions × 3 chunks × 3 seeds × 3 passes) = 189` logical calls.
- Track B official: the private schedule has four 20-step, four 40-step, and three 80-step cases; three seeds and two variants produce 24, 24, and 18 traces. `(24 traces × 1 chunk + 24 × 2 + 18 × 3) × 3 passes = 378` logical calls.
- Track B autonomous: one seed and two variants produce 8, 8, and 6 traces at those limits. `(8 traces × 1 chunk + 8 × 2 + 6 × 3) × 3 passes = 126` logical calls.
- Fresh fixed-schedule plan across the two documented workflows: `189 + 378 + 126 = 693` logical calls.

A logical call is one chunk in one inspection pass, not an HTTP attempt. A second completion request is made only when the first response has `finish_reason=length`; it is truncation recovery, not generic schema or structured-response correction. Each completion request permits at most three HTTP attempts and at most 45 cumulative seconds sleeping between transport retries. The checkpoint records `logical_calls` and `http_attempts` separately.

The hard cap of 700 logical calls is enforced independently by each campaign command against that output directory's checkpoint. It is not a cross-command global counter or a guarantee that two resumed or rerun directories will stay below 700 in aggregate. A fresh fixed Track A plus Track B schedule plans 693 calls; completed reusable inspection chunks do not add calls on resume, while failed or invalidated chunks retain their recorded calls and every rerun attempt adds a new logical call. HTTP retries add `http_attempts`, not logical calls. Do not raise these limits to hide provider or schema failures.

## Resume, integrity, and failure behavior

Campaign identity binds the build content and path, project root, effective endpoint, fixed models, prompts, rubric, scenario or mission configuration, seeds, inspection normalizer, and relevant execution options. Both tracks bind autonomous steering prompt construction, its relevant code dependencies, prompt version, and planning horizon; Track B official replay unit identity remains independent of that autonomous-only unit digest. Resume behavior is fail-closed:

- A non-empty output without a valid `qa-campaign-checkpoint/v1` checkpoint is rejected.
- A campaign or unit input hash mismatch is rejected; results from different builds or configurations are never mixed.
- A completed unit is reused only when every recorded artifact still exists at the recorded path with the same size and SHA-256 digest.
- Missing or modified unit artifacts are not trusted. The unit is rerun, and dependent inspections are invalidated and moved under `.superseded-inspections/`.
- Failed or started-but-incomplete units are rerun. Published reports from an earlier complete pass are moved under `.superseded-results/` before new publication.
- Action replays validate the build hash, command sequence, command digest, and replay digest before use. A malformed or modified replay fails the command.

Use a different output directory when intentionally changing any fixed input. Do not copy a checkpoint between output directories because recorded artifact paths are part of its identity.

Both campaign commands use these process outcomes:

| Exit | Meaning |
|---:|---|
| `0` | The complete manifest and reports were published. This may include trace-level invalid statuses such as `INSPECTION_ERROR`; it is infrastructure completion, not a detection-rate pass. |
| `2` | Campaign-level argument, configuration, build, credential or model initialization, checkpoint, replay, gameplay, inspection contract, publication, or infrastructure failure. Review the `incomplete` manifest and checkpoint when the output was safe to publish; an invalid or non-resumable output may be rejected before either file can be written. |

There is no exit code `1` gameplay-quality threshold for these commands. A first-run detection rate never creates a performance gate. A failed or malformed individual model response is recorded as a failed pass; when the three-pass trace verdict has no valid majority, that trace is `INSPECTION_ERROR`, the pair is invalid, and the campaign can still publish `complete` with exit 0. Campaign-level initialization, infrastructure, cap, artifact, or contract failures instead fail closed with `incomplete` and exit 2. During execution `campaign-manifest.json` has status `running`; success replaces it with `complete`, and campaign failure publishes `incomplete`. Final JSON and Markdown reports are authoritative only beside a `complete` manifest.

## Artifact layout

Track A publishes:

```text
QAArtifacts/evaluation/track-a/
├── campaign-manifest.json             # qa-campaign-manifest/v1
├── checkpoint.json                    # qa-campaign-checkpoint/v1
├── exploration-report.json            # qa-exploration-report/v1
├── exploration-report.ko.md
├── traces/<mission>/<seed>/
│   ├── launch-manifest.json
│   └── episode-result.json
└── inspections/<opaque-trace-id>/pass-<1..3>/
    ├── inspection.json                # qa-inspection/v2
    └── inspection-chunk-*.audit.json
```

Track B publishes:

```text
QAArtifacts/evaluation/track-b/
├── campaign-manifest.json             # qa-campaign-manifest/v1
├── checkpoint.json                    # qa-campaign-checkpoint/v1
├── detection-benchmark.json           # qa-detection-benchmark/v1, combined
├── detection-benchmark.ko.md
├── metrics/
│   ├── official.json
│   ├── official.ko.md
│   ├── autonomous.json
│   └── autonomous.ko.md
├── pilots/<private-case>/<seed>/
│   ├── episode-result.json
│   └── action-replay.json             # qa-action-replay/v1
├── traces/<private-unit>/episode-result.json
└── inspections/<opaque-trace-id>/pass-<1..3>/
    ├── inspection.json                # qa-inspection/v2
    └── inspection-chunk-*.audit.json
```

Opaque trace IDs prevent the inspector request path from revealing the clean/fault identity. Evaluator-side manifests and reports remain private QA artifacts and may contain pair bookkeeping; do not publish the output directory as a player-facing artifact.

## Sanitized schema examples

These examples show public shape only. Digest strings are illustrative and the replay excerpt is not an executable artifact. They intentionally omit credentials, authorization headers, private evaluator payloads, target registries, ground truth, scenario intent, and injection metadata.

### `qa-inspection/v2`

```json
{
  "schema_version": "qa-inspection/v2",
  "findings": [
    {
      "kind": "numeric",
      "field": "player_view.health",
      "comparison": "!=",
      "expected_value": 10.0,
      "observed_value": 12.0,
      "statement": "Displayed health differs from raw health in this observation.",
      "evidence_refs": ["obs-000012"]
    }
  ]
}
```

### `qa-action-replay/v1`

```json
{
  "schema_version": "qa-action-replay/v1",
  "replay_id": "pilot-redacted-9101",
  "scenario_id": "scenario-redacted",
  "seed": 9101,
  "build_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "bootstrap_command": {"action": "observe", "arguments": {}},
  "target_command_index": 0,
  "commands": [
    {
      "sequence": 0,
      "action": "move",
      "arguments": {"x": 0.5, "y": 0.0, "duration": 1.0},
      "duration_seconds": 1.0,
      "semantic_selection": null,
      "expected_phase_before": "active_gameplay",
      "expected_phase_after": "active_gameplay"
    }
  ],
  "command_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "replay_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

### `qa-campaign-manifest/v1`

```json
{
  "schema_version": "qa-campaign-manifest/v1",
  "campaign_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "status": "complete",
  "track": "A",
  "models": {
    "steering": "gpt-4o-mini",
    "inspection": "gpt-5.6-luna",
    "inspection_effort": "low"
  },
  "limits": {
    "inspection_repetitions": 3,
    "planned_inspection_calls": 189,
    "logical_inspection_call_cap": 700
  },
  "counts": {
    "scheduled_traces": 24,
    "logical_inspection_calls": 189,
    "http_attempts": 189
  },
  "reports": {
    "json": "exploration-report.json",
    "markdown": "exploration-report.ko.md"
  }
}
```

### `qa-exploration-report/v1`

```json
{
  "schema_version": "qa-exploration-report/v1",
  "metadata": {"track": "A", "campaign_id": "0123456789abcdef"},
  "summary": {
    "scheduled_traces": 24,
    "eligible_gameplay_traces": 23,
    "candidate_evidence_traces": 24,
    "coverage_not_reached_traces": 1,
    "excluded_harness_traces": 0,
    "candidate_count": 1
  },
  "surface_counts": {
    "planner_only": 0,
    "inspector_only": 1,
    "shared": 0,
    "runtime_oracle": 0,
    "union": 1
  },
  "tier_counts": {
    "validated-invariant candidate": 0,
    "evidence-linked candidate": 1
  },
  "priority_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 1},
  "candidates": [
    {
      "candidate_id": "0123456789abcdef",
      "category": "behavior",
      "rule": "restart-state-isolation",
      "field": "",
      "phase": "active-gameplay",
      "event": "restart",
      "tier": "evidence-linked candidate",
      "surface": "inspector_only",
      "runtime_oracle_supported": false,
      "reproduced": false,
      "priority": "P3",
      "inspection_agreement_by_trace": {"opaque-trace-01": "1/3"},
      "missions": ["core-death-restart-isolation"],
      "seeds_by_mission": {"core-death-restart-isolation": [9101]},
      "statements": ["A visible restart transition may have retained run-local state."],
      "evidence": [
        {
          "trace_id": "opaque-trace-01",
          "mission_id": "core-death-restart-isolation",
          "seed": 9101,
          "source": "inspector",
          "evidence_refs": ["obs-000018"],
          "inspection_pass": 1
        }
      ]
    }
  ],
  "harness_failures": [],
  "interpretation": {
    "candidate_scope": "Observed evidence candidate; not a confirmed game bug.",
    "zero_scope": "Zero candidates would not prove that the game is bug-free."
  }
}
```

### `qa-detection-benchmark/v1`

```json
{
  "schema_version": "qa-detection-benchmark/v1",
  "metadata": {"track": "B", "score_surface": "official"},
  "counts": {
    "pairs": {"total": 33, "valid": 32, "invalid": 1},
    "confusion_traces": {"TP": 20, "FN": 12, "FP": 2, "TN": 30},
    "invalid_traces": {
      "BASELINE_CONFLICT": 0,
      "FAULT_NOT_ACTIVATED": 0,
      "NOT_REACHED": 1,
      "UNOBSERVABLE": 0,
      "INSPECTION_ERROR": 0,
      "ERROR": 0
    }
  },
  "metrics": {
    "micro_detection_rate": {
      "numerator": 20,
      "denominator": 32,
      "value": 0.625,
      "wilson_95": {"low": 0.4525, "high": 0.7710}
    }
  },
  "per_fault": {},
  "pairs": []
}
```

The real detection JSON retains evaluator-private per-case and trace evidence needed for audit. Do not copy that section into prompts, public examples, or player-visible reports.

## Report interpretation

### Track A candidate funnel

Read `exploration-report.ko.md` for the operator summary and use `exploration-report.json` for evidence review.

1. Start with 24 scheduled traces. `eligible_gameplay_traces` reached the mission coverage objective; `candidate_evidence_traces` were structurally valid for candidate evidence. A coverage gap does not discard an otherwise evidence-valid candidate.
2. Review `validated-invariant candidate` separately from `evidence-linked candidate`. The first also matches a recorded invariant validation; neither label means a confirmed game bug.
3. Compare detection surfaces: `planner_only`, `inspector_only`, `shared`, and their LLM `union`. `runtime_oracle` is reported separately and is never added to the LLM union.
4. An evidence-valid inspector candidate is retained when it appears in at least one pass. Agreement is recorded per trace as `1/3`, `2/3`, or `3/3`; Track A does not discard a `1/3` candidate by applying the Track B majority rule.
5. `reproduced=true` requires the same normalized category, rule, field, phase, and event in the same mission on a different fixed seed.
6. Review deterministic priorities: P0 is a crash, hang, or unrecoverable blocker; P1 is reproduced major progression/state damage; P2 is a reproduced local numeric, UI, or combat issue; P3 is single-run or low-impact evidence.
7. Keep `harness_failures` and coverage failures outside game-candidate counts. Fix bridge, schema, model, or inspection failures before interpreting the candidate sample.

Track A never reports a true-positive rate because clean exploration has no complete bug ground truth. A candidate is not a confirmed bug, zero candidates do not mean the game is bug-free, and the report must not be presented as exhaustive game validation.

### Track B detection metrics

Read the two score surfaces separately:

- `metrics/official.*` is the inspector-isolated result. A deterministic pilot supplies an action replay, and the same replay is run independently on clean and fault launches. Pilot traces are excluded.
- `metrics/autonomous.*` is the end-to-end result, including pure-LLM reachability and steering as well as inspection.
- `detection-benchmark.*` combines both for audit convenience; it must not replace the separate official and autonomous interpretation.

The private evaluator counts a detection only when a numeric finding names a real observed field, gives numeric expected and observed values, states the violated comparison, cites valid evidence, and matches the private target relation. Text-only behavior findings and numeric alerts for another relation remain incidental candidates. They do not become target TP or clean FP.

The confusion-matrix unit is one trace verdict after three inspection passes, never one model call. Two detecting passes produce a target alert; two non-detecting passes produce no target alert. Fewer than two valid passes, or a one-to-one split among only two valid passes, produces `INSPECTION_ERROR`. Valid fault traces become TP/FN and valid clean traces become FP/TN.

Validity is paired: if either the clean trace or the fault trace has an invalid status, the entire pair is excluded. Neither trace in that pair contributes to TP/FN/FP/TN or their derived denominators, even if the other trace has a confusion verdict. The report still counts each invalid trace status separately, so one invalid pair can contribute one or two invalid-trace counts.

Invalid statuses are:

| Status | Interpretation |
|---|---|
| `BASELINE_CONFLICT` | The clean oracle did not pass. |
| `FAULT_NOT_ACTIVATED` | The fault oracle did not fail. |
| `NOT_REACHED` | Required coverage or the target point was not reached, including pre-target replay divergence. |
| `UNOBSERVABLE` | No target relation could be constructed from the available observations. |
| `INSPECTION_ERROR` | The three-pass verdict lacked a valid majority. |
| `ERROR` | Gameplay or artifact execution did not complete. |

Review TP/FN/FP/TN, per-case results, macro and micro detection rates, coverage, precision, specificity, clean false-positive rate, paired success rate, inspection agreement, and Wilson 95% intervals. Post-target replay divergence is retained as evidence; pre-target divergence is `NOT_REACHED`. The first result set is a baseline only and defines no detection-rate pass threshold.

## Acceptance and artifact review

Run the non-trainer checks without silently collecting PPO tests:

```sh
uv run --locked pytest -q qa_agent_runtime/tests qa_llm_agent/tests qa_smoke
scripts/qa/test-contracts.sh
uv run --locked pre-commit run check-merge-conflict --all-files
uv run --locked pre-commit run check-yaml --all-files
uv run --locked pre-commit run check-json --all-files
uv run --locked pre-commit run check-toml --all-files
uv run --locked pre-commit run check-added-large-files --all-files
uv run --locked pre-commit run shell-syntax --all-files
```

The full repository Python and pre-commit runs require the optional trainer group because pytest collects `qa_pytorch_ppo`:

```sh
uv sync --locked --extra trainer
uv run --locked --extra trainer pytest -q
uv run --locked --extra trainer pre-commit run --all-files
```

Do not remove, skip, or weaken PPO tests when `torch` is unavailable. Record the missing optional dependency as an environment limitation and run the full collection in a trainer-enabled environment.

Run the available Unity suites with the repository scripts; they discover `UNITY_EDITOR` or require it to point to the exact Unity executable:

```sh
scripts/qa/test-editmode.sh
scripts/qa/test-playmode.sh
```

For a real-model acceptance, run the clean harness and both campaign commands from this guide with a clean current build and an approved credential. A fresh complete live acceptance executes the 145-launch schedule and plans 693 logical inspection calls across Track A and Track B. Each command enforces its own 700-call checkpoint cap; resumed or rerun work follows the cumulative accounting described above. Unit tests use fake adapters and do not substitute for this run.

Before accepting a campaign:

- Confirm `campaign-manifest.json` has the expected schema, `status=complete`, track, model roles, counts, hashes, and report paths.
- Confirm Track A has 24 scheduled traces and an empty fault list in every `launch-manifest.json`.
- Confirm Track B has 33 unscored pilots, 66 official replay traces, 22 autonomous traces, and separate official/autonomous reports.
- Review `checkpoint.json` usage counts and failed units; a complete manifest must not be used to conceal a later incomplete rerun.
- Inspect chunk audits for opaque trace IDs, `source_tools_enabled=false`, sanitized payloads, normalized findings, usage, timing, and hashes. Do not move private evaluator fields into the request payload.
- Search for credential field names without printing credential values:

  ```sh
  rg -n 'Authorization|QA_API_KEY|OPENAI_API_KEY' QAArtifacts/evaluation/track-a QAArtifacts/evaluation/track-b
  ```

  The command should produce no matches.
- Confirm generated outputs remain ignored and untracked:

  ```sh
  git check-ignore -v QAArtifacts/evaluation/track-a/campaign-manifest.json
  git check-ignore -v QAArtifacts/evaluation/track-b/campaign-manifest.json
  git status --short --untracked-files=all
  ```

The repository-wide `/QAArtifacts/` ignore rule covers campaign directories, checkpoints, traces, audits, reports, superseded results, and local player builds.
