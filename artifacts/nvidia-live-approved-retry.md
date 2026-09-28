# Stop-Force 장애 보고서

현재 지표와 시나리오 원인 해결을 확인했습니다.

- 환경: in_memory_simulator
- 모드: nvidia_live
- 모델: nvidia/nemotron-3.5-lightning-30b-a3b
- 정책: ON
- 시작: 2026-09-28T12:56:57.090730+00:00
- 종료: 2026-09-28T12:58:28.974973+00:00
- 경과 시간: 91.89초 (사람 승인 대기 포함)
- 복구: recovered / 종료 이유: finish
- 증상 해결: True / 원인 해결: True
- 사람 이관: False / 실행 실패: False
- 실제 API 요청: 8 / 내부 chat: 8
- API 사용량: {"api_calls": 8, "prompt_tokens": 15948, "completion_tokens": 576, "total_tokens": 16524, "usage_responses": 8, "duration_seconds": 91.85800000000017, "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "endpoint": "https://integrate.api.nvidia.com/v1", "mode": "native", "temperature": 0.6, "top_p": 0.95, "enable_thinking": false, "max_tokens": 2048}

## 전후 검증

최종 근거: `e0019`

| 지표 | 시작 | 최종 |
|---|---|---|
| recovery_status | unresolved | recovered |
| max_error_rate | 0.38 | 0.003 |
| max_latency_ms | 620 | 190 |
| max_disk | 52 | 52 |
| db | 18/100 | 18/100 |
| destroyed | [] | [] |

## 명령 요청과 실제 결과

| 명령 | 상태 | 승인 ID | 실행/판정 근거 |
|---|---|---|---|
| kubectl rollout undo deployment/checkout-api | succeeded | approval_001 | e0015 |

## 승인 기록

- approval_001: approved / kubectl rollout undo deployment/checkout-api / e0014

## 원인 설명 (모델의 해석)

checkout-api v2.4.0 배포 직후(14시경) 쿠폰 엔진(CouponEngine.java:88)에서 NullPointerException이 발생해 500 에러율이 38%까지 치솟았음

모델의 설명은 하네스가 독립 확인한 실행 사실과 구분합니다.

## 남은 원인과 후속 제안

- 모델 제안: 쿠폰 엔진 관련 통합 테스트 커버리지 확인
- 모델 제안: 다음 배포 시 롤백 계획 사전 검토

## 관측 근거

- e0003: list_services {}
- e0005: load_skill {"name": "bad-deploy-rollback"}
- e0007: read_logs {"service": "deploy-history"}
- e0009: read_logs {"service": "checkout-api", "grep": "Exception", "lines": 30}
- e0011: get_metrics {"service": "checkout-api"}
- e0018: get_metrics {"service": "checkout-api"}

전체 이벤트 본문과 서비스별 상태는 함께 제공되는 JSON에서 확인할 수 있습니다.
지원 패턴에 대한 비밀값 가림이며 모든 형태의 민감정보를 탐지하는 것은 아닙니다.
