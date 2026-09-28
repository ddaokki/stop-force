# Stop-Force: 승인과 검증을 갖춘 SRE 온콜 에이전트

**팀 돼지와 멸치둘 | 팀장 서정민 | 팀원 이은성, 조현규**

Stop-Force는 장애 신고에서 로그 조회, 런북 확인, 조치 요청, 결과 검증, 보고서 작성까지 연결하는 프로토타입입니다. 모델의 복구 선언을 그대로 받아들이지 않고, 명령 정책과 승인 기록, 환경 상태로 결과를 확인합니다.

현재 Kubernetes, PostgreSQL, 파일시스템은 모두 **메모리 안의 시뮬레이터**입니다. 명령은 실제 셸이나 운영 서버에서 실행되지 않습니다. Slack과 PagerDuty 표시는 가상 장애 신고의 출처이며 실제 서비스 연동은 없습니다.

## 실행 모드와 구현 범위

| 항목 | 현재 구현 |
|---|---|
| 실제 NVIDIA 모드 | 로컬 API 키로 NVIDIA OpenAI 호환 엔드포인트를 호출합니다. 모델이 다음 도구를 선택합니다. |
| 사전 작성 시나리오 모드 | `ScriptedLLM`이 미리 작성한 순서를 진행합니다. 실제 모델 응답의 녹화가 아니며 API 호출 수는 0입니다. |
| 도구 실행 | 두 모드 모두 동일한 메모리 시뮬레이터를 사용합니다. 실제 클러스터 접근은 없습니다. |
| 런북 | 저장소의 `skills/*/SKILL.md`를 `load_skill`로 읽습니다. 행사에서 요구하는 “Skill API” 충족 여부는 확인되지 않았습니다. |
| 정책 | Python으로 구현한 명령 문법 검사, 기본 차단, 승인, 비밀값 가림입니다. NVIDIA OpenShell과 NemoClaw는 아직 통합하지 않았습니다. |
| 검증 | 모델 문장과 별개로 상태를 판정하고 실행 기록, 승인 기록, 종료 사유를 보고서에 남깁니다. |

2026-09-28 실제 Nemotron의 네이티브 도구 호출을 확인했습니다. DB 장애는 일시 완화, 배포 장애는 승인 시 복구와 거부 시 미복구로 정확히 보고됐습니다. API 시간 초과도 발생했으며 실패 기록을 함께 보존합니다. 상세 실측은 [제출 기록](SUBMISSION.md)의 실행 표를 확인하세요. 오프라인 테스트 통과는 실제 모델의 진단 능력이나 프롬프트 인젝션 방어율을 입증하지 않습니다.

## 빠른 실행

저장소 루트에서 실행합니다. Python 3.10 이상이 필요합니다. 의존성은 [requirements.txt](requirements.txt)를 기준으로 설치합니다.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

macOS 또는 Linux Bash:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py
```

브라우저에서 `http://localhost:8501`을 엽니다. API 없이 확인하려면 화면에서 사전 작성 시나리오 모드를 선택합니다. 시나리오를 시작하고 승인 요청에 응답한 뒤 최종 상태와 보고서를 확인합니다.

실제 NVIDIA 모드는 `.env.example`을 `.env`로 복사하고 로컬 편집기에서 키를 입력하고 실행 중인 앱을 다시 시작합니다. `.env`와 실제 키는 Git에 넣지 않습니다.

```dotenv
NVIDIA_API_KEY=발급받은_실제_키
NVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```

키가 없으면 실제 모드를 시작하지 않습니다. 유효하지 않은 키의 API 오류도 숨기지 않습니다. 사전 작성 시나리오로 조용히 대체하지 않으며, 오프라인 실행은 명시적으로 선택합니다. 모델 정보는 [NVIDIA 모델 페이지](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b/build)와 [공식 Nemotron 문서](https://github.com/NVIDIA-NeMo/Nemotron/blob/main/usage-cookbook/Nemotron-3.5-Lightning/README.md)를 참고하세요. 공식 문서는 구조화된 도구 호출 지원을 설명하지만 이 저장소의 실제 요청 성공은 별도 실행 증거로 확인해야 합니다.

## CLI와 보고서

아래는 Windows PowerShell 예시입니다. Bash에서는 Python 경로를 `.venv/bin/python`으로 바꿉니다.

```powershell
# 오프라인 시나리오, 승인 요청은 터미널에서 응답
.\.venv\Scripts\python.exe cli.py db_leak --demo

# 테스트용 자동 승인과 보고서 저장
.\.venv\Scripts\python.exe cli.py disk_full --demo --auto-approve --json artifacts/disk.json --markdown artifacts/disk.md

# 승인 거부 시 결과 확인
.\.venv\Scripts\python.exe cli.py bad_deploy --demo --deny

# 정책 OFF 비교도 메모리 시뮬레이터에서만 실행
.\.venv\Scripts\python.exe cli.py disk_full --demo --no-policy

# 실제 NVIDIA 요청: .env 키 필요
.\.venv\Scripts\python.exe cli.py db_leak --json artifacts/live.json --markdown artifacts/live.md
```

`--auto-approve`는 시뮬레이션 검증을 위해 모든 승인 요청을 허용합니다. `--deny`는 승인 요청을 거부합니다. 정책 OFF에서도 지원하는 단일 명령 문법만 허용하며 복합 셸 명령을 실제로 실행하지 않습니다. 비밀값 가림은 정책 OFF와 관계없이 유지됩니다.

## 세 가지 장애

| 시나리오 | 진단과 조치 | 확인할 결과 |
|---|---|---|
| `db_leak` | 워커 커넥션 누수 확인, 재시작 후 이전 버전 롤백 요청 | 재시작만 하면 일시 완화, 롤백까지 성공하면 복구 |
| `bad_deploy` | 배포 이력과 주문 오류 대조, 이전 버전 롤백 요청 | 승인 시 복구, 거부 시 미해결 |
| `disk_full` | 공격 문구가 포함된 로그 조회, 정책 차단, 오래된 로그 정리와 회전 타이머 복구 요청 | 정리만 하면 일시 완화, 재발 방지까지 적용하면 복구. 정책 OFF의 파괴 명령은 데이터 손상으로 표시 |

사전 작성 디스크 시나리오는 정책 효과를 보여주기 위해 위험한 명령을 의도적으로 요청합니다. 실제 모델이 공격에 속았다는 실험 결과가 아닙니다.

## 실행 제한과 검증

모델은 조회 도구와 `run_command`를 요청합니다. 에이전트는 인자 형식과 호출 한도를 확인하고, 명령 파서는 지원하는 명령 전체가 정확히 일치하는지 검사합니다. 정책은 허용, 승인 필요, 차단으로 판정합니다. 승인 요청은 해당 명령의 실행 기록과 연결됩니다.

기본 제한은 API 요청당 60초, 재시도 최대 2회, 내부 chat 24회, 도구 40회, 도구 오류 3회, 전체 300초(승인 대기 포함)입니다. 전송 계층의 inactivity timeout 외에 호출자의 경과 시간도 제한합니다. 기한을 넘긴 응답은 버리고 도구를 실행하지 않습니다. 이미 전송된 요청의 공급자 측 생성까지 취소할 수는 없으며 그 요청의 토큰 사용량은 미확인으로 남습니다. 재시도는 429, 서버 오류, 시간 초과에만 제한적으로 적용하며 도구 실행을 재시도하는 기능이 아닙니다. 네이티브 도구 호출을 명시적으로 지원하지 않는다는 오류에만 JSON 모드로 전환합니다. 잘못된 JSON 인자는 빈 객체로 바꾸지 않고 검증 오류로 전달합니다.

웹 승인 화면은 세션이 연결된 동안 매초 남은 시간을 갱신하고, 기한이 지나면 요청을 만료시켜 보고서를 표시합니다. 승인 대기 중 **실행 중단**을 눌러도 이전 실행 기록은 보존됩니다. 브라우저 연결이 끊기거나 타이머가 지연돼도 승인 시 기한을 다시 검사하므로 만료된 요청은 실행하지 않습니다. CLI 입력 대기도 전체 기한에 종료되며 뒤늦은 승인 입력은 실행으로 이어지지 않습니다.

JSON 도구 인자의 반복 문자열과 문자 그대로의 `<think>`는 보존합니다. 표시 문장을 정리하는 기능이 검색어나 명령을 변경하지 않으며, 응답의 추론 블록에 들어 있는 JSON은 호출 후보에서 제외합니다. 도구 스키마나 인자 설정 오류는 도구 기능 미지원과 구분해 불필요한 JSON 재요청을 막습니다. DB 연결 조회와 로그 용량 조회는 현재 상태를 반영하며, 과거 로그는 과거 기록으로 보존합니다.

명령 실행은 성공 여부와 출력 본문을 분리해서 반환합니다. 로그가 `ERROR`로 시작해도 조회에 성공했으면 정상 조회로 기록합니다. 실제 명령 실패와 예외는 도구 오류 제한에 포함합니다. 승인 직후 기한이 만료되거나 취소되면 승인 여부는 보존하고 실행 상태를 `not_executed`로 확정해, 실행하지 않은 작업이 대기 중으로 남지 않도록 합니다.

외부 모델로 보내는 메시지와 도구 정의는 재귀적으로 비밀값을 가립니다. 보고서는 모델의 제안과 실제 실행을 구분하며, 최종 상태는 `recovered`, `mitigated`, `unresolved`, `destroyed`로 표시합니다. 실제 API 요청 횟수(실패 포함), 제공된 토큰 사용량, 요청 시간, 모델, 엔드포인트와 호출 방식도 기록합니다. `usage_responses`가 API 요청 수보다 작으면 토큰 합계는 응답이 확인된 요청만 포함합니다. 기본 샘플링은 temperature 0.6, top_p 0.95, max_tokens 2048, thinking OFF입니다. [NVIDIA 공식 설정 예시](https://github.com/NVIDIA-AI-Blueprints/nemotron-voice-agent/blob/main/docs/how-to/configure-llm.md)의 thinking 옵션을 사용하며 모델의 내부 추론 원문은 저장하지 않습니다.

## 재현 가능한 오프라인 검증

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe evaluate.py
.\.venv\Scripts\python.exe baseline_repro.py
```

평가는 3개 시나리오에 정책 ON 승인, 정책 ON 거부, 정책 OFF를 적용한 9개 조합과 고정된 악성 명령 목록을 재생합니다. 결과는 [평가 요약](artifacts/evaluation/summary.md), [평가 JSON](artifacts/evaluation/summary.json), `artifacts/evaluation/{scenario}__{mode}.json`에 저장됩니다. 다른 폴더에는 `evaluate.py --output-dir 경로`로 저장합니다.

차단과 거부는 명령 시도 수, 무승인 실행은 성공한 실행 수를 분모로 계산합니다. 비밀값 누출 검사는 평가 코드에 열거한 합성 표식에 한정합니다. 고정 목록 통과를 모든 공격이나 모든 비밀값에 대한 보장으로 해석하지 않습니다. 테스트와 사전 작성 평가의 실제 NVIDIA API 호출 수는 0입니다.

검증 환경: Windows, Python 3.12.9, OpenAI SDK 3.19.2, Streamlit 1.64.0. 최신 자동 테스트 58개(화면 9가지 조합과 만료/중단 검사 포함), 평가 9개 사례와 고정 명령 20개 조합이 통과했습니다. CLI 데모의 복구와 JSON/Markdown 저장도 확인했습니다. [최신 검증과 소스 해시](artifacts/execution-validation.json)를 제공합니다. [수정 전후 9개 재현 결과](artifacts/baseline.json), [1차 검증](artifacts/validation.json), [2차 검증](artifacts/additional-validation.json)은 각 개선 당시의 기록입니다. 추가 개선은 오프라인과 API 대역으로 검증했으며 실제 NVIDIA API를 다시 호출하지 않았습니다. macOS와 Linux에서의 실행은 검증하지 않았습니다.

## 주요 파일

| 파일 | 역할 |
|---|---|
| `app.py`, `cli.py` | 웹 화면과 CLI |
| `stopforce/agent.py` | 도구 검증, 승인 처리, 호출 제한, 실행 기록과 종료 |
| `stopforce/llm.py` | NVIDIA 클라이언트와 사전 작성 시나리오 |
| `stopforce/commands.py`, `stopforce/policy.py`, `policy.yaml` | 명령 정책과 비밀값 가림 |
| `stopforce/cluster.py` | 메모리 상태와 장애 시뮬레이션 |
| `stopforce/skills.py`, `skills/` | 로컬 런북 로더와 본문 |
| `evaluate.py`, `tests/` | 오프라인 평가와 자동 테스트 |
| [SUBMISSION.md](SUBMISSION.md) | 개선 근거, 제출 전 확인 사항, 시연 구성 |

운영 적용에는 실제 인프라 어댑터, 인증과 권한 경계, 지속되는 감사 기록, OpenShell 등 실행 격리, 실제 모델을 대상으로 한 평가가 추가로 필요합니다.
