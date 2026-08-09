# 플레이어 배포

이 문서는 빌드한 Unity player를 팀원에게 전달하고, 받은 쪽에서 실행 가능한 상태로 만드는 절차를 설명한다. 학습과 평가 자체는 [PPO 연결 quickstart](PPO_QUICKSTART.ko.md)를 참조한다.

팀원이 각자 머신에서 학습과 평가를 돌리려면 **저장소 클론과 플랫폼별 player가 둘 다** 필요하다. player만으로는 동작하지 않는다 — `config/qa-presets.json`, `qa_pytorch_ppo`, `uv.lock`이 모두 저장소에 있다.

원본 게임은 Unity `2021.3.21f1`이며 ML-Agents가 들어있지 않다. 학습을 붙이기 위해 스택을 올리고 ML-Agents를 새로 추가했다.

| 항목 | 버전 |
|---|---|
| Unity | `6000.0.80f1` |
| [uv](https://docs.astral.sh/uv/) | `0.12.x` |
| Python | `3.10.12` |
| PyTorch | `2.8.0` |

## 빌드하는 쪽

macOS 한 대에서 세 플랫폼을 전부 만들 수 있다.

```sh
scripts/qa/build-player.sh            # macOS  → QAArtifacts/player/QaGameplay.app
scripts/qa/build-player-linux.sh      # Linux  → QAArtifacts/dist/linux/
scripts/qa/build-player-windows.sh    # Windows → QAArtifacts/dist/windows/
```

Unity Hub에서 해당 플랫폼의 Build Support 모듈이 설치되어 있어야 한다. 없으면 빌드가 `Unity is missing build support for ...`로 실패한다.

각 스크립트는 활성 빌드 타깃을 전환한 뒤 Addressables를 다시 만들고 player를 빌드한다. 이 순서를 지키지 않으면 다른 플랫폼 번들이 들어간 player가 나오고, **이 실패는 조용하다** — 실행은 되고 에셋만 로드되지 않는다. 스크립트를 거치지 않고 수동으로 빌드할 때 주의한다.

| 플랫폼 | 산출물 | 크기 |
|---|---|---:|
| macOS | `QAArtifacts/player/QaGameplay.app` | 약 157MB |
| Linux | `QAArtifacts/dist/linux/` | 약 156MB |
| Windows | `QAArtifacts/dist/windows/` | 약 125MB |

## 패키징

```sh
scripts/qa/package-players.sh
```

빌드되어 있는 player를 전부 찾아 `QAArtifacts/dist/`에 압축한다.

| 산출물 | 크기 |
|---|---:|
| `qa-player-macos.zip` | 약 53MB |
| `qa-player-linux.zip` | 약 50MB |
| `qa-player-windows.zip` | 약 42MB |

**반드시 이 스크립트가 만든 압축본을 전달한다.** 빌드 디렉터리를 그대로 올리면 macOS는 `.app` 번들 구조가, Linux·Windows는 `QaGameplay_Data/`와 런타임 라이브러리가 보존되지 않는다.

## 받는 쪽

1. 저장소를 클론하고 의존성을 설치한다. `torch`와 `mlagents`가 optional dependency이므로 `--extra trainer`가 없으면 설치되지 않는다.

```sh
git clone -b feat/ai-agent-qa https://github.com/crescent-stdio/VampireSurvivorsClone-qa
cd VampireSurvivorsClone-qa
uv python install 3.10.12
uv sync --locked --extra trainer
```

2. 받은 압축을 **`QAArtifacts/player/`에 푼다.** 플랫폼별 기본 경로와 일치하므로 `--player`를 지정할 필요가 없다.

| 플랫폼 | 압축을 풀었을 때 있어야 하는 경로 |
|---|---|
| macOS | `QAArtifacts/player/QaGameplay.app` |
| Linux | `QAArtifacts/player/QaGameplay.x86_64` |
| Windows | `QAArtifacts/player/QaGameplay.exe` |

3. 플랫폼별로 한 단계가 더 필요하다.

```sh
# macOS: 다운로드로 붙은 격리 속성 제거
xattr -dr com.apple.quarantine QAArtifacts/player/QaGameplay.app

# Linux: 실행 권한 부여
chmod +x QAArtifacts/player/QaGameplay.x86_64
```

macOS 격리 속성을 지우지 않으면 **오류 대화상자 없이 프로세스가 `SIGKILL`(종료 코드 137)로 죽고** episode 산출물이 만들어지지 않는다. 압축 방식과 무관하며 원본 빌드부터 그렇다.

4. 학습과 평가를 실행한다.

```sh
uv run --locked --extra trainer python -m qa_pytorch_ppo.cli train
uv run --locked --extra trainer python -m qa_pytorch_ppo.cli evaluate --seed 9101
```

다중 seed 품질 판단은 평가를 seed마다 돌린 뒤 집계한다.

```sh
uv run --locked python -m qa_agent_runtime.sweep \
  --artifact-root QAArtifacts/player/QAArtifacts \
  --seeds 9101 9102 9103 9104 9105
```

## 셸 래퍼는 macOS 전용이다

`scripts/qa/*.sh`는 POSIX sh이고, `common.sh`의 player 해석이 macOS `.app` 번들 구조를 가정한다. **Windows·Linux 팀원은 위의 Python CLI를 직접 사용한다.**

| macOS 셸 래퍼 | 다른 플랫폼에서의 대체 |
|---|---|
| `scripts/qa/train-pytorch.sh` | `python -m qa_pytorch_ppo.cli train` |
| `scripts/qa/evaluate-pytorch.sh` | `python -m qa_pytorch_ppo.cli evaluate --seed <n>` |
| `scripts/qa/evaluate-sweep.sh` | 위 evaluate 반복 + `python -m qa_agent_runtime.sweep` |
| `scripts/qa/smoke.sh` | 대체 없음 (macOS 전용 회귀 검사) |

Python CLI는 프리셋 선택, 지문 검증, time scale 적용을 셸 래퍼와 동일하게 수행한다. 셸이 하는 추가 작업은 Unity 버전 확인과 `uv lock --check`뿐이다.

**현재 Linux·Windows player의 실제 동작은 미검증이다.** 이 저장소에서는 빌드 성공과 플랫폼별 Addressables 번들 생성까지만 확인했다. 해당 OS에서 첫 실행 시 위 절차대로 동작하는지 확인하고 결과를 공유한다.

## 문제 해결

| 증상 | 원인과 대처 |
|---|---|
| `Unity is missing build support for ...` | Unity Hub에서 해당 플랫폼의 Build Support 모듈을 설치한다 |
| `Unity player executable does not exist` | Linux·Windows에서 player 경로가 틀렸다. 압축을 `QAArtifacts/player/`에 풀었는지 확인한다 |
| `Unrecognized Unity player suffix` | `--player`에 압축 파일이나 디렉터리를 넘겼다. 실행 파일(`.x86_64`, `.exe`) 또는 `.app` 번들을 지정한다 |
| macOS player가 오류 없이 종료 코드 137로 죽음 | 다운로드 격리 속성이다. `xattr -dr com.apple.quarantine <경로>` 실행 |
| macOS player를 Drive 등에서 받았는데 앱이 아니라 폴더로 보임 | `.app`을 압축하지 않고 올린 것이다. `scripts/qa/package-players.sh`로 만든 zip을 전달한다 |
