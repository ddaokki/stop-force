"""Stop-Force 웹 데모 — 실행: streamlit run app.py"""
import os
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from stopforce.agent import Agent
from stopforce.cluster import SCENARIOS, make_cluster
from stopforce.llm import DEFAULT_MODEL, NvidiaLLM, ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

st.set_page_config(page_title="Stop-Force", page_icon="🛡", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1200px;}
.ev {border-left: 4px solid #999; padding: 6px 12px; margin: 6px 0; border-radius: 6px; background: rgba(127,127,127,.06);}
.ev pre {white-space: pre; overflow-x: auto; font-size: 12px; margin: 4px 0 0 0;} .ev .b {margin-top:3px; line-height:1.55; overflow-wrap:anywhere;}
.ev.thought {border-color:#76b900;} .ev.tool {border-color:#3b82f6;} .ev.result {border-color:#94a3b8;}
.ev.policy {border-color:#f59e0b;} .ev.deny {border-color:#ef4444; background: rgba(239,68,68,.08);}
.ev.approval {border-color:#8b5cf6;} .ev.error {border-color:#ef4444;}
.ev .t {font-weight:600;}
.ht {width:100%; border-collapse:collapse; font-size:15px;} .ht td,.ht th {padding:7px 10px; border-bottom:1px solid rgba(127,127,127,.25); text-align:left;} .ev .r {opacity:.7; font-size:12.5px;}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------- 사이드바
with st.sidebar:
    st.header("⚙️ 설정")
    has_key = bool(os.getenv("NVIDIA_API_KEY"))
    mode = st.radio("에이전트 두뇌", ["NVIDIA Nemotron (실제 LLM)", "데모 모드 (녹화 재생)"],
                    index=0 if has_key else 1,
                    help="데모 모드는 API 키 없이 UI와 보안 정책을 보여주기 위한 녹화 재생입니다.")
    if mode.startswith("NVIDIA"):
        if has_key:
            st.success("NVIDIA_API_KEY 감지됨")
        else:
            st.error(".env에 NVIDIA_API_KEY를 넣어주세요")
        model = st.text_input("모델", os.getenv("NVIDIA_MODEL", DEFAULT_MODEL))
    scenario = st.selectbox("장애 시나리오", list(SCENARIOS), format_func=lambda k: SCENARIOS[k]["title"])
    policy_on = st.toggle("🛡 보안 정책 (OpenShell 스타일)", value=True,
                          help="끄면 에이전트 명령이 검사 없이 실행됩니다 (비교 시연용).")
    incident = st.text_area("장애 신고 내용", SCENARIOS[scenario]["incident"], height=120)
    start = st.button("🚨 에이전트 출동", type="primary", use_container_width=True)
    with st.expander("policy.yaml 보기"):
        st.code((ROOT / "policy.yaml").read_text(encoding="utf-8"), language="yaml")


def new_agent():
    cl = make_cluster(scenario)
    if mode.startswith("NVIDIA"):
        llm = NvidiaLLM(model=model)
    else:
        llm = ScriptedLLM(scenario)
    ag = Agent(cl, Policy(ROOT / "policy.yaml", enabled=policy_on), SkillLibrary(ROOT / "skills"), llm, incident)
    st.session_state.update(agent=ag, before=cl.health(), running=True, demo=not mode.startswith("NVIDIA"))


if start:
    if mode.startswith("NVIDIA") and not has_key:
        st.sidebar.error("API 키가 없어 실행할 수 없습니다.")
    else:
        new_agent()

# ---------------------------------------------------------------------- 헤더
st.title("🛡 Stop-Force")
st.caption("장애 신고를 받으면 스스로 진단·조치·검증·보고까지 하는 SRE 에이전트 — "
           "위험한 명령은 OpenShell 스타일 정책이 막고, 중요한 변경은 사람이 승인합니다.")

ag: Agent | None = st.session_state.get("agent")
if not ag:
    st.info("왼쪽에서 시나리오를 고르고 **에이전트 출동**을 누르세요.")
    c1, c2, c3 = st.columns(3)
    c1.markdown("#### 🧠 Nemotron 에이전트\n계획 → 도구 호출 → 검증을 스스로 반복")
    c2.markdown("#### 📚 Agent Skills\n장애 유형별 런북(SKILL.md)을 필요할 때 불러와 따름")
    c3.markdown("#### 🛡 보안 정책\ndeny-by-default, 사람 승인, 비밀정보 가림, 프롬프트 인젝션 방어")
    st.stop()


def health_table(before, now):
    def cells(h):
        ok = lambda b: "#16a34a" if b else "#dc2626"
        return [
            ("상태", "🟢 정상" if h["healthy"] else ("💥 사고" if h["destroyed"] else "🔴 장애"), None),
            ("에러율", f"{h['max_error_rate']*100:.1f}%", ok(h["max_error_rate"] < 0.02)),
            ("p95 지연", f"{h['max_latency_ms']:,}ms", ok(h["max_latency_ms"] < 1000)),
            ("DB 커넥션", h["db"], ok(int(h["db"].split("/")[0]) < 90)),
            ("디스크", f"{h['max_disk']}%", ok(h["max_disk"] < 90)),
        ]
    b, n = cells(before), cells(now)
    rows = "".join(
        f"<tr><td>{b[i][0]}</td><td style='color:{b[i][2] or 'inherit'}'>{b[i][1]}</td>"
        f"<td style='color:{n[i][2] or 'inherit'};font-weight:700'>{n[i][1]}</td></tr>"
        for i in range(len(b)))
    st.html(f"""<table class="ht"><tr><th>지표</th><th>조치 전</th><th>현재</th></tr>{rows}</table>""")


def render(ev, box):
    kind = ev.kind
    cls = "deny" if "DENY" in ev.title or ev.title.startswith("⛔") else kind
    body = ev.body
    if kind == "final":
        return
    reason = ev.meta.get("reason")
    html = f'<div class="ev {cls}"><div class="t">{ev.title}</div>'
    if reason:
        html += f'<div class="r">이유: {reason}</div>'
    if body:
        esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if kind == "result" and esc.count("\n") > 14:
            esc = "\n".join(esc.split("\n")[:14]) + "\n…"
        html += f'<div class="b">{esc}</div>' if kind == "thought" else f"<pre>{esc}</pre>"
    html += "</div>"
    with box:
        st.html(html)


st.markdown(f"**장애 신고:** {ag.incident}")
st.caption(f"두뇌: {ag.llm.label} · 보안 정책: {'ON' if ag.policy.enabled else '⚠️ OFF'}")

left, right = st.columns([3, 2])

with right:
    st.subheader("📊 시스템 상태")
    health_table(st.session_state["before"], ag.cluster.health())
    s = ag.stats
    st.subheader("🛡 보안 정책 통계")
    k = st.columns(4)
    k[0].metric("허용", s["allow"])
    k[1].metric("승인 요청", s["approval"])
    k[2].metric("차단", s["deny"])
    k[3].metric("비밀 가림", s["redacted"])

with left:
    st.subheader("🧭 에이전트 작업 로그")
    box = st.container()
    for ev in ag.events:
        render(ev, box)

    if st.session_state.get("running"):
        demo = st.session_state.get("demo")

        def live(ev):
            render(ev, box)
            if demo:
                time.sleep(0.35)

        with st.spinner("에이전트가 작업 중…"):
            action = st.session_state.pop("resolve", None)
            if action is not None:
                ag.resolve(action, live)
            else:
                ag.run(live)
        st.session_state["running"] = False
        st.rerun()

    if ag.pending:
        p = ag.pending
        with st.container(border=True):
            st.markdown("### ⏸ 온콜 엔지니어 승인 필요")
            st.code(p["command"], language="bash")
            st.markdown(f"- **에이전트가 말한 이유:** {p['reason']}\n- **승인이 필요한 이유(정책):** {p['policy_reason']}")
            b1, b2 = st.columns(2)
            if b1.button("✅ 승인하고 실행", type="primary", use_container_width=True):
                st.session_state.update(resolve=True, running=True)
                st.rerun()
            if b2.button("⛔ 거부", use_container_width=True):
                st.session_state.update(resolve=False, running=True)
                st.rerun()

    if ag.report:
        r = ag.report
        v = r["verified"]
        with st.container(border=True):
            st.markdown("### 📋 장애 보고서")
            if v["destroyed"]:
                st.error("💥 복구 불가능한 사고 발생: " + ", ".join(v["destroyed"]))
            elif v["healthy"]:
                st.success("자동 검증: 모든 지표 정상 범위로 복구됨")
            else:
                st.warning("자동 검증: 아직 정상 범위가 아님 — 사람의 확인이 필요합니다")
            st.markdown(f"**요약**  \n{r.get('summary', '')}")
            st.markdown(f"**근본 원인**  \n{r.get('root_cause', '')}")
            st.markdown("**수행한 조치**\n" + "\n".join(f"- {a}" for a in r.get("actions_taken", [])))
            if r.get("follow_ups"):
                st.markdown("**재발 방지 / 후속 작업**\n" + "\n".join(f"- {a}" for a in r["follow_ups"]))
    elif ag.done and not ag.pending:
        st.warning("에이전트가 보고서 없이 종료했습니다. 작업 로그를 확인하세요.")
