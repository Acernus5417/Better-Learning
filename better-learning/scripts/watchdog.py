"""Independent watchdog process for the transcription stage.

Started detached from the main agent, it periodically nudges the orchestrator
with "检查子agent是否正常工作" plus read-only diagnostics. It never fails an
attempt, never collects, never releases a lease: only the orchestrator may do
that after the host confirms a member really stopped.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from _common import atomic_text, inside, read_json, utc_now, write_json

STATE_DIR = '_工作区/看门狗'
PID_FILE = STATE_DIR + '/pid.json'
STATUS_FILE = STATE_DIR + '/状态.json'
REMINDER_FILE = STATE_DIR + '/提醒.jsonl'
LEDGER = '_工作区/转写任务.json'
NUDGE_TEXT = '检查子agent是否正常工作'
DEFAULT_INTERVAL = 90
DEFAULT_MAX_RUNTIME_MINUTES = 720
IDLE_CYCLES_TO_EXIT = 3


def now():
    return utc_now()


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _age_seconds(value):
    stamp = _parse_iso(value)
    if stamp is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))


def _alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(course):
    path = course / PID_FILE
    return read_json(path) if path.exists() else None


def is_running(course):
    """True only when a watchdog process is actually alive for this course."""
    pid = read_pid(course)
    if not pid or not _alive(pid.get('pid')):
        return False
    return not pid.get('stopped_at')


def _stage_mtime(course, attempt):
    """Newest mtime and file count of an attempt's staging directory."""
    folder = attempt.get('staging_dir')
    if not folder:
        return None, 0
    try:
        root = inside(course, folder)
    except ValueError:
        return None, 0
    if not root.exists():
        return None, 0
    newest, count = None, 0
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        count += 1
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest, count


def inspect(course):
    """Read-only health snapshot of the transcription stage."""
    ledger_path = course / LEDGER
    ledger = read_json(ledger_path) if ledger_path.exists() else {}
    attempts = ledger.get('attempts', []) or []
    active = [a for a in attempts if a.get('state') in {'reserved', 'running', 'produced'}]
    rows = []
    for attempt in active:
        newest, count = _stage_mtime(course, attempt)
        age = _age_seconds(attempt.get('started_at') or attempt.get('reserved_at'))
        silent = None
        if newest is not None:
            silent = max(0, int((datetime.now(timezone.utc) - newest).total_seconds()))
        elif age is not None:
            silent = age
        rows.append({'attempt': attempt['id'], 'state': attempt['state'],
                     'member': attempt.get('logical_member'),
                     'host_agent_id': attempt.get('host_agent_id'),
                     'batch_no': attempt.get('dispatch_batch_no'),
                     'age_seconds': age, 'silent_seconds': silent,
                     'staged_files': count,
                     'suspected_stuck': bool(silent is not None and silent > 900)})
    ready = 0
    for source in ledger.get('sources', []) or []:
        for unit in source.get('units', []) or []:
            if (unit.get('processing_route') == 'vision' and not unit.get('blocked')
                    and unit.get('status') in {'pending', 'failed', 'unresolved'}
                    and unit.get('attempt_count', 0) < ledger.get('max_attempts', 3)):
                ready += 1
    return {'at': now(), 'active': len(active), 'ready_units': ready,
            'batch_no': ledger.get('current_batch_no', 0), 'members': rows,
            'ledger_exists': ledger_path.exists()}


def render(report):
    """One-line reminder; metadata only, never transcript content."""
    parts = [NUDGE_TEXT + '：']
    if not report['ledger_exists']:
        return parts[0] + '台账不存在，若转写尚未开始属正常；否则先 prepare。'
    parts.append(f"活动 {report['active']} 个，待处理单元 {report['ready_units']} 个，本批 {report['batch_no']}")
    stuck = [m['attempt'] for m in report['members'] if m['suspected_stuck']]
    if stuck:
        parts.append('；疑似无进展：' + ', '.join(stuck[:5]))
    parts.append('。停止成员必须经宿主确认后再 collect/recover，看门狗不会代替你判定。')
    return ''.join(parts)


def _append_reminder(course, text, report):
    path = course / REMINDER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': report['at'], 'message': text, 'report': report},
                                ensure_ascii=False) + '\n')


def tick(course, host, interval, max_runtime_minutes):
    """Loop owned by the detached process; exits on idle or runtime limit."""
    started = datetime.now(timezone.utc)
    idle = 0
    while True:
        report = inspect(course)
        message = render(report)
        _append_reminder(course, message, report)
        write_json(course / STATUS_FILE, {'running': True, 'host': host,
                                          'interval_seconds': interval,
                                          'last_at': report['at'],
                                          'last_message': message, 'report': report})
        if not report['ledger_exists'] or (report['active'] == 0 and report['ready_units'] == 0):
            idle += 1
        else:
            idle = 0
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if idle >= IDLE_CYCLES_TO_EXIT:
            write_json(course / STATUS_FILE, {'running': False, 'host': host,
                                              'stopped_at': now(), 'reason': 'idle',
                                              'last_message': message})
            return 0
        if max_runtime_minutes and elapsed > max_runtime_minutes * 60:
            write_json(course / STATUS_FILE, {'running': False, 'host': host,
                                              'stopped_at': now(), 'reason': 'max_runtime',
                                              'last_message': message})
            return 0
        time.sleep(interval)


def start(course, host, interval=DEFAULT_INTERVAL, max_runtime_minutes=DEFAULT_MAX_RUNTIME_MINUTES):
    existing = read_pid(course)
    if existing and _alive(existing.get('pid')) and not existing.get('stopped_at'):
        return {'started': False, 'already_running': True, 'pid': existing.get('pid'),
                'next': '看门狗已在运行；转写收口后运行 stop'}
    folder = course / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), '--course', str(course),
               'tick', '--host', host, '--interval', str(interval),
               '--max-runtime', str(max_runtime_minutes)]
    log = folder / 'watchdog.log'
    stream = log.open('a', encoding='utf-8')
    popen = {'stdin': subprocess.DEVNULL, 'stdout': stream, 'stderr': subprocess.STDOUT}
    if sys.platform.startswith('win'):
        flags = 0
        for name in ('CREATE_NEW_PROCESS_GROUP', 'DETACHED_PROCESS', 'CREATE_NO_WINDOW'):
            flags |= getattr(subprocess, name, 0)
        popen['creationflags'] = flags
    else:
        popen['start_new_session'] = True
    process = subprocess.Popen(command, **popen)
    write_json(course / PID_FILE, {'pid': process.pid, 'started_at': now(), 'host': host,
                                   'interval_seconds': interval,
                                   'max_runtime_minutes': max_runtime_minutes,
                                   'course': str(course), 'log': STATE_DIR + '/watchdog.log'})
    write_json(course / STATUS_FILE, {'running': True, 'host': host,
                                      'interval_seconds': interval, 'started_at': now(),
                                      'last_message': NUDGE_TEXT})
    return {'started': True, 'pid': process.pid, 'interval_seconds': interval,
            'reminder_file': REMINDER_FILE, 'status_file': STATUS_FILE,
            'next': '主代理每被唤醒一次就读取 %s；转写 check 通过且无活动成员后运行 stop' % STATUS_FILE}


def stop(course):
    pid = read_pid(course)
    if not pid:
        return {'stopped': True, 'reason': 'no_pid', 'next': '看门狗未启动或状态文件已清理'}
    if not _alive(pid.get('pid')):
        write_json(course / PID_FILE, dict(pid, stopped_at=now(), exit_reason='already_exited'))
        return {'stopped': True, 'reason': 'already_exited'}
    target = int(pid['pid'])
    try:
        os.kill(target, signal.SIGTERM)
    except OSError:
        if sys.platform.startswith('win'):
            subprocess.run(['taskkill', '/PID', str(target), '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if not _alive(target):
            break
        time.sleep(0.25)
    stopped = not _alive(target)
    write_json(course / PID_FILE, dict(pid, stopped_at=now(),
                                       exit_reason='stopped' if stopped else 'terminate_failed'))
    status_path = course / STATUS_FILE
    status = read_json(status_path) if status_path.exists() else {}
    status.update(running=False, stopped_at=now(), reason='orchestrator_stop')
    write_json(status_path, status)
    return {'stopped': stopped, 'pid': target}


def status(course):
    pid = read_pid(course)
    state = read_json(course / STATUS_FILE) if (course / STATUS_FILE).exists() else {}
    running = is_running(course)
    return {'running': running, 'pid': (pid or {}).get('pid'),
            'host': (pid or {}).get('host'), 'interval_seconds': (pid or {}).get('interval_seconds'),
            'started_at': (pid or {}).get('started_at'), 'stopped_at': (pid or {}).get('stopped_at'),
            'last_at': state.get('last_at'), 'last_message': state.get('last_message'),
            'report': state.get('report'), 'reminder_file': REMINDER_FILE}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('start')
    p.add_argument('--host', default='codex')
    p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL,
                   help='提醒间隔秒数，默认 90（1 分 30 秒）')
    p.add_argument('--max-runtime', type=int, default=DEFAULT_MAX_RUNTIME_MINUTES,
                   help='最长运行分钟数，超时自行退出')
    p = commands.add_parser('tick')
    p.add_argument('--host', default='codex')
    p.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    p.add_argument('--max-runtime', type=int, default=DEFAULT_MAX_RUNTIME_MINUTES)
    commands.add_parser('stop')
    commands.add_parser('status')
    args = parser.parse_args()
    course = args.course.resolve()
    try:
        if args.command == 'start':
            result = start(course, args.host, args.interval, args.max_runtime)
        elif args.command == 'tick':
            return tick(course, args.host, args.interval, args.max_runtime)
        elif args.command == 'stop':
            result = stop(course)
        else:
            result = status(course)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
