# v4 하네스 실행 가이드

이 문서는 `python -m qa_smoke.cli`가 제공하는 **결함 주입 스위트와 회귀 baseline**을 다룬다. LLM 자율 플레이는 [BYOK Bridge 가이드](BYOK_BRIDGE.ko.md)에, ML-Agents lane은 [PPO 퀵스타트](PPO_QUICKSTART.ko.md)에 있다.

v4 하네스는 LLM을 쓰지 않는다. driver는 전부 `scripted`이고 policy는 `heuristic`으로 고정된다. **에이전트가 결함을 찾는지**를 재는 것이 아니라, **결함이 주입되면 오라클이 반드시 fail을 내고 결함이 없으면 반드시 pass를 내는지**를 재는 층이다. 이 층이 흔들리면 그 위에서 측정한 `agent_detection` 수치는 의미가 없다.

## 1. 두 개의 스위트

| suite | 정의 파일 | 시나리오 수 | 오라클 |
|---|---|---|---|
| `v4-core` | `config/qa-scenarios-v4.json` | 8 (결함 5 + control 3) | `evaluate_v4_oracle` |
| `legacy-contract` | `config/qa-scenarios.json` | 9 (결함 6 + control 3) | 기존 coverage/oracle 평가기 |

`--suite all`은 둘 다 실행한다. 기본값은 `v4-core`다.

`legacy-contract`는 승인된 ground truth를 가진 기존 9개 시나리오이며 `qa_smoke.benchmark`의 결정론적 게이트가 이 집합을 그대로 쓴다(§5). v4-core는 그 위에 **raw/view 채널 분리**를 요구하는 새 오라클을 얹은 것이라 기존 것을 대체하지 않는다.

### v4-core 시나리오

각 v4 시나리오는 `legacy_scenario_id`로 기존 시나리오의 charter/limits를 재사용하되, 자기 오라클과 자기 fault로 판정한다.

| id | bug_type | 오라클 (kind) | 주입 fault | 검증 대상 |
|---|---|---|---|---|
| `easy-hp-on-hit` | logic_error | `hp_decreases_on_hit` (transition) | `hp_not_decreased_on_hit` | 피격 시 raw HP 감소 |
| `easy-view-health` | description_flaw | `view_state_match` (invariant) | `health_bar_desync` | 체력바가 raw HP를 반영 |
| `medium-item-effect` | logic_error | `item_effect_applied` (transition) | `item_effect_not_applied` | 아이템 사용이 피해를 발생 |
| `medium-item-hit-range` | logic_error | `item_hit_range` (transition) | `item_hit_range_mismatch` | 반경 내 모든 대상에 적용 |
| `hard-exp-conservation` | data_inconsistency | `exp_conservation` (invariant) | `experience_display_drift` | raw EXP와 표시 EXP 일치 |
| `control-easy-observation` | control | `valid_observation` (invariant) | — | 오탐 검사 |
| `control-medium-item` | control | `normal_state_transitions` (transition) | — | 오탐 검사 |
| `control-hard-progression` | control | `stable_long_progression` (trajectory) | — | 오탐 검사 |

`goal.reached_when`과 `oracle.id`는 `qa_smoke/scenarios.py`의 `REGISTERED_V4_GOALS` / `REGISTERED_V4_ORACLES`에 등록된 값만 허용한다. 오타 난 오라클 이름이 조용히 "평가 안 함"으로 흘러가는 것을 막기 위해서다. `verified_path`(`scripted:seed=<n>`)의 시드는 반드시 `seeds`에 포함되어야 한다.

## 2. 실행

빌드는 BYOK Bridge와 같은 플레이어를 쓴다. 저장소 루트에서 실행한다.

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/windows/VampireSurvivorsClone.exe \
  --suite v4-core \
  --headless
```

| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--build` | (필수) | 플레이어 실행 파일 |
| `--project-root` | `.` | `config/`를 찾는 기준 |
| `--suite` | `v4-core` | `v4-core` \| `legacy-contract` \| `all` |
| `--output` | `QAArtifacts/runs/<uuid>` | 산출물 루트 |
| `--baseline` | 없음 | 지정하면 회귀 diff 모드(§4) |
| `--seed` | 시나리오별 `verified_path` 시드 | 시나리오 `seeds`에 없는 값은 오류 |
| `--headless` / `--quiet` | off | 플레이어에 그대로 전달 |

`--seed`를 생략하면 각 시나리오의 검증된 시드(현재 전부 `9101`)를 쓴다. 명시하면 모든 시나리오에 같은 시드를 적용하며, 그 시드를 `seeds`에 갖지 않은 시나리오가 하나라도 있으면 실행 전에 거부된다.

기본 실행은 **결함을 주입하지 않는다.** `run`은 현재 빌드가 깨끗한지 보는 명령이다.

## 3. validate-faults — 결함이 실제로 잡히는지

```sh
uv run --locked python -m qa_smoke.cli validate-faults \
  --build QAArtifacts/bridge-player/windows/VampireSurvivorsClone.exe \
  --headless
```

`run`과 옵션이 동일하되 각 suite를 `clean`과 `injected` 두 variant로 실행한다. clean variant에는 결함을 넣지 않는다. injected variant는 `config/qa-ground-truth-v4.json`에서 시나리오별 `fault_id`를 읽어 `--fault`로 주입한다. control 시나리오는 `fault_id`가 `null`이므로 두 variant 모두 주입 없이 실행된다.

기대 결과는 결함 5개 전부 `FAIL`, control 3개 전부 `PASS`다. 결함 시나리오가 `PASS`로 나오면 오라클이 그 결함을 못 보는 것이고, control이 `FAIL`로 나오면 오라클이 오탐하는 것이다. 둘 다 하네스의 결함이지 게임의 결함이 아니다.

주입은 `--fault` 인자로만 일어나며 `Assets/Scripts/QA/QaFaultInjection.cs`가 시나리오 범위를 검사한다. 시나리오와 맞지 않는 fault는 활성화되지 않는다. `--bridge-scenario-id`로 v4 시나리오 id를 따로 넘기는 이유가 이것이다 — charter는 legacy 시나리오에서 오지만 fault 범위는 v4 시나리오 기준으로 판정되어야 한다.

**ground truth는 에이전트 채널로 새지 않는다.** `fault_id`와 `ground_truth`는 러너와 평가자만 본다.

## 4. baseline과 회귀 diff

### 승인

깨끗하다고 판단한 `run` 산출물을 baseline으로 고정한다.

```sh
uv run --locked python -m qa_smoke.cli baseline set QAArtifacts/runs/<run> \
  --path QAArtifacts/regression/baseline.json
```

인자는 스위트 디렉터리가 아니라 **run 루트**다. 내부에서 `<run>/v4-core/suite-manifest.json`을 읽는다. baseline은 `v4-core`만 기록한다.

### 비교

```sh
uv run --locked python -m qa_smoke.cli validate-faults \
  --build ... --suite v4-core \
  --baseline QAArtifacts/regression/baseline.json
```

`run --baseline`은 기존 단일 clean 회귀 비교를 유지한다. `validate-faults --baseline`은 `baseline set`이 만든 시나리오 id를 paired clean id에 맞춰 비교한다. injected variant는 clean baseline 비교에서 제외하고 현재 deterministic oracle 결과로 별도 표시한다. 따라서 variant 접두사를 손으로 붙인 baseline을 만들 필요가 없다.

paired 비교의 `regression-diff.json`은 `qa-regression-diff/v2`이며 다음 공개 키를 갖는다.

- `baseline_scope: paired-clean-only`
- `clean_baseline_diffs`: 승인 clean baseline과 현재 clean variant의 diff
- `injected_current_results`: baseline과 비교하지 않은 현재 injected oracle 결과, 기대 verdict, 일치 여부

`report.md`도 clean baseline diff와 injected current 결과를 별도 절로 표시한다. injected 기대값은 비공개 authoritative scenario→fault 매핑으로 정하고, 실행 manifest의 suite·scenario·`fault_id`가 이 등록값과 정확히 일치하는지 먼저 검증한다. 결함이 등록된 시나리오는 `FAIL`, control은 `PASS`여야 성공이다. current clean이 `PASS`가 아니거나 clean diff에 `NEW FAIL`/`STILL FAIL`이 있거나 injected current가 이 기대값과 다르면 종료 코드 `1`이다. 어느 variant든 `ERROR`이거나 clean/injected 쌍 계약이 잘못되면 종료 코드 `2`다.

| DiffKind | 조건 |
|---|---|
| `NEW FAIL` | PASS → FAIL, 또는 baseline에 없던 시나리오가 FAIL |
| `FIXED` | FAIL → PASS |
| `STILL FAIL` | FAIL → FAIL |
| `NEWLY NOT_REACHED` | PASS → NOT_REACHED |
| `FLAKY` | 양쪽 중 하나라도 stability가 `flaky` |
| `ERROR` | 현재 verdict가 ERROR |
| `NEW SCENARIO` | baseline에 없고 FAIL도 아님 |
| `UNCHANGED` | 그 외 |

`FLAKY`와 `ERROR`가 verdict 비교보다 우선한다. 실행 자체가 실패한 것을 "고쳐짐"으로 읽지 않기 위해서다. baseline에만 있고 현재에 없는 시나리오는 diff에서 제외된다.

### 종료 코드

| 코드 | 상황 |
|---|---|
| `0` | (`run`) 전부 PASS / (`run --baseline`) 회귀 없음 / (`validate-faults`) clean은 PASS이고 injected는 fault=FAIL, control=PASS / (`validate-faults --baseline`) 여기에 clean 회귀도 없음 |
| `1` | `run` 결과가 전부 PASS가 아님 / paired current clean이 non-PASS / `NEW FAIL` 또는 `STILL FAIL` 존재 / paired injected current가 기대 verdict와 다름 |
| `2` | `ERROR` 존재 또는 paired suite/manifest 계약 불일치 — 판정 불가, 인프라 문제로 취급 |

## 5. 판정 우선순위

`verdict.json`의 `final_verdict`는 다음 순서로 결정된다. v4 스위트는 실행 후 원본 trace(`steps.jsonl`)에 v4 오라클을 다시 적용해 `verdict.json`을 `qa-run-verdict/v2`로 덮어쓰되, legacy 필드는 보존한다.

1. `execution_status != completed` → **ERROR**
2. `oracle_verdict == fail` → **FAIL**
3. `oracle_verdict == not_evaluated` → **NOT_REACHED**
4. 그 외 → **PASS**

`NOT_REACHED`가 `PASS`와 구분되는 것이 핵심이다. 목표 지점에 도달하지 못해 오라클이 판정할 입력을 못 받은 것과, 판정해서 통과한 것은 다른 사건이다. 회귀 diff에서 `PASS → NOT_REACHED`가 별도 종류인 이유도 같다.

## 6. 결정론적 게이트 (legacy 9개)

`legacy-contract` 집합은 별도 게이트를 갖는다. 이쪽은 결함/control을 한 번에 돌려 **6/6 탐지, 0/3 오탐**을 강제한다.

```sh
uv run --locked python -m qa_smoke.benchmark \
  --game-exe QAArtifacts/bridge-player/windows/VampireSurvivorsClone.exe \
  --output QAArtifacts/gate \
  --seed 9101 \
  --deterministic-gate \
  --headless
```

게이트는 실행 전에 요청을 검증한다. 승인된 9개 시나리오 전체여야 하고, 시드는 정확히 하나여야 하며, `--mode qa --policy heuristic`이어야 한다. LLM이나 hybrid policy는 받지 않는다.

실행 후에는 산출물마다 다음을 확인한다.

- `manifest.json`의 scenario id, seed, preset, `scenario_fingerprint`가 일치하는가
- `execution_status == completed`, `coverage_status == reached`인가
- `evidence_refs`가 비어 있지 않고 전부 실제 `steps.jsonl`의 observation/event id로 해소되는가
- 에이전트 채널(`steps.jsonl` + `*request*.jsonl`)에 `ground_truth`나 `fault_id` 문자열이 없는가
- 결함 시나리오는 `oracle_verdict == fail`, control은 `pass`인가

하나라도 어긋나면 `contract_error`로 종료 코드 `2`를 낸다. `--deterministic-gate` 없이 실행하면 위 검사는 생략되고 return code만 집계한다.

## 7. 산출물 배치

```
QAArtifacts/runs/<uuid>/
├── report.md                        # validate-faults paired 결과 또는 --suite all 결과
├── regression-diff.json             # validate-faults --baseline 또는 --suite all --baseline
├── v4-core/
│   ├── clean/                       # validate-faults paired clean variant
│   ├── injected/                    # validate-faults current injected variant
│   ├── suite-manifest.json          # run 단일-variant 실행 시
│   ├── report.md                    # run 단일-variant verdict/diff 표
│   ├── regression-diff.json         # run --baseline 지정 시
│   └── <scenario-id>/<seed>/        # run 단일-variant 실행 시
│       ├── manifest.json
│       ├── steps.jsonl              # 에이전트 채널 (raw trace)
│       └── verdict.json             # qa-run-verdict/v2
└── legacy-contract/
    └── ...
```

`validate-faults`는 clean/injected 두 suite root를 합치므로 `report.md`와 `regression-diff.json`을 run 루트에 둔다. `--suite all`도 같은 위치를 쓴다. 단일 variant인 `run`은 해당 suite root에 둔다.

## 8. 이 층이 답하지 않는 것

- **에이전트가 결함을 보고했는가** — `agent_detection` 축의 질문이며 [BYOK Bridge 가이드 §3-4](BYOK_BRIDGE.ko.md)에 있다. v4 하네스는 오라클만 본다.
- **시드 간 안정성** — 현재 스위트는 시나리오당 시드 하나만 돌린다. `ScenarioResult.stability`는 `flaky`를 표현할 수 있지만 CLI는 항상 `stable`로 기록한다. 반복 실행 기반 flaky 판정은 아직 없다.
- **결함 5개 밖의 회귀** — 오라클이 없는 동작은 어떤 verdict도 만들지 않는다. 새 결함군을 다루려면 `REGISTERED_V4_ORACLES`에 오라클을 추가하고 `evaluate_v4_oracle`을 확장해야 한다.
