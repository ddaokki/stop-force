"""OpenShell 스타일 정책 엔진: deny-by-default, 사람 승인, 비밀정보 가림."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

URL_RE = re.compile(r"https?://[^\s'\"|]+")


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
        cmds = self.cfg.get("commands", {})
        self.rules = {
            k: [(re.compile(r["pattern"], re.I), r.get("reason", ""), r["pattern"]) for r in cmds.get(k, [])]
            for k in ("deny", "approval", "allow")
        }
        self.allow_hosts = set(self.cfg.get("network", {}).get("allow_hosts", []))
        self.redactions = [(re.compile(r["pattern"]), r["name"]) for r in self.cfg.get("redact", [])]

    def check(self, command: str) -> Decision:
        if not self.enabled:
            return Decision("allow", "정책 OFF (비교용)")
        cmd = command.strip()

        for url in URL_RE.findall(cmd):
            host = urlparse(url).hostname or ""
            if host not in self.allow_hosts:
                return Decision("deny", f"허용되지 않은 네트워크 목적지: {host}", "network.allow_hosts")

        for rx, reason, pat in self.rules["deny"]:
            if rx.search(cmd):
                return Decision("deny", reason, pat)
        for rx, reason, pat in self.rules["approval"]:
            if rx.search(cmd):
                return Decision("approval", reason, pat)
        for rx, _reason, pat in self.rules["allow"]:
            if rx.search(cmd):
                return Decision("allow", "허용 목록", pat)
        return Decision("deny", "허용 목록에 없는 명령 (deny-by-default)", "default")

    def redact(self, text: str) -> tuple[str, int]:
        if not self.enabled:
            return text, 0
        n = 0
        for rx, name in self.redactions:
            text, k = rx.subn(f"[REDACTED:{name}]", text)
            n += k
        return text, n
