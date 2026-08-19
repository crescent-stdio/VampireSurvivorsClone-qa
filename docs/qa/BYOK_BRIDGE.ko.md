# BYOK QA Bridge 실행 가이드

이 경로는 기존 `QA Gameplay`/ML-Agents lane을 대체하지 않는다. 별도 Windows 플레이어가 실제 `Main Menu -> Level 1` 흐름을 시작하고, `QABridge`가 매 의사결정 시점의 게임 상태를 JSON으로 내보내며 Python agent의 action을 적용한다. 게임은 action을 기다리는 동안 일시정지되고, action이 도착하면 지정된 horizon 동안 계속 움직인다. 매 프레임 API를 호출하지는 않는다. protocol version은 `1.5`다.

## 1. 준비와 검증

저장소 루트의 PowerShell에서 실행한다.

```powershell
py -3 -m uv sync --locked --extra trainer
$env:PATH = 'C:\Program Files\Git\bin;' + $env:PATH
py -3 -m uv run --locked --extra trainer pytest -q
```

Unity 프로젝트 버전은 `6000.0.80f1`이다. Bridge 전용 플레이어는 production scene 순서를 유지해 Main Menu에서 시작한다.

```powershell
.\tools\build_qa_player.ps1 `
  -UnityExe 'C:\path\to\6000.0.80f1\Editor\Unity.exe'
```

빌드 결과는 추적되지 않는 `QAArtifacts\bridge-player\windows\`에 생성된다.

## 2. API 없이 Bridge smoke

먼저 heuristic policy로 빌드, scene 진입, 관측/action 교환, 리포트 생성을 확인한다.

```powershell
.\tools\run_smoke.ps1 `
  -Policy heuristic `
  -Mode qa `
  -MaxSimulationSeconds 30 `
  -MaxSteps 40 `
  -Headless
```

`-Headless`를 빼면 게임 창을 직접 볼 수 있다. 결과는 기본적으로 `QAArtifacts\bridge-runs\latest\report.md`와 `report.json`에 기록된다.

## 3. BYOK LLM 자율 플레이

아래 wrapper는 API key를 마스킹 입력으로 한 번만 받아 해당 프로세스에서만 보관한다. key는 repository나 report에 저장하지 않는다.

```powershell
.\tools\run_observed_byok_qa.ps1 `
  -Model 'gpt-4o-mini' `
  -Objective '전체적으로 동쪽 끝까지 진행하되, 적을 피하고 가까운 보물상자를 능동적으로 수집하면서 최대한 오래 생존하라.' `
  -MovementConstraint east `
  -MaxSimulationSeconds 120 `
  -PlanHorizonSeconds 6
```

창을 직접 관찰하는 실행이 기본이며 `run_observed_byok_qa.ps1`은 headless 옵션을 주지 않는다. `MovementConstraint`는 `free`, `east`, `west`, `north`, `south` 중 하나다. 방향은 매 action을 한 축으로 강제하는 규칙이 아니라 장기 진행 목표이며, LLM은 생존과 chest 접근을 위해 횡이동이나 짧은 후퇴를 선택할 수 있다.

환경 변수 방식도 사용할 수 있다.

```powershell
$env:OPENAI_API_KEY = '<session-only key>'
.\tools\run_smoke.ps1 `
  -Policy llm `
  -Model 'gpt-4o-mini' `
  -Objective '북쪽으로 탐색하면서 최대한 오래 생존하고 가까운 chest를 수집하라.' `
  -MovementConstraint north
Remove-Item Env:OPENAI_API_KEY
```

OpenAI 호환 endpoint는 `-ApiUrl`로 바꿀 수 있다. 모델 ID는 해당 계정과 endpoint가 실제로 제공하는 정확한 문자열이어야 한다.

## 3-1. hybrid policy: 매 프레임 규칙기반 생존 보조

`--policy llm`은 LLM이 지시한 벡터를 horizon 내내 보정 없이 그대로 유지한다. 위협이 포화되면 (`world.danger_score`가 1.0에 고정) 회피가 전혀 일어나지 않고, `danger_spike`/`low_health` 인터럽트는 임계값을 넘는 전이에서만 발동하는 edge-trigger라 그 구간에서 다시 울리지 않는다. 그 결과 5초짜리 horizon 하나마다 체력이 크게 깎인다.

`--policy hybrid`는 동일한 LLM planner를 쓰되, bridge가 **매 시뮬레이션 프레임** 규칙기반 회피 항을 섞는다.

```
executed = normalize(llm_vector + escape_vector * W * clamp01(0.35 + danger))
```

- `W`는 `--assist-survival-weight` (기본 `0.6`). `steer` 경로의 `1.4`를 절반으로 낮춘 값으로, 회피는 하되 LLM의 의도를 뒤집지 않는 수준을 노린다. 이 값은 아직 실측으로 검증되지 않았으므로 필요하면 sweep한다.
- LLM 벡터는 **대체되거나 재조준되지 않는다.** chest 자동 조준과 heading 강제(`EnforceForwardProgress`)는 적용하지 않는다. 후자는 위협 포화 시 필요한 역방향 회피를 상쇄해 버리기 때문이다.
- 요청한 벡터 크기는 보존된다. `Character.Move`가 크기를 가속도 배율로 쓰므로, 정규화하면 이동 속도가 조용히 바뀌어 llm arm과 비교가 불가능해진다.

보조는 charter의 `control_policy.automatic_enemy_avoidance`와 system prompt에 **명시된다.** bridge가 벡터를 건드리지 않는다고 알고 있는 agent는 실제 궤적과 지시 벡터의 차이를 게임 결함으로 오인해 `agent_detection` 축에 거짓 양성을 남기기 때문이다.

`--assist-survival-weight`는 hybrid가 아닌 policy와 함께 쓰면 조용히 무시되지 않고 오류가 된다. 두 policy는 command field로 갈리므로 **플레이어 빌드 하나로 양쪽을 모두 실행**할 수 있고, 동일 조건 A/B가 가능하다.

```sh
QA_BRIDGE_OUTPUT=$PWD/QAArtifacts/bridge-runs/ab-9102-hybrid \
  ./scripts/qa/run-bridge-smoke.sh --scenario easy-health-ratio --seed 9102 \
    --mode qa --policy hybrid --assist-survival-weight 0.6 --model "$QA_MODEL"
```

결과 비교는 `report.json`의 `metrics.max_level_time`, `metrics.event_counts.player_death`, `metrics.bridge_assist`, 그리고 `verdict.json`의 `oracle_verdict`를 함께 본다. 생존이 늘었더라도 주입된 결함을 더 이상 탐지하지 못하면 개선이 아니라 회귀다.

deterministic benchmark gate는 여전히 `heuristic`만 받는다.

## 3-2. 중단된 실행과 판정 복구

LLM 실행은 rate limit(HTTP 429), 응답 잘림, 플레이어 크래시로 중단될 수 있다. 중단돼도 그때까지 기록된 관측은 `steps.jsonl`에 그대로 남아 있고, 오라클은 그 trace만으로 판정할 수 있다.

- 오라클 `fail`은 **중단된 실행에서도 보고된다.** 실패는 기록된 관측이 불변식을 위반했기 때문에 발생하며 evidence ref가 그 관측을 가리킨다. trace가 잘렸다고 없던 위반이 생기지는 않는다.
- 오라클 `pass`는 **중단된 실행에서 `not_evaluated`로 격하된다.** "여기까지는 위반이 없었다"는 "위반이 없다"가 아니다. `verdict.json`의 `trace_completeness`가 `partial`이면 이 판정이 앞부분만 보고 내려진 것이다.
- `final_verdict`는 완주하지 않은 실행에서 항상 `ERROR`이며, 종료 코드도 그대로다. 결정론적 벤치마크 게이트는 여전히 완주한 heuristic 실행만 받는다.

이미 쌓여 있는 아티팩트는 재평가할 수 있다.

```sh
uv run --locked python -m qa_smoke.reevaluate --dry-run QAArtifacts/bridge-runs/<run>/
uv run --locked python -m qa_smoke.reevaluate QAArtifacts/bridge-runs/<run>/
```

`execution_status`는 절대 바뀌지 않는다. `steps.jsonl`은 append 모드로 열리므로 출력 디렉터리를 재사용하면 여러 세션이 한 파일에 쌓이는데, 재평가는 manifest의 `run_id`와 일치하는 행만 판정한다. 섞인 채로 평가하면 결함 주입 세션이 무결함 세션에 새어 들어가 **대조군 거짓 양성**을 만든다.

## 3-3. LLM API 실패 처리

일시적 실패는 실행을 끝내지 않는다.

- 재시도 대상: HTTP 429, 5xx, 408, 425, 연결 오류, 타임아웃. 대기 시간은 `Retry-After` 헤더 → `x-ratelimit-reset-*` → 응답 본문의 `try again in ...` 순으로 읽는다. 실제 429는 본문에만 값을 담아 보내는 경우가 많다.
- 재시도하지 않음: 400, 401, 403, 404, 413, 422. 설정 오류라서 재시도해도 같은 결과이며 quota만 소모한다.
- 한계: 시도 3회(`--llm-max-attempts`), 회당 최대 20초, 요청당 누적 대기 45초(`--llm-retry-budget-seconds`). `--llm-max-attempts 1`은 재시도 이전 동작으로 되돌린다.
- 계획 응답 상한은 700 토큰이며, 잘리면 1400으로 한 번 더 시도한 뒤 실패한다.

재시도 통계는 `report.json`의 `metrics.api_usage`에 `llm_retries`, `llm_retry_wait_ms`, `llm_http_attempts`로 기록된다. 실패한 호출이 소모한 토큰도 함께 집계되므로, 다음 실행의 TPM 예산을 이 값으로 잡을 수 있다.

## 3-4. agent_detection: 에이전트가 결함을 찾았는가

`oracle_verdict`와 `agent_detection`은 서로 다른 질문이다.

- `oracle_verdict: fail` = **결함이 존재한다.** 사람이 작성한 규칙 기반 불변식 검사기가 관측에서 위반을 찾았다는 뜻이다.
- `agent_detection: match` = **에이전트가 그걸 보고했다.** 둘은 독립이며, 실제로 첫 측정에서 모든 LLM 실행이 `oracle_verdict: fail` + `agent_detection: miss`였다.

판정은 `config/qa-detection-rubric.json`의 결정론적 키워드 루브릭으로 내린다. 결함마다 `topic_terms`(어느 하위 시스템인가)와 `symptom_terms`(무엇이 잘못됐는가)를 두고, **같은 텍스트 안에서 양쪽이 모두 맞아야** 후보로 인정한다. 에이전트는 관측의 18번 중 17번에서 "health"를 쓰므로 topic만으로는 서술과 보고를 구분할 수 없다.

채점 대상은 **에이전트가 직접 쓴 자유 텍스트뿐**이다(`plan`, `hypothesis`, `qa_observation`, `expected_effect`, `reflection.summary`, 가설 statement, 최종 보고). `decision.arguments`는 제외한다 — 차터가 매 `direct_steer`에 `interrupt_health_ratio`를 주입하므로, 통째로 스캔하면 아무것도 보고하지 않은 실행에서 18스텝 중 16번이 걸린다.

| 상황 | 결과 |
|---|---|
| `--policy heuristic` | `not_evaluated` (에이전트 텍스트 채널 자체가 없음) |
| 결함 없음 + 확정 가설/버그 후보 보고 | `false_positive` |
| 결함 없음 + 침묵 | `not_evaluated` (대조군의 침묵은 정답이지 성과가 아님) |
| 에이전트가 관측할 수 없는 결함 | `not_evaluated` + 사유 |
| 오라클이 통과했거나 위반 전이가 없음 | `not_evaluated` |
| 키워드 적중 + 오라클이 지적한 전이 인용 | **`match`** |
| 부분 trace | `not_evaluated` (앞부분만으로 "보고한 적 없음"을 증명할 수 없음) |
| 그 외 | **`miss`** |

결함이 주입된 실행에서는 `false_positive`가 나오지 않는다. 다른 결함을 자신 있게 보고한 에이전트는 `miss`다 — 주입된 것을 못 찾은 건 사실이고, 그 다른 주장의 타당성 판단은 사람 리뷰어의 몫이다.

`health_bar_desync`와 `experience_display_drift`는 `observable_in_agent_channel: false`다. 둘 다 raw 값과 화면 표시값의 불일치인데 `state_channels`가 `player_view`를 에이전트 관측에서 제거하므로 **원리적으로 탐지할 수 없다.** 이를 `miss`로 채점하면 에이전트 능력이 아니라 채널 설계를 측정하게 된다.

### 한계를 분명히

- `match`는 "에이전트가 버그를 찾았다"가 아니라 **"에이전트가 결함을 지목하는 표현을 쓰면서 오라클이 문제 삼는 전이를 인용했다"**는 뜻이다. `agent-detection.json`이 이를 명시한다.
- `annotations.jsonl`과 `aggregate_annotations`(리뷰어 3명 다수결)가 **버그 타당성의 유일한 권위**로 남는다. 자동 채점은 별도 축이며 그쪽에 병합되지 않는다.
- 키워드 방식이라 프롬프트 문구에 따라 값이 흔들린다. `agent-detection.json`에 `rubric_version`과 `prompt_version`을 함께 기록하니, 탐지율은 **이 둘이 고정된 범위 안에서만** 비교할 수 있다.
- `agent_detection`은 `final_verdict`에 영향을 주지 않는다. 게임의 판정이 에이전트의 서술 능력에 좌우돼선 안 된다.

저장된 아티팩트는 `python -m qa_smoke.reevaluate`로 함께 채점된다.

## 3-5. 검사관(inspector): 조향과 버그 탐지의 분리

조향과 버그 탐지는 성격이 다른 작업이다.

| | 조향 | 버그 탐지 |
|---|---|---|
| 호출 횟수 | 스텝마다(약 19회) | **1회** |
| 필요한 문맥 | 누적 대화 이력 (~28k/콜) | 관측 목록만 |
| 지연 민감도 | 높음 (게임이 대기) | 없음 (런 종료 후) |
| 필요한 능력 | 방향 결정 | 산술과 정밀한 대조 |

`--inspector-model`은 두 번째 작업만 별도 모델에 맡긴다. `--policy`와 **직교**하므로 네 조합이 모두 유효하다.

| 조향 | 검사관 | 용도 |
|---|---|---|
| `llm` | 없음 | 기존 동작 |
| `llm` | 있음 | LLM 능력 평가 + 탐지 측정 |
| `heuristic` | 있음 | 저비용·결정론적 회귀 검사 |
| `heuristic` | 없음 | 결정론적 게이트 (변경 없음) |

```sh
./scripts/qa/run-bridge-smoke.sh --scenario easy-health-ratio --seed 9102 \
  --mode qa --policy heuristic --fault health_ratio_out_of_range \
  --inspector-model gpt-5.6-luna --inspector-effort low
```

### 왜 분리했는가

측정 결과 `gpt-4o-mini`는 조향은 하지만 탐지를 못 한다. v6 프롬프트가 시킨 계산은 정확히 수행하고도(`96/100 = 0.96`) 보고값 1.25와 대조하지 않고 "valid"로 결론냈다. 이를 고치려고 조향까지 강한 모델로 올리면 19콜 전부가 비싸진다.

실측: LLM 조향 런은 프롬프트 50만 토큰/19콜. 검사관은 관측 18개를 압축해 **1콜 약 20k 토큰**이다.

### 정제 — 검사관은 답을 받지 않는다

검사관 입력에는 세 계층이 모두 적용되며 어느 것도 생략할 수 없다.

1. `state_channels.build_agent_observation` — `fault_id`, `ground_truth`, `player_view` 등 제거
2. `planners.compact_observation` — 화이트리스트, 엔티티 목록 8개로 제한
3. `memory.sanitize_agent_channel` — `oracle`, `oracle_verdict`, `verdict`, `manifest`를 제거하는 **유일한** 계층

`recorder.steps`의 관측은 **원본**이라 `player_view`가 그대로 들어 있다. 그냥 넘기면 답을 주는 것과 같다.

요청 로그는 `inspector-request.jsonl`로 저장한다. 결정론적 게이트가 `*request*.jsonl`을 누출 검사 대상으로 스캔하므로, 이 이름이 곧 무료 검증이 된다. 응답은 별도 파일에 둔다 — 결함을 정확히 서술한 검사관이 fault_id에 가까운 표현을 쓸 수 있기 때문이다.

### 채점 — 형용사가 아니라 숫자로

검사관은 구조화된 findings를 반환한다.

```json
{"field": "player.health_ratio", "computed_value": 0.96,
 "reported_value": 1.25, "statement": "...", "evidence_refs": ["...-obs-00000003"]}
```

`computed_value != reported_value`이고 `evidence_refs`가 오라클이 문제 삼은 전이와 겹칠 때만 `match`다. 키워드 루브릭은 실제 발견을 표현 차이로 세 번이나 놓쳤고(`exceed`/`exceeds`, `inconsistency`/`inconsistent`, `disagree`), 매번 동의어를 더하는 방향은 점수를 올리는 쪽이라 드리프트를 만든다. 수치 판정에는 그 경로가 없다 — 문장이 아무리 요란해도 두 값이 같으면 발견이 아니다.

키워드 루브릭은 in-loop 플래너 산문 채점이라는 원래 역할로만 남는다.

### 실측 결과

| | 결함 주입 (llm 조향) | 대조군 (heuristic 조향) |
|---|---|---|
| `oracle_verdict` | `fail` | `pass` |
| `agent_detection` | **`match`** | **`not_evaluated`** |
| 검사관 findings | 18건 | **0건** |
| API 호출 | 20 | **1** |

검사관은 체력이 변할 때마다(1.0 → 0.96 → … → 0.66) 매번 계산해 1.25와 대조했고, 결함 없는 런에서는 아무것도 만들어내지 않았다.

### reasoning 모델

`gpt-5.6-luna` 같은 reasoning 모델은 `max_tokens`를 거부하고 `max_completion_tokens`를 쓰며, 숨은 추론이 출력 예산을 먼저 소모한다. 하네스가 자동으로 판별해 예산을 8000으로 올리고 `--inspector-effort`로 `reasoning_effort`를 전달한다. 실측 reasoning 토큰은 호출당 288~826개로 비용에 거의 영향이 없다.

### 한계

- `match`는 "에이전트가 버그를 찾았다"의 **구조화된 증거**이지 사람의 타당성 판정이 아니다. `annotations.jsonl`과 `aggregate_annotations`(리뷰어 3명 다수결)가 여전히 유일한 권위다.
- `agent_detection`은 이제 **에이전트 시스템**을 측정한다. in-loop 플래너만 측정하던 이전 값과 직접 비교할 수 없다.
- 검사관 실패는 anomaly로 격하되며 절대 `fatal_error`가 되지 않는다. 네트워크 문제로 9개 시나리오 게이트가 무너지면 안 되기 때문이다.

## 4. 동작과 종료 조건

- planner는 매 프레임이 아니라 기본 6초 horizon, 중요한 event, stall 또는 terminal 상태에서 호출된다.
- API 응답을 기다리는 동안 게임은 기본적으로 정지한다(`pause_reason=event_decision_boundary`). `continue_during_planning`이 성립하는 horizon에서만 직전 벡터를 유지한 채 시뮬레이션이 이어지며, 이때도 hybrid의 매 프레임 보조는 동일하게 적용된다.
- 상태에는 player 위치/체력/레벨/경험치, 가까운 적과 chest, upgrade dialog, game-over, 최근 event가 포함된다.
- action에는 이동 벡터, upgrade/inventory 선택, restart, 다음 horizon과 판단 근거가 포함된다.
- 게임이 끝났고 restart budget을 소진하면 다음 API 호출 전에 종료한다. 따라서 terminal 상태에서 `restart -> observe`가 반복되는 무한 API loop를 만들지 않는다.
- 리포트는 trajectory, event, anomaly, token usage, 비용 추정이 가능한 provider usage를 함께 보존한다.

`QAArtifacts/`와 `.venv/`는 git에 포함하지 않는다. 커밋 전에는 `git status --short`, `git diff --check`, 전체 pytest와 Bridge player smoke를 다시 실행한다.
