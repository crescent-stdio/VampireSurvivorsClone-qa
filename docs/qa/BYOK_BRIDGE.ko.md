# BYOK QA Bridge 실행 가이드

이 경로는 기존 `QA Gameplay`/ML-Agents lane을 대체하지 않는다. 별도 Windows 플레이어가 실제 `Main Menu -> Level 1` 흐름을 시작하고, `QABridge`가 매 의사결정 시점의 게임 상태를 JSON으로 내보내며 Python agent의 action을 적용한다. 게임은 action을 기다리는 동안 일시정지되고, action이 도착하면 지정된 horizon 동안 계속 움직인다. API 응답 대기 중에는 직전 이동을 유지하므로 매 프레임 API를 호출하지 않는다.

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

## 4. 동작과 종료 조건

- planner는 매 프레임이 아니라 기본 6초 horizon, 중요한 event, stall 또는 terminal 상태에서 호출된다.
- 상태에는 player 위치/체력/레벨/경험치, 가까운 적과 chest, upgrade dialog, game-over, 최근 event가 포함된다.
- action에는 이동 벡터, upgrade/inventory 선택, restart, 다음 horizon과 판단 근거가 포함된다.
- 게임이 끝났고 restart budget을 소진하면 다음 API 호출 전에 종료한다. 따라서 terminal 상태에서 `restart -> observe`가 반복되는 무한 API loop를 만들지 않는다.
- 리포트는 trajectory, event, anomaly, token usage, 비용 추정이 가능한 provider usage를 함께 보존한다.

`QAArtifacts/`와 `.venv/`는 git에 포함하지 않는다. 커밋 전에는 `git status --short`, `git diff --check`, 전체 pytest와 Bridge player smoke를 다시 실행한다.
