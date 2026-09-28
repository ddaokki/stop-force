"""Portable, evidence-linked exports from the harness-owned report."""
import json
import os
from pathlib import Path
import tempfile


def write_report(path: Path, content: str) -> None:
    """Atomically replace one UTF-8 report; multiple reports are independent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        os.close(descriptor)
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def report_json(agent) -> str:
    return json.dumps(agent.export(), ensure_ascii=False, indent=2)


def report_markdown(agent) -> str:
    r = agent.export()["report"]
    if not r:
        return "# Stop-Force\n\n실행이 아직 끝나지 않았습니다.\n"

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["# Stop-Force 장애 보고서", "", r["summary"], "",
        f"- 환경: {r['environment']}", f"- 모드: {r['mode']}",
        f"- 모델: {r['model'] or '사전 작성 시나리오 또는 테스트 대역'}",
        f"- 정책: {'ON' if r['policy_enabled'] else 'OFF (합성 비교)'}",
        f"- 시작: {r['started_at']}", f"- 종료: {r['finished_at']}",
        f"- 경과 시간: {r['elapsed_seconds']}초 (사람 승인 대기 포함)",
        f"- 복구: {r['recovery_status']} / 종료 이유: {r['termination']}",
        f"- 증상 해결: {r['symptoms_resolved']} / 원인 해결: {r['root_cause_resolved']}",
        f"- 사람 이관: {r['human_handoff']} / 실행 실패: {r['execution_failed']}",
        f"- 실제 API 요청: {r['telemetry'].get('api_calls', 0)} / 내부 chat: {r['stats']['chat_calls']}",
        f"- API 사용량: {json.dumps(r['telemetry'], ensure_ascii=False)}", "",
        "## 전후 검증", "", f"최종 근거: `{r['verification_event_id']}`", "",
        "| 지표 | 시작 | 최종 |", "|---|---|---|"]
    for key in ("recovery_status", "max_error_rate", "max_latency_ms", "max_disk", "db", "destroyed"):
        lines.append(f"| {key} | {cell(r['before'][key])} | {cell(r['verified'][key])} |")
    lines += ["", "## 명령 요청과 실제 결과", "",
        "| 명령 | 상태 | 승인 ID | 실행/판정 근거 |", "|---|---|---|---|"]
    for action in r["actions"]:
        lines.append(f"| {cell(action['command'])} | {action['status']} | {action['approval_id'] or '-'} | {action['event_id']} |")
    if not r["actions"]:
        lines.append("| 없음 | 미실행 | - | - |")
    lines += ["", "## 승인 기록", ""]
    for approval in r["approvals"]:
        lines.append(f"- {approval['id']}: {approval['status']} / {approval['command']} / {approval.get('decision_event_id', approval['request_event_id'])}")
    lines += ["", "## 원인 설명 (모델의 해석)", "", r["root_cause"], "",
        "모델의 설명은 하네스가 독립 확인한 실행 사실과 구분합니다.", "",
        "## 남은 원인과 후속 제안", ""]
    lines += [f"- 확인된 미해결: {cause}" for cause in r["remaining_causes"]]
    lines += [f"- 모델 제안: {item}" for item in r["follow_ups"]]
    if r["termination_reason"]:
        lines += ["", f"중단 사유: {r['termination_reason']}"]
    lines += ["", "## 관측 근거", ""]
    lines += [f"- {o['event_id']}: {o['tool']} {json.dumps(o['arguments'], ensure_ascii=False)}" for o in r["observations"]]
    lines += ["", "전체 이벤트 본문과 서비스별 상태는 함께 제공되는 JSON에서 확인할 수 있습니다.",
              "지원 패턴에 대한 비밀값 가림이며 모든 형태의 민감정보를 탐지하는 것은 아닙니다.", ""]
    return "\n".join(lines)
