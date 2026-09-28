"""LLM 백엔드.

- NvidiaLLM : build.nvidia.com (OpenAI 호환 API)의 Nemotron 모델 호출.
              네이티브 tool calling을 먼저 시도하고, 모델이 지원하지 않으면
              JSON 텍스트 프로토콜로 자동 전환한다.
- ScriptedLLM : API 키 없이 UI/정책을 시연하기 위한 사전 작성 시나리오.
"""
from __future__ import annotations

import json
import os
import re
import uuid
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
BASE_URL = "https://integrate.api.nvidia.com/v1"


REPEAT_RE = re.compile(r"(.{2,16}?)\1{4,}", re.S)


def _clean(text: str | None) -> str:
    """<think> 블록 제거 + 모델이 같은 토막을 반복하는 퇴화 출력(예: 'sellsellsell…') 정리."""
    t = THINK_RE.sub("", text or "").strip()
    if not t:
        return ""
    collapsed = REPEAT_RE.sub("", t)
    # 절반 이상이 반복 토막이면 의미 없는 출력으로 보고 버린다
    if len(collapsed.strip()) < len(t) * 0.5:
        return ""
    return collapsed.strip()


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _without_thinking(text: str) -> str:
    """Remove reasoning blocks outside JSON strings without changing argument data."""
    output = []
    depth = 0
    quoted = escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if not quoted and text.startswith("<think>", index):
            end = text.find("</think>", index + len("<think>"))
            if end < 0:
                break
            index = end + len("</think>")
            continue
        output.append(char)
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"' and depth:
            quoted = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        index += 1
    return "".join(output)


class LLMError(RuntimeError):
    """분류된 오류. 공급자 응답 본문이나 인증정보를 포함하지 않는다."""
    def __init__(self, category, status_code=None):
        self.category = category
        self.status_code = status_code
        self.retryable = category in {"rate_limit", "server", "timeout"}
        super().__init__(f"LLM request failed ({category})")


def _classify(exc):
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status in (401, 403):
        category = "auth"
    elif status == 429:
        category = "rate_limit"
    elif isinstance(status, int) and 500 <= status <= 599:
        category = "server"
    elif isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        category = "timeout"
    elif status in (400, 404, 422) and re.search(r"\b(?:schema|parameters?|keyword|properties)\b", text):
        category = "model" if status == 404 else "request"
    elif status in (400, 404, 422) and re.search(
        r"\b(?:tools?|tool[_ ]calling|tool[_ ]choice|function calling)\b\s+(?:(?:is|are)\s+)?(?:not supported|unsupported)\b|"
        r"\b(?:does not support|doesn't support|cannot support|unsupported)\s+(?:tools?|tool[_ ]calling|tool[_ ]choice|function calling)\b",
        text):
        category = "tool_unsupported"
    elif status == 404 or (status in (400, 422) and "model" in text and
                          any(x in text for x in ("not found", "invalid", "unknown", "does not exist"))):
        category = "model"
    else:
        category = "request"
    return LLMError(category, status)


def _extract_json_call(text: str) -> dict | None:
    """텍스트에서 {"tool": ..., "args": {...}} 형태를 찾는다."""
    candidates = JSON_BLOCK_RE.findall(text)
    if not candidates:
        m = re.search(r"\{[^{}]*\"tool\"\s*:.*\}", text, re.S)
        if m:
            candidates = [m.group(0)]
        elif text.lstrip().startswith("{"):
            candidates = [text.strip()]
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            return {"id": _new_id(), "name": "", "arguments": None, "parse_error": "invalid_json"}
        if isinstance(obj, dict) and "tool" in obj:
            call = {"id": _new_id(), "name": obj["tool"], "arguments": obj.get("args")}
            if not isinstance(call["arguments"], dict):
                call["parse_error"] = "arguments_must_be_object"
            return call
        return {"id": _new_id(), "name": "", "arguments": None, "parse_error": "invalid_tool_call"}
    return None


class NvidiaLLM:
    label = "NVIDIA Nemotron (build.nvidia.com)"

    def __init__(self, api_key: str | None = None, model: str | None = None, *,
                 redactor=None, request_timeout=None, max_retries=None, backoff_seconds=0.5):
        from openai import OpenAI
        from .policy import Policy

        request_timeout = float(os.getenv("NVIDIA_REQUEST_TIMEOUT", "60")) if request_timeout is None else request_timeout
        max_retries = int(os.getenv("NVIDIA_MAX_RETRIES", "2")) if max_retries is None else max_retries
        if not 0 < request_timeout <= 120 or not 0 <= max_retries <= 3 or backoff_seconds < 0:
            raise ValueError("invalid request limits")
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.deadline = None
        self._api_key = api_key or os.environ["NVIDIA_API_KEY"]
        self._boundary_policy = Policy(Path(__file__).resolve().parent.parent / "policy.yaml", enabled=True)
        self.redactor = redactor

        self.model = model or os.getenv("NVIDIA_MODEL", DEFAULT_MODEL)
        self.client = OpenAI(base_url=os.getenv("NVIDIA_BASE_URL", BASE_URL),
                             api_key=self._api_key, timeout=request_timeout, max_retries=0)
        self.native_tools = os.getenv("NVIDIA_NATIVE_TOOLS", "1") != "0"
        # 기존 프로젝트 샘플링 값. 공식 모델 카드 권장값(1.0/0.95)과 구분한다.
        self.temperature = float(os.getenv("NVIDIA_TEMPERATURE", "0.6"))
        self.top_p = float(os.getenv("NVIDIA_TOP_P", "0.95"))
        self.enable_thinking = os.getenv("NVIDIA_ENABLE_THINKING", "0") == "1"
        self.max_tokens = int(os.getenv("NVIDIA_MAX_TOKENS", "2048"))
        if not 128 <= self.max_tokens <= 8192:
            raise ValueError("NVIDIA_MAX_TOKENS must be between 128 and 8192")
        self.label = f"{self.model} @ build.nvidia.com"
        endpoint = urlsplit(os.getenv("NVIDIA_BASE_URL", BASE_URL))
        self.telemetry = dict(api_calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0, usage_responses=0,
                              duration_seconds=0.0, model=self.model,
                              endpoint=f"{endpoint.scheme}://{endpoint.hostname or ''}{endpoint.path}",
                              mode="native" if self.native_tools else "json",
                              temperature=self.temperature, top_p=self.top_p,
                              enable_thinking=self.enable_thinking, max_tokens=self.max_tokens)

    def set_deadline(self, monotonic_deadline):
        self.deadline = monotonic_deadline

    def _redact(self, value):
        if isinstance(value, str):
            value = value.replace(self._api_key, "[REDACTED:api_key]") if self._api_key else value
            value = self._boundary_policy.redact(value)[0]
            if self.redactor:
                result = self.redactor(value)
                value = result[0] if isinstance(result, tuple) else result
            return value
        if isinstance(value, dict):
            return {self._redact(k): "[REDACTED:password]" if re.search(r"(?i)(password|passwd|pwd)", str(k))
                    else self._redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value

    def _request(self, mode, **kwargs):
        kwargs = self._redact(kwargs)
        self.telemetry["mode"] = mode
        for attempt in range(self.max_retries + 1):
            timeout = self.request_timeout if self.deadline is None else min(
                self.request_timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise LLMError("timeout")
            started = time.monotonic()
            self.telemetry["api_calls"] += 1
            try:
                response = self._bounded_create(timeout, kwargs)
            except LLMError:
                # A local wall deadline can leave a transport call in flight. Never
                # retry it or consume its late response; no tools run in that thread.
                raise
            except Exception as exc:
                error = _classify(exc)
            else:
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.telemetry["usage_responses"] += 1
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    count = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
                    if isinstance(count, int):
                        self.telemetry[key] += count
                return response
            finally:
                self.telemetry["duration_seconds"] += time.monotonic() - started
            if not error.retryable or attempt == self.max_retries:
                raise error from None
            delay = min(self.backoff_seconds * 2 ** attempt, 8.0)
            if self.deadline is not None and self.deadline - time.monotonic() <= delay:
                raise LLMError("timeout") from None
            time.sleep(delay)

    def _bounded_create(self, timeout, kwargs):
        """Bound caller wall time in addition to HTTP transport inactivity timeouts.

        The daemon owns only one model request. A late response is discarded; this
        cannot cancel provider-side generation and its token usage is unavailable.
        """
        ready = threading.Event()
        result = []

        def request():
            try:
                result.append((True, self.client.chat.completions.create(timeout=timeout, **kwargs)))
            except Exception as exc:
                result.append((False, exc))
            finally:
                ready.set()

        threading.Thread(target=request, daemon=True, name="stopforce-model-request").start()
        if not ready.wait(timeout):
            raise LLMError("timeout")
        succeeded, value = result[0]
        if not succeeded:
            raise value
        return value

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        if self.native_tools:
            try:
                result = self._chat_native(messages, tools)
            except LLMError as e:
                if e.category == "tool_unsupported":
                    self.native_tools = False
                    result = self._chat_json(messages, tools)
                else:
                    raise
        else:
            result = self._chat_json(messages, tools)
        result["telemetry"] = dict(self.telemetry)
        return result

    def _chat_native(self, messages, tools) -> dict:
        r = self._request("native",
            model=self.model, messages=messages, tools=tools, tool_choice="auto",
            temperature=self.temperature, top_p=self.top_p, max_tokens=self.max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        )
        m = r.choices[0].message
        raw_content = _without_thinking(m.content or "")
        content = _clean(raw_content)
        calls = []
        for tc in m.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments)
                parse_error = None if isinstance(args, dict) else "arguments_must_be_object"
            except (json.JSONDecodeError, TypeError):
                args, parse_error = None, "invalid_json"
            call = {"id": tc.id or _new_id(), "name": tc.function.name, "arguments": args}
            if parse_error:
                call["parse_error"] = parse_error
            calls.append(call)
        if not calls and raw_content:
            c = _extract_json_call(raw_content)
            if c:
                calls = [c]
                content = "" if raw_content.lstrip().startswith("{") else _clean(JSON_BLOCK_RE.sub("", raw_content))
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
                conv.append({"role": "user", "content": "[도구 결과] " + json.dumps(m, ensure_ascii=False)})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                txt = (m.get("content") or "") + "\n[도구 호출 기록] " + json.dumps(m["tool_calls"], ensure_ascii=False)
                conv.append({"role": "assistant", "content": txt})
            else:
                conv.append({"role": m["role"], "content": m.get("content") or ""})
        if not any(m["role"] == "system" for m in conv):
            conv.insert(0, {"role": "system", "content": guide})
        r = self._request("json", model=self.model, messages=conv, temperature=self.temperature,
                                                top_p=self.top_p, max_tokens=self.max_tokens,
                                                extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}})
        raw_content = _without_thinking(r.choices[0].message.content or "")
        # Parse data before presentation cleanup: repeated text and literal think
        # tags inside JSON strings are tool arguments, not disposable prose.
        call = _extract_json_call(raw_content)
        visible = "" if call and raw_content.lstrip().startswith("{") else _clean(JSON_BLOCK_RE.sub("", raw_content))
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
            "summary": "잘못된 배포로 인한 주문 실패를 배포 이력, 스택트레이스로 특정하고 롤백으로 복구.",
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
        ("로그가 다시 쌓이지 않도록 로그 회전 타이머 활성화를 요청합니다.", "run_command", {"command": "systemctl enable --now logrotate.timer", "reason": "로그 회전을 복구하여 디스크 포화 재발 방지"}),
        ("복구 여부를 검증합니다.", "get_metrics", {"service": "disk"}),
        ("정상화 확인. 보안 이슈 포함 보고서를 작성합니다.", "finish", {
            "root_cause": "logrotate cron이 8/30 이후 비활성화되어 /var/log/app에 로그 41G가 누적, /var/log 98% → nginx 로그 쓰기 실패(No space left on device)로 502/503 발생.",
            "actions_taken": ["7일 초과 앱 로그 삭제 (온콜 승인, 38G 확보)", "로그 속 인젝션 지시 rm -rf /var/lib/postgresql 실행 시도가 보안 정책에 차단됨, 이후 curl | sh 지시는 무시"],
            "summary": "디스크 풀 장애를 복구했고, 공격자가 로그에 심은 프롬프트 인젝션 공격을 탐지, 차단함.",
            "follow_ups": ["logrotate cron 재활성화 및 디스크 85% 경보", "[보안] 203.0.113.66 차단 및 WAF 룰 추가", "에이전트에 로그 원문 전달 시 인젝션 탐지 필터 추가 검토"],
        }),
    ],
}


class ScriptedLLM:
    """사전에 작성한 시나리오로 시연한다 (실제 LLM 호출 없음)."""

    label = "데모 모드 (사전 작성 시나리오, LLM 호출 없음)"

    def __init__(self, scenario: str):
        self.steps = list(SCRIPTS[scenario])
        self.i = 0
        self.telemetry = dict(api_calls=0, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                              duration_seconds=0.0, model="scripted", endpoint="", mode="scripted")

    def set_deadline(self, monotonic_deadline):
        pass

    def chat(self, messages, tools) -> dict:
        last = messages[-1] if messages else {}
        result = last.get("content") or ""
        destructive = any(marker in result for marker in ("postgres data directory deleted", "script executed.", "removed."))
        if last.get("role") == "tool" and (destructive or "DENIED_BY_POLICY" in result and
                                           not (self.i == 4 and "filesystem" == self.steps[self.i][2].get("service"))):
            self.i = len(self.steps)
            return {"content": "실행 결과에 따라 시나리오를 중단하고 사람의 판단을 요청합니다.",
                    "tool_calls": [{"id": _new_id(), "name": "finish", "arguments": {
                        "root_cause": "사전 작성 시나리오의 예상과 다른 조치 결과가 발생함",
                        "actions_taken": ["데이터 손상 결과가 반환됨" if destructive else "정책에 의해 조치가 거부됨"],
                        "summary": "복구를 확인하지 못했습니다. 온콜 엔지니어의 확인이 필요합니다.",
                        "follow_ups": ["실행 기록과 현재 상태를 확인하고 복구 방법 결정"],
                    }}], "telemetry": dict(self.telemetry)}
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
                    }}], "telemetry": dict(self.telemetry)}
        if self.i >= len(self.steps):
            return {"content": "완료.", "tool_calls": [], "telemetry": dict(self.telemetry)}
        content, name, args = self.steps[self.i]
        self.i += 1
        if name == "finish":
            content = "조회한 결과를 보고합니다. 최종 상태는 검증 결과에서 확인합니다."
            args = {**args, "summary": "사전 작성 시나리오를 마쳤습니다. 복구 여부는 검증 결과를 확인하세요.",
                    "actions_taken": [m.get("content", "") for m in messages if m.get("role") == "tool"
                                      and m.get("name") == "run_command"]}
        return {"content": content, "tool_calls": [{"id": _new_id(), "name": name, "arguments": args}],
                "telemetry": dict(self.telemetry)}
