# LLM 게임플레이 QA 사용법

이 문서는 두 가지 LLM 평가를 실제로 실행하고 결과를 읽는 방법을 설명한다.

- **Track A — 자율 탐색 버그 후보 수집**: 버그를 주입하지 않은 정상 빌드에서 LLM 플레이어가 게임을 진행하고, 관찰된 이상 징후를 후보로 기록한다.
- **Track B — 주입 버그 시나리오 탐지**: 같은 플레이를 정상판과 결함판에서 대조 실행하고, 검사 모델이 비공개 정답 수치 관계를 보고했는지 계산한다.

두 결과는 결정적 회귀 게이트(`run`, `validate-faults`, `baseline`)와 별개의 참고 지표다. Track A에서 후보가 0개여도 게임에 버그가 없다는 뜻은 아니며, Track B의 첫 결과도 합격선이 아니라 기준선이다.

## 1. 준비

저장소 루트에서 실행한다.

- Unity `6000.0.80f1`로 평가할 커밋의 QA Bridge 플레이어를 새로 빌드한다.
- Python `3.10.12`, `uv 0.12.x`를 준비하고 의존성을 고정 설치한다.
- `QA_API_KEY` 또는 `OPENAI_API_KEY`를 시크릿 매니저를 통해 셸 환경에 주입한다. 키를 명령행, 저장소 파일, 결과 폴더, URL에 기록하지 않는다.
- 필요할 때만 `QA_API_URL` 환경변수 또는 `--api-url`로 호환 엔드포인트를 지정한다. 명령행 옵션이 환경변수보다 우선한다.

```sh
uv sync --locked
scripts/qa/build-bridge-player.sh
```

빌드가 정상인지 먼저 결정적 하네스로 확인한다. 이 단계의 `run`은 LLM 평가가 아니다.

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all \
  --output QAArtifacts/acceptance/clean-harness \
  --headless
```

정상판 빌드는 결함 목록 없이 실행되는 하나의 깨끗한 플레이어여야 한다. Track B는 실행 시점에 평가용 결함 하나를 켜므로 결함별 Unity 빌드를 따로 만들지 않는다.

## 2. Track A: 정상 빌드 자율 탐색

빈 결과 폴더에서 다음을 실행한다.

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-a \
  --headless
```

고정 일정은 8개 미션 × 3개 시드(`9101`, `9102`, `9103`) = 24개 게임 트레이스다. 핵심 미션은 메뉴/레벨 진입, 생존 전투, 성장·강화, 아이템·상자·인벤토리, 사망·재시작 격리이고, 장기 미션은 지속 전투, 다중 레벨 성장, 아이템·상자·범위 효과다.

중단된 동일 캠페인을 이어갈 때만 `--resume`을 붙인다.

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-a \
  --headless --resume
```

`--resume` 없이 이미 내용이 있는 폴더를 사용하면 거부된다. 빌드, 모델, 프롬프트, 스키마, 시드 등 고정 입력이 바뀌면 새 결과 폴더를 사용한다.

### Track A 결과 읽기

- 요약: `exploration-report.ko.md`
- 감사용 원본: `exploration-report.json`
- 실행 목록: `traces/<mission>/<seed>/`
- 검사 감사 로그: `inspections/<opaque-trace-id>/pass-1..3/`

후보는 `evidence-linked candidate`와 `validated-invariant candidate`로 나뉜다. 전자는 관찰 근거가 하나 이상 있는 후보이고, 후자는 기록된 런타임 불변식 검증도 일치한 후보다. 둘 다 확정 버그라는 뜻은 아니다. `planner_only`, `inspector_only`, `shared`, `union`은 발견 표면을 구분하며, 런타임 오라클 결과는 LLM union에 섞지 않는다. 같은 미션에서 다른 고정 시드로 동일 정규화 후보가 다시 나오면 `reproduced=true`가 된다.

현재 구현은 트레이스마다 검사 모델을 3회 독립 호출한다. Track A는 1회만 나온 후보도 버리지 않고 `1/3`, `2/3`, `3/3` 동의도를 기록한다. 따라서 Track A 후보 수를 다수결 통과 수로 해석하면 안 된다.

## 3. Track B: 정상판/버그판 대조 탐지

빈 결과 폴더에서 다음을 실행한다.

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-b \
  --headless
```

고정 일정은 다음과 같다.

- 비채점 파일럿 33개: 비공개 결함 11개 × 시드 3개. 공식 점수에서 제외한다.
- 공식 대조 66개: 각 파일럿 replay를 정상판과 결함판에서 각각 실행한다.
- 자율 대조 22개: 결함 11개에 대해 정상/결함 쌍을 LLM 플레이어로 실행한다.

따라서 전체 고정 실행은 145개 런치이며, 검사 논리 호출 계획은 693회다. 700회 제한은 두 명령 전체가 아니라 각 캠페인의 체크포인트별 제한이다. HTTP 재시도는 논리 호출 수와 별도로 기록된다.

`benchmark-detection`에는 `--resume` 옵션이 없다. 같은 출력 폴더에 같은 명령을 다시 실행하면 체크포인트와 캠페인 식별자가 정확히 일치할 때만 자동 재개한다. 입력을 바꿨다면 새 폴더를 사용한다.

### Track B 결과 읽기

- 종합: `detection-benchmark.json`, `detection-benchmark.ko.md`
- 공식 대조 점수: `metrics/official.json`, `metrics/official.ko.md`
- 자율 대조 점수: `metrics/autonomous.json`, `metrics/autonomous.ko.md`
- 비공개 파일럿·페어 정보: `pilots/`, `traces/`

공식 점수는 결정적 replay가 정상판과 결함판을 대조하므로 검사 모델의 탐지 능력을 보는 표면이다. 자율 점수는 LLM의 도달·조작·검사 전 과정을 포함한다. 공식 `run_id`, observation ID, event ID에는 결함명이나 시나리오명이 들어가지 않으며, 매핑은 평가기 내부 파일에만 남는다.

정답 탐지는 검사 결과가 다음 조건을 모두 만족할 때만 센다.

1. 실제 관찰된 숫자 필드를 지정한다.
2. 기대값과 관측값을 숫자로 제시한다.
3. 위반한 비교 관계(`==`, `!=`, `<=` 등)를 명시한다.
4. 유효한 observation 근거를 인용한다.
5. 비공개 결함의 목표 수치 관계와 일치한다.

현재 구현은 트레이스별 3회 검사에서 2회 이상 같은 결론이면 그 트레이스의 판정을 만든다. 유효한 다수결이 없으면 `INSPECTION_ERROR`다. 결함 트레이스의 유효 판정은 TP/FN, 정상 트레이스의 유효 판정은 FP/TN이 된다. 정상 또는 결함 쪽 한 트레이스라도 무효이면 해당 페어 전체를 점수에서 제외한다.

무효 상태는 `BASELINE_CONFLICT`(정상판 오라클 실패), `FAULT_NOT_ACTIVATED`(결함판 오라클이 실패하지 않음), `NOT_REACHED`(필요 지점 미도달), `UNOBSERVABLE`(목표 수치 구성 불가), `INSPECTION_ERROR`(검사 다수결 불가), `ERROR`(실행·아티팩트 실패)다. TP/FN/FP/TN, 탐지율, 정밀도, 특이도, 정상판 오탐률, 페어 성공률을 공식과 자율 표면에서 나누어 본다.

## 4. 종료 코드와 재개 안전성

| 코드 | 의미 |
|---:|---|
| `0` | 완전한 manifest와 보고서가 발행됨. 일부 트레이스가 `INSPECTION_ERROR`일 수 있지만 캠페인 인프라는 완료됨. |
| `2` | 인자·설정·빌드·인증·모델 초기화·체크포인트·replay·게임플레이·검사 계약·발행 중 캠페인 수준 오류. |

모델 한 번의 잘못된 응답은 해당 pass 실패로 기록한다. 반대로 빌드 해시, replay, 스키마, 아티팩트 무결성, 호출 상한, 결과 발행이 깨지면 fail-closed로 `incomplete` manifest를 남긴다. `status=complete`인 manifest 옆의 JSON/Markdown만 최종 결과로 사용한다.

완료된 아티팩트는 경로·크기·SHA-256이 모두 같을 때만 재사용된다. 변경·누락된 아티팩트와 의존 검사 결과는 무효화되어 `.superseded-inspections/` 또는 `.superseded-results/`로 이동한다. 체크포인트를 다른 출력 폴더에 복사하지 않는다.

## 5. 결정적 회귀 baseline (선택)

LLM 평가와 별개로 정상판과 주입 결함 시나리오의 하네스 오라클을 확인하려면 다음 순서를 사용한다.

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all \
  --output QAArtifacts/runs/clean \
  --headless

uv run --locked python -m qa_smoke.cli baseline set QAArtifacts/runs/clean \
  --path QAArtifacts/regression/baseline.json

uv run --locked python -m qa_smoke.cli validate-faults \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all \
  --output QAArtifacts/runs/fault-check \
  --baseline QAArtifacts/regression/baseline.json \
  --headless
```

`baseline set`의 인자는 run 루트다. `validate-faults --baseline`은 정상판을 baseline과 비교하고, 주입판은 비공개 시나리오 등록값에 따라 결함 시 `FAIL`, control 시 `PASS`인지 별도로 확인한다. 이 결정적 결과의 종료 코드와 보고서는 LLM 탐지율과 합치지 않는다. 자세한 규칙은 [V4 하네스 가이드](V4_HARNESS.ko.md)를 참고한다.

## 6. 검증 명령

코드와 계약을 빠르게 확인하려면 다음을 실행한다.

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

전체 Python 테스트 수집에는 선택적 trainer 의존성(PyTorch)이 필요하다.

```sh
uv sync --locked --extra trainer
uv run --locked --extra trainer pytest -q
```

Unity EditMode/PlayMode와 실제 API를 사용하는 145-launch acceptance는 별도로 실행해야 한다. 키, Unity 라이선스, 플레이어 빌드가 없는 환경에서는 해당 실행을 성공으로 간주하지 않는다.

관련 문서:

- [영문 상세 운영 가이드](LLM_EVALUATION_OPERATOR_GUIDE.md)
- [한국어 AI Agent QA 안내](AI_AGENT_QA_GUIDE.ko.md)
- [한국어 BYOK Bridge 안내](BYOK_BRIDGE.ko.md)
- [한국어 v4 하네스 안내](V4_HARNESS.ko.md)
