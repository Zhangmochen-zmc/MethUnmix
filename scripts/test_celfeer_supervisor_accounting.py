#!/usr/bin/env python3
"""Non-scientific regression tests for portable supervisor accounting."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / 'scripts/celfeer_targeted_replay_supervisor.py'
TELEMETRY = ROOT / 'scripts/celfeer_telemetry_v2.py'


def run_case(base: Path, name: str, code: str, timeout: float = 3.0, telemetry: Path = TELEMETRY) -> dict:
    cwd = base / name
    cwd.mkdir()
    script = cwd / 'task.py'
    script.write_text(code)
    command = cwd / 'command.json'
    command.write_text(json.dumps([sys.executable, str(script)]) + '\n')
    output = cwd / 'runtime_processes.jsonl'
    time_log = cwd / 'time.log'
    result = subprocess.run(
        [sys.executable, str(SUPERVISOR), '--task-cwd', str(cwd), '--telemetry', str(telemetry), '--telemetry-output', str(output), '--stage', name, '--task-id', name, '--max-seconds', str(timeout), '--interval', '0.05', '--time-log', str(time_log), '--task-command-json', str(command)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    accounting = json.loads((cwd / 'stage_accounting.json').read_text())
    rows = [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
    return {'return_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'accounting': accounting, 'rows': rows}


def main() -> int:
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix='methunmix-supervisor-regression-') as tmp:
        base = Path(tmp)
        results['successful_child'] = run_case(base, 'successful_child', 'import time; time.sleep(0.30)')
        results['nonzero_child'] = run_case(base, 'nonzero_child', 'raise SystemExit(7)')
        results['short_child'] = run_case(base, 'short_child', 'import time; time.sleep(0.12)')
        results['cpu_bound_child'] = run_case(base, 'cpu_bound_child', 's=0\nfor i in range(7000000): s=(s+i*i)&0xffffffff\nprint(s)')
        results['parent_child_tree'] = run_case(base, 'parent_child_tree', 'import subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(0.35)"])\ntime.sleep(0.25)\np.wait()')
        results['timeout'] = run_case(base, 'timeout', 'import time; time.sleep(2)', timeout=0.25)
        results['sampler_failure'] = run_case(base, 'sampler_failure', 'pass', telemetry=base / 'missing_sampler.py')

    checks = {
        'successful_child': results['successful_child']['return_code'] == 0,
        'nonzero_child': results['nonzero_child']['return_code'] == 7,
        'short_child_accounting': results['short_child']['accounting']['child_return_code'] == 0,
        'cpu_bound_accounting': results['cpu_bound_child']['accounting']['user_cpu_seconds'] is not None,
        'parent_child_rows': any(row.get('descendant_count', 0) >= 1 or row.get('direct_child_count', 0) >= 1 for row in results['parent_child_tree']['rows']),
        'timeout': results['timeout']['return_code'] == 124 and results['timeout']['accounting']['timed_out'] is True,
        'sampler_failure_propagation': results['sampler_failure']['return_code'] == 70,
        'sampler_cpu_excluded': all(value['accounting']['sampler_cpu_excluded_from_stage_accounting'] for value in results.values()),
        'accounting_fields': all(all(key in value['accounting'] for key in ('start_monotonic','end_monotonic','wall_seconds','child_return_code','user_cpu_seconds','system_cpu_seconds','maximum_rss_kib')) for value in results.values()),
        'no_gnu_time_dependency': '/usr/bin/time' not in SUPERVISOR.read_text(),
    }
    payload = {'schema': 'methunmix-celfeer-supervisor-accounting-regression-v1', 'tests': results, 'checks': checks, 'status': 'PASS' if all(checks.values()) else 'FAIL'}
    output = Path.cwd() / 'supervisor_accounting_regression.json'
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'status': payload['status'], 'checks': checks, 'output': str(output)}, sort_keys=True))
    return 0 if payload['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
