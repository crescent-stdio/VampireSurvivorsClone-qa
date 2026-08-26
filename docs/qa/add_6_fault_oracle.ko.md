# 신규 게임플레이 버그 6종 결정적 오라클 추가

# 1. 작업 요약

최신 `feat/llm-qa-evaluation` 브랜치를 기준으로 게임플레이 결함 6종을 실행 시점에 선택적으로 켤 수 있도록 추가하고, 각 결함을 자동 판정하는 deterministic v4 오라클과 검증 시나리오를 추가했습니다.

| 항목 | 내용 |
| --- | --- |
| 기준 브랜치 | `feat/llm-qa-evaluation` |
| 작업 브랜치 | `feat/add-gameplay-fault-oracles` |
| 신규 결함 | 6종 |
| 변경 파일 | 의도된 파일 20개 |
| Python 전체 테스트 | 556/556 PASS |
| 신규 Unity fault 테스트 | 7/7 PASS |

기존 Track B 공식 11종, 비공개 채점 방식과 API 호출 예산은 변경하지 않았습니다.

---

# 2. 추가한 게임플레이 결함

| fault id | 화면에서 보이는 증상 | 영역 |
| --- | --- | --- |
| `upgrade_dialog_stuck_open` | 업그레이드를 선택해도 창이 닫히지 않고 게임이 정지 상태로 남음 | UI 전이 |
| `movement_input_inverted` | 입력한 방향과 반대로 이동 | 이동 입력 |
| `monster_spawning_stops` | 60초 이후 일반 몬스터 신규 생성이 중단됨 | 스폰 시스템 |
| `weapon_cooldown_stuck_after_first_attack` | 시작 무기가 첫 공격 뒤 다시 공격하지 않음 | 무기 cooldown |
| `contact_damage_cooldown_not_reset` | 접촉 공격 후 재공격 cooldown이 초기화되지 않음 | 전투 timer |
| `projectile_passes_through_enemies` | 투사체가 적과 충돌해도 피해·소모 없이 통과 | 투사체 충돌 |

결함별 Unity 빌드를 따로 만들지 않았습니다. 하나의 QA 빌드에 다음 실행 인자를 주면 지정한 결함 하나만 활성화됩니다.

```text
-qaFault=<fault_id>
```

`-qaFault`를 주지 않으면 정상 동작하고, 알 수 없는 fault나 시나리오와 맞지 않는 fault는 시작 단계에서 거부됩니다.

---

# 3. 게임 코드 수정 위치

| 결함 | 수정 파일 | 변경 내용 |
| --- | --- | --- |
| 업그레이드 창 고착 | `Assets/Scripts/UI/AbilitySelectionDialog.cs` | 선택 뒤 blocking dialog 종료 전이를 선택적으로 중단 |
| 이동 입력 반전 | `Assets/Scripts/Character/Character.cs` | 실제 이동에 적용되는 방향 벡터를 선택적으로 반전 |
| 몬스터 스폰 중단 | `Assets/Scripts/Gameplay/LevelManager.cs` | 일반 몬스터 spawn block을 60초 이후 선택적으로 중단 |
| 무기 cooldown 정지 | `Assets/Scripts/Character/Abilities/MeleeAbility.cs` | 첫 공격 뒤 반복 공격 진입을 선택적으로 중단 |
| 접촉 cooldown 미초기화 | `Assets/Scripts/Monsters/MeleeMonster.cs` | 접촉 피해 뒤 공격 timer reset을 선택적으로 생략 |
| 투사체 적 통과 | `Assets/Scripts/Projectiles/Projectile.cs` | 적 충돌 뒤 damage·consumption 처리를 선택적으로 생략 |

정상 실행에서는 기존 게임 밸런스, 이동속도, 공격력, 적 수와 아이템 확률을 변경하지 않습니다.

---

# 4. fault 등록과 관측 추가

| 파일 | 추가·수정 내용 |
| --- | --- |
| `Assets/Scripts/QA/QaFaultInjection.cs` | 신규 fault id 6개, 허용 시나리오, opt-in 분기와 evaluator telemetry 등록 |
| `Assets/Scripts/QA/QABridge.cs` | 공격·스폰·접촉·투사체 결과를 evaluator 전용 관측에 기록 |
| `Assets/Scripts/QA/QABridgeModels.cs` | 신규 telemetry 직렬화 필드 정의 |

오라클은 active fault id를 정답으로 읽지 않습니다. 실제 게임에서 기록된 상태와 전이가 정상 계약을 만족했는지를 판정합니다.

신규 telemetry는 Bridge가 저장하는 원본 trace의 `evaluator_state.telemetry`에 포함됩니다. deterministic 오라클은 이 원본 값을 사용하지만, `build_agent_observation()`에서 `evaluator_state`가 제거되므로 조종 LLM과 검사 LLM에게는 전달되지 않습니다.

업그레이드 창과 이동 방향 오라클은 기존 공개 관측인 `phase`, `menu.upgrade_open`, `controller.steering`, `player.velocity`를 사용합니다. 스폰·무기·접촉·투사체 오라클은 이번에 추가한 evaluator 전용 telemetry를 사용합니다. 따라서 이번 작업은 결정적 오라클 관측을 확장한 것이며, 신규 6종의 Track B LLM 관측까지 확장한 것은 아닙니다.

---

# 5. 추가한 시나리오와 오라클

| 결함 | oracle id | 판정 계약 |
| --- | --- | --- |
| 업그레이드 창 고착 | `upgrade_dialog_closes` | 유효한 선택 뒤 upgrade phase와 열린 menu에서 벗어나야 함 |
| 이동 입력 반전 | `movement_matches_input` | 여러 steering–velocity 표본이 지속적으로 같은 방향이어야 함 |
| 몬스터 스폰 중단 | `regular_monster_spawning_continues` | 활성 spawn schedule에서 실제 expected delay보다 과도하게 긴 spawn 공백이 없어야 함 |
| 무기 cooldown 정지 | `weapon_cooldown_repeats` | 실제 configured cooldown에 맞춰 반복 공격해야 함 |
| 접촉 cooldown 미초기화 | `contact_damage_respects_cooldown` | contact damage 처리 뒤 attacker cooldown reset 결과가 있어야 함 |
| 투사체 적 통과 | `projectile_enemy_collision_applies` | 유효한 적 collision마다 damage와 projectile consumption 결과가 있어야 함 |

관련 설정과 Python 코드는 다음 위치에 추가했습니다.

| 파일 | 역할 |
| --- | --- |
| `config/qa-scenarios-v4.json` | 신규 6개 시나리오와 도달 조건 |
| `config/qa-ground-truth-v4.json` | fault id와 기대 동작 연결 |
| `config/qa-detection-rubric.json` | 신규 증상 분류 표현 |
| `qa_smoke/scenarios.py` | 신규 시나리오·오라클 계약 등록 |
| `qa_smoke/evaluation.py` | 신규 오라클 6종 구현 |



---

# 6. 최신 Track A·Track B와의 관계


| 평가 영역 | 이번 신규 6종 상태 |
| --- | --- |
| `validate-faults` 결정적 오라클 | 등록 및 검증 완료 |
| Track A 정상 빌드 자율 탐색 | 공식 일정 변경 없음 |
| Track B 기존 11종 공식 대조 | 기존 방식 그대로 유지 |
| Track B 신규 6종 공식 채점 | 아직 편입하지 않음 |


`qa_smoke/detection_campaign.py`는 신규 fault가 레지스트리에 추가되어도 Track B 공식 11종과 고정 paired campaign 일정이 자동으로 17종 기준으로 확장되지 않도록 호환 처리했습니다. 이에 따라 Track A와 Track B를 합친 full profile의 693회 cross-track 검사 논리 호출 계획도 그대로 유지됩니다. 기존 공식 11종 중 하나가 빠지면 여전히 오류가 발생합니다.

---

# 7. 테스트와 반복 검증

## 3개 시드 게임 검증

seed `9101`, `9102`, `9103`에서 전체 v4-core 14개 시나리오를 정상판과 결함판으로 실행했습니다.

```text
14 scenarios × 2 variants × 3 seeds = 84 runs
```

신규 6종 직접 비교는 총 36회입니다.

| 신규 결함 | 정상판 | 결함판 |
| --- | --- | --- |
| 업그레이드 창 | 3 PASS | oracle FAIL 3회 |
| 이동 방향 | 3 PASS | 3 FAIL |
| 스폰 지속 | oracle PASS 3회 | 3 FAIL |
| 무기 cooldown | 3 PASS | 3 FAIL |
| 접촉 cooldown | 3 PASS | 3 FAIL |
| 투사체 충돌 | 1 PASS, 2 NOT_REACHED | 1 FAIL, 2 NOT_REACHED |

판정 가능한 정상 실행은 16/16 PASS로 오탐이 없었고, 판정 가능한 결함 실행은 16/16 oracle FAIL이었습니다. 투사체 seed 2개는 정상·결함 양쪽 모두 collision 자체가 발생하지 않아 대칭적으로 NOT_REACHED였습니다.

업그레이드 창이 안 닫히는 상황의 seed 9102는 결함 판정 뒤 선택 창에서 heuristic driver가 선택을 반복하다 실행 `ERROR`가 발생했지만, 그 전에 수집된 부분 trace에는 `oracle_verdict=fail`이 기록됐습니다. 결함 검출과 종료 안정성을 분리해 기록했습니다.

## 기존 결함과 control 유지

- 도달한 기존 view health·EXP 결함: 6/6 검출
- 기존 HP hit·item effect·item range: 해당 세 시드에서 양쪽 모두 NOT_REACHED
- control 3종: clean/injected 합계 18/18 PASS

따라서 신규 결함을 추가하면서 기존 등록과 오라클을 제거하지 않았고, 도달한 기존 결함의 판정도 유지됐습니다.


---

# 8. 테스트 및 안정화 코드

| 파일 | 추가·수정 내용 |
| --- | --- |
| `Assets/Tests/EditMode/QaFaultInjectionTests.cs` | 신규 fault 등록, opt-in과 telemetry 단위 테스트 |
| `qa_smoke/test_v4_harness.py` | 신규 시나리오, ground truth와 오라클 위반·정상 계약 테스트 |
| `qa_smoke/test_smoke.py` | Bridge 관측과 회귀 테스트 |
| `qa_smoke/bridge_client.py` | Windows command file 교체 중 간헐적 `WinError 5` 재시도 |

Bridge 재시도는 게임 규칙 변경이 아니라 반복 실행 안정화를 위한 변경입니다.

---
