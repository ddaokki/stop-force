# Stop-Force 장애 보고서

장애가 남아 있어 추가 조치가 필요합니다.

- 환경: in_memory_simulator
- 모드: nvidia_live
- 모델: nvidia/nemotron-3.5-lightning-30b-a3b
- 정책: ON
- 시작: 2026-09-28T12:55:05.460315+00:00
- 종료: 2026-09-28T12:55:50.631875+00:00
- 경과 시간: 45.171초 (사람 승인 대기 포함)
- 복구: unresolved / 종료 이유: timeout
- 증상 해결: False / 원인 해결: False
- 사람 이관: True / 실행 실패: False
- 실제 API 요청: 4 / 내부 chat: 4
- API 사용량: {"api_calls": 4, "prompt_tokens": 4652, "completion_tokens": 84, "total_tokens": 4736, "usage_responses": 3, "duration_seconds": 45.155000000000655, "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "endpoint": "https://integrate.api.nvidia.com/v1", "mode": "native", "temperature": 0.6, "top_p": 0.95, "enable_thinking": false, "max_tokens": 2048}

## 전후 검증

최종 근거: `e0008`

| 지표 | 시작 | 최종 |
|---|---|---|
| recovery_status | unresolved | unresolved |
| max_error_rate | 0.38 | 0.38 |
| max_latency_ms | 620 | 620 |
| max_disk | 52 | 52 |
| db | 18/100 | 18/100 |
| destroyed | [] | [] |

## 명령 요청과 실제 결과

| 명령 | 상태 | 승인 ID | 실행/판정 근거 |
|---|---|---|---|
| 없음 | 미실행 | - | - |

## 승인 기록


## 원인 설명 (모델의 해석)

모델 설명 없음. 관측 결과를 확인하세요.

모델의 설명은 하네스가 독립 확인한 실행 사실과 구분합니다.

## 남은 원인과 후속 제안

- 확인된 미해결: checkout-api v2.4.0 coupon bug

중단 사유: LLMError: LLM request failed (timeout)

## 관측 근거

- e0003: list_services {}
- e0005: load_skill {"name": "bad-deploy-rollback"}
- e0007: read_logs {"service": "deploy-history", "lines": 30}

전체 이벤트 본문과 서비스별 상태는 함께 제공되는 JSON에서 확인할 수 있습니다.
지원 패턴에 대한 비밀값 가림이며 모든 형태의 민감정보를 탐지하는 것은 아닙니다.
