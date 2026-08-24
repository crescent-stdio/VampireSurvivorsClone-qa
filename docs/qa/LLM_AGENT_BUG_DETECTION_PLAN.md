# Instrumented Autonomous QA and Injected-Fault Detection Plan

## Goal

Build two independently reported evaluation tracks on the existing QA Bridge:

- **Track A — autonomous exploration:** run a pure LLM gameplay policy on a clean QA build and report evidence-backed bug candidates without claiming that they are confirmed game bugs.
- **Track B — injected-fault detection:** compare clean and fault-enabled runs from the same build, inspect each trace blindly, and score numeric findings against private ground truth.

The steering model and inspection model are separate roles. Reports must preserve who drove the game and which model produced each finding.

## Global Constraints

- Work from the clean QA build: the QA Bridge remains enabled and all injected faults are off unless a Track B fault run explicitly enables one.
- Use `gpt-4o-mini` for pure LLM steering and `gpt-5.6-luna` with low reasoning effort for inspection. Do not change either model version.
- Do not expose source code, source tools, scenario names, mission goals, fault IDs, ground truth, oracle output, expected behavior, evaluator-private state, or injection logs to the inspector.
- The Track B autonomous driver may receive only a neutral reachability goal. The inspector receives the generic inspection prompt and sanitized trace observations only.
- Whitelist only the raw/displayed health and experience values needed from `player_view`; keep all other private channels removed.
- A Track B success requires a numeric expected value, numeric observed value, violated comparison, a real observation field, valid evidence references, and a match to the private target relation. Text-only alerts never count as detection.
- Preserve off-target alerts as incidental candidates. They are neither target true positives nor clean false positives.
- Store sanitized prompts/payloads, raw model responses, normalized findings, scoring evidence, model/prompt/rubric/build/config hashes, usage, retries, and timing. Never store credentials or authorization headers.
- Tests must be written first and observed failing before production changes. Do not weaken existing tests or silently swallow contract errors.
- Runtime Markdown reports use Korean explanations with standard English metric names. Code, schemas, comments, and repository documentation remain English.

## Fixed Evaluation Parameters

- Seeds: `9101`, `9102`, `9103`; failed seeds are not replaced.
- Inspection chunks: at most 32 observations with two-observation overlap; no fault-specific window selection.
- Inspection repetitions: three independent passes per trace; each pass scans all chunks.
- Track B trace verdict: two of three valid passes. Fewer than two valid pass results is `INSPECTION_ERROR`.
- Logical inspection-call cap: 700. HTTP attempts remain capped at three with a 45-second cumulative retry-sleep budget per request.
- Track B official driver: deterministic pilot, then symmetric replay on both clean and fault builds.
- Track B autonomous policy and all Track A gameplay: pure `llm`, never `hybrid`.
- Track A reproduction: the same normalized rule, phase, and state anomaly in the same mission on a different seed.
- Initial detection results are a baseline only; no detection-rate pass threshold is introduced.

## Public Schemas and Commands

Add versioned artifacts:

- `qa-action-replay/v1`
- `qa-inspection/v2`
- `qa-detection-benchmark/v1`
- `qa-exploration-report/v1`
- `qa-campaign-manifest/v1`

Keep existing `run`, `validate-faults`, and `baseline` interfaces compatible. Add:

- `python -m qa_smoke.cli benchmark-detection`
- `python -m qa_smoke.cli explore`

An existing output directory resumes only when build, model, prompt, rubric, and campaign configuration hashes match. Otherwise fail without mixing results.

## Task 1: Harden the observation channel and add inspection v2

Implement the shared inspection foundation.

- Add a narrow public `player_view` projection containing only raw/displayed health and experience values.
- Sanitize nested structured values and free-text fields, including `recent_logs`, against private keys, known fault IDs, injection messages, source paths, and context references.
- Disable source tools for both evaluation commands regardless of scenario defaults.
- Define strict `qa-inspection/v2` finding models for numeric and behavior findings.
- Replace equality-only findings with explicit comparison operators and expected/observed values while retaining a compatibility reader for existing v1 inspection artifacts.
- Split sanitized observations into generic 32-observation chunks with two-observation overlap and stable chunk identifiers.
- Normalize and deduplicate overlapping findings without using private oracle data.
- Add focused tests proving allowed display telemetry remains visible, private data never leaks, chunk boundaries are stable, malformed findings are rejected, and v1 artifacts remain readable.

Acceptance: focused tests and the existing `qa_smoke` suite pass with no new warnings introduced by this task.

## Task 2: Repair deterministic suite and oracle contracts

Fix the known deterministic harness gaps before building new campaign runners.

- Register and implement the v4 control oracles `valid_observation`, `normal_state_transitions`, and `stable_long_progression` in the v4 dispatch path.
- Convert unreadable artifacts and oracle contract failures into explicit `ERROR` results instead of silently leaving legacy verdicts in place.
- Pass each v4 scenario's `max_simulation_seconds` and `max_steps` to the actual run.
- Make `validate-faults --suite all` execute clean and injected runs for both legacy and v4 suites.
- Keep the existing deterministic gate semantics separate from LLM detection-rate baselines.
- Add regression tests for all three controls, limit forwarding, legacy injection, clean/fault manifests, and explicit error propagation.

Acceptance: all deterministic CLI tests and the existing `qa_smoke` suite pass.

## Task 3: Implement private numeric scoring and aggregate metrics

Create the scoring layer without launching Unity or calling an external model.

- Define private numeric expectations for all 11 existing unique fault IDs using exact integer comparisons and the existing float tolerance of `1e-5`.
- Score only findings whose field, comparison, values, and evidence match the target relation.
- Classify `BASELINE_CONFLICT`, `FAULT_NOT_ACTIVATED`, `NOT_REACHED`, `UNOBSERVABLE`, `INSPECTION_ERROR`, and execution `ERROR` separately.
- Treat off-target alerts from clean or fault traces as incidental candidates.
- Aggregate three inspection passes into a trace-level majority verdict.
- Compute target TP/FN/FP/TN at trace level, per-fault counts and rates, Wilson 95% intervals, equal-weight macro detection rate, micro detection rate, coverage, precision, specificity, clean false-positive rate, paired success rate, and inspection agreement.
- Generate `qa-detection-benchmark/v1` JSON and a Korean Markdown report with a trace-level confusion matrix.
- Add table-driven tests for every fault and for malformed values, wrong evidence, text-only alerts, partial inspection errors, invalid pairs, confidence intervals, macro/micro aggregation, and JSON/Markdown consistency.

Acceptance: all scorer/report tests and the existing `qa_smoke` suite pass.

## Task 4: Add action replay and the injected-fault benchmark runner

Implement the Track B orchestration and CLI.

- Record deterministic pilot commands, durations, semantic selections, phase expectations, seed, scenario, build hash, and command digest as `qa-action-replay/v1`.
- Exclude pilot traces from detection scoring.
- Replay the same action artifact on both clean and fault runs; never use the adaptive clean pilot as the official clean trace.
- Preserve post-target replay divergence as evidence. Classify pre-target replay divergence as `NOT_REACHED` and never force state synchronization.
- Run 33 pilots and schedule 33 clean/fault official pairs across the 11 faults and three seeds.
- Run 11 clean/fault autonomous pairs on seed `9101` with a neutral reachability objective and pure LLM steering.
- Inspect clean and fault traces independently with opaque identities; group three chunked passes into trace verdicts after inspection.
- Enforce the 700 logical-call cap and bounded retries.
- Checkpoint every pilot, replay, chunk, and inspection pass. Resume only exact-hash matches and rerun failed or incomplete units.
- Write `qa-campaign-manifest/v1`, audit artifacts, JSON metrics, and Korean Markdown under the selected output directory.
- Add fake-adapter integration tests for symmetry, digest validation, replay divergence, blinding, call caps, checkpoint resume, config mismatch rejection, and CLI parsing.

Acceptance: fake-adapter benchmark integration tests, deterministic harness tests, and the existing Python QA suites pass. A real build run is a documented acceptance command, not required in unit tests.

## Task 5: Add autonomous exploration campaigns and candidate aggregation

Implement Track A on the clean QA build.

- Add five core missions, each on three seeds: menu-to-Level-1 (20 steps), combat/survival (40), progression/upgrades (80), items/chests/inventory (80), and death/restart isolation (80).
- Add three long missions, each on three seeds with 240 seconds and 80 steps: sustained combat, multi-level growth/upgrade combinations, and item/chest/area-effect combinations.
- Enforce an empty fault list in every Track A launch manifest.
- Use pure LLM steering and no source tools.
- Inspect every trace in three chunked passes.
- Preserve any evidence-valid candidate found by at least one inspection pass and record `1/3`, `2/3`, or `3/3` inspection agreement.
- Separate `validated-invariant candidate` from `evidence-linked candidate`.
- Report planner-only, inspector-only, shared, and union candidate counts.
- Deduplicate by normalized category, rule, field, phase, and event. Mark `reproduced` only for the same mission on a different seed.
- Assign P0-P3 deterministically: P0 crash/hang/unrecoverable blocker; P1 reproduced major progression/state damage; P2 reproduced local numeric/UI/combat issue; P3 single-run or low-impact issue.
- Keep harness failures outside game-candidate statistics.
- Generate `qa-exploration-report/v1` JSON and Korean Markdown. Never claim confirmed bugs, a true-positive rate, or a bug-free game.
- Add tests for mission schedules, clean-launch enforcement, two-tier candidates, detection surfaces, agreement, reproduction, priority, harness separation, zero-candidate wording, resume, and JSON/Markdown consistency.

Acceptance: exploration integration tests and the existing Python QA suites pass.

## Task 6: Integrate, document, and verify the complete workflow

Finish the operator-facing workflow without changing the evaluation semantics.

- Update `docs/qa/README.md` and the QA operator guide with the new commands, artifact layout, resume rules, model requirements, call cap, and interpretation limits.
- Keep the architecture overview separate from Track A and Track B report examples: Track A shows candidate funnels and priorities; Track B shows the trace-level confusion matrix and inspector-isolated versus end-to-end scores.
- Add schema/version examples that contain no credentials or private evaluator payloads.
- Run the full non-trainer Python suite, pre-commit checks, and available Unity EditMode/PlayMode tests.
- Document the optional trainer dependency requirement for the PPO suite rather than weakening or skipping its tests silently.
- Verify the branch diff against this plan and ensure generated campaign artifacts remain untracked.

Acceptance: fresh verification evidence is captured in the task report, all available required checks pass, and any environment-only limitation is explicitly documented.

## Final Acceptance Totals

- Track A gameplay: 24 runs.
- Track B gameplay: 33 pilots, 66 official replay traces, and 22 autonomous traces.
- Maximum scheduled gameplay: 145 runs.
- Maximum logical inspection calls under fixed step budgets: 693; hard cap 700.
- Track B confusion matrix unit: one three-pass trace verdict, never individual inspection calls.
- No performance gate is created from the first result set; infrastructure contract violations still fail the command.
