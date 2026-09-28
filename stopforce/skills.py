"""Agent Skills 로더 (SKILL.md 형식, NVIDIA/skills 와 같은 구조).

시스템 프롬프트에는 스킬 이름과 설명만 넣고, 에이전트가 필요할 때
load_skill 도구로 본문을 읽는다 (progressive disclosure).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
    return {"name": meta.get("name", path.parent.name), "description": meta.get("description", ""), "body": body.strip()}


class SkillLibrary:
    def __init__(self, root: str | Path):
        self.skills = {}
        for p in sorted(Path(root).glob("*/SKILL.md")):
            s = _parse(p)
            self.skills[s["name"]] = s

    def index(self) -> str:
        return "\n".join(f"- {s['name']}: {s['description']}" for s in self.skills.values())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            return f"error: unknown skill '{name}'. available: {', '.join(self.skills)}"
        return s["body"]
