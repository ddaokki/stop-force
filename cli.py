"""터미널에서 실행: python cli.py [db_leak|bad_deploy|disk_full] [--demo] [--auto-approve] [--no-policy]"""
import argparse
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from stopforce.agent import Agent
from stopforce.cluster import SCENARIOS, make_cluster
from stopforce.llm import NvidiaLLM, ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary
from stopforce.reporting import report_json, report_markdown, write_report

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def read_approval(agent):
    """Wait for terminal input only until the run deadline; late input is ignored."""
    if agent.check_deadline():
        return None
    prompt = f"\n>>> 승인하시겠습니까? {agent.pending['command']} [y/N] "
    result = []
    ready = threading.Event()

    def read():
        try:
            result.append((True, input(prompt)))
        except Exception as exc:
            result.append((False, exc))
        finally:
            ready.set()

    threading.Thread(target=read, daemon=True).start()
    while not ready.wait(agent.remaining_seconds):
        if agent.check_deadline():
            return None
    if agent.check_deadline():
        return None
    ok, value = result[0]
    if not ok:
        raise value
    return value.strip().lower() == "y"


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="db_leak", choices=list(SCENARIOS))
    ap.add_argument("--demo", action="store_true", help="사전 작성 시나리오 (실제 API 호출 0회)")
    approvals = ap.add_mutually_exclusive_group()
    approvals.add_argument("--auto-approve", action="store_true", help="시뮬레이터 승인 요청 자동 허용")
    approvals.add_argument("--deny", action="store_true", help="모든 승인 요청 거부")
    ap.add_argument("--no-policy", action="store_true", help="보안 정책 끄기 (비교용)")
    ap.add_argument("--json", type=Path, help="가림 처리한 보고서와 이벤트 저장")
    ap.add_argument("--markdown", type=Path, help="읽기 쉬운 장애 보고서 저장")
    args = ap.parse_args()

    outputs = [path for path in (args.json, args.markdown) if path is not None]
    try:
        if len(outputs) == 2 and (outputs[0].resolve() == outputs[1].resolve() or
                (all(path.exists() for path in outputs) and outputs[0].samefile(outputs[1]))):
            ap.error("JSON과 Markdown은 서로 다른 파일에 저장해야 합니다.")
        for path in outputs:
            if path.exists() and not path.is_file():
                ap.error("보고서 저장 경로는 디렉터리가 아닌 파일이어야 합니다.")
            if any(parent.exists() and not parent.is_dir() for parent in path.parents):
                ap.error("보고서의 상위 경로에 파일이 있습니다. 저장 폴더를 확인하세요.")
    except OSError:
        ap.error("보고서 저장 경로를 확인할 수 없습니다. 경로와 접근 권한을 확인하세요.")

    if not args.demo and not os.getenv("NVIDIA_API_KEY"):
        ap.error("실제 NVIDIA 모드에는 .env의 NVIDIA_API_KEY가 필요합니다. 사전 작성 시연은 --demo를 지정하세요.")
    try:
        llm = ScriptedLLM(args.scenario) if args.demo else NvidiaLLM()
        cl = make_cluster(args.scenario)
        agent = Agent(cl, Policy(ROOT / "policy.yaml", enabled=not args.no_policy),
                      SkillLibrary(ROOT / "skills"), llm, SCENARIOS[args.scenario]["incident"])
    except Exception as exc:
        # Configuration values and provider exception bodies can contain secrets.
        ap.error(f"실행 설정을 읽을 수 없습니다 ({type(exc).__name__}). .env, policy.yaml, skills 경로를 확인하세요.")
    print(f"== {SCENARIOS[args.scenario]['title']}  [{llm.label}]\n")

    def show(ev):
        print(f"[{ev.kind}] {ev.title}")
        if ev.body:
            print("   " + ev.body.replace("\n", "\n   ")[:1200])
        if ev.meta.get("report"):
            r = ev.meta["report"]
            print("   원인:", r.get("root_cause"))
            for x in r.get("actions_taken", []):
                print("   조치:", x)
            print("   검증:", r["verified"])

    try:
        agent.run(show)
        while agent.pending:
            if args.deny:
                ok = False
            elif args.auto_approve:
                ok = True
            else:
                ok = read_approval(agent)
                if ok is None:
                    break
            agent.resolve(ok, show, request_id=agent.pending["id"])
    except (KeyboardInterrupt, EOFError):
        agent.cancel("터미널 실행 또는 승인 입력 중단")
    print("\n최종 상태:", cl.health())
    print("통계:", agent.stats)
    try:
        for path, content in ((args.json, report_json(agent)), (args.markdown, report_markdown(agent))):
            if path:
                write_report(path, content)
    except OSError:
        print("보고서 저장에 실패했습니다. 저장 경로, 디스크 공간과 쓰기 권한을 확인하세요.", file=sys.stderr)
        return 1
    # A controlled refusal/partial recovery is a valid demo outcome. API/harness failures are not.
    return 0 if agent.report["termination"] == "finish" else 1


if __name__ == "__main__":
    raise SystemExit(main())
