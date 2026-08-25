# Unblock the LLM Evaluation Campaign Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the repository to a state where `explore` and `benchmark-detection` complete end to end with a `status=complete` manifest, so Track A and Track B results can be reported.

**Architecture:** Three defects block the run today: the Bridge player binary predates the `capture_screenshot` command, the fixed inspector model has never returned a response over the current endpoint, and four dead campaign directories occupy the documented output paths. Each is verified by an observable artifact rather than by inspection, and the paid full run happens only after a cheap PoC proves the whole pipeline.

**Tech Stack:** Unity 6000.0.80f1 (macOS player), Python 3.10.12, `uv` 0.12.x, `qa_smoke` CLI, an OpenAI-compatible chat completions endpoint.

**Spec:** `docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.ko.md` (Korean operator guide) and `docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.md` (English detail).

## Global Constraints

- Unity `6000.0.80f1`. The player must be rebuilt from the commit under evaluation.
- Python `3.10.12`, `uv 0.12.x`. Always run through `uv run --locked`.
- `QA_API_KEY` or `OPENAI_API_KEY` is injected through the shell environment only. Never place a key on the command line, in a repository file, in a result folder, or in a URL.
- Steering model is fixed to `gpt-4o-mini`; inspector model is fixed to `gpt-5.6-luna` (`qa_smoke/detection_campaign.py:67-72`). Changing either is a contract change that must be committed, not an ad hoc override.
- Campaign results are only valid next to a manifest whose `status` is `complete`.
- `--profile poc` results are never reported as an official detection rate.
- Every task ends green: `uv run --locked pytest -q qa_agent_runtime/tests qa_llm_agent/tests qa_smoke` and `scripts/qa/test-contracts.sh`.

---

## File Structure

No new modules are required. Work touches:

- `QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app` — rebuilt binary (Task 1).
- `QAArtifacts/evaluation/` — stale campaign directories quarantined (Task 3).
- `QAArtifacts/preflight/` — throwaway single-episode evidence (Tasks 1, 2, 4).
- `qa_smoke/detection_campaign.py:72` — only if Task 2 proves `gpt-5.6-luna` is unreachable and a model change is approved.
- `docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.ko.md` — records the preflight step (Task 6).

---

### Task 1: Rebuild the Bridge player so `capture_screenshot` exists

The shipped binary is dated 2026-08-18; `QABridge.cs` gained the `capture_screenshot` case on 2026-08-25. The old bridge answers `ok=false` with `Unknown action`, so every trace records `screenshot_error` and retains nothing.

**Files:**
- Modify (rebuild artifact): `QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app`
- Read: `scripts/qa/build-bridge-player.sh`, `Assets/Scripts/QA/QABridge.cs:250-256`
- Test: `Assets/Tests/EditMode/QaBridgeScreenshotTests.cs`

**Interfaces:**
- Consumes: nothing.
- Produces: a player binary whose bridge answers `capture_screenshot` with `ok=true` and writes `final-frame.pending.png` into the session directory. Tasks 2, 4, and 5 launch this binary.

- [ ] **Step 1: Confirm the source has the command before spending a build**

```sh
grep -n "capture_screenshot" Assets/Scripts/QA/QABridge.cs
```

Expected: a `case "capture_screenshot":` line near 250.

- [ ] **Step 2: Run the Unity EditMode tests for the screenshot service**

```sh
scripts/qa/test-editmode.sh
```

Expected: `QaBridgeScreenshotTests` passes. If the script name differs in this checkout, list `scripts/qa/` and use the EditMode runner there.

- [ ] **Step 3: Rebuild the player**

```sh
scripts/qa/build-bridge-player.sh
```

Expected: the script prints the `.app` path and exits `0`. The build log is `QAArtifacts/logs/bridge-player-build-macos.log`.

- [ ] **Step 4: Verify the binary is newer than the bridge source**

```sh
stat -f "%Sm %N" -t "%Y-%m-%d %H:%M" \
  QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  Assets/Scripts/QA/QABridge.cs
```

Expected: the `.app` timestamp is later than `QABridge.cs`. A stale timestamp means the build silently reused a cached player — do not continue.

- [ ] **Step 5: Prove the command works against the real bridge**

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all --output QAArtifacts/preflight/build-check
```

Then inspect the session directory that run created:

```sh
find QAArtifacts/preflight/build-check -name "final-frame*" | head
```

Expected: the deterministic suite exits `0`. `final-frame*` files appear only for runs that pass `--capture-final-screenshot`, so an empty result here is acceptable; Task 2 Step 4 is the real screenshot gate.

- [ ] **Step 6: Commit nothing, record the build hash**

The player is a generated artifact and is git-ignored. Record its hash in your run notes so the campaign manifest's `hashes.build` can be traced back:

```sh
uv run --locked python -c "from qa_smoke.detection_campaign import hash_path; from pathlib import Path; print(hash_path(Path('QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app')))"
```

---

### Task 2: Prove both fixed models answer over the configured endpoint

`QAArtifacts/evaluation/track-a-visible` shows three inspector calls, all `LLMTransportError` with `raw_response: null`. Until one inspector call succeeds, a campaign can only produce `INSPECTION_ERROR` everywhere.

**Files:**
- Read: `qa_smoke/planners.py:18-24` (URL precedence), `qa_smoke/detection_campaign.py:2253-2266` (inspector adapter)
- Create: `QAArtifacts/preflight/model-check/`
- Modify (only if Step 5 forces it): `qa_smoke/detection_campaign.py:72`

**Interfaces:**
- Consumes: the player binary from Task 1.
- Produces: a confirmed-working `(QA_API_URL, steering model, inspector model)` triple. Tasks 4 and 5 assume both models answer.

- [ ] **Step 1: Confirm the key is present without printing it**

```sh
test -n "${QA_API_KEY:-}${OPENAI_API_KEY:-}" && echo "key present" || echo "NO KEY"
```

Expected: `key present`. If not, inject it from the secret manager into this shell.

- [ ] **Step 2: Show which endpoint will be used**

```sh
uv run --locked python -c "from qa_smoke.planners import resolve_llm_api_url; print(resolve_llm_api_url(None))"
```

Expected: either the default `https://api.openai.com/v1/chat/completions` or your `QA_API_URL`. Write it down — a campaign hashes this value.

- [ ] **Step 3: Run one short episode that exercises both models**

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --policy llm --model gpt-4o-mini --inspector-model gpt-5.6-luna \
  --max-steps 3 --max-simulation-seconds 30 \
  --output QAArtifacts/preflight/model-check
```

Expected: exit `0`. This is the cheapest call that touches the steering model, the inspector model, and the bridge.

- [ ] **Step 4: Read the evidence rather than trusting the exit code**

```sh
uv run --locked python - <<'PY'
import json, glob
for path in glob.glob("QAArtifacts/preflight/model-check/**/report.json", recursive=True):
    report = json.load(open(path))
    usage = (report.get("metrics") or {}).get("api_usage") or {}
    print(path)
    print("  fatal_error:", report.get("fatal_error") or "none")
    print("  steering calls:", usage.get("calls"), "prompt:", usage.get("prompt_tokens"))
    print("  inspector present:", bool(report.get("llm_assessment")))
PY
```

Expected: `fatal_error: none`, a non-zero steering call count, and `inspector present: True`. `inspector present: False` means the inspector call failed even though the episode succeeded — that is the blocking condition.

- [ ] **Step 5: Only if the inspector failed — classify before changing anything**

```sh
grep -rl "InspectionCallError\|LLMTransportError" QAArtifacts/preflight/model-check | head
```

Read the `cause_type` in the matching audit JSON and act on it:

| `cause_type` | Meaning | Action |
|---|---|---|
| `LLMTransportError` with HTTP 404 / `model_not_found` | The endpoint does not serve `gpt-5.6-luna` | Step 6 |
| `LLMTransportError` with HTTP 401 / 403 | Key lacks access | Fix the key or its project scope, then repeat Step 3 |
| `LLMTransportError` with a connection failure | Network or proxy | Fix connectivity, then repeat Step 3 |
| `LLMSchemaError` | The model answered but broke the findings schema | Not a transport block; continue to Task 3 and expect some `INSPECTION_ERROR` |

- [ ] **Step 6: Only if the model is genuinely unavailable — list what the endpoint does serve**

```sh
uv run --locked python - <<'PY'
import json, os, urllib.request
from qa_smoke.planners import resolve_llm_api_url

base = resolve_llm_api_url(None).rsplit("/chat/completions", 1)[0]
key = os.environ.get("QA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
request = urllib.request.Request(
    f"{base}/models", headers={"Authorization": f"Bearer {key}"}
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
for entry in sorted(item.get("id", "") for item in payload.get("data", [])):
    print(entry)
PY
```

Expected: the model list the key can reach. Pick the inspector replacement from this list and get it approved before editing anything — a reasoning-capable model is required, since the inspector must emit the findings schema.

- [ ] **Step 7: Change the constant deliberately**

This is a contract change: it alters the campaign hash and the "fixed inspector model" claim in the operator guide. Do not do it silently, and do not do it as an environment override.

Write the failing test first, in `qa_smoke/test_detection_campaign.py`, substituting the model id chosen in Step 6:

```python
def test_inspector_model_is_the_approved_fixed_model() -> None:
    from qa_smoke.detection_campaign import INSPECTOR_MODEL

    assert INSPECTOR_MODEL == "gpt-5.6-luna"  # replace with the id chosen in Step 6
```

Run it and watch it fail:

```sh
uv run --locked pytest -q qa_smoke/test_detection_campaign.py::test_inspector_model_is_the_approved_fixed_model
```

Expected: FAIL, showing `gpt-5.6-luna`.

Change `INSPECTOR_MODEL` in `qa_smoke/detection_campaign.py:72` to the approved model, rerun the test until it passes, then run the full suites:

```sh
uv run --locked pytest -q qa_agent_runtime/tests qa_llm_agent/tests qa_smoke
```

Commit:

```sh
git add qa_smoke/detection_campaign.py qa_smoke/test_detection_campaign.py
git commit -m "fix(qa): pin the inspector to a model the endpoint actually serves"
```

Then update both operator guides where they name the inspector model, and repeat Step 3 to confirm the new model answers.

---

### Task 3: Quarantine the dead campaign directories

`QAArtifacts/evaluation/` holds four campaigns from 2026-08-25: `track-a` and `track-b` stuck at `status=running`, `track-a-visible` at `status=incomplete`, `track-b-visible` at `status=running`. The operator guide's examples pass `--output QAArtifacts/evaluation/track-a`, which now collides with a dead campaign, and `track-a` doubles as the parent namespace for timestamped runs.

**Files:**
- Move: `QAArtifacts/evaluation/{track-a,track-a-visible,track-b,track-b-visible}` → `QAArtifacts/evaluation-archive/2026-08-25/`

**Interfaces:**
- Consumes: nothing.
- Produces: an empty `QAArtifacts/evaluation/` so Tasks 4 and 5 can reserve clean timestamped directories.

- [ ] **Step 1: Record what is being archived**

```sh
uv run --locked python - <<'PY'
import glob, json
for path in glob.glob("QAArtifacts/evaluation/*/campaign-manifest.json"):
    manifest = json.load(open(path))
    print(path, manifest.get("track"), manifest.get("status"), manifest.get("campaign_hash", "")[:16])
PY
```

Expected: the four dead campaigns listed. Keep this output in your run notes.

- [ ] **Step 2: Move them aside rather than deleting**

```sh
mkdir -p QAArtifacts/evaluation-archive/2026-08-25
mv QAArtifacts/evaluation/track-a \
   QAArtifacts/evaluation/track-a-visible \
   QAArtifacts/evaluation/track-b \
   QAArtifacts/evaluation/track-b-visible \
   QAArtifacts/evaluation-archive/2026-08-25/
```

A checkpoint is only valid beside its own artifacts, so never copy one back into a live campaign directory.

- [ ] **Step 3: Verify the path is clear**

```sh
ls -A QAArtifacts/evaluation 2>/dev/null || echo "evaluation directory is empty or absent"
```

Expected: empty output or the fallback message.

---

### Task 4: PoC run of both tracks with the real API

This is the first run that must produce `status=complete`. It also confirms the `use_item` ungating reaches a live bridge, because `AvailableActions` exposes `use_item` during active gameplay (`Assets/Scripts/QA/QABridge.cs:1498`).

**Files:**
- Create: `QAArtifacts/evaluation/track-a/<timestamp>/`, `QAArtifacts/evaluation/track-b/<timestamp>/`

**Interfaces:**
- Consumes: Task 1's player, Task 2's confirmed models, Task 3's clean directory.
- Produces: two complete PoC campaigns. Task 5 only starts if both are complete.

- [ ] **Step 1: Run Track A PoC in window mode and watch it**

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . --profile poc
```

Expected: about 20 minutes of simulation plus model latency, then one JSON line on stdout with `manifest`, `traces: 6`, `candidates`, `resumed: false`. Do not close the game window; disable screen lock, or wrap the command in `caffeinate -disu`.

- [ ] **Step 2: Verify the manifest and the screenshot evidence**

```sh
uv run --locked python - <<'PY'
import glob, json
path = sorted(glob.glob("QAArtifacts/evaluation/track-a/*/campaign-manifest.json"))[-1]
manifest = json.load(open(path))
print(path)
print("status:", manifest.get("status"), "| profile:", manifest.get("profile"))
print("counts:", json.dumps(manifest.get("counts") or {}, ensure_ascii=False))
PY
```

Expected: `status: complete`, `profile: poc`, and `counts` containing a non-zero `logical_inspection_calls`. A non-zero `capture_error_count` with a zero `retained_screenshot_count` means Task 1 did not take effect — stop and redo it.

- [ ] **Step 3: Confirm the agent can now use items**

```sh
grep -rho '"action": "use_item"' QAArtifacts/evaluation/track-a/*/traces/*/*/steps.jsonl | wc -l
```

Expected: a count of at least `1` across the six traces, provided any trace collected an item. Zero with no collected items is inconclusive rather than a failure; zero while `inventory.slots` shows `count > 0` means the ungating is not reaching the live contract, and Task 4 stops here.

- [ ] **Step 4: Run Track B PoC**

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . --profile poc
```

Expected: about 35 minutes of simulation plus latency, then one JSON line with `pairs: 6`.

- [ ] **Step 5: Read the PoC scores as a pipeline check, not as performance**

```sh
uv run --locked python - <<'PY'
import glob, json
path = sorted(glob.glob("QAArtifacts/evaluation/track-b/*/detection-benchmark.json"))[-1]
report = json.load(open(path))
print("counts:", json.dumps(report["counts"], ensure_ascii=False))
print("metrics:", json.dumps(report["metrics"], ensure_ascii=False))
print("screenshots:", json.dumps(report.get("screenshot_summary") or {}, ensure_ascii=False))
PY
```

Expected: `invalid_traces` shows which statuses dominate. With three official and three autonomous pairs, treat any rate as an anecdote. What must hold is that `INSPECTION_ERROR` is not the dominant status — if it is, the inspector is still failing and Task 2 was not truly resolved.

- [ ] **Step 6: Record the real cost before committing to the full run**

Read the actual billed amount from the endpoint dashboard for the window covering Tasks 2 and 4. Multiply by roughly six for a full-profile estimate. Decide whether Task 5 runs both tracks or only Track A at full profile.

---

### Task 5: Full-profile run

**Files:**
- Create: `QAArtifacts/evaluation/track-a/<timestamp>/`, optionally `QAArtifacts/evaluation/track-b/<timestamp>/`

**Interfaces:**
- Consumes: two complete PoC campaigns from Task 4.
- Produces: the campaigns that the final report cites.

- [ ] **Step 1: Commit everything before launching**

The manifest records a build hash, not a commit. A dirty tree makes the result unreproducible.

```sh
git status --short
```

Expected: empty. Commit anything outstanding first.

- [ ] **Step 2: Run Track A at full profile, headless**

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . --headless
```

Expected: 24 traces, roughly 81 minutes of simulation plus latency. Screenshots will error under `--headless` because `-nographics` gives the player no graphics device; run without `--headless` if human-readable evidence matters more than desk availability.

- [ ] **Step 3: Verify completion before starting Track B**

Repeat Task 4 Step 2 against the new Track A directory. Expected: `status: complete`, `profile: full`, `traces: 24`.

- [ ] **Step 4: Run Track B at full profile if the cost decision allowed it**

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . --headless
```

Expected: 121 launches, roughly 264 minutes of simulation plus latency. If it dies, rerun the identical command against the same directory; it resumes only when the checkpoint and campaign hash match exactly.

- [ ] **Step 5: Publish only from complete manifests**

```sh
uv run --locked python - <<'PY'
import glob, json
for path in glob.glob("QAArtifacts/evaluation/*/*/campaign-manifest.json"):
    manifest = json.load(open(path))
    print(manifest.get("status"), manifest.get("track"), manifest.get("profile"), path)
PY
```

Expected: the directories you intend to cite report `complete`. Anything `running` or `incomplete` is excluded from the report.

---

### Task 6: Record the preflight in the operator guide

Tasks 1 and 2 are now known prerequisites, and the guide does not mention either.

**Files:**
- Modify: `docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.ko.md` (section 6, `첫 실행 순서와 결과 해석`)

**Interfaces:**
- Consumes: the commands proven in Tasks 1 and 2.
- Produces: documentation that keeps the next operator from repeating this investigation.

- [ ] **Step 1: Add the two preflight items to the recommended order**

Insert before the existing first item in `### 권장 실행 순서`:

```markdown
1. `scripts/qa/build-bridge-player.sh` — 평가할 커밋으로 플레이어를 새로 빌드한다. 오래된 빌드는 `capture_screenshot`을 모르며, 모든 트레이스가 `screenshot_error`로 끝난다.
2. `run --policy llm --model gpt-4o-mini --inspector-model gpt-5.6-luna --max-steps 3` — 조종 모델과 검사 모델이 실제로 응답하는지 1회 호출로 확인한다. 검사 모델이 응답하지 않으면 캠페인 전체가 `INSPECTION_ERROR`로 끝난다.
```

Renumber the existing four items to 3 through 6.

- [ ] **Step 2: Verify the document still reads correctly**

```sh
grep -n "^#\{2,3\} " docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.ko.md
```

Expected: section numbering 1 through 8 unchanged.

- [ ] **Step 3: Commit**

```sh
git add docs/qa/LLM_EVALUATION_OPERATOR_GUIDE.ko.md
git commit -m "docs(qa): add the build and model preflight to the first-run order"
```

---

## Out of Scope

- Changing the fixed steering model, the mission schedule, the seed set, or the fault registry.
- Reporting `--profile poc` numbers as an official detection rate.
- `ruff` cleanliness. The project gate is `pytest`, `scripts/qa/test-contracts.sh`, and the configured `pre-commit` hooks; `ruff` is not wired into any of them and its 106 findings are pre-existing.
