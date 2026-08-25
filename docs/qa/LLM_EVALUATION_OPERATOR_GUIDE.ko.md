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

### 결과 폴더 지정

`explore`와 `benchmark-detection`의 `--output`은 선택 옵션이다. 생략하면 `QAArtifacts/evaluation/track-a/<YYYYMMDD-HHMMSS>` 또는 `QAArtifacts/evaluation/track-b/<YYYYMMDD-HHMMSS>` 폴더를 새로 만들어 사용한다. 폴더 이름은 실행 시작 시각(로컬 타임존)이며, 같은 초에 두 번 실행하면 `-01`, `-02` 접미사가 붙는다. 폴더는 명령 시작 시점에 선점되므로 두 캠페인이 같은 폴더를 덮어쓰지 않는다.

`--resume`으로 중단된 Track A 캠페인을 이어갈 때는 반드시 원래 폴더를 `--output`으로 명시해야 한다. `explore --resume`을 `--output` 없이 실행하면 인자 오류로 거부된다.

### 실행 프로파일: `--profile full` / `--profile poc`

`explore`와 `benchmark-detection`은 `--profile` 옵션을 받는다. 기본값은 `full`이며, 문서 전체에서 말하는 고정 합격 일정이다. `poc`는 시연·연습용 축소 일정으로, 공식 수치를 대체하지 않는다.

| 항목 | `full` | `poc` |
|---|---:|---:|
| Track A 트레이스 | 8 미션 × 3 시드 = 24 | 3 미션 × 2 시드 = 6 |
| Track A 검사 논리 호출 계획 | 189 | 48 |
| Track B 파일럿 | 33 | 3 |
| Track B 공식 대조 페어 | 33 (66 런치) | 3 (6 런치) |
| Track B 자율 대조 페어 | 11 (22 런치) | 3 (6 런치) |
| 두 명령 합계 런치 | 145 | 21 |
| 검사 논리 호출 계획(cross-track) | 693 | 120 |

`poc`의 고정 부분집합은 다음과 같다.

- Track A 미션: `core-combat-survival`, `core-progression-upgrades`, `core-death-restart-isolation`. 시드: `9101`, `9102`.
- Track B 결함: `health_bar_desync`, `item_effect_not_applied`, `experience_display_drift`. 공식 대조 시드는 자율 시드 1개만 사용한다.

프로파일은 캠페인 해시와 `campaign-manifest.json`의 `profile` 필드에 기록된다. 따라서 같은 폴더에서 프로파일만 바꿔 `--resume`하거나 재개할 수 없고, 새 폴더가 필요하다. PoC 결과를 공식 탐지율로 보고하지 않는다.

### 화면을 보면서 실행하기 (headless를 쓰지 않는 경우)

`--headless`는 선택 옵션이며, **붙이지 않는 것이 기본값**이다. 즉 화면을 보면서 실행하려면 명령에서 `--headless`만 빼면 된다. 내부적으로 `--headless`는 Unity 플레이어를 `-batchmode -nographics`로 띄우고, 옵션을 빼면 1280×720 창 모드(전체화면 아님)로 띄운다.

필요 조건은 다음과 같다.

- macOS 로그인 세션의 실제 데스크톱에서 실행한다. SSH 원격 셸, GUI 없는 CI, 잠긴 화면에서는 사용할 수 없다.
- 실행 중 화면 보호기·자동 잠금·절전을 끈다. 잠금 화면으로 전환되면 렌더링이 멈춰 실행이 타임아웃될 수 있다. 필요하면 `caffeinate -disu uv run ...` 형태로 감싼다.
- 창을 닫거나 강제 종료하지 않는다. 플레이어 종료는 하네스가 담당하며, 수동 종료는 `infrastructure_error`로 기록된다.
- 145 런치(`full`)를 창 모드로 돌리면 그 시간 동안 해당 데스크톱을 다른 작업에 쓰기 어렵다. 관찰 목적이면 `--profile poc`부터 사용한다.

명령 예시는 다음과 같다. 결정적 하네스는 다음처럼 실행한다.

```sh
uv run --locked python -m qa_smoke.cli run \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --suite all \
  --output QAArtifacts/acceptance/clean-harness-visible
```

Track A 자율 탐색도 같은 방식으로 실행한다. `--output`을 생략하면 타임스탬프 폴더가 생성된다.

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --profile poc
```

Track B 대조 실행은 다음과 같다.

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --profile poc
```

화면 표시 여부는 캠페인 해시에 포함된다. `--headless`로 일부 실행한 Track A를 창 모드로 `--resume`하거나 그 반대로 재개하지 말고, 모드를 바꿀 때는 새 출력 폴더를 사용한다. 화면 표시 모드에서도 API 키, 고정 모델, 실행 일정과 판정 규칙은 동일하다.

증거 스크린샷(4장 참고)은 창 모드에서만 쓸 수 있다. 같은 에피소드를 두 모드로 캡처해 비교한 실측 결과는 다음과 같다.

| | `--headless` (`-batchmode -nographics`) | 창 모드 |
|---|---|---|
| `screenshot_error` | 없음 | 없음 |
| PNG 생성 | 됨 (13.5 KB) | 됨 (1.17 MB) |
| 내용 | **회색 단색** | 실제 게임 화면 |

즉 헤드리스 캡처는 실패하지 않는다. **오류 없이 성공으로 기록되고, 회색 단색 이미지가 저장된다.** `screenshot_error`는 비어 있고 `retained_screenshot_count`도 정상적으로 올라가므로, 보고서 수치만 봐서는 이 상태를 감지할 수 없다. 기존 스모크 레인(`scripts/qa/smoke.sh`)이 `-batchmode`만 쓰고 `-nographics`를 쓰지 않는 이유도 같다. 사람이 볼 증거가 필요하면 반드시 `--headless` 없이 실행한다.

## 2. Track A: 정상 빌드 자율 탐색

빈 결과 폴더에서 다음을 실행한다.

```sh
uv run --locked python -m qa_smoke.cli explore \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-a \
  --headless
```

`--profile full`(기본값)의 고정 일정은 8개 미션 × 3개 시드(`9101`, `9102`, `9103`) = 24개 게임 트레이스다. `--profile poc`는 3개 미션 × 2개 시드 = 6개 트레이스로 줄어든다. 핵심 미션은 메뉴/레벨 진입, 생존 전투, 성장·강화, 아이템·상자·인벤토리, 사망·재시작 격리이고, 장기 미션은 지속 전투, 다중 레벨 성장, 아이템·상자·범위 효과다.

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
- 보존된 증거 스크린샷: `screenshots/<opaque-trace-id>.png`

후보는 `evidence-linked candidate`와 `validated-invariant candidate`로 나뉜다. 전자는 관찰 근거가 하나 이상 있는 후보이고, 후자는 기록된 런타임 불변식 검증도 일치한 후보다. 둘 다 확정 버그라는 뜻은 아니다. `planner_only`, `inspector_only`, `shared`, `union`은 발견 표면을 구분하며, 런타임 오라클 결과는 LLM union에 섞지 않는다. 같은 미션에서 다른 고정 시드로 동일 정규화 후보가 다시 나오면 `reproduced=true`가 된다.

현재 구현은 트레이스마다 검사 모델을 3회 독립 호출한다. Track A는 1회만 나온 후보도 버리지 않고 `1/3`, `2/3`, `3/3` 동의도를 기록한다. 따라서 Track A 후보 수를 다수결 통과 수로 해석하면 안 된다.

보고서의 `screenshots` 배열과 요약의 `retained_screenshot_count`, `capture_error_count`는 4장에서 설명하는 증거 스크린샷을 가리킨다.

## 3. Track B: 정상판/버그판 대조 탐지

빈 결과 폴더에서 다음을 실행한다.

```sh
uv run --locked python -m qa_smoke.cli benchmark-detection \
  --build QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/evaluation/track-b \
  --headless
```

`--profile full`(기본값)의 고정 일정은 다음과 같다.

- 비채점 파일럿 33개: 비공개 결함 11개 × 시드 3개. 공식 점수에서 제외한다.
- 공식 대조 66개: 각 파일럿 replay를 정상판과 결함판에서 각각 실행한다.
- 자율 대조 22개: 결함 11개에 대해 정상/결함 쌍을 LLM 플레이어로 실행한다.

따라서 전체 고정 실행은 145개 런치이며, 검사 논리 호출 계획은 693회다. `--profile poc`는 파일럿 3개, 공식 대조 6개, 자율 대조 6개로 줄어들고, 두 명령 합계 21개 런치와 120회 검사 논리 호출 계획을 가진다. 700회 제한은 두 명령 전체가 아니라 각 캠페인의 체크포인트별 제한이다. HTTP 재시도는 논리 호출 수와 별도로 기록된다.

`benchmark-detection`에는 `--resume` 옵션이 없다. 같은 출력 폴더에 같은 명령을 다시 실행하면 체크포인트와 캠페인 식별자가 정확히 일치할 때만 자동 재개한다. 입력을 바꿨다면 새 폴더를 사용한다.

### Track B 결과 읽기

- 종합: `detection-benchmark.json`, `detection-benchmark.ko.md`
- 공식 대조 점수: `metrics/official.json`, `metrics/official.ko.md`
- 자율 대조 점수: `metrics/autonomous.json`, `metrics/autonomous.ko.md`
- 비공개 파일럿·페어 정보: `pilots/`, `traces/`
- 보존된 증거 스크린샷: `screenshots/<opaque-trace-id>.png`

공식 점수는 결정적 replay가 정상판과 결함판을 대조하므로 검사 모델의 탐지 능력을 보는 표면이다. 자율 점수는 LLM의 도달·조작·검사 전 과정을 포함한다. 공식 `run_id`, observation ID, event ID에는 결함명이나 시나리오명이 들어가지 않으며, 매핑은 평가기 내부 파일에만 남는다.

정답 탐지는 검사 결과가 다음 조건을 모두 만족할 때만 센다.

1. 실제 관찰된 숫자 필드를 지정한다.
2. 기대값과 관측값을 숫자로 제시한다.
3. 위반한 비교 관계(`==`, `!=`, `<=` 등)를 명시한다.
4. 유효한 observation 근거를 인용한다.
5. 비공개 결함의 목표 수치 관계와 일치한다.

현재 구현은 트레이스별 3회 검사에서 2회 이상 같은 결론이면 그 트레이스의 판정을 만든다. 유효한 다수결이 없으면 `INSPECTION_ERROR`다. 결함 트레이스의 유효 판정은 TP/FN, 정상 트레이스의 유효 판정은 FP/TN이 된다. 정상 또는 결함 쪽 한 트레이스라도 무효이면 해당 페어 전체를 점수에서 제외한다.

무효 상태는 `BASELINE_CONFLICT`(정상판 오라클 실패), `FAULT_NOT_ACTIVATED`(결함판 오라클이 실패하지 않음), `NOT_REACHED`(필요 지점 미도달), `UNOBSERVABLE`(목표 수치 구성 불가), `INSPECTION_ERROR`(검사 다수결 불가), `ERROR`(실행·아티팩트 실패)다. TP/FN/FP/TN, 탐지율, 정밀도, 특이도, 정상판 오탐률, 페어 성공률을 공식과 자율 표면에서 나누어 본다.

## 4. 증거 스크린샷

두 캠페인은 각 트레이스의 마지막 프레임을 캡처한 뒤, 채점이 끝나고 나서 보존 여부를 결정한다.

동작 순서는 다음과 같다.

1. 에피소드 종료 직전 브리지에 `capture_screenshot`을 보내 트레이스 폴더에 `final-frame.pending.png`를 남긴다.
2. 검사와 채점이 끝난다. 검사 모델은 이 이미지를 보지 못하며, 스크린샷은 어떤 판정에도 입력되지 않는다.
3. 보존 축이 하나라도 있으면 `screenshots/<opaque-trace-id>.png`로 옮기고, 없으면 pending 파일을 삭제한다.

보존 축(`retention_axes`)은 서로 독립적인 이상 신호를 뜻한다.

- Track A: `planner`(플레이어 모델이 이상을 보고), `inspector`(검사 모델이 후보를 보고), `runtime_oracle`(런타임 불변식 위반).
- Track B: `oracle`(해당 트레이스의 오라클이 `fail`), `inspector`(목표 결함 판정 또는 부수 후보 발견).

즉 아무 이상 신호가 없는 정상 트레이스의 스크린샷은 남지 않는다. 파일명은 결함명·미션명이 들어가지 않은 불투명 trace ID이므로, 스크린샷 파일 목록만으로 어떤 결함이 주입됐는지 알 수 없다.

보고서에 기록되는 필드는 다음과 같다.

| 필드 | 위치 | 의미 |
|---|---|---|
| `screenshot_path` | 트레이스별 결과, manifest `traces[]` | 보존된 경로. 보존되지 않으면 `null` |
| `screenshot_retention_axes` | 같음 | 보존을 유발한 축 목록 |
| `screenshot_error` | 같음 | 캡처·이동 실패 시 예외 타입만 기록 |
| `retained_screenshot_count` / `capture_error_count` | Track A 요약, Track B `screenshot_summary` | 보존 수와 캡처 오류 수 |

캡처 실패는 캠페인을 실패시키지 않는다. 종료 코드와 판정은 그대로이고, 실패 사실만 `screenshot_error`로 남는다. 반대로 보존된 스크린샷은 체크포인트 아티팩트에 포함되므로, 재개 시 파일이 없거나 해시가 다르면 해당 트레이스는 무효화되어 다시 실행된다. 결과 폴더에서 `screenshots/`만 따로 지우지 않는다.

스크린샷 품질에 대한 주의는 1장의 창 모드 안내와 같다. `--headless`(`-batchmode -nographics`)에서는 그래픽 디바이스가 없어 회색 단색 이미지가 저장되며, 이것은 캡처 오류로 기록되지 않는다. 사람이 볼 증거가 필요하면 반드시 `--headless` 없이 실행한다.

## 5. 종료 코드와 재개 안전성

| 코드 | 의미 |
|---:|---|
| `0` | 완전한 manifest와 보고서가 발행됨. 일부 트레이스가 `INSPECTION_ERROR`일 수 있지만 캠페인 인프라는 완료됨. |
| `2` | 인자·설정·빌드·인증·모델 초기화·체크포인트·replay·게임플레이·검사 계약·발행 중 캠페인 수준 오류. |

성공한 캠페인은 표준 출력에 JSON 한 줄을 남긴다.

```json
{"manifest": "...//campaign-manifest.json", "traces": 6, "candidates": 3, "resumed": false}
```

```json
{"manifest": "...//campaign-manifest.json", "pairs": 6, "resumed": false}
```

앞이 `explore`, 뒤가 `benchmark-detection`이다. `resumed`는 체크포인트에서 이어서 실행했는지를 뜻하며, 재개 없이 처음부터 돌았으면 `false`다. 실패하면 표준 오류로 다음 형태만 나간다. 스택 트레이스, 키, 결함명은 나오지 않는다.

```json
{"status": "incomplete", "error_type": "CampaignContractError"}
```

모델 한 번의 잘못된 응답은 해당 pass 실패로 기록한다. 반대로 빌드 해시, replay, 스키마, 아티팩트 무결성, 호출 상한, 결과 발행이 깨지면 fail-closed로 `incomplete` manifest를 남긴다. `status=complete`인 manifest 옆의 JSON/Markdown만 최종 결과로 사용한다.

완료된 아티팩트는 경로·크기·SHA-256이 모두 같을 때만 재사용된다. 변경·누락된 아티팩트와 의존 검사 결과는 무효화되어 `.superseded-inspections/` 또는 `.superseded-results/`로 이동한다. 체크포인트를 다른 출력 폴더에 복사하지 않는다.

## 6. 첫 실행 순서와 결과 해석

### 권장 실행 순서

처음 이 평가를 돌린다면 다음 순서를 따른다. 앞 단계가 깨진 상태에서 뒤 단계 수치를 해석하지 않는다.

1. **플레이어 재빌드** — 평가할 커밋으로 브리지 플레이어를 새로 빌드한다.

```sh
scripts/qa/build-bridge-player.sh
```

오래된 빌드는 브리지의 `capture_screenshot` 명령을 모른다. 구버전 브리지는 `Unknown action`으로 즉시 거절하므로 캠페인이 죽지는 않지만, 모든 트레이스가 `screenshot_error`로 끝나고 보존 스크린샷은 0장이 된다.

빌드 여부는 `.app`의 타임스탬프로 판정하지 않는다. macOS 번들은 Unity가 내용을 다시 써도 최상위 디렉터리 mtime이 그대로일 수 있다. 빌드된 어셈블리에서 문자열을 직접 찾는다. 문자열 리터럴은 `#US` 힙에 UTF-16으로 저장되므로 macOS `strings`로는 보이지 않는다.

```sh
uv run --locked python -c "from pathlib import Path; b=Path('QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app/Contents/Resources/Data/Managed/Vampire.Runtime.dll').read_bytes(); print('capture_screenshot' .encode('utf-16-le') in b)"
```

2. **모델 응답 확인** — 조종 모델과 검사 모델이 실제로 응답하는지 3스텝짜리 에피소드 1회로 확인한다.

```sh
uv run --locked python -m qa_smoke.run \
  --game-exe QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app \
  --project-root . \
  --output QAArtifacts/preflight/model-check \
  --mode qa --policy llm --model gpt-4o-mini \
  --inspector-model gpt-5.6-luna \
  --seed 9101 --max-steps 3 --max-simulation-seconds 60
```

단일 에피소드 실행기는 `qa_smoke.run`이며 `--game-exe`를 받는다. `qa_smoke.cli run`은 시나리오 스위트 실행기라 `--build`와 `--suite`만 받는다는 점에 주의한다. 비용을 묶는 것은 `--max-steps`이므로, 시뮬레이션 시간은 등록된 최단 시나리오와 같은 60초로 두어 게임 진입 전에 끝나지 않게 한다.

종료 코드만 믿지 말고 산출물을 읽는다. `report.json`의 `fatal_error`가 없고, `metrics.api_usage.calls`가 0이 아니며, `llm_assessment`가 존재해야 한다. 검사 모델이 응답하지 않으면 캠페인 전체가 `INSPECTION_ERROR`로 끝나므로 여기서 멈추고 원인을 먼저 해결한다.

3. `run --suite all` — LLM 없이 빌드와 하네스가 정상인지 확인한다.
4. `explore --profile poc` — 창 모드로 실행해 플레이어가 실제로 게임을 굴리는지 눈으로 확인한다.
5. `benchmark-detection --profile poc` — 대조 파이프라인이 끝까지 도는지 확인한다.
6. 위 단계가 모두 통과하면 `--profile full`을 `--headless`로 실행한다.

### 소요 시간 감각

아래는 고정 일정에 들어 있는 **게임 시뮬레이션 시간의 합계**다. 실제 벽시계 시간은 여기에 스텝마다의 모델 응답 대기와 플레이어 기동 시간이 더해지므로 반드시 더 길다.

| 명령 | `poc` | `full` |
|---|---:|---:|
| `explore` | 1,200초 (20분) | 4,860초 (81분) |
| `benchmark-detection` | 2,100초 (35분) | 15,840초 (264분) |

`full`을 창 모드로 돌리면 그동안 해당 데스크톱을 쓸 수 없다는 점을 고려한다.

### 흔한 결과 패턴

종료 코드가 `0`이어도 수치를 그대로 성능으로 읽으면 안 되는 경우가 있다.

| 증상 | 먼저 의심할 것 | 조치 |
|---|---|---|
| Track A `candidate_count=0` | 정상이다. "이번 고정 일정에서 근거 연결된 후보가 없음"이지 게임에 버그가 없다는 뜻이 아니다 | 그대로 기록한다 |
| Track A 후보 0 + `coverage_not_reached_traces`가 대부분 | 플레이어가 미션 표면(업그레이드·재시작·아이템)에 도달하지 못함. 탐지 성능 문제가 아니다 | 창 모드로 `poc`를 다시 돌려 플레이를 직접 본다 |
| Track A `excluded_harness_traces`가 큼 | 하네스·아티팩트 실패 | `harness_failures` 목록과 Unity 로그를 본다 |
| 보존된 스크린샷이 전부 회색 단색 | `--headless`의 `-nographics`. `capture_error_count`는 0이고 `retained_screenshot_count`는 정상이라 수치로는 안 잡힌다 | 이미지를 직접 열어 확인한다. 증거가 필요하면 `--headless` 없이 다시 실행한다 |
| Track B 페어 성공률이 낮고 `NOT_REACHED`·`FAULT_NOT_ACTIVATED`가 다수 | 도달 능력 문제. 이때의 탐지율은 표본이 너무 작아 의미가 없다 | 탐지율 대신 페어 성공률을 먼저 보고한다 |
| Track B 공식은 TP가 나오는데 자율만 전부 무효 | 검사 능력이 아니라 플레이어의 도달·조작 능력 | 두 표면을 분리해 보고한다 |
| Track B `BASELINE_CONFLICT`가 여러 결함에 걸쳐 발생 | 주입 결함이 아니라 정상 빌드 자체 또는 replay 재현성 문제 | 7장의 `validate-faults`로 결정적 확인을 먼저 한다 |
| Track B `INSPECTION_ERROR`가 다수 | 검사 모델 응답이 계약 스키마를 못 맞춤 | `inspections/` 감사 로그의 실패 pass를 확인한다 |
| 어느 쪽이든 종료 코드 `2` | 캠페인 수준 실패. manifest는 `incomplete` | `error_type`을 확인하고, 그 폴더의 보고서는 결과로 쓰지 않는다 |

두 트랙 모두 결정적 회귀 게이트가 아니다. Track A 후보 수와 Track B 탐지율로 릴리스 판정을 하지 않는다.

## 7. 결정적 회귀 baseline (선택)

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

## 8. 검증 명령

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
