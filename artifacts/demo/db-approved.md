# Stop-Force 장애 보고서

현재 지표와 시나리오 원인 해결을 확인했습니다.

- 환경: in_memory_simulator
- 모드: scripted
- 모델: 사전 작성 시나리오 또는 테스트 대역
- 정책: ON
- 시작: 2026-09-28T12:50:19.298021+00:00
- 종료: 2026-09-28T12:50:19.305020+00:00
- 경과 시간: 0.015초 (사람 승인 대기 포함)
- 복구: recovered / 종료 이유: finish
- 증상 해결: True / 원인 해결: True
- 사람 이관: False / 실행 실패: False
- 실제 API 요청: 0 / 내부 chat: 10
- API 사용량: {"api_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "duration_seconds": 0.0, "model": "scripted", "endpoint": "", "mode": "scripted"}

## 전후 검증

최종 근거: `e0035`

| 지표 | 시작 | 최종 |
|---|---|---|
| recovery_status | unresolved | recovered |
| max_error_rate | 0.23 | 0.003 |
| max_latency_ms | 10240 | 260 |
| max_disk | 52 | 52 |
| db | 100/100 | 22/100 |
| destroyed | [] | [] |

## 명령 요청과 실제 결과

| 명령 | 상태 | 승인 ID | 실행/판정 근거 |
|---|---|---|---|
| psql -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state" | succeeded | - | e0013 |
| kubectl rollout restart deployment/payment-worker | succeeded | - | e0023 |
| kubectl rollout undo deployment/payment-worker | succeeded | approval_001 | e0029 |

## 승인 기록

- approval_001: approved / kubectl rollout undo deployment/payment-worker / e0028

## 원인 설명 (모델의 해석)

9/26 배포된 payment-worker v1.8.2의 batch settlement 기능이 트랜잭션 커넥션을 풀에 반납하지 않아 postgres 커넥션(100/100)이 고갈됨. payment-api가 커넥션을 못 얻어 10초 타임아웃 → 503.

모델의 설명은 하네스가 독립 확인한 실행 사실과 구분합니다.

## 남은 원인과 후속 제안

- 모델 제안: v1.8.2 batch settlement 코드에 커넥션 close/try-with-resources 추가
- 모델 제안: idle_in_transaction_session_timeout 설정
- 모델 제안: 커넥션 80% 경보 추가

## 관측 근거

- e0004: list_services {}
- e0007: load_skill {"name": "db-connection-exhaustion"}
- e0010: read_logs {"service": "payment-api", "lines": 10}
- e0017: read_logs {"service": "payment-worker", "lines": 10}
- e0020: read_logs {"service": "deploy-history"}
- e0033: get_metrics {"service": "payment-api"}

전체 이벤트 본문과 서비스별 상태는 함께 제공되는 JSON에서 확인할 수 있습니다.
지원 패턴에 대한 비밀값 가림이며 모든 형태의 민감정보를 탐지하는 것은 아닙니다.
