# Track A PoC 실행 보고서 (2026-08-25)

정상 빌드에서 LLM 플레이어가 자율 플레이하고, 검사 모델이 관측 기록만 보고 이상 징후를 보고한 결과다. 축소 일정(`--profile poc`)이므로 공식 수치가 아니다.

## 1. 실행 정보

| 항목 | 값 |
|---|---|
| 캠페인 | `QAArtifacts/evaluation/track-a/poc-2` |
| manifest 상태 | `complete` |
| 프로파일 | `poc` (3 미션 × 2 시드 = 6 트레이스) |
| 빌드 해시 | `b31e6c87bb23d644ee4d619929ccdf5d…` |
| 조종 모델 | `gpt-4o-mini` |
| 검사 모델 | `gpt-5.6-luna` (트레이스당 3회 독립 호출) |
| 실행 모드 | 창 모드 (헤드리스 아님) |

| 지표 | 값 |
|---|---:|
| 예정 트레이스 | 6 |
| coverage 미도달 | 0 |
| harness 제외 | 0 |
| 검사 논리 호출 | 39 (계획 48) — 실패 0 |
| 보존 스크린샷 | 3 (캡처 오류 0) |
| 후보 합계 | 8 |

표면 분류는 `inspector_only` 8건, `planner_only`·`shared`·`runtime_oracle` 0건이다. 우선순위는 전부 `P3`이며, 모든 후보의 검사 동의도는 `1/3`이다.

## 2. 후보를 만들어낸 경로

1. **트레이스 수집** — 조종 모델이 매 스텝 행동을 정하고, 하네스가 `(관측, 결정, 결과)`를 `steps.jsonl`에 기록한다. 이 단계는 버그를 찾지 않는다.
2. **채널 정제** — `build_inspection_payload`가 관측만 추리고 `sanitize_agent_channel`이 평가자 내부 상태를 제거한다. 검사 모델은 무엇을 찾아야 하는지 모르며, 스크린샷도 보지 않는다.
3. **3회 독립 검사** — 같은 청크를 3번 따로 호출한다.
4. **스키마 강제** — 자유 서술이 불가능하다. 숫자 주장에는 `field`, `comparison`, `expected_value`, `observed_value`, `evidence_refs`가 모두 필요하다.
5. **근거 대조** — 인용한 observation ID가 실제 트레이스에 없으면 그 발견은 버려진다.
6. **정규화·집계** — `category/rule/field/phase/event`로 중복을 합치고, 같은 미션의 다른 시드에서 재출현하면 `reproduced=true`가 된다.

## 3. 후보 검증

후보 8건은 세 갈래로 묶인다. 아래 값은 모두 인용된 observation을 `steps.jsonl`에서 직접 조회해 확인한 것이다.

### 3.1 재시작 후 상자 타깃이 남는다 — 근거 확인됨 (후보 4건)

**주장**: 재시작 뒤에도 `controller.target_id`가 이전 세계의 상자를 가리킨다.

`core-progression-upgrades/9101` (단일 런, obs-27에서 restart 실행):

| observation | phase | controller.target_id | visible_chests |
|---|---|---:|---|
| `…obs-00000025` | `game_over` | `-6030` | `[-6030, -6050, -3224, -3204, -1502]` |
| `…obs-00000027` | `active_gameplay` | `-6030` | `[-9928]` |

재시작 직후 세계에는 `-9928`만 존재하는데 타깃은 `-6030`에 그대로 남았다.

`core-progression-upgrades/9102` (obs-28에서 restart 실행):

| observation | phase | controller.target_id | visible_chests |
|---|---|---:|---|
| `…obs-00000029` | `active_gameplay` | `-1502` | `[-6730]` |
| `…obs-00000030` | `active_gameplay` | `-1502` | `[-6730]` |
| `…obs-00000031` | `active_gameplay` | `-1502` | `[-6730]` |

두 미션·두 시드에서 같은 형태가 관측된다.

**유의점**: 세 관측 모두 `controller.active=false`다. 즉 추적을 실제로 수행 중은 아니고, 필드가 정리되지 않은 상태로 남아 있는 것이다. 표시·상태 정리 문제인지 실제 추적 로직 결함인지는 재시작 경로 코드를 확인해야 갈린다. **코드 확인 필요.**

### 3.2 게임오버 관측에서 체력 값이 서로 어긋난다 — 근거 확인됨 (후보 3건)

**주장**: 같은 observation 안에서 `player.health`와 `player_view.health`가 다르다.

| observation | 미션/시드 | player.health | player_view.health | alive |
|---|---|---:|---:|---|
| `…obs-00000026` | `core-progression-upgrades/9102` | `-1.0` | `0.0` | false |
| `…obs-00000028` | `core-progression-upgrades/9102` | `-2.0` | `0.0` | false |
| `…obs-00000020` | `core-death-restart-isolation/9102` | `-3.0` | `0.0` | false |

전부 `game_over` 페이즈이고, `player_view` 쪽만 0으로 하한이 걸려 있다. 사망 처리에서 한쪽 채널만 클램프하는 것으로 보인다. 세 관측이 **두 미션에 걸쳐** 나타나므로 일회성 흔들림으로 보기 어렵다.

### 3.3 명시적 restart 없이 게임오버에서 활성 상태로 돌아간다 — **오탐** (후보 1건)

**주장**: 트레이스가 게임오버에서 restart 행동 없이 새 게임으로 되돌아간다.

인용된 두 관측을 조회하면 주장 자체는 기록과 일치한다.

| observation | run id | phase |
|---|---|---|
| `3d124872…-obs-00000028` | `3d124872…` | `game_over` |
| `14df322a…-obs-00000003` | `14df322a…` | `active_gameplay` |

그러나 **run id가 서로 다르다.** `core-progression-upgrades/9102` 트레이스의 `steps.jsonl`에는 두 개의 런이 이어붙어 있고, 그 경계에는 restart 결정이 없다. 이 트레이스에 기록된 restart 결정은 두 번째 런 내부의 `14df322a…-obs-00000028` 하나뿐이다.

즉 검사 모델은 **기록된 데이터에 대해서는 정확한 진술**을 했지만, 그 원인은 게임 결함이 아니라 트레이스가 두 플레이어 프로세스를 하나로 이어붙인 구조다.

**이것은 하네스에 대한 발견이다.** 프로세스 경계를 넘는 트레이스 연결이 존재하지 않는 상태 전이를 만들어내고, 검사 모델은 그것을 게임 이상으로 보고한다. 검사 페이로드에 런 경계를 표시하면 이 부류의 오탐을 없앨 수 있다.

## 4. 해석 시 유의사항

- **후보는 확정 버그가 아니다.** 사람이 판단할 대상이다.
- **동의도는 전부 `1/3`이다.** Track A는 1회만 나온 후보도 버리지 않고 기록한다. Track B의 TP 판정(2/3 다수결 + 비공개 정답 대조)과는 기준이 다르다.
- **`reproduced`는 전부 `false`다.** 다만 3.2의 세 후보는 정규화 키가 서로 달라 병합되지 않았을 뿐, 실질적으로는 같은 현상이 세 관측에서 나타났다. `reproduced` 플래그를 "한 번만 나왔다"로 읽으면 안 된다.
- **표본이 작다.** PoC는 미션당 시드 2개다. 교차 재현 기회가 적으므로 후보 수를 탐지 능력으로 환산하지 않는다.
- **후보 0건이었어도 게임에 버그가 없다는 뜻은 아니다.**

## 5. 이번 실행에서 함께 확인된 것

- **검사 경로 복구**: 검사 호출 39/39 성공. 직전까지 모든 캠페인이 `LLMTransportError`로 죽었는데, 원인은 네트워크가 아니라 `FINDINGS_SCHEMA`가 Structured Outputs 미지원 키워드(`oneOf`)를 보낸 계약 위반(HTTP 400)이었다.
- **아이템 사용 경로 복구**: 조종 모델이 `game.use_item`으로 Red Potion을 소비하는 것이 실행 로그에서 확인됐다. `use_item`이 `bridge_assist`에 묶여 있어 `--policy llm`인 캠페인에서는 선택 자체가 불가능했던 문제를 수정한 결과다.
- **스크린샷 증거**: 3장 모두 실제 게임 화면이다. 헤드리스로 돌렸다면 오류 없이 회색 단색 이미지가 저장됐을 자리다.

## 6. 산출물

| 경로 | 내용 |
|---|---|
| `QAArtifacts/evaluation/track-a/poc-2/exploration-report.ko.md` | 한국어 요약 |
| `QAArtifacts/evaluation/track-a/poc-2/exploration-report.json` | 감사용 원본 |
| `QAArtifacts/evaluation/track-a/poc-2/traces/<mission>/<seed>/` | 실행 기록 |
| `QAArtifacts/evaluation/track-a/poc-2/inspections/<trace>/pass-1..3/` | 검사 감사 로그 |
| `QAArtifacts/evaluation/track-a/poc-2/screenshots/` | 보존된 증거 스크린샷 3장 |

## 7. 후속 작업

1. 재시작 경로에서 `controller.target_id`가 정리되는지 코드 확인 (3.1)
2. 사망 처리에서 `player.health`와 `player_view.health`의 클램프 차이 확인 (3.2)
3. 검사 페이로드에 런 경계 표시 추가 검토 (3.3 오탐 제거)
4. full 프로파일(8 미션 × 3 시드) 실행 시 `reproduced` 판정이 달라지는지 확인
