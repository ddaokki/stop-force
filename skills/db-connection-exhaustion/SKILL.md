---
name: db-connection-exhaustion
description: DB 커넥션 풀 고갈(too many clients, Connection is not available, idle in transaction)로 API가 느려지거나 5xx를 낼 때 쓰는 런북
---

# DB 커넥션 고갈 대응 런북

## 1. 확인
- `get_metrics("postgres")`로 커넥션 사용량 확인 (max 대비 90% 이상이면 고갈)
- `run_command("psql -c \"SELECT state, count(*) FROM pg_stat_activity GROUP BY state\"")` 로 idle in transaction 비율 확인
- 어느 애플리케이션이 커넥션을 쥐고 있는지 `read_logs("postgres")`, 워커 로그 확인
- `read_logs("deploy-history")` 로 최근 배포와 시점이 겹치는지 확인

## 2. 조치 (위에서부터 순서대로, 가장 덜 위험한 것부터)
1. 누수 원인 서비스 재시작: `kubectl rollout restart deployment/<svc>` — 즉시 완화
2. 최근 배포가 원인이면 롤백: `kubectl rollout undo deployment/<svc>` — 근본 조치 (사람 승인 필요)
3. 긴급 시 idle 세션 종료: `psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle in transaction'"` (사람 승인 필요)

## 3. 금지
- DB 재시작, DROP, 데이터 디렉터리 삭제 금지
- max_connections를 무작정 올리지 말 것 (누수를 숨길 뿐)

## 4. 검증
- 조치 후 `get_metrics`로 커넥션 < 80%, API p95 < 1000ms, 에러율 < 2% 확인
