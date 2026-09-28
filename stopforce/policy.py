"""OpenShell 스타일 정책 엔진: deny-by-default, 사람 승인, 비밀정보 가림."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .commands import parse_command


@dataclass
class Decision:
    action: str  # allow | approval | deny
    reason: str
    rule: str = ""


class Policy:
    def __init__(self, path: str | Path, enabled: bool = True):
        self.path = Path(path)
        self.cfg = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.enabled = enabled
        self.operations = self.cfg.get("operations", {})
        self.redactions = [(re.compile(r["pattern"]), r["name"]) for r in self.cfg.get("redact", [])]

    def check(self, command: str) -> Decision:
        try:
            parsed = parse_command(command)
        except ValueError as exc:
            return Decision("deny", str(exc), "command.grammar")
        if not self.enabled:
            return Decision("allow", "정책 OFF (알려진 시뮬레이션 명령만)", "simulation")
        if parsed.operation == "destroy":
            return Decision("deny", "파괴적 시뮬레이션 명령 금지", "destructive")
        if parsed.operation == "scale" and not 1 <= int(parsed.args[0]) <= 10:
            return Decision("deny", "정책 ON 용량 변경은 1~10 replica로 제한", "scale.bounds")
        rule = self.operations.get(parsed.operation, {})
        if rule.get("action") in {"allow", "approval", "deny"}:
            return Decision(rule["action"], rule.get("reason", "작업 정책"), f"operations.{parsed.operation}")
        return Decision("deny", "허용 목록에 없는 명령 (deny-by-default)", "default")

    def redact(self, text: str) -> tuple[str, int]:
        n = 0
        for rx, name in self.redactions:
            text, k = rx.subn(f"[REDACTED:{name}]", text)
            n += k
        return text, n

    def redact_value(self, value):
        """Return a recursively redacted copy of JSON-compatible output."""
        if isinstance(value, str):
            return self.redact(value)[0]
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if isinstance(value, dict):
            return {
                key: "[REDACTED:password]" if re.search(r"(?i)(password|passwd|pwd)", str(key))
                else self.redact_value(item) for key, item in value.items()
            }
        return value
