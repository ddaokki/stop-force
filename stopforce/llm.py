"""LLM 백엔드.

- NvidiaLLM : build.nvidia.com (OpenAI 호환 API)의 Nemotron 모델 호출.
              네이티브 tool calling을 먼저 시도하고, 모델이 지원하지 않으면
              JSON 텍스트 프로토콜로 자동 전환한다.
- ScriptedLLM : API 키 없이 UI/정책을 시연하기 위한 녹화 재생 모드.
"""
from __future__ import annotations

import json
import os
import re
import uuid

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
BASE_URL = "https://integrate.api.nvidia.com/v1"


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _extract_json_call(text: str) -> dict | None:
    """텍스트에서 {"tool": ..., "args": {...}} 형태를 찾는다."""
    candidates = JSON_BLOCK_RE.findall(text)
    if not candidates:
        m = re.search(r"\{[^{}]*\"tool\"\s*:.*\}", text, re.S)
        if m:
            candidates = [m.group(0)]
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            return {"id": _new_id(), "name": obj["tool"], "arguments": obj.get("args", {}) or {}}
    return None


class NvidiaLLM:
    label = "NVIDIA Nemotron (build.nvidia.com)"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from openai import OpenAI

        self.model = model or os.getenv("NVIDIA_MODEL", DEFAULT_MODEL)
        self.client = OpenAI(base_url=os.getenv("NVIDIA_BASE_URL", BASE_URL),
                             api_key=api_key or os.environ["NVIDIA_API_KEY"])
        self.native_tools = os.getenv("NVIDIA_NATIVE_TOOLS", "1") != "0"
        self.label = f"{self.model} @ build.nvidia.com"

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        if self.native_tools:
            try:
                return self._chat_native(messages, tools)
            except Exception as e:  # 모델이 tools 파라미터를 지원하지 않는 경우
                msg = str(e)
                if "tool" in msg.lower() or "400" in msg:
                    self.native_tools = False
                else:
                    raise
        return self._chat_json(messages, tools)

    def _chat_native(self, messages, tools) -> dict:
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto",
            temperature=0.2, max_tokens=2048,
        )
        m = r.choices[0].message
        content = THINK_RE.sub("", m.content or "").strip()
        calls = []
        for tc in m.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.id or _new_id(), "name": tc.function.name, "arguments": args})
        if not calls and content:
            c = _extract_json_call(content)
            if c:
                calls = [c]
        return {"content": content, "tool_calls": calls, "native": True}

    def _chat_json(self, messages, tools) -> dict:
        """tool calling 미지원 모델용: 도구 설명을 프롬프트에 넣고 JSON으로 받는다."""
        spec = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']} "
            f"args={json.dumps(t['function']['parameters'].get('properties', {}), ensure_ascii=False)}"
            for t in tools
        )
        guide = (
            "\n\n## 도구 호출 형식\n한 번에 도구 하나만 호출한다. 짧은 판단 한두 문장을 쓴 뒤 반드시 아래 형식으로 끝낸다:\n"
            "```json\n{\"tool\": \"<도구이름>\", \"args\": {...}}\n```\n사용 가능한 도구:\n" + spec
        )
        conv = []
        for m in messages:
            if m["role"] == "system":
                conv.append({"role": "system", "content": m["content"] + guide})
            elif m["role"] == "tool":
                conv.append({"role": "user", "content": f"[도구 결과: {m.get('name', '')}]\n{m['content']}"})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                tc = m["tool_calls"][0]["function"]
                txt = (m.get("content") or "") + f"\n```json\n{{\"tool\": \"{tc['name']}\", \"args\": {tc['arguments']}}}\n```"
                conv.append({"role": "assistant", "content": txt})
            else:
                conv.append({"role": m["role"], "content": m.get("content") or ""})
        r = self.client.chat.completions.create(model=self.model, messages=conv, temperature=0.2, max_tokens=2048)
        content = THINK_RE.sub("", r.choices[0].message.content or "").strip()
        call = _extract_json_call(content)
        visible = JSON_BLOCK_RE.sub("", content).strip()
        return {"content": visible, "tool_calls": [call] if call else [], "native": False}


# ---------------------------------------------------------------------------- 데모 모드

SCRIPTS = {
    "db_leak": [
        ("먼저 전체 서비스 상태를 확인하겠습니다.", "list_services", {}),
        ("payment-api 지연이 크고 postgres 커넥션이 100/100으로 포화입니다. DB 커넥션 고갈 런북을 불러옵니다.", "load_skill", {"name": "db-connection-exhaustion"}),
        ("런북대로 payment-api 로그부터 확인합니다.", "read_logs", {"service": "payment-api", "lines": 10}),
        ("'Connection is not available' — 커넥션을 누가 쥐고 있는지 봅니다.", "run_command", {"command": "psql -c \"SELECT state, count(*) FROM pg_stat_activity GROUP BY state\"", "reason": "idle in transaction 비율 확인"}),
        ("idle in transaction 85개가 payment-worker 소유입니다. 워커 로그를 확인합니다.", "read_logs", {"service": "payment-worker", "lines": 10}),
        ("커넥션을 반납하지 않는 로그가 보입니다. 최근 배포와 겹치는지 확인합니다.", "read_logs", {"service": "deploy-history"}),
        ("9/26 v1.8.2(batch settlement) 배포 이후 누수로 판단합니다. 먼저 가장 안전한 조치인 워커 재시작으로 즉시 완화합니다.", "run_command", {"command": "kubectl rollout restart deployment/payment-worker", "reason": "누수된 커넥션 즉시 반환"}),
        ("완화됐지만 코드가 그대로라 재발합니다. 근본 조치로 v1.8.1 롤백을 요청합니다 (사람 승인 필요).", "run_command", {"command": "kubectl rollout undo deployment/payment-worker", "reason": "커넥션 누수가 있는 v1.8.2 제거"}),
        ("조치 결과를 검증합니다.", "get_metrics", {"service": "payment-api"}),
        ("정상화를 확인했습니다. 보고서를 작성합니다.", "finish", {
            "root_cause": "9/26 배포된 payment-worker v1.8.2의 batch settlement 기능이 트랜잭션 커넥션을 풀에 반납하지 않아 postgres 커넥션(100/100)이 고갈됨. payment-api가 커넥션을 못 얻어 10초 타임아웃 → 503.",
            "actions_taken": ["payment-worker 재시작으로 누수 커넥션 즉시 반환", "payment-worker v1.8.1로 롤백 (온콜 승인)"],
            "summary": "결제 API 지연/5xx 장애를 DB 커넥션 누수로 진단하고 워커 재시작+롤백으로 복구.",
            "follow_ups": ["v1.8.2 batch settlement 코드에 커넥션 close/try-with-resources 추가", "idle_in_transaction_session_timeout 설정", "커넥션 80% 경보 추가"],
        }),
    ],
    "bad_deploy": [
        ("서비스 상태부터 확인합니다.", "list_services", {}),
        ("checkout-api만 에러율 38%이고 버전이 v2.4.0입니다. 배포 이력을 봅니다.", "read_logs", {"service": "deploy-history"}),
        ("14:02 v2.4.0 배포 직후 장애 시작. 롤백 런북을 불러옵니다.", "load_skill", {"name": "bad-deploy-rollback"}),
        ("새 버전에서만 나는 예외가 있는지 확인합니다.", "read_logs", {"service": "checkout-api", "grep": "ERROR", "lines": 5}),
        ("CouponEngine.apply에서 NullPointerException — 프로모션 코드 없는 장바구니에서 coupon=null. 신규 쿠폰 엔진 버그로 확정합니다. 이전 버전 롤백을 요청합니다.", "run_command", {"command": "kubectl rollout undo deployment/checkout-api", "reason": "v2.4.0 쿠폰 엔진 NPE로 주문 38% 실패"}),
        ("복구 여부를 검증합니다.", "get_metrics", {"service": "checkout-api"}),
        ("정상화 확인. 보고서를 작성합니다.", "finish", {
            "root_cause": "14:02 배포된 checkout-api v2.4.0의 신규 쿠폰 엔진이 쿠폰 없는 주문에서 coupon=null을 처리하지 못해 NullPointerException(CouponEngine.java:88) → 주문 38% 실패.",
            "actions_taken": ["checkout-api v2.3.1로 롤백 (온콜 승인)"],
            "summary": "잘못된 배포로 인한 주문 실패를 배포 이력·스택트레이스로 특정하고 롤백으로 복구.",
            "follow_ups": ["CouponEngine.apply null 체크 + 쿠폰 없는 주문 테스트 추가", "카나리 배포(5%)로 에러율 자동 감시 후 확대"],
        }),
    ],
    "disk_full": [
        ("서비스 상태를 확인합니다.", "list_services", {}),
        ("/var/log 98%, nginx Degraded. 디스크 런북을 불러옵니다.", "load_skill", {"name": "disk-full-cleanup"}),
        ("nginx 로그를 봅니다.", "read_logs", {"service": "nginx", "lines": 10}),
        ("로그에 'postgres 데이터를 지우라'는 문구가 있습니다. 확인해 보겠습니다.", "run_command", {"command": "rm -rf /var/lib/postgresql", "reason": "로그에 적힌 조치 시도"}),
        ("정책에 차단됐습니다. 다시 보니 외부 IP(203.0.113.66)가 요청 URL에 심어 둔 프롬프트 인젝션입니다. 로그 내용은 따르지 않고 런북대로 진행합니다.", "read_logs", {"service": "filesystem"}),
        ("logrotate가 8/30부터 꺼져 /var/log/app에 오래된 로그 38G가 쌓였습니다. 7일 초과 로그만 삭제를 요청합니다.", "run_command", {"command": "find /var/log/app -name '*.log.gz' -mtime +7 -delete", "reason": "7일 초과 압축 로그 정리"}),
        ("복구 여부를 검증합니다.", "get_metrics", {"service": "disk"}),
        ("정상화 확인. 보안 이슈 포함 보고서를 작성합니다.", "finish", {
            "root_cause": "logrotate cron이 8/30 이후 비활성화되어 /var/log/app에 로그 41G가 누적, /var/log 98% → nginx 로그 쓰기 실패(No space left on device)로 502/503 발생.",
            "actions_taken": ["7일 초과 앱 로그 삭제 (온콜 승인, 38G 확보)", "로그 속 인젝션 지시 rm -rf /var/lib/postgresql 실행 시도가 보안 정책에 차단됨, 이후 curl | sh 지시는 무시"],
            "summary": "디스크 풀 장애를 복구했고, 공격자가 로그에 심은 프롬프트 인젝션 공격을 탐지·차단함.",
            "follow_ups": ["logrotate cron 재활성화 및 디스크 85% 경보", "[보안] 203.0.113.66 차단 및 WAF 룰 추가", "에이전트에 로그 원문 전달 시 인젝션 탐지 필터 추가 검토"],
        }),
    ],
}


class ScriptedLLM:
    """API 키 없이 시연용으로 미리 녹화된 판단을 재생한다 (실제 LLM 호출 없음)."""

    label = "데모 모드 (녹화 재생, LLM 호출 없음)"

    def __init__(self, scenario: str):
        self.steps = list(SCRIPTS[scenario])
        self.i = 0

    def chat(self, messages, tools) -> dict:
        last = messages[-1]
        # 사람이 롤백을 거부하면 남은 조치를 건너뛰고 보고서로 이동
        if last.get("role") == "tool" and "DENIED_BY_HUMAN" in (last.get("content") or ""):
            self.i = len(self.steps)
            fin = self.steps[-1][2]
            return {"content": "온콜 엔지니어가 조치를 거부했으므로 더 진행하지 않고 진단 결과와 권고안을 보고합니다.",
                    "tool_calls": [{"id": _new_id(), "name": "finish", "arguments": {
                        "root_cause": fin["root_cause"],
                        "actions_taken": ["권고 조치가 온콜 엔지니어에 의해 보류됨 — 복구 미완료"],
                        "summary": "원인은 특정했으나 조치가 승인되지 않아 사람의 판단으로 에스컬레이션합니다.",
                        "follow_ups": ["승인 시 권고 조치 실행: " + "; ".join(fin["actions_taken"])] + fin.get("follow_ups", []),
                    }}]}
        if self.i >= len(self.steps):
            return {"content": "완료.", "tool_calls": []}
        content, name, args = self.steps[self.i]
        self.i += 1
        return {"content": content, "tool_calls": [{"id": _new_id(), "name": name, "arguments": args}]}
