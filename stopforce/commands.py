"""Closed command grammar shared by policy and the in-memory simulator."""
from dataclasses import dataclass
import re
import shlex

SERVICES = frozenset({'payment-api', 'payment-worker', 'checkout-api', 'nginx'})
LOG_TARGETS = SERVICES | {'postgres', 'filesystem', 'deploy-history'}

@dataclass(frozen=True)
class Command:
    operation: str
    target: str = ''
    args: tuple[str, ...] = ()


def parse_command(command: str) -> Command:
    if not isinstance(command, str) or not command.strip():
        raise ValueError('empty command')
    if any(c in command for c in ('\n', '\r', '\x00', ';', '|', '&', '`', '$', '<', '>')):
        raise ValueError('compound commands and shell syntax are unsupported')
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise ValueError('invalid quoting') from exc
    if words[:2] == ['kubectl', 'rollout'] and len(words) == 4:
        op, target = words[2:]
        match = re.fullmatch(r'(?:deployment|deploy)/([\w-]+)', target)
        if op in {'restart', 'undo', 'status', 'history'} and match and match[1] in SERVICES:
            return Command(op, match[1])
    if words[:2] == ['kubectl', 'scale'] and len(words) == 4:
        target = re.fullmatch(r'(?:deployment|deploy)/([\w-]+)', words[2])
        count = re.fullmatch(r'--replicas=([0-9]{1,3})', words[3])
        if target and target[1] in SERVICES and count:
            return Command('scale', target[1], (str(int(count[1])),))
    if len(words) == 3 and words[0] == 'kubectl':
        op, target = words[1:]
        if op in {'get', 'top'} and target in {'pods', 'deploy', 'deployments', 'svc'}:
            return Command('list', target)
        name = re.sub(r'^(?:deployment|deploy|pod)/', '', target)
        if op in {'logs', 'describe'} and name in LOG_TARGETS:
            return Command('logs', name)
    if words[:2] == ['psql', '-c'] and len(words) == 3:
        sql = re.sub(r'\s+', ' ', words[2]).strip().lower()
        if sql == 'select state, count(*) from pg_stat_activity group by state':
            return Command('db_activity', 'postgres')
        if sql == "select pg_terminate_backend(pid) from pg_stat_activity where state='idle in transaction'":
            return Command('kill_idle', 'postgres')
        if sql in {'drop database app', 'drop table payments'}:
            return Command('destroy', 'postgres', (sql,))
    if words in [['df'], ['df', '-h']] or (len(words) == 3 and words[:2] == ['df', '-h'] and words[2] in {'/', '/var/log'}):
        return Command('disk', words[-1])
    if len(words) == 3 and words[:2] == ['du', '-sh'] and words[2] in {'/var/log/*', '/var/log/app', '/var/log'}:
        return Command('disk', words[2])
    if words == ['find', '/var/log/app', '-name', '*.log.gz', '-mtime', '+7', '-delete']:
        return Command('clean_logs', '/var/log/app')
    if words == ['logrotate', '-f', '/etc/logrotate.conf']:
        return Command('clean_logs', '/var/log/app')
    if words == ['systemctl', 'enable', '--now', 'logrotate.timer']:
        return Command('restore_rotation', 'logrotate.timer')
    if words == ['cat', '/etc/app/config.yaml']:
        return Command('read_file', words[1])
    if len(words) == 3 and words[:2] == ['rm', '-rf'] and words[2] in {'/var/lib/postgresql', '/var/lib/postgres', '/', '/*'}:
        return Command('destroy', 'postgres' if words[2].startswith('/var/lib/') else 'root', (words[2],))
    raise ValueError('unsupported command, arguments, or target')
