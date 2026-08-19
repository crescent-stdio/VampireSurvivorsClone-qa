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

## 4. 동작과 종료 조건

- planner는 매 프레임이 아니라 기본 6초 horizon, 중요한 event, stall 또는 terminal 상태에서 호출된다.
- API 응답을 기다리는 동안 게임은 기본적으로 정지한다(`pause_reason=event_decision_boundary`). `continue_during_planning`이 성립하는 horizon에서만 직전 벡터를 유지한 채 시뮬레이션이 이어지며, 이때도 hybrid의 매 프레임 보조는 동일하게 적용된다.
- 상태에는 player 위치/체력/레벨/경험치, 가까운 적과 chest, upgrade dialog, game-over, 최근 event가 포함된다.
- action에는 이동 벡터, upgrade/inventory 선택, restart, 다음 horizon과 판단 근거가 포함된다.
- 게임이 끝났고 restart budget을 소진하면 다음 API 호출 전에 종료한다. 따라서 terminal 상태에서 `restart -> observe`가 반복되는 무한 API loop를 만들지 않는다.
- 리포트는 trajectory, event, anomaly, token usage, 비용 추정이 가능한 provider usage를 함께 보존한다.

`QAArtifacts/`와 `.venv/`는 git에 포함하지 않는다. 커밋 전에는 `git status --short`, `git diff --check`, 전체 pytest와 Bridge player smoke를 다시 실행한다.
