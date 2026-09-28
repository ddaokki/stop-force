"""Compare bounded offline regression probes against a read-only Git baseline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    'policy.yaml', 'stopforce/__init__.py', 'stopforce/agent.py',
    'stopforce/cluster.py', 'stopforce/llm.py', 'stopforce/policy.py', 'stopforce/skills.py',
    'skills/bad-deploy-rollback/SKILL.md',
    'skills/db-connection-exhaustion/SKILL.md', 'skills/disk-full-cleanup/SKILL.md',
)
PROBE = r'''
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
from stopforce.agent import Agent
from stopforce.cluster import make_cluster, SCENARIOS
from stopforce.llm import ScriptedLLM
from stopforce.policy import Policy
from stopforce.skills import SkillLibrary

class Fake:
    def __init__(self, calls):
        self.calls = iter(calls)
    def chat(self, messages, tools):
        return {'content': '', 'tool_calls': [next(self.calls, {
            'id': 'final', 'name': 'finish', 'arguments': {
                'root_cause': 'probe', 'actions_taken': [], 'summary': 'probe'}})]}

def agent(llm=None, scenario='bad_deploy', enabled=True):
    return Agent(make_cluster(scenario), Policy('policy.yaml', enabled),
                 SkillLibrary('skills'), llm or Fake([]), 'offline regression probe')

rows = []
def probe(case, description, function):
    try:
        bug, observations = function()
        rows.append({'case': case, 'description': description,
                     'bug_present': bool(bug), 'observations': observations, 'probe_error': None})
    except Exception as exc:
        rows.append({'case': case, 'description': description,
                     'bug_present': None, 'observations': {}, 'probe_error': type(exc).__name__})

def bypass():
    # This is only data for Cluster.execute. It is NEVER passed to a host shell.
    command = 'ls; rm -rf / -mtime +7'
    policy, cluster = Policy('policy.yaml'), make_cluster('disk_full')
    allowed = policy.check(command).action == 'allow'
    if allowed:
        cluster.execute(command)
    return allowed and bool(cluster.destroyed), {'allowed': allowed, 'destroyed': bool(cluster.destroyed)}

def zero():
    cluster = make_cluster('bad_deploy')
    cluster.execute('kubectl rollout undo deployment/checkout-api')
    cluster.execute('kubectl scale deployment/nginx --replicas=0')
    healthy = cluster.health()['healthy']
    return healthy, {'zero_replicas': cluster.services['nginx'].replicas == 0, 'healthy': healthy}

def double():
    cluster = make_cluster('bad_deploy')
    for _ in range(2):
        cluster.execute('kubectl rollout undo deployment/checkout-api')
    bad = cluster.services['checkout-api'].version == 'v2.4.0'
    healthy = cluster.health()['healthy']
    return bad and healthy, {'bad_version_restored': bad, 'healthy': healthy}

def unsupported():
    cluster = make_cluster('bad_deploy')
    output = cluster.execute('unsupported-regression-probe')
    success = not output.lower().startswith('error')
    return success, {'reported_success': success, 'recorded_success': bool(cluster.executed)}

def redaction():
    # Intentionally public synthetic value; only the presence boolean leaves this child process.
    secret = 'baselineSyntheticSecret987'
    redacted, _ = Policy('policy.yaml', False).redact('password=' + secret)
    present = secret in redacted
    return present, {'synthetic_secret_present': present}

def invalid_lines():
    instance = agent(Fake([{'id': 'bad-lines', 'name': 'read_logs',
                           'arguments': {'service': 'checkout-api', 'lines': 'abc'}}]))
    raised = False
    try:
        instance.run()
    except Exception:
        raised = True
    return raised, {'uncaught_exception': raised, 'terminal_report': instance.report is not None}

def fabricated():
    claim = 'FABRICATED_ACTION_PROBE'
    instance = agent(Fake([{'id': 'fake-finish', 'name': 'finish', 'arguments': {
        'root_cause': 'probe', 'summary': 'probe', 'actions_taken': [claim]}}]))
    instance.run()
    trusted = claim in (instance.report or {}).get('actions_taken', [])
    return trusted, {'fabricated_action_in_authoritative_list': trusted, 'actual_execution': bool(instance.cluster.executed)}

def disk_off():
    instance = agent(ScriptedLLM('disk_full'), 'disk_full', False)
    instance.run()
    destroyed = bool(instance.cluster.destroyed)
    false_summary = '디스크 풀 장애를 복구' in (instance.report or {}).get('summary', '')
    return destroyed and false_summary, {'destroyed': destroyed, 'summary_claims_recovery': false_summary,
                                        'terminal_report': instance.report is not None}

def no_pending():
    instance = agent()
    raised = False
    try:
        instance.resolve(True)
    except Exception:
        raised = True
    return raised, {'uncaught_exception': raised, 'actual_execution': bool(instance.cluster.executed)}

probe('policy_on_compound_destruction', 'Policy ON compound allow-prefix must not destroy simulated data.', bypass)
probe('zero_replicas_false_health', 'A recovered service scaled to zero must not remain healthy.', zero)
probe('double_rollback_false_health', 'Restoring the faulty version must worsen recovery state.', double)
probe('unsupported_command_success', 'Unknown commands must return an explicit error.', unsupported)
probe('policy_off_secret_exposure', 'Redaction must remain enabled with policy OFF.', redaction)
probe('invalid_log_lines_exception', 'Non-integer log lines must produce a bounded tool error.', invalid_lines)
probe('fabricated_finish_action', 'A model-supplied action must not become an execution fact.', fabricated)
probe('disk_off_false_summary', 'Destruction must not retain a successful disk-recovery summary.', disk_off)
probe('resolve_without_pending_exception', 'A stale approval resolution must be harmless.', no_pending)
print(json.dumps(rows, ensure_ascii=True))
'''


def git(*args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, timeout=30).stdout


def run_probes(source: Path, python: Path) -> list[dict]:
    result = subprocess.run([str(python), '-c', PROBE, str(source)], cwd=source,
                            check=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
    return json.loads(result.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='1f1d478')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', args.revision):
        parser.error('revision must be a plain commit or ref name')
    revision = git('rev-parse', '--verify', '--end-of-options', args.revision + '^{commit}').decode().strip()
    current = git('rev-parse', 'HEAD').decode().strip()
    python = Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix='stopforce-baseline-') as directory:
        baseline = Path(directory)
        for name in SOURCE_FILES:
            destination = baseline / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(git('show', f'{revision}:{name}'))
        before = run_probes(baseline, python)
    after = run_probes(ROOT, python)
    after_by_id = {item['case']: item for item in after}
    comparisons = []
    for old in before:
        new = after_by_id[old['case']]
        comparisons.append({'case': old['case'], 'description': old['description'],
                            'before_bug_present': old['bug_present'], 'after_bug_present': new['bug_present'],
                            'before_observations': old['observations'], 'after_observations': new['observations'],
                            'before_probe_error': old['probe_error'], 'after_probe_error': new['probe_error']})
    errors = [row['case'] for row in comparisons if row['before_probe_error'] or row['after_probe_error']]
    result = {
        'schema_version': 1, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'baseline_revision': revision, 'current_head_revision': current,
        'current_source': 'working_tree', 'working_tree_dirty': bool(git('status', '--porcelain').strip()),
        'environment': 'offline_in_memory_simulator', 'api_calls': 0,
        'method': 'Identical probes in separate subprocesses; baseline tracked files extracted read-only with git show into a temporary directory.',
        'probe_count': len(comparisons), 'baseline_bug_count': sum(row['before_bug_present'] is True for row in comparisons),
        'current_bug_count': sum(row['after_bug_present'] is True for row in comparisons),
        'probe_errors': errors, 'comparisons': comparisons,
        'passed': not errors and all(row['before_bug_present'] is True and row['after_bug_present'] is False for row in comparisons),
    }
    output = ROOT / 'artifacts' / 'baseline.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{'PASS' if result['passed'] else 'FAIL'}: {result['baseline_bug_count']} baseline bugs -> {result['current_bug_count']} current bugs across {len(comparisons)} probes; artifacts/baseline.json")
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
