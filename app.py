"""Stop-Force 웹 데모 — 실행: streamlit run app.py"""
import os
import time
from math import ceil
from html import escape
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from stopforce.agent import Agent
from stopforce.cluster import SCENARIOS, make_cluster
from stopforce.llm import DEFAULT_MODEL, NvidiaLLM, ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary
from stopforce.reporting import report_json, report_markdown

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
    mode = st.radio("에이전트 두뇌", ["NVIDIA Nemotron (실제 LLM)", "데모 모드 (사전 작성 시나리오)"],
                    index=0 if has_key else 1,
                    help="데모 모드는 사람이 미리 작성한 도구 호출 순서입니다. 실제 모델 녹화가 아니며 API 호출은 0회입니다.")
    if mode.startswith("NVIDIA"):
        if has_key:
            st.success("NVIDIA_API_KEY 감지됨")
        else:
            st.error(".env에 NVIDIA_API_KEY를 넣어주세요")
        model = st.text_input("모델", os.getenv("NVIDIA_MODEL", DEFAULT_MODEL))
    scenario = st.selectbox("장애 시나리오", list(SCENARIOS), format_func=lambda k: SCENARIOS[k]["title"])
    policy_on = st.toggle("🛡 보안 정책 (OpenShell 스타일)", value=True,
                          help="OFF에서는 알려진 위험 명령을 메모리 안에서만 비교합니다. 명령 문법 검사와 비밀값 가림은 계속 적용됩니다.")
    incident = st.text_area("장애 신고 내용", SCENARIOS[scenario]["incident"], height=120)
    start = st.button("🚨 에이전트 출동", type="primary", width="stretch")
    with st.expander("policy.yaml 보기"):
        st.code((ROOT / "policy.yaml").read_text(encoding="utf-8"), language="yaml")


def new_agent():
    cl = make_cluster(scenario)
    if mode.startswith("NVIDIA"):
        llm = NvidiaLLM(model=model)
    else:
        llm = ScriptedLLM(scenario)
    ag = Agent(cl, Policy(ROOT / "policy.yaml", enabled=policy_on), SkillLibrary(ROOT / "skills"), llm, incident)
    previous = st.session_state.get("agent")
    if previous and not previous.done:
        previous.cancel("새 시나리오 실행으로 중단")
    st.session_state.pop("resolve", None)
    st.session_state.update(agent=ag, before=ag.before, running=True, demo=not mode.startswith("NVIDIA"))


if start:
    if mode.startswith("NVIDIA") and not has_key:
        st.sidebar.error("API 키가 없어 실행할 수 없습니다.")
    else:
        try:
            new_agent()
        except Exception as exc:
            safe, _ = Policy(ROOT / "policy.yaml").redact(str(exc))
            st.sidebar.error(f"실행 설정 오류: {safe}")

# ---------------------------------------------------------------------- 헤더
st.title("🛡 Stop-Force")
st.caption("장애 신고를 받으면 진단, 조치, 검증, 보고를 수행하는 SRE 에이전트. "
           "위험한 명령은 OpenShell 스타일 정책이 막고, 중요한 변경은 사람이 승인합니다.")
st.caption("실행 대상: 메모리 내 합성 클러스터 | 실제 서버 제어와 OpenShell 격리는 구현하지 않았습니다.")

ag: Agent | None = st.session_state.get("agent")
if not ag:
    st.info("왼쪽에서 시나리오를 고르고 **에이전트 출동**을 누르세요.")
    c1, c2, c3 = st.columns(3)
    c1.markdown("#### 🧠 Nemotron 에이전트\n계획 → 도구 호출 → 검증을 스스로 반복")
    c2.markdown("#### 📚 Agent Skills\n장애 유형별 런북(SKILL.md)을 필요할 때 불러와 따름")
    c3.markdown("#### 🛡 보안 정책\n전체 명령 검사, 사람 승인, 지원 비밀 패턴 가림")
    st.stop()


def health_table(before, now):
    def cells(h):
        ok = lambda b: "#16a34a" if b else "#dc2626"
        return [
            ("상태", {"recovered": "🟢 복구", "mitigated": "🟠 일시 완화", "destroyed": "💥 데이터 파괴", "unresolved": "🔴 미복구"}[h["recovery_status"]], None),
            ("원인 해결", "확인" if h["root_cause_resolved"] else "미해결", None),
            ("가용 서비스", f"{sum(s['available_replicas'] > 0 for s in h['services'].values())}/{len(h['services'])}", None),
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
    cls = "deny" if "DENY" in ev.title or "거부" in ev.title or "실패" in ev.title else kind
    body = ev.body
    if kind in {"final", "observation", "verification"}:
        return
    reason = ev.meta.get("reason")
    html = f'<div class="ev {escape(cls)}"><div class="t">{escape(ev.id)} | {escape(ev.title)}</div>'
    if reason:
        html += f'<div class="r">이유: {escape(reason)}</div>'
    if body:
        esc = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if kind == "result" and esc.count("\n") > 14:
            esc = "\n".join(esc.split("\n")[:14]) + "\n…"
        html += f'<div class="b">{esc}</div>' if kind == "thought" else f"<pre>{esc}</pre>"
    html += "</div>"
    with box:
        st.html(html)


@st.fragment(run_every=1)
def approval_panel(agent):
    if not agent.pending:
        return
    if agent.check_deadline():
        st.session_state.pop("resolve", None)
        st.session_state["running"] = False
        st.rerun()
    p = agent.pending
    with st.container(border=True):
        st.markdown("### ⏸ 온콜 엔지니어 승인 필요")
        st.code(p["command"], language="bash")
        st.caption(f"요청 ID: {p['id']} | 대상: {p['target']} | 상태: 승인 대기")
        st.markdown(f"- **에이전트가 말한 이유:** {p['reason']}\n- **승인이 필요한 이유(정책):** {p['policy_reason']}")
        st.info(f"예상 영향: {p['impact']}")
        st.caption(f"남은 승인 가능 시간: {ceil(agent.remaining_seconds)}초. "
                   f"전체 실행 제한 {agent.max_seconds}초에 승인 대기가 포함됩니다. 실제 서버에는 적용되지 않습니다.")
        b1, b2 = st.columns(2)
        if b1.button("✅ 승인하고 실행", type="primary", width="stretch"):
            st.session_state.update(resolve={"approved": True, "id": p["id"]}, running=True)
            st.rerun()
        if b2.button("⛔ 거부", width="stretch"):
            st.session_state.update(resolve={"approved": False, "id": p["id"]}, running=True)
            st.rerun()
        if st.button("🛑 실행 중단", width="stretch"):
            agent.cancel("사용자가 승인 대기 중 실행을 중단함")
            st.session_state.pop("resolve", None)
            st.session_state["running"] = False
            st.rerun()


st.markdown(f"**장애 신고:** {ag.incident}")
st.caption(f"두뇌: {ag.llm.label} | 명령 정책: {'ON' if ag.policy.enabled else '⚠️ OFF'} | 비밀 패턴 가림: ON")
if not ag.policy.enabled:
    st.warning("합성 위험 비교: 승인 없이 파괴 명령이 시뮬레이터에 적용될 수 있습니다.")

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
    st.caption(f"실제 모델 API 요청 {s['llm_calls']}회 | 내부 chat {s['chat_calls']}회 | 도구 {s['tool_calls']}회")
    with st.expander("서비스별 버전과 가용 replica"):
        st.json(ag.cluster.health()["services"])

with left:
    st.subheader("🧭 에이전트 작업 로그")
    box = st.container(height=440)
    for ev in ag.events:
        render(ev, box)

    if st.session_state.get("running"):
        demo = st.session_state.get("demo")

        def live(ev):
            render(ev, box)
            if demo:
                time.sleep(float(os.getenv("STOPFORCE_DEMO_DELAY", "0.08")))

        with st.spinner("에이전트가 작업 중…"):
            action = st.session_state.pop("resolve", None)
            if action is not None:
                ag.resolve(action["approved"], live, request_id=action["id"])
            else:
                ag.run(live)
        st.session_state["running"] = False
        st.rerun()

    if ag.pending:
        approval_panel(ag)

    if ag.report:
        r = ag.report
        v = r["verified"]
        with st.container(border=True):
            st.markdown("### 📋 장애 보고서")
            if v["destroyed"]:
                st.error("💥 복구 불가능한 사고 발생: " + ", ".join(v["destroyed"]))
            elif v["healthy"]:
                st.success("독립 검증: 현재 지표 정상화와 시나리오 원인 해결 확인")
            elif r["symptoms_resolved"]:
                st.warning("일시 완화: 지표는 정상이나 원인이 남아 재발할 수 있습니다.")
            else:
                st.warning("자동 검증: 아직 정상 범위가 아님 — 사람의 확인이 필요합니다")
            st.markdown(f"**요약**  \n{r.get('summary', '')}")
            if r["termination"] != "finish":
                st.error(f"실행 중단: {r['termination']} / {r['termination_reason']}")
            if r["execution_failed"]:
                st.error("실패한 명령이 있습니다. 실행 기록을 확인하세요.")
            st.caption(f"사람 이관: {'필요' if r['human_handoff'] else '불필요'} | 최종 검증 근거: {r['verification_event_id']}")
            st.markdown(f"**원인 설명 (모델 해석)**  \n{r.get('root_cause', '')}")
            st.markdown("**실제 실행한 명령**\n" + ("\n".join(f"- {a}" for a in r.get("actions_taken", [])) or "없음"))
            if r["remaining_causes"]:
                st.markdown("**확인된 미해결 원인**\n" + "\n".join(f"- {c}" for c in r["remaining_causes"]))
            with st.expander("명령 시도, 승인과 차단 근거", expanded=True):
                st.dataframe([{k: a.get(k) for k in ("command", "status", "approval_id", "event_id")} for a in r["actions"]], hide_index=True, width="stretch")
            if r.get("follow_ups"):
                st.markdown("**재발 방지 / 후속 작업**\n" + "\n".join(f"- {a}" for a in r["follow_ups"]))
            dl1, dl2 = st.columns(2)
            dl1.download_button("JSON 보고서와 이벤트", report_json(ag), file_name=f"stopforce-{ag.cluster.scenario}.json", mime="application/json")
            dl2.download_button("Markdown 보고서", report_markdown(ag), file_name=f"stopforce-{ag.cluster.scenario}.md", mime="text/markdown")
            st.caption(f"측정 경과 {r['elapsed_seconds']}초 (승인 대기 포함). 합성 시나리오 수치이며 실서비스 성과가 아닙니다.")
    elif ag.done and not ag.pending:
        st.warning("에이전트가 보고서 없이 종료했습니다. 작업 로그를 확인하세요.")
