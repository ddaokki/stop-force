"""터미널에서 실행: python cli.py [db_leak|bad_deploy|disk_full] [--demo] [--auto-approve] [--no-policy]"""
import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from stopforce.agent import Agent
from stopforce.cluster import SCENARIOS, make_cluster
from stopforce.llm import NvidiaLLM, ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="db_leak", choices=list(SCENARIOS))
    ap.add_argument("--demo", action="store_true", help="API 키 없이 녹화 재생")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--no-policy", action="store_true", help="보안 정책 끄기 (비교용)")
    args = ap.parse_args()

    use_demo = args.demo or not os.getenv("NVIDIA_API_KEY")
    llm = ScriptedLLM(args.scenario) if use_demo else NvidiaLLM()
    cl = make_cluster(args.scenario)
    agent = Agent(cl, Policy(ROOT / "policy.yaml", enabled=not args.no_policy),
                  SkillLibrary(ROOT / "skills"), llm, SCENARIOS[args.scenario]["incident"])
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

    agent.run(show)
    while agent.pending:
        if args.auto_approve:
            ok = True
        else:
            ok = input(f"\n>>> 승인하시겠습니까? {agent.pending['command']} [y/N] ").strip().lower() == "y"
        agent.resolve(ok, show)
    print("\n최종 상태:", cl.health())
    print("통계:", agent.stats)


if __name__ == "__main__":
    main()
